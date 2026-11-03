"""Spec 09 §12 — candidate posts."""
import io
import tempfile
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient, APITestCase

from apps.access.models import Role, ScopeType
from apps.access.services import grant_role, revoke_role
from apps.accounts.models import User
from apps.accounts.tests.helpers import signed_webhook
from apps.audit.models import AuditLog
from apps.candidates.models import Candidate
from apps.follows.models import CandidateFollow
from apps.news.models import Article
from apps.notifications.models import Notification
from apps.notifications.services import deliver_post
from apps.moderation.models import Report
from apps.posts.models import CandidatePost, PostEvent

TMP_MEDIA = tempfile.mkdtemp(prefix="ndc-posts-test-")
NOW = timezone.now()
POST = {"kind": "announcement", "title": "Town hall in Ikeja on Saturday", "body": "Join us at 10am.\n\nBring your questions."}


def mk_user(cid, **kw):
    return User.objects.create_user(cid, email=f"{cid}@example.com", full_name=kw.pop("full_name", "Ada Obi"), phone_number="+2348031234567",
                                    profile_completed_at=timezone.now(), adult_confirmed_at=timezone.now(), **kw)


def mk_candidate(slug="amina-bello", name="Amina Bello", position="governor", state="lagos", published=True):
    return Candidate.objects.create(slug=slug, full_name=name, position=position, election_year=2027, state=state,
                                    status="published" if published else "draft", verified_at=timezone.now() if published else None,
                                    published_at=timezone.now() if published else None)


def link(user, candidate):
    return grant_role(user=user, role=Role.CANDIDATE, scope_type=ScopeType.CANDIDATE, scope_id=candidate.pk)


def follow(user, candidate, notify=True, changed=None):
    return CandidateFollow.objects.create(user=user, candidate=candidate, notify=notify,
                                          notify_changed_at=changed or (timezone.now() - timedelta(minutes=5)))


def client(user=None):
    c = APIClient()
    if user:
        c.force_authenticate(user)
    return c


class Base(APITestCase):
    def setUp(self):
        self.cand = mk_candidate()
        self.campaign = mk_user("user_campaign", full_name="Campaign Staff")
        self.assignment = link(self.campaign, self.cand)
        self.cc = client(self.campaign)
        self.admin = mk_user("user_sa", full_name="Sam Admin")
        grant_role(user=self.admin, role=Role.SYSTEM_ADMIN)

    def url(self, cand=None):
        return reverse("campaign-posts", kwargs={"candidate_id": (cand or self.cand).pk})

    def publish(self, c=None, cand=None, **over):
        with self.captureOnCommitCallbacks(execute=True):
            r = (c or self.cc).post(self.url(cand), {**POST, **over}, format="json")
        assert r.status_code == 201, r.data
        return CandidatePost.objects.get(pk=r.data["id"])


class PermissionTests(Base):
    def test_who_can_post(self):
        p = self.publish()
        self.assertEqual(p.status, "published")  # no Pending, ever
        other_cand = mk_candidate("bayo", "Bayo Adeyemi")
        other_campaign = mk_user("user_other_campaign")
        link(other_campaign, other_cand)
        for who in (mk_user("user_plain"), self.admin, other_campaign):
            self.assertEqual(client(who).post(self.url(), POST, format="json").status_code, 403, who)
            self.assertEqual(client(who).patch(reverse("campaign-post", kwargs={"candidate_id": self.cand.pk, "post_id": p.pk}),
                                               {"title": "x"}, format="json").status_code, 403)
        self.assertEqual(client().post(self.url(), POST, format="json").status_code, 401)
        # another candidate's post id through this candidate's URL → 404
        q = self.publish(c=client(other_campaign), cand=other_cand)
        self.assertEqual(self.cc.get(reverse("campaign-post", kwargs={"candidate_id": self.cand.pk, "post_id": q.pk})).status_code, 404)

    def test_unlinked_midway(self):
        p = self.publish()
        revoke_role(assignment=self.assignment)
        r = self.cc.patch(reverse("campaign-post", kwargs={"candidate_id": self.cand.pk, "post_id": p.pk}), {"title": "x"}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(client().get(reverse("candidate-post", kwargs={"slug": self.cand.slug, "pk": p.pk})).status_code, 200)

    def test_unpublished_candidate(self):
        self.cand.status = "unpublished"
        self.cand.save()
        r = self.cc.post(self.url(), POST, format="json")
        self.assertEqual((r.status_code, r.data["error"]["code"]), (409, "candidate_not_published"))


class RuleTests(Base):
    def test_validation_and_plain_text(self):
        r = self.cc.post(self.url(), {"kind": "op-ed", "title": "", "body": ""}, format="json")
        self.assertEqual(set(r.data["error"]["fields"]), {"kind", "title", "body"})
        r = self.cc.post(self.url(), {**POST, "body": "x" * 10_001}, format="json")
        self.assertIn("body", r.data["error"]["fields"])
        p = self.publish(body="<b>bold</b>")
        self.assertEqual(p.body, "<b>bold</b>")

    def test_edit_sets_edited_only_on_change_and_never_redelivers(self):
        fan = mk_user("user_fan")
        follow(fan, self.cand)
        p = self.publish()
        url = reverse("campaign-post", kwargs={"candidate_id": self.cand.pk, "post_id": p.pk})
        with self.captureOnCommitCallbacks(execute=True):
            self.cc.patch(url, {"title": POST["title"]}, format="json")
        p.refresh_from_db()
        self.assertIsNone(p.edited_at)
        with self.captureOnCommitCallbacks(execute=True):
            self.cc.patch(url, {"title": "New title"}, format="json")
        p.refresh_from_db()
        self.assertIsNotNone(p.edited_at)
        self.assertEqual(Notification.objects.filter(recipient=fan).count(), 1)

    @override_settings(MEDIA_ROOT=TMP_MEDIA)
    def test_image(self):
        p = self.publish()
        img = Image.new("RGB", (1000, 500), (46, 43, 125))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        url = reverse("campaign-post-image", kwargs={"candidate_id": self.cand.pk, "post_id": p.pk})
        r = self.cc.put(url, {"image": SimpleUploadedFile("p.jpg", buf.getvalue(), "image/jpeg"), "rights_confirmed": "true"}, format="multipart")
        self.assertEqual(r.status_code, 200, r.data)
        p.refresh_from_db()
        self.assertEqual((p.image_width, p.image_height), (1000, 500))
        self.assertTrue(AuditLog.objects.filter(action="post.image_updated").exists())
        self.assertIsNone(p.edited_at)  # the first image sent with publishing isn't an edit
        buf2 = io.BytesIO()
        img.save(buf2, format="JPEG")
        self.cc.put(url, {"image": SimpleUploadedFile("q.jpg", buf2.getvalue(), "image/jpeg"), "rights_confirmed": "true"}, format="multipart")
        p.refresh_from_db()
        self.assertIsNotNone(p.edited_at)  # replacing it is


class DeliveryTests(Base):
    def test_who_gets_notified(self):
        on = mk_user("user_on")
        follow(on, self.cand)
        off = mk_user("user_off")
        follow(off, self.cand, notify=False)
        late = mk_user("user_late")
        follow(late, self.cand, changed=timezone.now() + timedelta(hours=1))
        inactive = mk_user("user_inactive", is_active=False)
        follow(inactive, self.cand)
        p = self.publish()
        self.assertEqual(set(Notification.objects.filter(candidate_post=p).values_list("recipient__clerk_user_id", flat=True)), {"user_on"})
        self.assertEqual(deliver_post(p), 0)  # idempotent
        self.assertTrue(AuditLog.objects.filter(action="post.delivered").exists())

    def test_only_after_commit(self):
        follow(mk_user("user_on"), self.cand)
        r = self.cc.post(self.url(), POST, format="json")  # on_commit callbacks not executed: nothing delivered
        self.assertEqual(r.status_code, 201)
        self.assertEqual(Notification.objects.count(), 0)

    def test_notify_count(self):
        follow(mk_user("user_on"), self.cand)
        follow(mk_user("user_off"), self.cand, notify=False)
        data = self.cc.get(reverse("campaign-candidates")).data["results"]
        self.assertEqual((data[0]["notify_count"], data[0]["posting_paused_until"]), (1, None))

    def test_constraint(self):
        u = mk_user("user_x")
        a = Article.objects.create(source="official", status="successful", category="party", title="T", body="B")
        p = self.publish()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Notification.objects.create(recipient=u, kind="candidate_post", article=a)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Notification.objects.create(recipient=u, kind="announcement", article=a, candidate_post=p)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Notification.objects.create(recipient=u, kind="pvc_decision", candidate_post=p, decision="verified")


class VisibilityTests(Base):
    def test_profile_post_page_feed_and_notifications(self):
        fan = mk_user("user_fan")
        follow(fan, self.cand)
        p = self.publish()
        g = client()
        lst = g.get(reverse("candidate-posts", kwargs={"slug": self.cand.slug}))
        self.assertEqual(lst.data["count"], 1)
        self.assertIn("public", lst["Cache-Control"])
        d = g.get(reverse("candidate-post", kwargs={"slug": self.cand.slug, "pk": p.pk})).data
        for k in ("status", "created_by", "removed_reason", "author_email"):
            self.assertNotIn(k, d)
        self.assertEqual(d["candidate"]["full_name"], "Amina Bello")
        feed = g.get(reverse("news-list"), {"source": "candidate"}).data["results"]
        self.assertEqual((feed[0]["source"], feed[0]["candidate"]["slug"]), ("candidate", self.cand.slug))
        fc = client(fan)
        self.assertEqual(fc.get(reverse("my-notifications-unread")).data["unread"], 1)
        item = fc.get(reverse("my-notifications")).data["results"][0]
        self.assertEqual((item["kind"], item["post"]["title"], item["candidate"]["slug"]), ("candidate_post", POST["title"], self.cand.slug))
        # unpublish the candidate → hidden everywhere; republish → back
        self.cand.status = "unpublished"
        self.cand.save()
        self.assertEqual(g.get(reverse("candidate-post", kwargs={"slug": self.cand.slug, "pk": p.pk})).status_code, 404)
        self.assertEqual(g.get(reverse("news-list"), {"source": "candidate"}).data["count"], 0)
        self.assertEqual(fc.get(reverse("my-notifications-unread")).data["unread"], 0)
        self.cand.status = "published"
        self.cand.save()
        self.assertEqual(fc.get(reverse("my-notifications-unread")).data["unread"], 1)

    def test_delete_and_remove_restore(self):
        fan = mk_user("user_fan")
        follow(fan, self.cand)
        p = self.publish()
        self.assertEqual(client(self.admin).post(reverse("admin-post-remove", kwargs={"pk": p.pk}), {}, format="json").status_code, 400)
        r = client(self.admin).post(reverse("admin-post-remove", kwargs={"pk": p.pk}), {"reason": "Inciting language"}, format="json")
        self.assertEqual(r.data["error"]["code"], "rule_required")
        client(self.admin).post(reverse("admin-post-remove", kwargs={"pk": p.pk}), {"reason": "Inciting language", "rule": "R1"}, format="json")
        gone = client().get(reverse("candidate-post", kwargs={"slug": self.cand.slug, "pk": p.pk}))
        self.assertEqual((gone.status_code, gone.data["error"]["rule"]), (410, "R1"))  # spec 13 §4.5
        self.assertEqual(client(fan).get(reverse("my-notifications")).data["count"], 0)
        mine = self.cc.get(self.url()).data["results"]
        self.assertEqual((mine[0]["status"], mine[0]["removed_reason"]), ("removed", "Inciting language"))
        with self.captureOnCommitCallbacks(execute=True):
            client(self.admin).post(reverse("admin-post-restore", kwargs={"pk": p.pk}))
        self.assertEqual(client(fan).get(reverse("my-notifications")).data["count"], 1)
        self.assertEqual(Notification.objects.filter(recipient=fan).count(), 1)  # no new notification on restore
        self.assertEqual(self.cc.delete(reverse("campaign-post", kwargs={"candidate_id": self.cand.pk, "post_id": p.pk})).status_code, 204)
        self.assertFalse(Notification.objects.filter(candidate_post=p).exists())
        self.assertEqual(client().get(reverse("candidate-post", kwargs={"slug": self.cand.slug, "pk": p.pk})).status_code, 404)
        for action in ("post.created", "post.removed", "post.restored", "post.deleted"):
            self.assertTrue(AuditLog.objects.filter(action=action).exists(), action)
        self.assertFalse(AuditLog.objects.filter(action__startswith="notification").exists())

    def test_admin_only_and_no_self_moderation(self):
        p = self.publish()
        reviewer = mk_user("user_rev")
        grant_role(user=reviewer, role=Role.NEWS_REVIEWER)
        self.assertEqual(client(reviewer).get(reverse("admin-posts")).status_code, 403)
        linked_admin = mk_user("user_linked_admin")
        grant_role(user=linked_admin, role=Role.SYSTEM_ADMIN)
        link(linked_admin, self.cand)
        r = client(linked_admin).post(reverse("admin-post-remove", kwargs={"pk": p.pk}), {"reason": "x"}, format="json")
        self.assertEqual((r.status_code, r.data["error"]["code"]), (403, "self_moderation"))

    def test_events_append_only(self):
        p = self.publish()
        e = PostEvent.objects.filter(post=p).first()
        with self.assertRaises(RuntimeError):
            e.save()


class FeedTests(Base):
    def test_merge_paging_state_and_pinned(self):
        other = mk_candidate("president", "Pres Candidate", position="president", state="")
        pc = mk_user("user_pc")
        link(pc, other)
        for i in range(16):
            Article.objects.create(source="official", status="successful", category="party", title=f"A{i}", body="b",
                                   published_at=timezone.now() - timedelta(minutes=2 * i))
        posts = [self.publish(title=f"P{i}") for i in range(6)]
        for i, p in enumerate(posts):
            CandidatePost.objects.filter(pk=p.pk).update(published_at=timezone.now() - timedelta(minutes=2 * i + 1))
        self.publish(c=client(pc), cand=other, title="National")
        pinned = Article.objects.create(source="official", status="successful", category="party", title="Pinned", body="b",
                                        published_at=timezone.now() - timedelta(days=3), pinned_at=timezone.now())
        g = client()
        p1 = g.get(reverse("news-list")).data
        p2 = g.get(reverse("news-list"), {"page": 2}).data
        self.assertEqual((p1["count"], len(p1["results"]), len(p2["results"])), (23, 20, 3))
        self.assertEqual([x["title"] for x in p1["pinned"]], ["Pinned"])
        titles = [x["title"] for x in p1["results"] + p2["results"]]
        self.assertEqual(len(titles), 23)
        self.assertEqual(len(set(titles)), 23)  # no duplicates or gaps across pages
        self.assertNotIn("Pinned", titles)
        self.assertEqual(titles[0], "National")
        stamps = [x["published_at"] for x in p1["results"] + p2["results"]]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        self.assertEqual(g.get(reverse("news-list"), {"page": 3}).status_code, 404)
        kano = [x["title"] for x in g.get(reverse("news-list"), {"source": "candidate", "state": "kano"}).data["results"]]
        self.assertEqual(kano, ["National"])  # Lagos candidate excluded; national (no state) included
        self.assertEqual(g.get(reverse("news-list"), {"source": "candidate"}).data["pinned"], [])
        self.assertTrue(pinned.pk)


class ReportTests(Base):
    def test_reports(self):
        p = self.publish()
        url = reverse("candidate-post-report", kwargs={"slug": self.cand.slug, "pk": p.pk})
        self.assertEqual(client().post(url, {"reason": "abuse", "details": "Inciting"}, format="json", REMOTE_ADDR="10.1.1.1").status_code, 201)
        r = Report.objects.get()
        self.assertEqual(len(r.ip_hash), 64)
        u = client(mk_user("user_rep"))
        u.post(url, {"reason": "spam"}, format="json")
        u.post(url, {"reason": "false"}, format="json")
        self.assertEqual(Report.objects.filter(reporter__clerk_user_id="user_rep").count(), 1)
        ac = client(self.admin)
        lst = ac.get(reverse("admin-posts"), {"reported": 1}).data
        self.assertEqual((lst["count"], lst["counts"]["reported"]), (1, 1))
        d = ac.post(reverse("admin-post-report-dismiss", kwargs={"pk": r.pk}), {"note": "Fine"}, format="json").data
        self.assertEqual(d["case"]["open_reports"], 1)  # the member's report is still open, so the case stays open
        ac.post(reverse("admin-post-remove", kwargs={"pk": p.pk}), {"reason": "Abuse", "rule": "R2"}, format="json")
        self.assertEqual(Report.objects.filter(status="actioned").count(), 1)
        self.assertEqual(client().post(url, {"reason": "spam"}, format="json").status_code, 404)


class BlackoutTests(Base):
    def test_office_scoped_pause(self):
        p = self.publish()
        window = [{"positions": ["governor"], "start": (NOW - timedelta(hours=1)).isoformat(), "end": (NOW + timedelta(hours=5)).isoformat()}]
        with override_settings(CAMPAIGN_BLACKOUTS=window):
            r = self.cc.post(self.url(), POST, format="json")
            self.assertEqual((r.status_code, r.data["error"]["code"]), (409, "posting_paused"))
            self.assertIn("until", r.data["error"])
            url = reverse("campaign-post", kwargs={"candidate_id": self.cand.pk, "post_id": p.pk})
            self.assertEqual(self.cc.patch(url, {"title": "x"}, format="json").status_code, 409)
            self.assertIsNotNone(self.cc.get(reverse("campaign-candidates")).data["results"][0]["posting_paused_until"])
            pres = mk_candidate("pres", "Pres", position="president", state="")
            pc = mk_user("user_pc")
            link(pc, pres)
            self.assertEqual(client(pc).post(self.url(pres), POST, format="json").status_code, 201)  # other office not paused
            self.assertEqual(self.cc.delete(url).status_code, 204)  # delete still works


class LifecycleTests(Base):
    def test_unfollow_and_account_deletion(self):
        fan = mk_user("user_fan")
        follow(fan, self.cand)
        other = mk_candidate("bayo", "Bayo")
        oc = mk_user("user_oc")
        link(oc, other)
        follow(fan, other)
        self.publish()
        self.publish(c=client(oc), cand=other)
        self.assertEqual(Notification.objects.filter(recipient=fan).count(), 2)
        client(fan).delete(reverse("my-follow", kwargs={"slug": self.cand.slug}))
        self.assertEqual(list(Notification.objects.filter(recipient=fan).values_list("candidate_post__candidate__slug", flat=True)), ["bayo"])
        uid = "user_test123"
        u = mk_user(uid)
        follow(u, other)
        self.publish(c=client(oc), cand=other, title="Second")
        body, headers = signed_webhook("user.deleted", {"id": uid, "deleted": True, "object": "user"})
        APIClient().post(reverse("clerk-webhook"), data=body, content_type="application/json", **headers)
        self.assertFalse(Notification.objects.filter(recipient=u).exists())

    def test_prune(self):
        from django.core.management import call_command

        p = self.publish()
        CandidatePost.objects.filter(pk=p.pk).update(status="deleted", deleted_at=timezone.now() - timedelta(days=400))
        call_command("prune_posts", stdout=io.StringIO())
        self.assertFalse(CandidatePost.objects.filter(pk=p.pk).exists())
