"""Spec 17 §19.1."""
import io
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.access.models import Role, ScopeType
from apps.access.services import grant_role
from apps.accounts.models import User
from apps.candidates.models import Candidate
from apps.communities import retention, services, sync
from apps.communities.models import (Community, CommunityEvent, CommunityMembership, CommunityPost, CommunityReaction,
                                     How, Level)
from apps.core.integrity import _community_chain_broken, _community_counts_drift
from apps.geo import loading
from apps.geo.models import PollingUnit
from apps.moderation import services as M
from apps.moderation.models import Case, Restriction
from apps.notifications.models import Notification
from apps.privacy import consent
from apps.pvc.models import PvcVerification

SAMPLE = (Path(settings.BASE_DIR) / "data" / "geo" / "sample.csv").read_bytes()
PURPOSE = "community_membership"


def mk_user(cid, *roles, name=None, consent_given=True, age_days=3):
    then = timezone.now() - timedelta(days=age_days)
    u = User.objects.create_user(cid, email=f"{cid}@example.com", full_name=name or f"Ada {cid.title()}", phone_number="+2348031234567",
                                 profile_completed_at=then, adult_confirmed_at=then)
    for r in roles:
        grant_role(user=u, role=r)
    if consent_given:
        consent.grant(u, PURPOSE)
    return u


def verify(user, pu_delim="24-07-05-001"):
    pu = PollingUnit.objects.select_related("ward__lga__state").get(delimitation=pu_delim)
    return PvcVerification.objects.update_or_create(user=user, defaults=dict(
        vin_last4="1234", surname="Eze", state=pu.ward.lga.state.slug, lga=pu.ward.lga.name, polling_unit=pu.name, status="successful",
        consent_at=timezone.now(), reviewed_at=timezone.now(), geo_lga=pu.ward.lga, geo_ward=pu.ward, geo_polling_unit=pu))[0]


def c(path):
    return Community.objects.get(path=path)


def client(user=None):
    cl = APIClient()
    if user:
        cl.force_authenticate(user)
    return cl


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        loading.load(SAMPLE, source="sample")
        sync.sync()


# ---------------------------------------------------------------- structure
class StructureTests(Base):
    def test_sync_creates_every_level_once(self):
        self.assertEqual(Community.objects.filter(level=Level.NATIONAL).count(), 1)
        self.assertEqual(Community.objects.filter(level=Level.STATE).count(), 37)
        self.assertEqual((Community.objects.filter(level=Level.LGA).count(), Community.objects.filter(level=Level.WARD).count(),
                          Community.objects.filter(level=Level.POLLING_UNIT).count()), (3, 4, 5))
        pu = c("lagos/ikeja/05/001")
        self.assertEqual([x.path for x in services.chain(pu)], ["lagos/ikeja/05/001", "lagos/ikeja/05", "lagos/ikeja", "lagos", "national"])
        self.assertEqual(pu.name, "Polling unit 001 · Sample Primary School I")
        self.assertEqual(c("lagos/ikeja/05").name, "Ward 05 · Anifowoshe / Ikeja")
        self.assertEqual(c("fct").name, "Federal Capital Territory")
        self.assertEqual(c("akwa-ibom").name, "Akwa Ibom State")
        self.assertEqual((c("lagos").read_scope, c("lagos/ikeja").read_scope), ("signed_in", "members"))
        again = sync.sync()
        self.assertEqual((sum(again.created.values()), again.updated), (0, 0))

    def test_deactivated_geography_closes_the_community(self):
        PollingUnit.objects.filter(delimitation="24-07-05-002").update(active=False)
        r = sync.sync()
        self.assertEqual(r.deactivated, 1)
        self.assertFalse(c("lagos/ikeja/05/002").active)

    def test_dry_run_and_command(self):
        Community.objects.filter(level=Level.POLLING_UNIT).delete()
        r = sync.sync(dry_run=True)
        self.assertEqual(r.created["polling_unit"], 5)
        self.assertFalse(Community.objects.filter(level=Level.POLLING_UNIT).exists())
        out = io.StringIO()
        call_command("sync_communities", stdout=out)
        self.assertIn("5 polling-unit", out.getvalue())


class ConstraintTests(Base):
    def refused(self, fn):
        with self.assertRaises(IntegrityError), transaction.atomic():
            fn()

    def test_shapes(self):
        nat = c("national")
        self.refused(lambda: Community.objects.create(level="national", state="", name="x", path="x"))
        self.refused(lambda: Community.objects.create(level="state", state="", name="x", path="y", parent=nat))
        self.refused(lambda: Community.objects.create(level="lga", state="lagos", name="x", path="z", parent=nat))
        self.refused(lambda: Community.objects.create(level="ward", state="lagos", name="x", path="w", parent=nat,
                                                      geo_lga=c("lagos/ikeja").geo_lga))
        u = mk_user("u_c")
        self.refused(lambda: CommunityMembership.objects.create(user=u, community=c("lagos/ikeja"), level="lga", how="chosen"))
        CommunityMembership.objects.create(user=u, community=c("lagos"), level="state", how="chosen")
        self.refused(lambda: CommunityMembership.objects.create(user=u, community=c("kano"), level="state", how="chosen"))
        self.refused(lambda: CommunityMembership.objects.create(user=u, community=c("national"), level="national", how="chosen"))
        p = CommunityPost.objects.create(community=c("national"), author=u, body="Hello there all of you")
        CommunityReaction.objects.create(post=p, user=u)
        self.refused(lambda: CommunityReaction.objects.create(post=p, user=u))
        self.refused(lambda: CommunityPost.objects.filter(pk=p.pk).update(status="removed"))

    def test_events_append_only(self):
        u = mk_user("u_e")
        p = services.create_post(u, c("national"), "A first post for the nation")
        e = CommunityEvent.objects.filter(post=p).first()
        with self.assertRaisesRegex(IntegrityError, "append-only"), transaction.atomic():
            CommunityEvent.objects.filter(pk=e.pk).update(note="x")


# ---------------------------------------------------------------- placement
class PlacementTests(Base):
    def levels(self, u):
        return {m.level: (m.community.path, m.how) for m in services.memberships(u)}

    def test_needs_consent_and_a_successful_pvc(self):
        u = mk_user("u_p1", consent_given=False)
        verify(u)
        self.assertEqual(services.place(u), [])
        self.assertEqual(self.levels(u), {})
        consent.grant(u, PURPOSE)
        joined = services.place(u)
        self.assertEqual(len(joined), 4)
        self.assertEqual(self.levels(u), {"state": ("lagos", How.VERIFIED), "lga": ("lagos/ikeja", How.VERIFIED),
                                          "ward": ("lagos/ikeja/05", How.VERIFIED), "polling_unit": ("lagos/ikeja/05/001", How.VERIFIED)})
        self.assertEqual(c("lagos/ikeja/05/001").member_count, 1)
        self.assertTrue(Notification.objects.filter(recipient=u, kind="community_placed").exists())
        self.assertEqual(services.place(u), [])  # idempotent

    def test_moves_and_ends(self):
        u = mk_user("u_p2")
        verify(u)
        services.place(u)
        verify(u, "24-07-06-001")  # a different ward and polling unit, same LGA
        services.place(u)
        lv = self.levels(u)
        self.assertEqual((lv["ward"][0], lv["polling_unit"][0], lv["lga"][0]), ("lagos/ikeja/06", "lagos/ikeja/06/001", "lagos/ikeja"))
        self.assertEqual(CommunityMembership.objects.get(user=u, community=c("lagos/ikeja/05/001")).end_reason, "moved")
        self.assertEqual(c("lagos/ikeja/05/001").member_count, 0)
        from apps.pvc.services import erase_for_withdrawn_consent

        erase_for_withdrawn_consent(u)
        self.assertEqual(self.levels(u), {})
        self.assertEqual(set(CommunityMembership.objects.filter(user=u, end_reason="pvc_ended").values_list("level", flat=True)),
                         {"state", "lga", "ward", "polling_unit"})

    def test_chosen_state_replaced_or_confirmed(self):
        u = mk_user("u_p3")
        services.choose_state(u, "kano")
        verify(u)
        services.place(u)
        self.assertEqual(self.levels(u)["state"], ("lagos", How.VERIFIED))
        self.assertEqual(Notification.objects.get(recipient=u, kind="community_placed").decision, "moved")
        u2 = mk_user("u_p3b")
        services.choose_state(u2, "lagos")
        verify(u2)
        services.place(u2)
        self.assertEqual(self.levels(u2)["state"], ("lagos", How.VERIFIED))
        self.assertEqual(CommunityMembership.objects.filter(user=u2, level="state").count(), 1)  # same row, now verified

    def test_left_levels_respected_and_rejoin(self):
        u = mk_user("u_p4")
        verify(u)
        services.place(u)
        services.leave(u, c("lagos/ikeja/05/001"))
        services.place(u)
        self.assertNotIn("polling_unit", self.levels(u))
        services.rejoin(u, "polling_unit")
        self.assertIn("polling_unit", self.levels(u))
        services.leave(u, c("lagos"))
        self.assertEqual(self.levels(u), {})
        services.place(u)
        self.assertEqual(self.levels(u), {})

    def test_never_uses_the_membership_address(self):
        from apps.membership.models import Membership

        u = mk_user("u_p5")
        Membership.objects.create(user=u, status="pending_approval", state="lagos", full_name="X", lga="Agege", ward="Sample Ward One")
        services.place(u)
        self.assertEqual(self.levels(u), {})

    def test_pvc_decision_enqueues_user_id_only(self):
        from apps.core.models import OutboxJob
        from apps.pvc.services import verify as officer_verify

        u, officer = mk_user("u_p6"), mk_user("u_off", Role.PVC_OFFICER)
        rec = PvcVerification.objects.create(user=u, vin_last4="1", surname="S", state="lagos", lga="Ikeja", status="pending",
                                             consent_at=timezone.now())
        officer_verify(rec, officer)
        jobs = OutboxJob.objects.filter(kind="communities_place", payload__user=str(u.pk))
        self.assertEqual([j.payload for j in jobs], [{"user": str(u.pk)}])

    def test_state_change_rule(self):
        u = mk_user("u_p7")
        services.choose_state(u, "kano")
        with self.assertRaises(Exception) as e:
            services.choose_state(u, "oyo")
        self.assertEqual(e.exception.get_codes(), "state_change_too_soon")

    def test_resubmitted_pvc_clears_links(self):
        from apps.privacy.consent import grant
        from apps.pvc.services import submit

        u = mk_user("u_p8")
        verify(u)
        services.place(u)
        grant(u, "pvc_check")
        submit(u, vin="90F5B12345678901234", surname="Eze", state="lagos", lga="Agege")
        rec = PvcVerification.objects.get(user=u)
        self.assertIsNone(rec.geo_polling_unit_id)
        services.place(u)
        self.assertEqual(self.levels(u), {})


# ---------------------------------------------------------------- access
class AccessTests(Base):
    def setUp(self):
        self.voter = mk_user("u_voter")
        verify(self.voter)
        services.place(self.voter)

    def test_matrix(self):
        guest, unconsented, chosen, other = client(), mk_user("u_unc", consent_given=False), mk_user("u_ch"), mk_user("u_other")
        services.choose_state(chosen, "lagos")
        services.choose_state(other, "kano")
        lagos, pu = c("lagos"), c("lagos/ikeja/05/001")
        self.assertEqual(guest.get(reverse("community-posts", kwargs={"pk": lagos.pk})).status_code, 401)
        self.assertEqual(guest.get(reverse("community", kwargs={"pk": lagos.pk})).status_code, 200)
        self.assertEqual(client(unconsented).get(reverse("community-posts", kwargs={"pk": lagos.pk})).status_code, 200)
        r = client(unconsented).post(reverse("community-posts", kwargs={"pk": lagos.pk}), {"body": "Hello Lagos, good morning"}, format="json")
        self.assertEqual((r.status_code, r.data["error"]["code"], r.data["error"]["purpose"]), (403, "consent_required", PURPOSE))
        self.assertEqual(client(chosen).post(reverse("community-posts", kwargs={"pk": lagos.pk}), {"body": "Hello Lagos, good morning"},
                                             format="json").status_code, 201)
        r = client(other).post(reverse("community-posts", kwargs={"pk": lagos.pk}), {"body": "Hello Lagos from Kano"}, format="json")
        self.assertEqual(r.data["error"]["code"], "not_a_member")
        self.assertEqual(client(other).get(reverse("community-posts", kwargs={"pk": pu.pk})).status_code, 404)
        r = client(chosen).post(reverse("community-posts", kwargs={"pk": pu.pk}), {"body": "Hello polling unit"}, format="json")
        self.assertEqual(r.data["error"]["code"], "verification_required")
        self.assertEqual(client(self.voter).get(reverse("community-posts", kwargs={"pk": pu.pk})).status_code, 200)
        self.assertEqual(client(self.voter).post(reverse("community-posts", kwargs={"pk": pu.pk}), {"body": "Hello polling unit"},
                                                 format="json").status_code, 201)
        n = client(chosen).post(reverse("community-posts", kwargs={"pk": c("national").pk}), {"body": "Hello Nigeria, good day"}, format="json")
        self.assertEqual(n.status_code, 201)

    def test_switches_pause_and_wait(self):
        lagos = c("lagos")
        Community.objects.filter(pk=lagos.pk).update(posting_open=False)
        self.assertEqual(services.post_block(self.voter, c("lagos"))[0], "posting_paused")
        Community.objects.filter(pk=lagos.pk).update(posting_open=True, ndc_members_only=True)
        self.assertEqual(services.post_block(self.voter, c("lagos"))[0], "ndc_members_only")
        Community.objects.filter(pk=lagos.pk).update(ndc_members_only=False)
        services.pause(self.voter, None, 7, "R2", community=c("lagos"))
        self.assertEqual(services.post_block(self.voter, c("lagos/ikeja/05"))[0], "posting_paused")  # covers the levels below
        self.assertIsNone(services.post_block(self.voter, c("national")))
        self.assertTrue(services.can_read(self.voter, c("lagos")))
        new = mk_user("u_new", age_days=0)
        self.assertEqual(services.post_block(new, c("national"))[0], "too_soon")

    def test_no_member_lists_and_banded_counts(self):
        r = client(self.voter).get(reverse("community", kwargs={"pk": c("lagos").pk})).data
        self.assertEqual(r["members"], "Fewer than 10")
        text = str(client(self.voter).get(reverse("communities")).data) + str(r)
        self.assertNotIn(self.voter.email, text)
        self.assertNotIn("member_count", text)
        self.assertEqual(client(self.voter).get(reverse("community-posts", kwargs={"pk": c("lagos").pk}))["X-Robots-Tag"], "noindex")

    def test_lookup_and_children(self):
        r = client().get(reverse("community-lookup"), {"path": "lagos/ikeja"})
        self.assertEqual(r.data["name"], "Ikeja LGA")
        self.assertEqual([b["path"] for b in r.data["breadcrumb"]], ["national", "lagos", "lagos/ikeja"])
        kids = client().get(reverse("communities"), {"parent": str(c("lagos").pk)}).data
        self.assertEqual({k["path"] for k in kids}, {"lagos/ikeja", "lagos/agege"})
        self.assertEqual(len(client().get(reverse("communities")).data), 38)


# ---------------------------------------------------------------- posts, comments, likes
class PostTests(Base):
    def setUp(self):
        self.a, self.b = mk_user("u_a", name="Amaka Eze"), mk_user("u_b")
        self.nat = c("national")

    def code(self, fn):
        with self.assertRaises(Exception) as e:
            fn()
        return e.exception.get_codes() if hasattr(e.exception, "get_codes") else str(e.exception)

    def test_body_rules_and_duplicates(self):
        with self.assertRaises(Exception):
            services.create_post(self.a, self.nat, "short")
        with self.assertRaises(Exception):
            services.create_post(self.a, self.nat, "x" * 2001)
        with self.assertRaises(Exception):
            services.create_post(self.a, self.nat, "links " + " ".join(f"https://e{i}.ng" for i in range(6)))
        with self.assertRaises(Exception):
            services.create_post(self.a, self.nat, "see javascript:alert(1) now please")
        p = services.create_post(self.a, self.nat, "Polls open at eight​ tomorrow!!!!!!!!!!!!!!!!")
        self.assertNotIn("​", p.body)
        self.assertTrue(p.body.endswith("!" * 8))
        self.assertEqual(self.code(lambda: services.create_post(self.a, c("national"), "Polls  open at EIGHT tomorrow!!!!!!!!")),
                         "duplicate_post")
        self.assertEqual(c("national").post_count, 1)

    def test_edit_window_delete_and_counts(self):
        p = services.create_post(self.a, self.nat, "First version of my post")
        services.edit_post(self.a, p, "Second version of my post")
        p.refresh_from_db()
        self.assertIsNotNone(p.edited_at)
        self.assertEqual(CommunityEvent.objects.get(post=p, kind="edited").previous_body, "First version of my post")
        with self.assertRaises(Exception):
            services.edit_post(self.b, p, "Someone else editing it")
        M.report("community_post", p, "spam", user=self.b)
        self.assertEqual(self.code(lambda: services.edit_post(self.a, p, "Third version after report")), "edit_window_closed")
        CommunityPost.objects.filter(pk=p.pk).update(created_at=timezone.now() - timedelta(minutes=31))
        services.delete_post(self.a, CommunityPost.objects.get(pk=p.pk))
        p.refresh_from_db()
        self.assertEqual((p.status, c("national").post_count), ("deleted", 0))
        self.assertEqual(Case.objects.get(target_id=p.pk).decision, "author_removed")

    def test_comments_and_folded_replies(self):
        p = services.create_post(self.a, self.nat, "What do you think about turnout?")
        services.create_comment(self.b, p, "Turnout will be high")
        services.create_comment(self.b, p, "Especially in Lagos")
        n = Notification.objects.get(recipient=self.a, kind="community_reply")
        self.assertEqual(n.count, 2)
        self.assertEqual(CommunityPost.objects.get(pk=p.pk).comment_count, 2)
        services.create_comment(self.a, p, "Thanks both")
        self.assertEqual(Notification.objects.filter(recipient=self.a).count(), 1)  # not for your own comment
        CommunityPost.objects.filter(pk=p.pk).update(status="hidden", hidden_at=timezone.now())
        with self.assertRaises(Exception):
            services.create_comment(self.b, CommunityPost.objects.get(pk=p.pk), "On a hidden post")

    def test_reactions_private(self):
        p = services.create_post(self.a, self.nat, "Like this if you will vote")
        self.assertEqual(services.react(self.b, p, True), {"liked": True, "reaction_count": 1})
        self.assertEqual(services.react(self.b, p, True)["reaction_count"], 1)
        data = client(self.a).get(reverse("community-post", kwargs={"pk": p.pk})).data
        self.assertEqual((data["reaction_count"], data["liked"]), (1, False))
        self.assertNotIn(self.b.email, str(data))
        self.assertNotIn("u_b", str(data))
        self.assertEqual(services.react(self.b, p, False)["reaction_count"], 0)

    def test_bylines(self):
        p = services.create_post(self.a, self.nat, "Byline check for this post")
        data = client(self.b).get(reverse("community-post", kwargs={"pk": p.pk})).data
        self.assertEqual((data["author"]["name"], data["author"]["verified"], data["author"]["is_you"]), ("Amaka E.", False, False))

    @override_settings(CAMPAIGN_BLACKOUTS=[])
    def test_campaign_label_and_blackout(self):
        cand = Candidate.objects.create(slug="kola", full_name="Kola Ade", position="governor", election_year=2027, state="lagos",
                                        status="published", verified_at=timezone.now(), published_at=timezone.now())
        grant_role(user=self.a, role=Role.CANDIDATE, scope_type=ScopeType.CANDIDATE, scope_id=cand.pk)
        p = services.create_post(self.a, self.nat, "From the campaign team")
        self.assertEqual(p.campaign_candidate_id, cand.pk)
        data = client(self.b).get(reverse("community-post", kwargs={"pk": p.pk})).data
        self.assertEqual(data["author"]["campaign"], "Campaign team · Kola Ade")
        now = timezone.now()
        window = [{"positions": ["governor"], "start": (now - timedelta(hours=1)).isoformat(), "end": (now + timedelta(hours=1)).isoformat()}]
        with override_settings(CAMPAIGN_BLACKOUTS=window):
            self.assertEqual(self.code(lambda: services.create_post(self.a, self.nat, "During the blackout window")), "publishing_paused")
            r = client(self.a).post(reverse("community-posts", kwargs={"pk": self.nat.pk}), {"body": "Blackout over the API"}, format="json")
            self.assertEqual((r.status_code, r.data["error"]["code"]), (409, "publishing_paused"))  # §18: 409 with the end time
            self.assertIn("until", r.data["error"])
            services.create_post(self.b, self.nat, "Citizens can still talk")  # ordinary members unaffected


# ---------------------------------------------------------------- moderation
class ModerationTests(Base):
    def setUp(self):
        self.author = mk_user("u_auth")
        verify(self.author)
        services.place(self.author)
        self.post = services.create_post(self.author, c("lagos/ikeja"), "A post in Ikeja about roads")
        self.reporter = mk_user("u_rep")
        M.report("community_post", self.post, "false", user=self.reporter)  # "high", so the 48-hour rule can raise it
        self.case = Case.objects.get(target_kind="community_post", target_id=self.post.pk)
        self.lagos_mod = mk_user("u_lmod")
        grant_role(user=self.lagos_mod, role=Role.MODERATOR, scope_type=ScopeType.COMMUNITY, scope_id=c("lagos").pk)
        self.kano_mod = mk_user("u_kmod")
        grant_role(user=self.kano_mod, role=Role.MODERATOR, scope_type=ScopeType.COMMUNITY, scope_id=c("kano").pk)
        self.other_mod = mk_user("u_lmod2")
        grant_role(user=self.other_mod, role=Role.MODERATOR, scope_type=ScopeType.COMMUNITY, scope_id=c("lagos/ikeja").pk)

    def test_scope(self):
        self.assertIn(self.case, M.visible_cases(self.lagos_mod))
        self.assertNotIn(self.case, M.visible_cases(self.kano_mod))
        self.assertTrue(M.can_view(self.lagos_mod, self.case))
        self.assertFalse(M.can_view(self.kano_mod, self.case))
        self.assertTrue(M.is_moderation_staff(self.kano_mod))
        from apps.dashboard.services import admin_areas

        self.assertEqual(admin_areas(self.kano_mod), ["overview", "moderation"])

    def test_hide_remove_appeal_restore(self):
        M.claim(self.case, self.lagos_mod)
        M.hide(self.case, self.lagos_mod, "checking")
        self.post.refresh_from_db()
        self.assertEqual((self.post.status, c("lagos/ikeja").post_count), ("hidden", 0))
        data = client(self.author).get(reverse("community-post", kwargs={"pk": self.post.pk})).data
        self.assertEqual((data["body"], data["notice"]["kind"]), ("", "hidden"))
        M.remove(Case.objects.get(pk=self.case.pk), self.lagos_mod, "R2", "Please keep it civil")
        self.post.refresh_from_db()
        self.assertEqual((self.post.status, self.post.removed_rule), ("removed", "R2"))
        self.assertTrue(Notification.objects.filter(recipient=self.author, kind="content_removed").exists())
        notice = client(self.author).get(reverse("community-post", kwargs={"pk": self.post.pk})).data["notice"]
        self.assertEqual((notice["kind"], notice["rule"]), ("removed", "R2"))
        appeal = M.open_appeal(self.author, "I was quoting someone, please look again.", case=Case.objects.get(pk=self.case.pk))
        self.assertFalse(M.can_decide_appeal(self.lagos_mod, appeal))
        self.assertTrue(M.can_decide_appeal(self.other_mod, appeal))
        M.decide_appeal(appeal, self.other_mod, upheld=True, reason="Context makes it fine")
        self.post.refresh_from_db()
        self.assertEqual(self.post.status, "published")

    def test_keep_unhides_and_overdue_becomes_urgent(self):
        M.claim(self.case, self.lagos_mod)
        M.hide(self.case, self.lagos_mod)
        CommunityPost.objects.filter(pk=self.post.pk).update(hidden_at=timezone.now() - timedelta(hours=49))
        self.assertEqual(M.hidden_overdue(), 1)
        self.assertEqual(Case.objects.get(pk=self.case.pk).priority, "urgent")
        M.keep(Case.objects.get(pk=self.case.pk), self.lagos_mod)
        self.assertEqual(CommunityPost.objects.get(pk=self.post.pk).status, "published")

    def test_no_self_moderation_and_pause(self):
        own = mk_user("u_selfmod")
        grant_role(user=own, role=Role.MODERATOR)
        p = services.create_post(own, c("national"), "A moderator posting as a citizen")
        M.report("community_post", p, "spam", user=self.reporter)
        case = Case.objects.get(target_id=p.pk)
        with self.assertRaises(Exception) as e:
            M.claim(case, own)
        self.assertEqual(e.exception.get_codes(), "self_moderation")
        r = M.pause_community(self.case, self.lagos_mod, 7, "R2")
        self.assertEqual((r.kind, r.community_id), (Restriction.Kind.COMMUNITY_PAUSE, c("lagos/ikeja").pk))
        self.assertEqual(services.post_block(self.author, c("lagos/ikeja/05"))[0], "posting_paused")
        self.assertIsNone(services.post_block(self.author, c("national")))
        with self.assertRaises(Exception):
            M.pause_community(self.case, self.lagos_mod, 7, "R2", scope="all")

    def test_compliance_split(self):
        M.claim(self.case, self.lagos_mod)
        M.remove(Case.objects.get(pk=self.case.pk), self.lagos_mod, "R2")
        rep = M.compliance_report(timezone.localtime().year)
        self.assertEqual(rep["removed_by_kind"]["community_post"], 1)

    def test_api_hide(self):
        M.claim(self.case, self.lagos_mod)
        r = client(self.lagos_mod).post(reverse("admin-moderation-case-hide", kwargs={"pk": self.case.pk}), {"note": "x"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["item"]["status"], "hidden")
        self.assertEqual(client(self.kano_mod).get(reverse("admin-moderation-case", kwargs={"pk": self.case.pk})).status_code, 404)


# ---------------------------------------------------------------- announcements
class AnnouncementTests(Base):
    def setUp(self):
        self.admin = mk_user("u_sa", Role.SYSTEM_ADMIN)
        self.ikeja_member = mk_user("u_ik")
        verify(self.ikeja_member)
        services.place(self.ikeja_member)

    def test_audience_required_pinned_below_and_delivered(self):
        from apps.news import announcements
        from apps.notifications.services import deliver_announcement

        with self.assertRaises(Exception):
            announcements.publish(self.admin, {"category": "community", "title": "Town hall", "body": "Saturday at ten."})
        a = announcements.publish(self.admin, {"category": "community", "title": "Town hall", "body": "Saturday at ten.",
                                               "audience_community": c("lagos/ikeja").pk})
        self.assertEqual([x.pk for x in services.pinned_announcements(c("lagos/ikeja/05/001"))], [a.pk])
        self.assertEqual(list(services.pinned_announcements(c("lagos/agege"))), [])
        self.assertEqual(deliver_announcement(a), 1)
        self.assertTrue(Notification.objects.filter(recipient=self.ikeja_member, kind="announcement", article=a).exists())
        data = client(self.ikeja_member).get(reverse("community-posts", kwargs={"pk": c("lagos/ikeja/05").pk})).data
        self.assertEqual(data["pinned"][0]["audience"]["path"], "lagos/ikeja")


# ---------------------------------------------------------------- privacy and retention
class PrivacyTests(Base):
    def setUp(self):
        self.u = mk_user("u_priv")
        verify(self.u)
        services.place(self.u)
        self.other = mk_user("u_oth")
        self.p = services.create_post(self.u, c("national"), "My view on the election")
        services.react(self.other, self.p, True)
        services.react(self.u, self.p, True)

    def test_withdraw_keeps_posts_as_former_member(self):
        consent.withdraw(self.u, PURPOSE)
        self.assertFalse(services.memberships(self.u).exists())
        self.assertFalse(CommunityReaction.objects.filter(user=self.u).exists())
        self.assertEqual(CommunityPost.objects.get(pk=self.p.pk).status, "published")
        self.assertEqual(services.post_block(self.u, c("national"))[0], "consent_required")
        author = client(self.other).get(reverse("community-post", kwargs={"pk": self.p.pk})).data["author"]
        self.assertEqual((author["name"], author["verified"]), ("Former member", False))

    def test_withdraw_and_delete_content(self):
        consent.withdraw(self.u, PURPOSE, delete_content=True)
        p = CommunityPost.objects.get(pk=self.p.pk)
        self.assertEqual((p.status, p.body), ("deleted", ""))

    def test_withdraw_endpoint_option(self):
        r = client(self.u).delete(reverse("my-consent", kwargs={"purpose": PURPOSE}) + "?delete_content=true")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn("deleted", r.data["effect"])

    def test_download_and_account_deletion(self):
        from apps.accounts.services import mark_deleted
        from apps.communities.privacy import export_for

        data = export_for(self.u)
        self.assertEqual({m["level"] for m in data["memberships"]}, {"state", "lga", "ward", "polling_unit"})
        self.assertEqual(len(data["posts"]), 1)
        self.assertEqual(len(data["likes"]), 1)
        mark_deleted(self.u.clerk_user_id)
        self.assertFalse(CommunityMembership.objects.filter(user=self.u).exists())
        p = CommunityPost.objects.get(pk=self.p.pk)
        self.assertIsNone(p.author_id)
        self.assertEqual(client(self.other).get(reverse("community-post", kwargs={"pk": p.pk})).data["author"]["name"], "Former member")
        self.assertEqual(CommunityPost.objects.get(pk=self.p.pk).reaction_count, 1)

    def test_retention(self):
        old = timezone.now() - timedelta(days=31)
        services.delete_post(self.u, self.p)
        CommunityPost.objects.filter(pk=self.p.pk).update(deleted_at=old)
        r = retention.prune(timezone.now())
        self.assertEqual(r["erased"], 1)
        self.assertEqual(CommunityPost.objects.get(pk=self.p.pk).body, "")
        CommunityPost.objects.filter(pk=self.p.pk).update(deleted_at=timezone.now() - timedelta(days=366))
        retention.prune(timezone.now())
        self.assertFalse(CommunityPost.objects.filter(pk=self.p.pk).exists())
        services.leave(self.u, c("lagos"))
        CommunityMembership.objects.filter(user=self.u).update(left_at=old)
        self.assertGreater(retention.prune(timezone.now())["memberships"], 0)

    def test_retention_respects_legal_hold(self):
        moderator = mk_user("u_m", Role.MODERATOR)
        M.report("community_post", self.p, "abuse", user=self.other)
        case = Case.objects.get(target_id=self.p.pk)
        M.claim(case, moderator)
        M.remove(Case.objects.get(pk=case.pk), moderator, "R2")
        M.set_hold(Case.objects.get(pk=case.pk), mk_user("u_sa2", Role.SYSTEM_ADMIN), True)
        CommunityPost.objects.filter(pk=self.p.pk).update(removed_at=timezone.now() - timedelta(days=400))
        retention.prune(timezone.now())
        self.assertTrue(CommunityPost.objects.filter(pk=self.p.pk).exists())

    def test_badge_needs_consent_v2(self):
        from apps.privacy.models import ConsentRecord
        from apps.pvc.services import community_badge, public_badge

        PvcVerification.objects.filter(user=self.u).update(show_badge=True)
        ConsentRecord.objects.create(user=self.u, purpose="verified_badge", version=1, method="dialog")
        self.assertTrue(public_badge(self.u))
        self.assertFalse(community_badge(self.u))
        self.assertIn("verified_badge", consent.consents_needed(self.u))
        consent.grant(self.u, "verified_badge")
        self.assertTrue(community_badge(self.u))


# ---------------------------------------------------------------- integrity and admin
class IntegrityAndAdminTests(Base):
    def test_chain_and_counts(self):
        u = mk_user("u_i")
        verify(u)
        services.place(u)
        CommunityMembership.objects.filter(user=u, level="ward").delete()
        self.assertEqual(_community_chain_broken(), 1)
        Community.objects.filter(path="lagos").update(member_count=40)
        self.assertGreaterEqual(_community_counts_drift(), 1)
        self.assertEqual(c("lagos").member_count, 1)

    def test_admin_settings_audited_and_counts(self):
        from apps.audit.models import AuditLog

        sa = mk_user("u_sa3", Role.SYSTEM_ADMIN)
        r = client(sa).patch(reverse("admin-community", kwargs={"pk": c("lagos").pk}), {"posting_open": False}, format="json")
        self.assertEqual((r.status_code, r.data["posting_open"]), (200, False))
        self.assertTrue(AuditLog.objects.filter(action="community.settings_changed").exists())
        self.assertEqual(client(sa).get(reverse("admin-community", kwargs={"pk": c("lagos/ikeja/05/001").pk})).data["member_count"],
                         "Fewer than 10")
        self.assertEqual(client(mk_user("u_plain")).get(reverse("admin-communities")).status_code, 403)

    def test_team_picker(self):
        sa = mk_user("u_sa4", Role.SYSTEM_ADMIN)
        target = mk_user("u_tgt")
        r = client(sa).post(reverse("admin-team"), {"email": target.email, "role": "moderator", "community": str(c("lagos").pk)}, format="json")
        self.assertEqual((r.status_code, r.data["scope_type"], r.data["scope_label"]), (201, "community", "Lagos State (and below)"))
        bad = client(sa).post(reverse("admin-team"), {"email": target.email, "role": "moderator",
                                                      "community": str(c("lagos/ikeja/05").pk)}, format="json")
        self.assertEqual(bad.status_code, 400)

    def test_dashboard_cards(self):
        from apps.dashboard.services import overview

        sa = mk_user("u_sa5", Role.SYSTEM_ADMIN)
        keys = [x["key"] for x in overview(sa)["attention"]]
        self.assertIn("communities_unmoderated", keys)
        self.assertIn("community_hidden_waiting", keys)

    @override_settings(PRIVACY_CONSENT_REQUIRED=True)
    def test_join_asks_consent_then_places(self):
        u = mk_user("u_join", consent_given=False)
        verify(u)
        r = client(u).post(reverse("my-communities-join"))
        self.assertEqual((r.status_code, r.data["error"]["code"]), (403, "consent_required"))
        consent.grant(u, PURPOSE)
        r = client(u).post(reverse("my-communities-join"))
        self.assertEqual({m["level"] for m in r.data["memberships"]}, {"state", "lga", "ward", "polling_unit"})

    def test_my_communities_endpoint(self):
        u = mk_user("u_me")
        r = client(u).post(reverse("my-communities-state"), {"state": "oyo"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["memberships"][0]["community"]["path"], "oyo")
        oyo = c("oyo")
        self.assertEqual(client(u).delete(reverse("my-community", kwargs={"pk": oyo.pk})).data["memberships"], [])
