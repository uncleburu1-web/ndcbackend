"""Spec 11 §12 — admin dashboard, team, users, audit viewer, background jobs."""
import json
import threading
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.access.models import Role, RoleAssignment
from apps.access.services import RoleError, deactivate_user, grant_role, revoke_role
from apps.accounts.models import User
from apps.integrations.models import WebhookReceipt
from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.candidates.models import Candidate
from apps.core.jobs import KNOWN_JOBS, latest_runs, state, track
from apps.core.models import JobRun
from apps.dashboard.cards import Card, severity, working_days_between
from apps.dashboard.services import admin_areas
from apps.donations.models import Donation
from apps.follows.models import CandidateFollow
from apps.membership.models import Membership, MembershipPayment
from apps.moderation import services as moderation
from apps.news.models import Article
from apps.posts.models import CandidatePost
from apps.pvc import services as pvc

PHONE = "+2348031234567"


def mk(cid, email=None, **kw):
    return User.objects.create_user(cid, email=email or f"{cid}@example.com", full_name=kw.pop("full_name", f"Person {cid}"),
                                    phone_number=PHONE, profile_completed_at=timezone.now(), adult_confirmed_at=timezone.now(), **kw)


def client(user=None):
    c = APIClient()
    if user:
        c.force_authenticate(user)
    return c


def role_user(cid, *roles):
    u = mk(cid)
    for r in roles:
        grant_role(user=u, role=r)
    return u


def overview(user):
    cache.clear()
    r = client(user).get(reverse("admin-overview"))
    assert r.status_code == 200, r.data
    return r


def card_keys(data):
    return [c["key"] for c in data["attention"]]


def group_keys(data):
    return [g["group"] for g in data["stats"]]


def mk_candidate(slug="amina", status="published"):
    pub = status == "published"
    return Candidate.objects.create(slug=slug, full_name="Amina Bello", position="governor", election_year=2027, state="kano", status=status,
                                    verified_at=timezone.now() if pub else None, published_at=timezone.now() if pub else None)


def mk_article(author, status="pending", **kw):
    return Article.objects.create(source="community", status=status, title="Polls open early", body="x" * 200, author=author,
                                  submitted_at=kw.pop("submitted_at", timezone.now()), **kw)


def mk_donation(status="successful", kobo=500_000, designation="party", paid_at=None):
    extra = {"candidate": mk_candidate(slug=f"c-{Donation.objects.count()}"), "election_year": 2027} if designation == "candidate" else {}
    return Donation.objects.create(provider="fake", amount_kobo=kobo, designation=designation, donor_name="Secret Donor", **extra,
                                   donor_email="donor@example.com", donor_phone=PHONE, donor_state="lagos", donor_address="x",
                                   display_choice="guest", status=status, paid_at=paid_at or timezone.now())


class Base(APITestCase):
    def setUp(self):
        cache.clear()
        self.sa = role_user("u_sa", Role.SYSTEM_ADMIN)
        self.news = role_user("u_news", Role.NEWS_REVIEWER)
        self.pvc = role_user("u_pvc", Role.PVC_OFFICER)
        self.mem = role_user("u_mem", Role.MEMBERSHIP_OFFICER)
        self.fin = role_user("u_fin", Role.FINANCE_OFFICER)
        self.mod = role_user("u_mod", Role.MODERATOR)
        self.plain = mk("u_plain")


# --------------------------------------------------------------------------- overview
class OverviewAccessTests(Base):
    def test_guest_and_non_admin(self):
        self.assertEqual(client().get(reverse("admin-overview")).status_code, 401)
        self.assertEqual(client(self.plain).get(reverse("admin-overview")).status_code, 403)
        cand = mk("u_cand")
        c = mk_candidate()
        from apps.access.models import ScopeType

        grant_role(user=cand, role=Role.CANDIDATE, scope_type=ScopeType.CANDIDATE, scope_id=c.pk)
        self.assertEqual(client(cand).get(reverse("admin-overview")).status_code, 403)  # a candidate link isn't an admin role

    def test_each_role_gets_exactly_its_sections(self):
        expect = {
            "sa": (["incident_deadlines", "privacy_requests_due", "reports_review", "reports_urgent", "appeals_waiting", "legal_due",
                    "community_hidden_waiting", "events_reported", "events_hidden_waiting", "geo_review_waiting", "geo_units_changed",
                    "geo_preview_waiting", "news_pending", "pvc_pending",
                    "membership_approvals", "membership_payments_review", "donations_review", "candidates_unpublished", "ban_failed",
                    "background_tasks_failing", "data_integrity_issues", "communities_unmoderated", "events_in_blackout"],
                   ["people", "candidates", "content", "pvc", "membership", "donations"], True),
            "news": (["reports_review", "reports_urgent", "appeals_waiting", "news_pending"], ["content"], False),
            "pvc": (["geo_review_waiting", "pvc_pending"], ["pvc"], False),  # 19 §15.2: the review queue for officers
            "mem": (["geo_review_waiting", "pvc_pending", "membership_approvals", "membership_payments_review"], ["pvc", "membership"], False),
            "fin": (["donations_review"], ["donations"], False),
            "mod": (["reports_review", "reports_urgent", "appeals_waiting", "community_hidden_waiting", "events_reported",
                     "events_hidden_waiting"], [], False),  # 13 §4.9, 17 §15.2, 18 §15.2
        }
        for who, (cards, groups, sys_sections) in expect.items():
            data = overview(getattr(self, who)).data
            self.assertEqual(card_keys(data), cards, who)
            self.assertEqual(group_keys(data), groups, who)
            for k in ("readiness", "jobs", "activity"):
                self.assertEqual(k in data, sys_sections, (who, k))

    def test_combined_roles(self):
        u = role_user("u_combo", Role.NEWS_REVIEWER, Role.FINANCE_OFFICER)
        data = overview(u).data
        self.assertEqual(card_keys(data), ["reports_review", "reports_urgent", "appeals_waiting", "news_pending", "donations_review"])
        self.assertEqual([r["role"] for r in data["roles"]], ["finance_officer", "news_reviewer"])

    def test_private_and_no_people(self):
        mk_donation()
        mk_article(self.plain)
        r = overview(self.sa)
        self.assertIn("no-store", r["Cache-Control"])
        self.assertIn("private", r["Cache-Control"])
        text = json.dumps(r.data, default=str)
        for leak in ("Secret Donor", "donor@example.com", PHONE, "u_plain@example.com", "Person u_plain"):
            self.assertNotIn(leak, text)

    def test_finance_payload_has_no_other_numbers(self):
        Membership.objects.create(user=self.plain, status="active", activated_at=timezone.now(), membership_number="NDC-KN-000001")
        text = json.dumps(overview(self.fin).data, default=str)
        for other in ("membership", "pvc_pending", "news_pending", "Active members"):
            self.assertNotIn(other, text)


class CountTests(Base):
    def test_news_counts_exclude_own_and_match_queue(self):
        mk_article(self.plain, submitted_at=timezone.now() - timedelta(hours=30))
        mk_article(self.news)  # the reviewer's own submission
        cards = {c["key"]: c for c in overview(self.news).data["attention"]}
        self.assertEqual(cards["news_pending"]["count"], 1)
        self.assertEqual(cards["news_pending"]["severity"], "warning")  # over 24 h
        self.assertEqual({c["key"]: c for c in overview(self.sa).data["attention"]}["news_pending"]["count"], 2)

    def test_reports_cards(self):
        a = mk_article(self.plain, status="successful", published_at=timezone.now())
        moderation.report("article", a, "false")
        moderation.report("article", a, "abuse")
        c = mk_candidate()
        p = CandidatePost.objects.create(candidate=c, kind="news", title="t", body="b", published_at=timezone.now())
        moderation.report("post", p, "spam")
        cards = {c["key"]: c for c in overview(self.sa).data["attention"]}
        self.assertEqual(cards["reports_review"]["count"], 2)  # cases (one per item), not reports
        self.assertEqual(cards["reports_review"]["href"], "/admin/moderation")
        self.assertEqual(cards["reports_urgent"]["count"], 1)  # abuse raised the article's case to urgent
        news = {c["key"]: c for c in overview(self.news).data["attention"]}
        self.assertEqual(news["reports_review"]["count"], 1)  # news reviewers see community news only
        self.assertNotIn("legal_due", news)

    def test_pvc_excludes_own_check(self):
        pvc.submit(self.plain, vin="90F5B0F8E0295612345", surname="X", state="kano", lga="Fagge")
        pvc.submit(self.pvc, vin="90F5B0F8E0295612346", surname="Y", state="kano", lga="Fagge")
        self.assertEqual({c["key"]: c for c in overview(self.pvc).data["attention"]}["pvc_pending"]["count"], 1)

    def test_membership_and_donation_review(self):
        m = Membership.objects.create(user=self.plain, status="pending_approval")
        MembershipPayment.objects.create(membership=m, provider="fake", amount_kobo=2_000_000, status="review", payer_email="x@example.com")
        mk_donation(status="review")
        cards = {c["key"]: c for c in overview(self.sa).data["attention"]}
        self.assertEqual(cards["membership_approvals"]["count"], 1)
        self.assertEqual(cards["membership_payments_review"]["severity"], "warning")  # any is a warning
        self.assertEqual(cards["donations_review"]["severity"], "warning")

    def test_severity_rules(self):
        now = timezone.now()
        self.assertEqual(severity(Card("x", "x", 0, "/", oldest_at=now - timedelta(days=9), target_hours=24), now), "ok")
        self.assertEqual(severity(Card("x", "x", 1, "/", oldest_at=now - timedelta(hours=2), target_hours=24), now), "ok")
        self.assertEqual(severity(Card("x", "x", 1, "/", oldest_at=now - timedelta(hours=30), target_hours=24), now), "warning")
        self.assertEqual(severity(Card("x", "x", 1, "/", oldest_at=now - timedelta(hours=50), target_hours=24), now), "error")
        self.assertEqual(severity(Card("x", "x", 3, "/", info_only=True), now), "info")
        mon = timezone.make_aware(timezone.datetime(2026, 11, 2, 9, 0))
        self.assertEqual(working_days_between(mon - timedelta(days=3), mon), 1)  # Fri → Mon is one working day

    def test_stats_windows_and_donations(self):
        mk_donation(kobo=1_000_000, paid_at=timezone.now() - timedelta(days=2))
        mk_donation(kobo=300_000, designation="candidate", paid_at=timezone.now() - timedelta(days=10))
        mk_donation(kobo=999_999, paid_at=timezone.now() - timedelta(days=40))  # outside 30 days
        groups = {g["group"]: {s["key"]: s for s in g["stats"]} for g in overview(self.fin).data["stats"]}
        d = groups["donations"]
        self.assertEqual((d["count"]["total"], d["count"]["this_week"], d["count"]["last_week"]), (2, 1, 1))
        self.assertEqual(d["amount"]["total"], 1_300_000)
        self.assertTrue(d["amount"]["kobo"])
        self.assertEqual((d["party"]["total"], d["candidates"]["total"]), (1_000_000, 300_000))

    def test_people_and_follow_stats(self):
        c = mk_candidate()
        CandidateFollow.objects.create(user=self.plain, candidate=c, notify=True)
        groups = {g["group"]: {s["key"]: s for s in g["stats"]} for g in overview(self.sa).data["stats"]}
        self.assertEqual(groups["candidates"]["follows"]["total"], 1)
        self.assertEqual(groups["candidates"]["bell_on"]["total"], 1)
        self.assertEqual(groups["candidates"]["published"]["total"], 1)
        self.assertEqual(groups["people"]["accounts"]["total"], User.objects.count())

    def test_cache_is_per_role_set_not_per_user(self):
        other_fin = role_user("u_fin2", Role.FINANCE_OFFICER)
        cache.clear()
        client(self.fin).get(reverse("admin-overview"))
        mk_donation(status="review")
        r = client(other_fin).get(reverse("admin-overview"))  # same role set → same cached numbers for 30 s
        self.assertEqual({c["key"]: c for c in r.data["attention"]}["donations_review"]["count"], 0)
        summary_keys = [k for k in cache._cache.keys() if "admin-summary" in k]  # locmem
        self.assertTrue(summary_keys and all(str(self.fin.pk) not in k and str(other_fin.pk) not in k for k in summary_keys))

    def test_failing_app_does_not_blank_the_page(self):
        with mock.patch("apps.posts.dashboard.admin_summary", side_effect=RuntimeError("boom")), \
                self.assertLogs("apps.dashboard.services", "ERROR"):
            data = overview(self.sa).data
        self.assertEqual(data["failed"], [{"app": "posts", "label": "Candidate posts"}])
        self.assertIn("news_pending", card_keys(data))

    def test_query_budget(self):
        cache.clear()
        with CaptureQueriesContext(connection) as ctx:
            client(self.sa).get(reverse("admin-overview"))
        self.assertLess(len(ctx.captured_queries), 120)  # 17: communities cards and two readiness lines; 18: three event cards, two lines
        # 19: three geography cards and two readiness lines


class ReadinessTests(Base):
    @override_settings(DONATIONS_ENABLED=False, INEC_PVC_URL="https://secret-inec.example/pvc", CLERK_JWT_KEY="SECRETVALUE123")
    def test_booleans_only(self):
        r = overview(self.sa).data["readiness"]
        text = json.dumps(r, default=str)
        self.assertNotIn("secret-inec", text)
        self.assertNotIn("SECRETVALUE123", text)
        items = {(i["group"], i["label"]): i for i in r["items"]}
        self.assertEqual(items[("donations", "DONATIONS_ENABLED")]["state"], "not_set")
        self.assertEqual(items[("inec", "INEC PVC portal link")]["state"], "ready")
        self.assertEqual(items[("signin", "Last Clerk webhook")]["state"], "warning")
        WebhookReceipt.objects.create(provider="clerk", event_id="msg_1", event_type="user.created")
        items = {(i["group"], i["label"]): i for i in overview(self.sa).data["readiness"]["items"]}
        self.assertEqual(items[("signin", "Last Clerk webhook")]["state"], "ready")

    def test_blackouts(self):
        now = timezone.now()
        windows = [{"positions": ["governor"], "start": (now - timedelta(hours=1)).isoformat(), "end": (now + timedelta(hours=2)).isoformat()},
                   {"positions": ["president"], "start": (now + timedelta(days=3)).isoformat(), "end": (now + timedelta(days=4)).isoformat()},
                   {"positions": ["senator"], "start": (now - timedelta(days=9)).isoformat(), "end": (now - timedelta(days=8)).isoformat()}]
        with override_settings(CAMPAIGN_BLACKOUTS=windows):
            data = overview(self.news).data
            self.assertEqual(data["blackouts"]["active"][0]["offices"], ["Governor"])
            self.assertEqual(data["blackouts"]["next"]["offices"], ["President"])
            states = [w["state"] for w in overview(self.sa).data["readiness"]["blackouts"]]
        self.assertEqual(states, ["active", "upcoming", "past"])


# ------------------------------------------------------------------------------- jobs
class JobTests(APITestCase):
    def test_track_success_and_failure(self):
        with track("prune_news") as t:
            t.summary = "Deleted 3 rows"
        row = JobRun.objects.get(job="prune_news")
        self.assertTrue(row.ok)
        self.assertEqual(row.summary, "Deleted 3 rows")
        with self.assertRaises(ValueError):
            with track("prune_posts"):
                raise ValueError("bad thing\nsecond line with data")
        row = JobRun.objects.get(job="prune_posts")
        self.assertEqual((row.ok, row.error), (False, "ValueError: bad thing"))

    def test_states(self):
        now = timezone.now()
        self.assertEqual(state(None, "sweep_payments", now), "never")
        r = JobRun(job="sweep_payments", started_at=now - timedelta(hours=3), finished_at=now - timedelta(hours=3), ok=True)
        self.assertEqual(state(r, "sweep_payments", now), "overdue")
        r.finished_at = now
        self.assertEqual(state(r, "sweep_payments", now), "ok")
        r.finished_at, r.started_at = None, now - timedelta(hours=5)
        self.assertEqual(state(r, "sweep_payments", now), "stalled")
        r.started_at = now - timedelta(minutes=5)
        self.assertEqual(state(r, "sweep_payments", now), "running")
        r = JobRun(job="deliver_post", started_at=now - timedelta(days=30), finished_at=now - timedelta(days=30), ok=True)
        self.assertEqual(state(r, "deliver_post", now), "ok")  # manual jobs never go overdue
        self.assertEqual([j["job"] for j in latest_runs(now)], list(KNOWN_JOBS))

    def test_every_scheduled_command_records_a_run(self):
        for cmd in ("sweep_payments", "sweep_donations", "check_donations", "prune_pvc", "prune_news", "prune_notifications",
                    "prune_posts", "prune_jobruns"):
            call_command(cmd, stdout=StringIO())
            self.assertTrue(JobRun.objects.filter(job=cmd, ok=True).exists(), cmd)
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("deliver_post", "00000000-0000-0000-0000-000000000000", stdout=StringIO())
        self.assertTrue(JobRun.objects.filter(job="deliver_post", ok=False).exists())

    def test_prune_jobruns(self):
        JobRun.objects.create(job="prune_news", started_at=timezone.now() - timedelta(days=91))
        JobRun.objects.create(job="prune_news", started_at=timezone.now() - timedelta(days=10))
        call_command("prune_jobruns", stdout=StringIO())
        self.assertEqual(JobRun.objects.filter(job="prune_news").count(), 1)


# ------------------------------------------------------------------------------- team
class TeamTests(Base):
    def test_list_and_grant(self):
        r = client(self.sa).get(reverse("admin-team"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual([g["role"] for g in r.data["grantable"]],
                         ["system_admin", "news_reviewer", "pvc_officer", "membership_officer", "finance_officer", "moderator",
                          "privacy_officer"])
        own = [a for a in r.data["results"] if a["is_own"]]
        self.assertEqual([a["role"] for a in own], ["system_admin"])
        r = client(self.sa).post(reverse("admin-team"), {"email": "U_PLAIN@example.com", "role": "news_reviewer", "note": "Lagos desk"},
                                 format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["granted_by"], self.sa.email)
        self.assertTrue(AuditLog.objects.filter(action="role.granted", actor=self.sa).exists())
        self.assertIn("news", admin_areas(self.plain))

    def test_grant_errors(self):
        c = client(self.sa)
        url = reverse("admin-team")
        cases = [
            ({"email": "nobody@example.com", "role": "news_reviewer"}, 404, "user_not_found"),
            ({"email": "u_plain@example.com", "role": "candidate"}, 400, "invalid_role"),
            ({"email": "u_news@example.com", "role": "news_reviewer"}, 409, "role_exists"),
            ({"email": "u_sa@example.com", "role": "finance_officer"}, 403, "self_change"),
        ]
        for body, code, err in cases:
            r = c.post(url, body, format="json")
            self.assertEqual((r.status_code, r.data["error"]["code"]), (code, err), body)
        self.plain.is_active = False
        self.plain.save()
        r = c.post(url, {"email": "u_plain@example.com", "role": "news_reviewer"}, format="json")
        self.assertEqual(r.data["error"]["code"], "user_inactive")

    def test_only_system_admins(self):
        for u in (self.news, self.fin, self.plain):
            self.assertEqual(client(u).get(reverse("admin-team")).status_code, 403)

    def test_revoke_and_last_admin(self):
        a = RoleAssignment.objects.get(user=self.news, role="news_reviewer")
        r = client(self.sa).post(reverse("admin-team-revoke", args=[a.pk]), {"reason": "Left the team"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.data["is_active"])
        own = RoleAssignment.objects.get(user=self.sa, role="system_admin")
        r = client(self.sa).post(reverse("admin-team-revoke", args=[own.pk]), {}, format="json")
        self.assertEqual(r.data["error"]["code"], "self_change")
        sa2 = role_user("u_sa2", Role.SYSTEM_ADMIN)
        self.assertEqual(client(sa2).post(reverse("admin-team-revoke", args=[own.pk]), {}, format="json").status_code, 200)
        own2 = RoleAssignment.objects.get(user=sa2, role="system_admin")
        with self.assertRaises(RoleError) as e:  # CLI path: no actor, still guarded
            revoke_role(assignment=own2)
        self.assertEqual(e.exception.code, "last_system_admin")
        rv = client(sa2).get(reverse("admin-team") + "?revoked=1").data["results"]
        self.assertTrue(any(not x["is_active"] and x["revoked_by"] == sa2.email for x in rv))

    def test_candidate_links_not_revocable_here(self):
        from apps.access.models import ScopeType

        c = mk_candidate()
        a = grant_role(user=self.plain, role=Role.CANDIDATE, scope_type=ScopeType.CANDIDATE, scope_id=c.pk)
        r = client(self.sa).get(reverse("admin-team"))
        row = next(x for x in r.data["results"] if x["id"] == str(a.pk))
        self.assertEqual(row["scope_label"], "Amina Bello")
        self.assertEqual(client(self.sa).post(reverse("admin-team-revoke", args=[a.pk]), {}, format="json").status_code, 400)


class LastAdminRaceTests(TransactionTestCase):
    def test_two_admins_revoking_each_other(self):
        a, b = role_user("r_a", Role.SYSTEM_ADMIN), role_user("r_b", Role.SYSTEM_ADMIN)
        ra, rb = RoleAssignment.objects.get(user=a), RoleAssignment.objects.get(user=b)
        results, barrier = [], threading.Barrier(2)

        def go(actor, target):
            from django.db import connection as conn

            barrier.wait()
            try:
                revoke_role(assignment=RoleAssignment.objects.get(pk=target.pk), actor=actor)
                results.append("ok")
            except RoleError as exc:
                results.append(exc.code)
            finally:
                conn.close()

        ts = [threading.Thread(target=go, args=(a, rb)), threading.Thread(target=go, args=(b, ra))]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(results), ["last_system_admin", "ok"])
        self.assertEqual(RoleAssignment.objects.filter(role="system_admin", is_active=True).count(), 1)


# ------------------------------------------------------------------------------ users
@mock.patch("apps.accounts.clerk.ban_user", return_value=True)
@mock.patch("apps.accounts.clerk.unban_user", return_value=True)
class UserTests(Base):
    def test_search(self, *_):
        c = client(self.sa)
        self.assertEqual(c.get(reverse("admin-users") + "?q=ab").data["error"]["code"], "query_too_short")
        r = c.get(reverse("admin-users") + "?q=u_plain")
        self.assertEqual([u["email"] for u in r.data["results"]], ["u_plain@example.com"])
        r = c.get(reverse("admin-users") + "?q=Person u_ne")
        self.assertEqual(r.data["results"][0]["roles"], [{"role": "news_reviewer", "scope_type": "global"}])
        self.assertEqual(client(self.news).get(reverse("admin-users") + "?q=u_plain").status_code, 403)

    def test_detail_has_no_political_data_and_is_audited_once(self, *_):
        c = mk_candidate()
        CandidateFollow.objects.create(user=self.plain, candidate=c, notify=True)
        Membership.objects.create(user=self.plain, status="active", activated_at=timezone.now(), membership_number="NDC-KN-000002")
        url = reverse("admin-user", args=[self.plain.pk])
        r = client(self.sa).get(url)
        client(self.sa).get(url)
        self.assertEqual(r.data["phone_masked"], "•••• 4567")
        text = json.dumps(r.data, default=str).lower()
        for leak in ("follow", "membership", "donation", "pvc", "amina", PHONE):
            self.assertNotIn(leak.lower(), text)
        self.assertEqual(AuditLog.objects.filter(action="user.viewed", target_id=str(self.plain.pk)).count(), 1)

    def test_phone_reveal_audited(self, *_):
        r = client(self.sa).post(reverse("admin-user-phone", args=[self.plain.pk]))
        self.assertEqual(r.data["phone_number"], PHONE)
        self.assertTrue(AuditLog.objects.filter(action="user.phone_revealed", actor=self.sa).exists())

    def test_deactivate_and_reactivate(self, unban, ban):
        url = reverse("admin-user-deactivate", args=[self.plain.pk])
        self.assertEqual(client(self.sa).post(url, {}, format="json").status_code, 400)  # reason required
        r = client(self.sa).post(url, {"reason": "Spam"}, format="json")
        self.assertEqual((r.status_code, r.data["clerk_ban"]), (200, "ok"))
        self.assertFalse(r.data["user"]["is_active"])
        self.assertEqual(r.data["user"]["deactivation"]["reason"], "Spam")
        r = client(self.sa).post(reverse("admin-user-reactivate", args=[self.plain.pk]), {}, format="json")
        self.assertTrue(r.data["user"]["is_active"])

    def test_self_and_last_admin(self, *_):
        r = client(self.sa).post(reverse("admin-user-deactivate", args=[self.sa.pk]), {"reason": "x"}, format="json")
        self.assertEqual(r.data["error"]["code"], "self_change")
        with self.assertRaises(RoleError) as e:
            deactivate_user(user=self.sa, reason="x")  # CLI
        self.assertEqual(e.exception.code, "last_system_admin")

    def test_ban_failed_filter_and_card(self, unban, ban):
        ban.return_value = False
        r = client(self.sa).post(reverse("admin-user-deactivate", args=[self.plain.pk]), {"reason": "Spam"}, format="json")
        self.assertEqual(r.data["clerk_ban"], "failed")
        ids = [u["id"] for u in client(self.sa).get(reverse("admin-users") + "?filter=ban_failed").data["results"]]
        self.assertEqual(ids, [str(self.plain.pk)])
        self.assertEqual({c["key"]: c for c in overview(self.sa).data["attention"]}["ban_failed"]["count"], 1)
        ban.return_value = True
        client(self.sa).post(reverse("admin-user-deactivate", args=[self.plain.pk]), {"reason": "Retry"}, format="json")
        self.assertEqual(client(self.sa).get(reverse("admin-users") + "?filter=ban_failed").data["results"], [])


# ------------------------------------------------------------------------------ audit
class AuditViewerTests(Base):
    def test_filters_allow_list_and_self_audit(self):
        record(AuditLog.Action.MEMBERSHIP_REJECTED, target=self.plain, actor=self.mem, reason="Wrong ward", refund_due=True)
        record(AuditLog.Action.USER_DELETED, target=self.plain, actor_label="clerk:webhook", follows_removed=4, roles_revoked=1)
        c = client(self.sa)
        r = c.get(reverse("admin-audit") + "?area=membership")
        self.assertEqual([e["action"] for e in r.data["results"]], ["membership.rejected"])
        self.assertEqual(r.data["results"][0]["summary"], "Wrong ward")
        r = c.get(reverse("admin-audit") + "?action=user.deleted")
        self.assertEqual(r.data["results"][0]["metadata"], {"roles_revoked": 1})  # follows_removed never leaves the server
        self.assertEqual(r.data["results"][0]["actor"], "Clerk")
        r = c.get(reverse("admin-audit") + "?actor=U_MEM@example.com")
        self.assertTrue(all(e["actor"] == "u_mem@example.com" for e in r.data["results"]))
        self.assertEqual(c.get(reverse("admin-audit") + f"?target={self.plain.pk}").data["count"], 2)
        # audit views hidden unless asked for, and recorded once per filter set
        self.assertFalse(any(e["action"].startswith("audit.") for e in c.get(reverse("admin-audit")).data["results"]))
        views = c.get(reverse("admin-audit") + "?area=audit").data["results"]
        self.assertTrue(views and all(e["action"] == "audit.viewed" for e in views))
        stored = AuditLog.objects.filter(action="audit.viewed", metadata__filters__actor__isnull=False).first()
        self.assertEqual(stored.metadata["filters"]["actor"], str(self.mem.pk))  # stored as an ID, not the email
        n = AuditLog.objects.filter(action="audit.viewed").count()
        c.get(reverse("admin-audit") + "?area=membership")
        self.assertEqual(AuditLog.objects.filter(action="audit.viewed").count(), n)

    def test_dates_and_links(self):
        e = record(AuditLog.Action.CANDIDATE_PUBLISHED, target=mk_candidate(), actor=self.sa)
        from apps.core.history import tamper

        with tamper("audit_auditlog"):  # back-date a row (spec 16: audit rows can't normally change)
            AuditLog.objects.filter(pk=e.pk).update(created_at=timezone.now() - timedelta(days=5))
        c = client(self.sa)
        day = (timezone.localtime() - timedelta(days=5)).date().isoformat()
        r = c.get(reverse("admin-audit") + f"?area=candidates&from={day}&to={day}")
        self.assertEqual([x["id"] for x in r.data["results"]], [str(e.pk)])
        self.assertEqual(r.data["results"][0]["link"], f"/admin/candidates/{e.target_id}")
        self.assertEqual(client(self.news).get(reverse("admin-audit")).status_code, 403)
        areas = c.get(reverse("admin-audit-actions")).data["areas"]
        self.assertIn("approved community news", [a["label"] for x in areas if x["area"] == "news" for a in x["actions"]])


# --------------------------------------------------------------------------------- /me
class MeAreasTests(Base):
    def test_admin_areas(self):
        self.assertEqual(admin_areas(self.plain), [])
        self.assertEqual(admin_areas(self.mod), ["overview", "moderation"])
        self.assertEqual(admin_areas(self.news), ["overview", "moderation", "news"])
        self.assertEqual(admin_areas(self.pvc), ["overview", "pvc", "geography"])  # 19 §15.1: the review tab
        self.assertEqual(admin_areas(self.mem), ["overview", "pvc", "membership", "geography"])
        self.assertEqual(admin_areas(self.fin), ["overview", "donations"])
        self.assertEqual(admin_areas(self.sa), ["overview", "candidates", "donations", "pvc", "membership", "geography", "moderation", "news",
                                                "privacy",
                                                "announcements", "posts", "team", "audit", "integrations", "communities", "events"])
        r = client(self.news).get(reverse("me"))
        self.assertEqual(r.data["admin_areas"], ["overview", "moderation", "news"])
