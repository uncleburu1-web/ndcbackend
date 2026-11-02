"""Spec 13 §12 — reporting and moderation."""
import io
import json
import tempfile
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.access.models import Role, ScopeType
from apps.access.services import grant_role
from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.candidates.models import Candidate
from apps.core.exceptions import ServiceError
from apps.moderation import rules as R
from apps.moderation import services as S
from apps.moderation.models import (ALPHABET, Appeal, Case, CaseEvent, ComplianceYear, LegalNotice, Report, Restriction,
                                    new_reference)
from apps.news.models import Article
from apps.notifications.models import Notification
from apps.posts.models import CandidatePost
from apps.search.models import SearchEntry

NOW = timezone.now()
WINDOW = [{"positions": ["president"], "start": (NOW - timedelta(hours=1)).isoformat(), "end": (NOW + timedelta(hours=5)).isoformat()}]
PRIVATE = tempfile.mkdtemp(prefix="ndc-moderation-test-")


def mk_user(cid, *roles, **kw):
    u = User.objects.create_user(cid, email=f"{cid}@example.com", full_name=kw.pop("full_name", f"Person {cid}"), phone_number="+2348031234567",
                                 profile_completed_at=timezone.now(), **kw)
    for r in roles:
        grant_role(user=u, role=r)
    return u


def client(user=None):
    c = APIClient()
    if user:
        c.force_authenticate(user)
    return c


def mk_candidate(slug="amina-bello"):
    return Candidate.objects.create(slug=slug, full_name="Amina Bello", position="president", election_year=2027, state="lagos",
                                    status="published", verified_at=timezone.now(), published_at=timezone.now())


def mk_article(author=None, source="community", title="Polls open early in Ikeja"):
    return Article.objects.create(source=source, status="successful", title=title, body="Polling units opened on time. " * 10,
                                  author=author, state="lagos", category="party" if source == "official" else "",
                                  published_at=timezone.now(), submitted_at=timezone.now())


def mk_post(cand, title="Town hall in Ikeja"):
    return CandidatePost.objects.create(candidate=cand, kind="announcement", title=title, body="Join us.", published_at=timezone.now())


class Base(APITestCase):
    def setUp(self):
        cache.clear()
        self.sa = mk_user("u_sa", Role.SYSTEM_ADMIN)
        self.sa2 = mk_user("u_sa2", Role.SYSTEM_ADMIN)
        self.mod = mk_user("u_mod", Role.MODERATOR)
        self.mod2 = mk_user("u_mod2", Role.MODERATOR)
        self.news = mk_user("u_news", Role.NEWS_REVIEWER)
        self.author = mk_user("u_author")
        self.campaign = mk_user("u_campaign")
        self.cand = mk_candidate()
        grant_role(user=self.campaign, role=Role.CANDIDATE, scope_type=ScopeType.CANDIDATE, scope_id=self.cand.pk)
        self.article = mk_article(self.author)
        self.post = mk_post(self.cand)

    def report(self, kind, obj, reason="false", user=None, ip="10.0.0.1"):
        return S.report(kind, obj, reason, "details", user=user, ip=ip)

    def case(self, kind="article", obj=None):
        return Case.objects.get(target_kind=kind, target_id=(obj or self.article).pk, status__in=["open", "escalated"])

    def act(self, user, case, action, body=None):
        return client(user).post(reverse(f"admin-moderation-case-{action}", kwargs={"pk": case.pk}), body or {}, format="json")


# ================================================================= reporting
class ReportingTests(Base):
    def test_every_reason_maps_to_a_rule_or_route(self):
        ids = {r.id for r in R.RULES}
        for key, (label, rule_id, priority) in R.REASONS.items():
            self.assertTrue(label)
            self.assertTrue(rule_id in ids or priority == "route" or key == "other", key)
            self.assertIn(priority, list(R.TARGET_HOURS) + ["route"], key)
        for key, (label, rule_id, priority) in R.PROFILE_REASONS.items():
            self.assertEqual(priority, "normal", key)
        self.assertEqual([r.id for r in R.RULES], [f"R{i}" for i in range(1, 11)])

    def test_priority_mapping_and_due(self):
        self.report("article", self.article, "spam")
        c = self.case()
        self.assertEqual((c.priority, round((c.due_at - c.opened_at).total_seconds() / 3600)), ("low", 168))
        self.report("article", self.article, "false", ip="10.0.0.2")
        c.refresh_from_db()
        self.assertEqual(c.priority, "high")
        self.report("article", self.article, "abuse", ip="10.0.0.3")
        c.refresh_from_db()
        self.assertEqual(c.priority, "urgent")
        self.assertLessEqual(c.due_at, timezone.now() + timedelta(hours=24))
        self.report("article", self.article, "spam", ip="10.0.0.4")  # priority only goes up
        c.refresh_from_db()
        self.assertEqual(c.priority, "urgent")
        self.assertEqual(Case.objects.count(), 1)  # one open case per item
        self.assertEqual(c.reports.count(), 4)

    def test_election_period_override(self):
        self.assertEqual(R.REASONS["results"][2], "urgent")
        self.report("post", self.post, "violence")
        self.assertEqual(self.case("post", self.post).priority, "urgent")
        with override_settings(CAMPAIGN_BLACKOUTS=WINDOW):
            self.assertTrue(S.election_period())
        self.assertFalse(S.election_period(at=NOW - timedelta(days=400)))

    def test_ncii_legal_deadline_and_copyright_route(self):
        self.report("article", self.article, "ncii")
        c = self.case()
        self.assertEqual(c.priority, "urgent")
        self.assertAlmostEqual((c.legal_due_at - timezone.now()).total_seconds() / 3600, 48, delta=0.1)
        out = S.report("post", self.post, "copyright")
        self.assertEqual(out, {"route": "/report/copyright"})
        self.assertFalse(Report.objects.filter(target_kind="post").exists())
        r = client().post(reverse("candidate-post-report", kwargs={"slug": self.cand.slug, "pk": self.post.pk}), {"reason": "copyright"},
                          format="json")
        self.assertEqual((r.status_code, r.data["route"]), (200, "/report/copyright"))

    def test_one_open_report_per_person_and_reference(self):
        u = mk_user("u_rep")
        a = self.report("article", self.article, "spam", user=u)
        b = self.report("article", self.article, "abuse", user=u)
        self.assertEqual(a["reference"], b["reference"])
        self.assertEqual(Report.objects.get().reason, "abuse")
        refs = {new_reference("RPT") for _ in range(500)}
        self.assertGreater(len(refs), 490)
        for ref in refs:
            self.assertRegex(ref, r"^RPT-[" + ALPHABET + "]{6}$")
            self.assertFalse(set(ref[4:]) & set("01OIL"))

    def test_limits(self):
        from apps.news.views import ReportThrottle

        url = reverse("news-report", kwargs={"pk": self.article.pk})
        with mock.patch.object(ReportThrottle, "THROTTLE_RATES", {"news_report": "2/hour"}):
            codes = [client().post(url, {"reason": "spam"}, format="json", REMOTE_ADDR="10.9.9.9").status_code for _ in range(3)]
        self.assertEqual(codes, [201, 201, 429])

    def test_hidden_items_cant_be_reported(self):
        draft = Article.objects.create(source="community", status="pending", title="x", body="y", author=self.author)
        with self.assertRaises(ServiceError):
            self.report("article", draft)

    def test_candidate_profile_reports_go_to_system_admins(self):
        url = reverse("candidate-report", kwargs={"slug": self.cand.slug})
        r = client().post(url, {"reason": "profile_wrong", "details": "Wrong state"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(client().post(url, {"reason": "results"}, format="json").status_code, 400)  # profile reasons only
        c = self.case("candidate", self.cand)
        self.assertEqual(c.priority, "normal")
        self.assertEqual(self.act(self.mod, c, "keep").status_code, 404)
        self.assertEqual(self.act(self.news, c, "keep").status_code, 404)
        self.assertEqual(self.act(self.sa, c, "keep").status_code, 200)
        self.assertEqual(self.act(self.sa, Case.objects.get(pk=c.pk), "remove", {"rule": "R9"}).status_code, 409)  # closed now
        S.report("candidate", self.cand, "not_ndc", ip="10.1.1.1")
        r = self.act(self.sa, self.case("candidate", self.cand), "remove", {"rule": "R9"})
        self.assertEqual((r.status_code, r.data["error"]["code"]), (409, "invalid_transition"))  # profiles are corrected, not removed

    def test_status_lookup_reveals_only_status(self):
        ref = self.report("article", self.article, "spam")["reference"]
        r = client().get(reverse("report-status"), {"reference": ref.lower()})
        self.assertEqual(r.data, {"reference": ref, "status_label": "Open"})
        self.assertEqual(client().get(reverse("report-status"), {"reference": "RPT-ZZZZZZ"}).status_code, 404)

    def test_my_reports(self):
        u = mk_user("u_rep")
        self.report("article", self.article, "spam", user=u)
        rows = client(u).get(reverse("my-reports")).data["results"]
        self.assertEqual([(x["status_label"], x["title"]) for x in rows], [("Open", self.article.title)])
        self.assertNotIn("reporter", json.dumps(rows, default=str))


# ===================================================================== cases
class CaseTests(Base):
    def test_claim_release_and_auto_release(self):
        self.report("post", self.post)
        c = self.case("post", self.post)
        self.assertEqual(self.act(self.mod, c, "claim").status_code, 200)
        r = self.act(self.mod2, c, "keep")
        self.assertEqual((r.status_code, r.data["error"]["code"]), (403, "not_your_case"))
        self.assertEqual(self.act(self.mod2, c, "claim").data["error"]["code"], "not_your_case")
        Case.objects.filter(pk=c.pk).update(assigned_at=timezone.now() - timedelta(hours=5))
        call_command("moderation_tick", stdout=io.StringIO())
        c.refresh_from_db()
        self.assertIsNone(c.assigned_to_id)
        self.act(self.mod2, c, "claim")
        self.act(self.mod2, c, "release")
        c.refresh_from_db()
        self.assertIsNone(c.assigned_to_id)
        self.assertEqual(list(c.events.values_list("event", flat=True)), ["opened", "report_added", "claimed", "claimed", "released"])

    def test_queue_order_tabs_and_filters(self):
        self.report("article", self.article, "spam")
        self.report("post", self.post, "abuse")
        rows = client(self.mod).get(reverse("admin-moderation-cases")).data["results"]
        self.assertEqual([r["priority"] for r in rows], ["urgent", "low"])  # urgency, then age
        self.assertEqual(rows[0]["top_reason"]["reason"], "abuse")
        self.assertFalse(rows[0]["due"]["overdue"])
        news_rows = client(self.news).get(reverse("admin-moderation-cases")).data["results"]
        self.assertEqual([r["target_kind"] for r in news_rows], ["article"])  # news reviewers: community news only
        self.assertEqual(client(self.mod).get(reverse("admin-moderation-cases"), {"type": "posts"}).data["count"], 1)
        self.assertEqual(client(self.mod).get(reverse("admin-moderation-cases"), {"priority": "urgent"}).data["count"], 1)
        Case.objects.update(due_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(client(self.mod).get(reverse("admin-moderation-cases"), {"overdue": 1}).data["count"], 2)
        self.assertEqual(client(self.mod).get(reverse("admin-moderation-cases"), {"tab": "escalated"}).status_code, 403)
        self.assertEqual(client(self.author).get(reverse("admin-moderation-cases")).status_code, 403)

    def test_escalate(self):
        self.report("post", self.post)
        c = self.case("post", self.post)
        self.assertEqual(self.act(self.mod, c, "escalate").data["error"]["code"], "validation_error")
        self.act(self.mod, c, "escalate", {"note": "Possible legal issue"})
        c.refresh_from_db()
        self.assertEqual(c.status, "escalated")
        self.assertEqual(self.act(self.mod2, c, "keep").status_code, 403)
        self.assertEqual(client(self.sa).get(reverse("admin-moderation-cases"), {"tab": "escalated"}).data["count"], 1)
        self.assertEqual(self.act(self.sa, c, "keep").status_code, 200)


# ================================================================ role table
class RoleTableTests(Base):
    def test_every_role_kind_and_action(self):
        official = mk_article(None, source="official", title="Rally on Saturday")
        targets = {"community": ("article", self.article), "official": ("article", official), "post": ("post", self.post),
                   "candidate": ("candidate", self.cand)}
        allowed = {"sa": {"community", "official", "post", "candidate"}, "mod": {"community", "post"}, "news": {"community"}}
        for who, kinds in allowed.items():
            for name, (kind, obj) in targets.items():
                Case.objects.all().delete()
                reason = "profile_wrong" if kind == "candidate" else "spam"
                S.report(kind, obj, reason, ip=f"10.2.{len(who)}.{len(name)}")
                c = self.case(kind, obj)
                user = getattr(self, who)
                expected = 200 if name in kinds else 404
                for action, body in (("claim", {}), ("keep", {})):
                    r = self.act(user, Case.objects.get(pk=c.pk), action, body)
                    self.assertEqual(r.status_code, expected, (who, name, action, r.data))

    def test_self_moderation(self):
        # own content: a news reviewer's own community story
        own = mk_article(self.news, title="My own story")
        S.report("article", own, "spam")
        r = self.act(self.news, self.case("article", own), "keep")
        self.assertEqual((r.status_code, r.data["error"]["code"]), (403, "self_moderation"))
        # linked candidate: a moderator linked to the candidate
        grant_role(user=self.mod2, role=Role.CANDIDATE, scope_type=ScopeType.CANDIDATE, scope_id=self.cand.pk)
        S.report("post", self.post, "spam")
        r = self.act(self.mod2, self.case("post", self.post), "remove", {"rule": "R8"})
        self.assertEqual(r.data["error"]["code"], "self_moderation")
        # own report
        S.report("article", self.article, "spam", user=self.mod)
        r = self.act(self.mod, self.case(), "keep")
        self.assertEqual(r.data["error"]["code"], "self_moderation")

    def test_official_announcement_system_admin_only(self):
        official = mk_article(None, source="official", title="Rally on Saturday")
        S.report("article", official, "false")
        c = self.case("article", official)
        self.assertEqual(self.act(self.news, c, "remove", {"rule": "R4"}).status_code, 404)
        self.assertEqual(self.act(self.mod, c, "remove", {"rule": "R4"}).status_code, 404)
        self.assertEqual(self.act(self.sa, c, "remove", {"rule": "R4"}).status_code, 200)


# ================================================================= decisions
class DecisionTests(Base):
    def test_remove_needs_rule_then_hides_and_notifies(self):
        reporter = mk_user("u_rep")
        self.report("post", self.post, "abuse", user=reporter)
        c = self.case("post", self.post)
        self.assertTrue(SearchEntry.objects.filter(object_id=self.post.pk).exists())
        r = self.act(self.mod, c, "remove", {"note_to_author": "Please keep it civil"})
        self.assertEqual((r.status_code, r.data["error"]["code"]), (400, "rule_required"))
        self.assertEqual(self.act(self.mod, c, "remove", {"rule": "R99"}).data["error"]["code"], "rule_required")
        r = self.act(self.mod, c, "remove", {"rule": "R2", "note_to_author": "Please keep it civil", "internal_note": "clear case"})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((r.data["decision"], r.data["rule"], r.data["status"]), ("removed", "R2", "closed"))
        self.assertFalse(SearchEntry.objects.filter(object_id=self.post.pk).exists())  # through the app's own service
        gone = client().get(reverse("candidate-post", kwargs={"slug": self.cand.slug, "pk": self.post.pk}))
        self.assertEqual(gone.status_code, 410)
        self.assertEqual({k: gone.data["error"][k] for k in ("removed", "rule", "rule_title", "kind")},
                         {"removed": True, "rule": "R2", "rule_title": R.rule_title("R2"), "kind": "post"})
        self.assertNotIn(self.post.title, json.dumps(gone.data))
        self.assertEqual(Report.objects.get().status, "actioned")
        author_n = Notification.objects.get(recipient=self.campaign, kind="content_removed")
        self.assertEqual(author_n.case_id, c.pk)
        mine = client(self.campaign).get(reverse("my-notifications")).data["results"]
        self.assertEqual(mine[0]["case"]["rule"], "R2")
        self.assertNotIn("u_mod", json.dumps(mine, default=str))  # never names the moderator
        rep_n = client(reporter).get(reverse("my-notifications")).data["results"][0]
        self.assertEqual((rep_n["kind"], rep_n["decision"], rep_n["case"]["title"]), ("report_outcome", "removed", ""))
        view = client(self.campaign).get(reverse("my-moderation")).data
        self.assertTrue(view["removals"][0]["can_appeal"])
        # restore brings it back
        self.act(self.mod2, Case.objects.get(pk=c.pk), "restore", {"note": "Mistake"})
        self.assertEqual(client().get(reverse("candidate-post", kwargs={"slug": self.cand.slug, "pk": self.post.pk})).status_code, 200)
        self.assertTrue(SearchEntry.objects.filter(object_id=self.post.pk).exists())
        self.assertEqual(Case.objects.get(pk=c.pk).decision, "restored")
        for action in ("moderation.removed", "moderation.restored", "post.removed"):
            self.assertTrue(AuditLog.objects.filter(action=action).exists(), action)

    def test_keep_notifies_reporters(self):
        reporter = mk_user("u_rep")
        self.report("article", self.article, "false", user=reporter)
        self.act(self.mod, self.case(), "keep", {"note": "Accurate"})
        n = Notification.objects.get(recipient=reporter)
        self.assertEqual((n.kind, n.decision), ("report_outcome", "kept"))
        self.assertEqual(Report.objects.get().status, "dismissed")
        self.assertEqual(client().get(reverse("news-detail", kwargs={"pk": self.article.pk})).status_code, 200)

    def test_warn_pause_and_submission_block(self):
        self.report("article", self.article, "false")
        c = self.case()
        self.act(self.mod, c, "remove", {"rule": "R4", "warn": True})
        self.assertEqual(Restriction.objects.get().kind, "warning")
        self.assertTrue(Notification.objects.filter(recipient=self.author, kind="account_warning").exists())
        c = Case.objects.get(pk=c.pk)
        self.assertEqual(self.act(self.mod, c, "pause", {"days": 5, "rule": "R4"}).data["error"]["code"], "validation_error")
        r = self.act(self.mod, c, "pause", {"days": 7, "rule": "R4", "reason": "Repeated false stories"})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(Notification.objects.filter(recipient=self.author, kind="submissions_paused").exists())
        body = {"title": "Polls open early in Ikeja", "body": "Polling units in Ikeja opened on time this morning. " * 3, "state": "lagos",
                "source_url": "https://www.inecnigeria.org/x", "declarations": {"accurate": True, "no_private_info": True, "rules": True}}
        r = client(self.author).post(reverse("my-news"), body, format="json")
        self.assertEqual((r.status_code, r.data["error"]["code"]), (403, "submissions_paused"))
        self.assertIn("until", r.data["error"])
        pending = Article.objects.create(source="community", status="pending", title="Old draft", body="x" * 200, author=self.author)
        r = client(self.author).patch(reverse("my-news-detail", kwargs={"pk": pending.pk}), {"title": "New title"}, format="json")
        self.assertEqual(r.data["error"]["code"], "submissions_paused")
        # pauses end on time
        Restriction.objects.filter(kind="news_pause").update(ends_at=timezone.now() - timedelta(minutes=1))
        call_command("moderation_tick", stdout=io.StringIO())
        self.assertIsNone(S.news_paused_until(self.author))
        self.assertEqual(client(self.author).post(reverse("my-news"), body, format="json").status_code, 201)

    def test_suggestions_after_second_and_third_removal(self):
        for i in range(3):
            a = mk_article(self.author, title=f"Story {i}")
            S.report("article", a, "false", ip=f"10.3.0.{i}")
            S.remove(self.case("article", a), self.mod, "R4")
        a = mk_article(self.author, title="Story 4")
        S.report("article", a, "false", ip="10.3.1.1")
        d = client(self.mod).get(reverse("admin-moderation-case", kwargs={"pk": self.case("article", a).pk})).data
        self.assertEqual(d["standing"]["removals"], 3)
        self.assertIn("30-day pause", d["standing"]["suggestion"])
        self.assertEqual(Restriction.objects.count(), 0)  # never automatic

    def test_author_delete_closes_case(self):
        self.report("post", self.post)
        from apps.posts import services as post_services

        post_services.delete_post(self.post, self.campaign)
        self.assertEqual(Case.objects.get().decision, "author_removed")


# =================================================================== appeals
class AppealTests(Base):
    def removed_post_case(self):
        self.report("post", self.post, "abuse")
        c = self.case("post", self.post)
        S.remove(c, self.mod, "R2", "Keep it civil")
        return Case.objects.get(pk=c.pk)

    def test_window_one_only_and_same_decider(self):
        c = self.removed_post_case()
        url = reverse("my-appeals")
        self.assertEqual(client(self.author).post(url, {"case": str(c.pk), "statement": "x"}, format="json").status_code, 403)
        Case.objects.filter(pk=c.pk).update(decided_at=timezone.now() - timedelta(days=15))
        r = client(self.campaign).post(url, {"case": str(c.pk), "statement": "It was fair comment"}, format="json")
        self.assertEqual((r.status_code, r.data["error"]["code"]), (409, "appeal_window_closed"))
        Case.objects.filter(pk=c.pk).update(decided_at=timezone.now())
        self.assertEqual(client(self.campaign).post(url, {"case": str(c.pk), "statement": "It was fair comment"}, format="json").status_code,
                         201)
        r = client(self.campaign).post(url, {"case": str(c.pk), "statement": "Again"}, format="json")
        self.assertEqual(r.data["error"]["code"], "appeal_exists")
        a = Appeal.objects.get()
        rows = client(self.mod).get(reverse("admin-moderation-appeals")).data["results"]
        self.assertEqual((rows[0]["can_decide"], rows[0]["own_decision"]), (False, True))
        r = client(self.mod).post(reverse("admin-moderation-appeal-decide", kwargs={"pk": a.pk}), {"upheld": True}, format="json")
        self.assertEqual((r.status_code, r.data["error"]["code"]), (403, "same_decider"))
        r = client(self.sa).post(reverse("admin-moderation-appeal-decide", kwargs={"pk": a.pk}), {"upheld": True}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(CandidatePost.objects.get(pk=self.post.pk).status, "published")
        self.assertTrue(Notification.objects.filter(recipient=self.campaign, kind="appeal_decision", decision="upheld").exists())
        self.assertIn("appeal_upheld", list(c.events.values_list("event", flat=True)))

    def test_pause_appeal_denied_keeps_pause(self):
        S.report("article", self.article, "false")
        c = self.case()
        S.remove(c, self.mod, "R4")
        pause = S.pause_news(Case.objects.get(pk=c.pk), self.mod, 7, "R4", "Repeated")
        a = S.open_appeal(self.author, "I was right", restriction=pause)
        r = client(self.mod2).post(reverse("admin-moderation-appeal-decide", kwargs={"pk": a.pk}), {"upheld": False}, format="json")
        self.assertEqual(r.data["error"]["code"], "validation_error")  # a reason is required
        client(self.mod2).post(reverse("admin-moderation-appeal-decide", kwargs={"pk": a.pk}), {"upheld": False, "reason": "Rules apply"},
                               format="json")
        self.assertIsNotNone(S.news_paused_until(self.author))
        n = client(self.author).get(reverse("my-notifications")).data["results"][0]
        self.assertEqual((n["kind"], n["appeal"]["status"], n["appeal"]["reason"]), ("appeal_decision", "denied", "Rules apply"))


# ============================================================= legal notices
@override_settings(PRIVATE_MEDIA_ROOT=PRIVATE)
class LegalTests(Base):
    def log(self, **over):
        body = {"kind": "agency", "sender_name": "NITDA", "sender_contact": "legal@nitda.example", "received_at": timezone.now().isoformat(),
                "legal_basis": "NITDA Act s. 6", "items": [f"https://ndcconnect.example/news/community/{self.article.pk}"], **over}
        return client(self.sa).post(reverse("admin-moderation-legal"), body, format="json")

    def test_agency_notice_basis_deadline_items_outcomes_and_hold(self):
        r = self.log(legal_basis="")
        self.assertEqual(r.data["error"]["code"], "legal_basis_required")
        self.assertEqual(self.log(items=["https://example.com/nothing"]).status_code, 400)
        self.assertEqual(client(self.mod).post(reverse("admin-moderation-legal"), {}, format="json").status_code, 403)
        r = self.log()
        self.assertEqual(r.status_code, 201, r.data)
        n = LegalNotice.objects.get()
        self.assertAlmostEqual((n.due_at - n.received_at).total_seconds(), 48 * 3600, delta=1)
        c = self.case()
        self.assertEqual((c.priority, c.source), ("legal", "agency_notice"))
        url = reverse("admin-moderation-legal-acknowledge", kwargs={"pk": n.pk})
        self.assertEqual(client(self.sa).post(url, {}, format="json").status_code, 400)
        client(self.sa).post(url, {"how": "Email to NITDA desk"}, format="json")
        self.assertIsNotNone(LegalNotice.objects.get().acknowledged_at)
        item = n.items.get()
        r = client(self.sa).patch(reverse("admin-moderation-legal-item", kwargs={"pk": n.pk, "item_id": item.pk}), {"outcome": "removed"},
                                  format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Article.objects.get(pk=self.article.pk).removed_rule, "LEGAL")
        gone = client().get(reverse("news-detail", kwargs={"pk": self.article.pk})).data["error"]
        self.assertEqual(gone["source"], "legal")
        self.assertNotIn("NITDA", json.dumps(gone))  # never who sent it
        self.assertEqual(LegalNotice.objects.get().status, "closed")
        client(self.sa).post(reverse("admin-moderation-legal-hold", kwargs={"pk": n.pk}), {"on": True}, format="json")
        Article.objects.filter(pk=self.article.pk).update(removed_at=timezone.now() - timedelta(days=400))
        call_command("prune_news", stdout=io.StringIO())
        self.assertTrue(Article.objects.filter(pk=self.article.pk).exists())  # hold blocks pruning
        Case.objects.update(closed_at=timezone.now() - timedelta(days=400))
        call_command("prune_moderation", stdout=io.StringIO())
        self.assertTrue(Case.objects.exists())

    def test_copyright_form_counter_notice_and_restore(self):
        url = reverse("copyright-notice")
        r = client().post(url, {"name": "Owner", "contact": "o@example.com", "work": "My photo",
                                "url": f"/candidates/{self.cand.slug}/posts/{self.post.pk}", "signature": "Owner"}, format="json")
        self.assertEqual(r.data["error"]["code"], "validation_error")
        self.assertIn("good_faith", r.data["error"]["fields"])
        r = client().post(url, {"name": "Owner", "contact": "o@example.com", "work": "My photo", "signature": "Owner",
                                "url": f"/candidates/{self.cand.slug}/posts/{self.post.pk}", "authority": True, "good_faith": True,
                                "accurate": True}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        n = LegalNotice.objects.get(reference=r.data["reference"])
        c = self.case("post", self.post)
        self.assertEqual((c.priority, c.source), ("urgent", "copyright_notice"))
        S.set_item_outcome(n.items.get(), self.sa, "removed")
        self.assertEqual(CandidatePost.objects.get(pk=self.post.pk).removed_rule, "R7")
        view = client(self.campaign).get(reverse("my-moderation")).data["removals"][0]
        self.assertEqual((view["can_appeal"], view["can_counter_notice"]), (False, True))
        r = client(self.campaign).post(reverse("my-counter-notices"), {"notice": str(n.pk), "text": "I took the photo"}, format="json")
        self.assertEqual(r.status_code, 201)
        with override_settings(COPYRIGHT_COUNTER_NOTICE_DAYS=0):
            S.mark_owner_notified(LegalNotice.objects.get(pk=n.pk), self.sa)
            call_command("moderation_tick", stdout=io.StringIO())
        self.assertTrue(Case.objects.get(pk=c.pk).restorable)
        S.set_item_outcome(n.items.get(), self.sa, "restored", "Owner didn't answer")
        self.assertEqual(CandidatePost.objects.get(pk=self.post.pk).status, "published")

    def test_counter_notice_window_from_setting(self):
        self.log(kind="copyright", legal_basis="", work_description="Photo", signature="Owner", declaration_accepted=True)
        n = LegalNotice.objects.get()
        S.set_item_outcome(n.items.get(), self.sa, "removed")
        S.mark_owner_notified(n, self.sa)
        with override_settings(COPYRIGHT_COUNTER_NOTICE_DAYS=10):
            S.refresh_restorable(timezone.now() + timedelta(days=9))
            self.assertFalse(Case.objects.get().restorable)
            S.refresh_restorable(timezone.now() + timedelta(days=10, minutes=1))
            self.assertTrue(Case.objects.get().restorable)

    def test_attachments_private_and_audited(self):
        self.log()
        n = LegalNotice.objects.get()
        url = reverse("admin-moderation-legal-attachment", kwargs={"pk": n.pk})
        from django.core.files.uploadedfile import SimpleUploadedFile

        bad = SimpleUploadedFile("x.pdf", b"<html>not a pdf</html>", content_type="application/pdf")
        self.assertEqual(client(self.sa).post(url, {"file": bad}, format="multipart").status_code, 400)
        ok = SimpleUploadedFile("notice.pdf", b"%PDF-1.4 test", content_type="application/pdf")
        self.assertEqual(client(self.sa).post(url, {"file": ok}, format="multipart").status_code, 200)
        self.assertTrue(LegalNotice.objects.get().attachment.startswith("legal/"))
        self.assertEqual(client(self.mod).get(url).status_code, 403)
        r = client(self.sa).get(url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b"".join(r.streaming_content), b"%PDF-1.4 test")
        self.assertTrue(AuditLog.objects.filter(action="moderation.attachment_viewed").exists())


# ============================================================ burst & limits
class BurstTests(Base):
    def test_burst_flag_and_no_automatic_removal(self):
        for i in range(4):
            self.report("article", self.article, "false", ip=f"10.4.0.{i}")
        self.assertFalse(self.case().burst_flag)
        self.report("article", self.article, "false", ip="10.4.0.9")
        self.assertTrue(self.case().burst_flag)
        for i in range(100):
            S.report("post", self.post, "spam", ip=f"10.5.{i // 250}.{i % 250}")
        self.assertEqual(client().get(reverse("candidate-post", kwargs={"slug": self.cand.slug, "pk": self.post.pk})).status_code, 200)
        self.assertEqual(self.case("post", self.post).decision, "")

    def test_one_ip_across_items(self):
        items = [mk_article(self.author, title=f"Story {i}") for i in range(5)]
        for a in items:
            S.report("article", a, "spam", ip="10.6.6.6")
        self.assertTrue(Case.objects.get(target_id=items[-1].pk).burst_flag)


# ================================================================ migration
class MigrationTests(TransactionTestCase):
    """Spec 13 §5.3: old 07/09 rows become Report/Case with the same counts and statuses, and land in the queue."""

    def test_old_reports_move_into_cases(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        before = [("moderation", "0001_initial"), ("news", "0002_article_removed_rule"), ("posts", "0002_candidatepost_removed_rule"),
                  ("notifications", "0004_notification_candidate_post_alter_notification_kind_and_more")]
        ex = MigrationExecutor(connection)
        leaves = ex.loader.graph.leaf_nodes()
        ex.migrate(before)
        others = [n for n in leaves if n[0] not in {"moderation", "news", "posts", "notifications"}]
        old = MigrationExecutor(connection).loader.project_state(before + others).apps
        U, Art, Cand, Post = (old.get_model("accounts", "User"), old.get_model("news", "Article"), old.get_model("candidates", "Candidate"),
                              old.get_model("posts", "CandidatePost"))
        ArticleReport, PostReport = old.get_model("news", "ArticleReport"), old.get_model("posts", "PostReport")
        now = timezone.now()
        u = U.objects.create(clerk_user_id="u_old", email="old@example.com")
        a = Art.objects.create(source="community", status="successful", title="Old story", body="x", published_at=now)
        c = Cand.objects.create(slug="old-cand", full_name="Old Cand", position="president", election_year=2027, status="published",
                                verified_at=now, published_at=now)
        p = Post.objects.create(candidate=c, kind="news", title="Old post", body="b", published_at=now)
        ArticleReport.objects.create(article=a, reason="false", status="open", reporter=u)
        ArticleReport.objects.create(article=a, reason="abuse", status="open")
        ArticleReport.objects.create(article=a, reason="spam", status="dismissed", resolved_at=now)
        PostReport.objects.create(post=p, reason="privacy", status="actioned", resolved_at=now)
        PostReport.objects.create(post=p, reason="spam", status="open")
        MigrationExecutor(connection).migrate(leaves)
        self.assertEqual(Report.objects.count(), 5)
        self.assertEqual(sorted(Report.objects.values_list("status", flat=True)), ["actioned", "dismissed", "open", "open", "open"])
        art_open = Case.objects.get(target_kind="article", status="open")
        self.assertEqual((art_open.priority, art_open.reports.count()), ("urgent", 2))
        self.assertEqual(Case.objects.get(target_kind="post", status="closed").decision, "removed")
        self.assertEqual(Case.objects.filter(status="closed").count(), 2)
        sa = mk_user("u_sa_mig", Role.SYSTEM_ADMIN)
        self.assertEqual(client(sa).get(reverse("admin-moderation-cases")).data["count"], 2)  # open news and post reports in the queue


# =============================================================== compliance
class ComplianceTests(Base):
    def test_figures_csv_and_no_personal_data(self):
        year = timezone.localtime().year
        self.report("article", self.article, "results")
        S.remove(self.case(), self.mod, "R3")
        self.report("post", self.post, "abuse")
        S.remove(self.case("post", self.post), self.mod, "R2")
        S.restore(Case.objects.get(target_id=self.post.pk), self.mod2)
        other = mk_article(self.author, title="Another")
        self.report("article", other, "spam")
        rep = client(self.sa).get(reverse("admin-moderation-compliance"), {"year": year}).data
        f = {i["key"]: i["value"] for i in rep["items"]}
        self.assertEqual((f["removed_user_report"], f["restored"], f["complaints"], f["resolved"], f["unresolved"]), (2, 1, 3, 2, 1))
        self.assertEqual((f["disinformation"], f["harmful"], f["large_platform"]), (1, 1, "No"))
        self.assertTrue(all(i["definition"] for i in rep["items"]))
        self.assertEqual(client(self.mod).get(reverse("admin-moderation-compliance")).status_code, 403)
        r = client(self.sa).get(reverse("admin-moderation-compliance"), {"year": year, "format": "csv"})
        text = r.content.decode()
        self.assertIn("Disinformation", text)
        for leak in ("u_author", "u_mod", "@example.com", self.article.title):
            self.assertNotIn(leak, text)
        self.assertTrue(AuditLog.objects.filter(action="moderation.compliance_exported").exists())


# ================================================================== privacy
class PrivacyTests(Base):
    def test_reporter_masked_reveal_audited_and_deletion(self):
        u = mk_user("u_rep")
        self.report("post", self.post, "abuse", user=u)
        c = self.case("post", self.post)
        d = client(self.mod).get(reverse("admin-moderation-case", kwargs={"pk": c.pk})).data
        self.assertEqual(d["reports"][0]["reporter_label"], "Signed-in member")
        self.assertNotIn("u_rep", json.dumps(d, default=str))
        rid = d["reports"][0]["id"]
        url = reverse("admin-moderation-reporter", kwargs={"pk": c.pk, "report_id": rid})
        self.assertEqual(client(self.mod).post(url).status_code, 403)
        self.assertEqual(client(self.sa).post(url).data["reporter"]["email"], "u_rep@example.com")
        self.assertTrue(AuditLog.objects.filter(action="moderation.reporter_viewed").exists())
        self.assertIn("reporter_viewed", list(CaseEvent.objects.values_list("event", flat=True)))
        from apps.accounts.services import mark_deleted

        mark_deleted(u.clerk_user_id)
        self.assertFalse(Report.objects.filter(reporter=u).exists())
        self.assertEqual(Report.objects.get().contact_email, "")

    def test_prune_keeps_aggregates(self):
        self.report("article", self.article, "false")
        S.keep(self.case(), self.mod)
        old = timezone.now() - timedelta(days=400)
        Case.objects.update(opened_at=old, closed_at=old)
        Report.objects.update(created_at=old)
        held = mk_article(self.author, title="Held")
        S.report("article", held, "spam", ip="10.7.7.7")
        S.keep(self.case("article", held), self.mod)
        Case.objects.filter(target_id=held.pk).update(opened_at=old, closed_at=old, legal_hold=True)
        before = S._compute(timezone.localtime(old).year)
        out = S.prune()
        self.assertEqual(out["cases"], 1)
        self.assertEqual(list(Case.objects.values_list("target_id", flat=True)), [held.pk])  # the hold stays
        frozen = ComplianceYear.objects.get(year=timezone.localtime(old).year)
        self.assertEqual(frozen.figures["complaints"], before["complaints"])  # aggregates outlive the rows
        self.assertTrue(S.compliance_report(frozen.year)["frozen"])
