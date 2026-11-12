"""Spec 07 §12 — news, official announcements, reports and notifications."""
import io
import tempfile
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient, APITestCase

from apps.access.models import Role
from apps.access.services import grant_role
from apps.accounts.models import User
from apps.accounts.tests.helpers import signed_webhook
from apps.audit.models import AuditLog
from apps.news import services
from apps.moderation.models import Report
from apps.news.models import Article, ArticleEvent, ArticleStatus as S
from apps.notifications.models import Notification, NotificationPreference

TMP_MEDIA = tempfile.mkdtemp(prefix="ndc-news-test-")
BODY = "Polling units in Ikeja opened on time this morning, according to the INEC officers on ground. " * 2
DECL = {"accurate": True, "no_private_info": True, "rules": True}
NOW = timezone.now()
WINDOW = [{"positions": ["president"], "start": (NOW - timedelta(hours=1)).isoformat(), "end": (NOW + timedelta(hours=5)).isoformat()}]


def mk_user(cid, complete=True, **extra):
    return User.objects.create_user(cid, email=f"{cid}@example.com", full_name=extra.pop("full_name", "Ada Obi"),
                                    phone_number="+2348031234567", profile_completed_at=timezone.now() if complete else None,
                                    adult_confirmed_at=timezone.now() if complete else None, **extra)


def mk_role(cid, role, **kw):
    u = mk_user(cid, **kw)
    grant_role(user=u, role=role)
    return u


def image_file(w=900, h=600):
    img = Image.new("RGB", (w, h), (46, 43, 125))
    out = io.BytesIO()
    exif = Image.Exif()
    exif[0x8825] = {1: "N", 2: (6.0, 27.0, 0.0)}
    img.save(out, format="JPEG", exif=exif)
    return SimpleUploadedFile("pic.jpg", out.getvalue(), content_type="image/jpeg")


class Base(APITestCase):
    def setUp(self):
        self.member = mk_user("user_member")
        self.reviewer = mk_role("user_reviewer", Role.NEWS_REVIEWER, full_name="Rita Reviewer")
        self.admin = mk_role("user_sysadmin", Role.SYSTEM_ADMIN, full_name="Sam Admin")
        self.client.force_authenticate(self.member)

    def submit(self, client=None, **over):
        c = client or self.client
        r = c.post(reverse("my-news"), {"title": "Polls open early in Ikeja", "body": BODY, "state": "lagos",
                                        "source_url": "https://www.inecnigeria.org/x", "declarations": DECL, **over}, format="json")
        assert r.status_code == 201, r.data
        return Article.objects.get(pk=r.data["id"])

    def as_(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def announce(self, **over):
        r = self.as_(self.admin).post(reverse("admin-announcements"), {"category": "party", "title": "Rally on Saturday",
                                                                       "body": "Details of the rally.", **over}, format="json")
        assert r.status_code == 201, r.data
        return Article.objects.get(pk=r.data["id"])


# ============================================================== submission
class SubmissionTests(Base):
    def test_profile_required_and_guest_401(self):
        c = self.as_(mk_user("user_incomplete", complete=False))
        r = c.post(reverse("my-news"), {"title": "x", "body": BODY, "declarations": DECL}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(APIClient().post(reverse("my-news"), {}, format="json").status_code, 401)

    def test_validation(self):
        r = self.client.post(reverse("my-news"), {"title": "", "body": "short", "declarations": DECL}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("title", r.data["error"]["fields"])
        self.assertIn("body", r.data["error"]["fields"])
        r = self.client.post(reverse("my-news"), {"title": "T", "body": BODY, "source_url": "http://x.ng", "declarations": DECL},
                             format="json")
        self.assertIn("source_url", r.data["error"]["fields"])
        r = self.client.post(reverse("my-news"), {"title": "T", "body": BODY, "declarations": {"accurate": True}}, format="json")
        self.assertIn("declarations", r.data["error"]["fields"])
        r = self.client.post(reverse("my-news"), {"title": "T", "body": "x" * 5001, "declarations": DECL}, format="json")
        self.assertIn("body", r.data["error"]["fields"])

    def test_pending_not_public_and_limit_three(self):
        a = self.submit()
        self.assertEqual((a.status, a.show_author_name), (S.PENDING, False))
        self.assertEqual(APIClient().get(reverse("news-detail", kwargs={"pk": a.pk})).status_code, 404)
        self.assertEqual(APIClient().get(reverse("news-list")).data["count"], 0)
        self.submit()
        self.submit()
        r = self.client.post(reverse("my-news"), {"title": "4", "body": BODY, "declarations": DECL}, format="json")
        self.assertEqual((r.status_code, r.data["error"]["code"]), (409, "too_many_pending"))

    def test_plain_text_kept(self):
        a = self.submit(body="<script>alert(1)</script> " + BODY)
        self.assertTrue(a.body.startswith("<script>"))  # stored as text, rendered as text by the frontend
        self.assertTrue(AuditLog.objects.filter(action="news.submitted").exists())

    def test_others_ids_404(self):
        a = self.submit()
        other = self.as_(mk_user("user_other"))
        self.assertEqual(other.get(reverse("my-news-detail", kwargs={"pk": a.pk})).status_code, 404)
        self.assertEqual(other.post(reverse("my-news-withdraw", kwargs={"pk": a.pk})).status_code, 404)
        self.assertEqual(self.client.get(reverse("my-news")).data["count"], 1)

    @override_settings(MEDIA_ROOT=TMP_MEDIA)
    def test_image_pipeline(self):
        a = self.submit()
        url = reverse("my-news-image", kwargs={"pk": a.pk})
        r = self.client.put(url, {"image": image_file()}, format="multipart")
        self.assertIn("rights_confirmed", r.data["error"]["fields"])
        r = self.client.put(url, {"image": image_file(), "rights_confirmed": "true"}, format="multipart")
        self.assertEqual(r.status_code, 200, r.data)
        a.refresh_from_db()
        self.assertEqual((a.image_width, a.image_height), (900, 600))
        with Image.open(f"{TMP_MEDIA}/{a.image}") as img:
            self.assertEqual(img.format, "WEBP")
            self.assertFalse(img.getexif())
        r = self.client.put(url, {"image": SimpleUploadedFile("x.jpg", b"not an image", "image/jpeg"), "rights_confirmed": "true"},
                            format="multipart")
        self.assertEqual(r.data["error"]["code"], "invalid_photo")


# ================================================================== review
class ReviewTests(Base):
    def test_approve_publishes_and_notifies(self):
        a = self.submit()
        rc = self.as_(self.reviewer)
        r = rc.post(reverse("admin-news-approve", kwargs={"pk": a.pk}))
        self.assertEqual(r.data["status"], "successful")
        pub = APIClient().get(reverse("news-detail", kwargs={"pk": a.pk}))
        self.assertEqual(pub.status_code, 200)
        self.assertIsNone(pub.data["byline"])
        self.assertNotIn("status", pub.data)
        self.assertNotIn("author_email", str(pub.data))
        self.assertIn("public", pub["Cache-Control"])
        n = Notification.objects.get(recipient=self.member)
        self.assertEqual((n.kind, n.decision), ("news_decision", "approved"))
        self.assertTrue(AuditLog.objects.filter(action="news.approved", actor=self.reviewer).exists())

    def test_reject_reason_resubmit_then_approve(self):
        a = self.submit()
        rc = self.as_(self.reviewer)
        self.assertEqual(rc.post(reverse("admin-news-reject", kwargs={"pk": a.pk}), {"reason": ""}, format="json").status_code, 400)
        rc.post(reverse("admin-news-reject", kwargs={"pk": a.pk}), {"reason": "Election results must come from INEC."}, format="json")
        mine = self.client.get(reverse("my-news-detail", kwargs={"pk": a.pk})).data
        self.assertEqual((mine["status"], mine["decision_reason"]), ("rejected", "Election results must come from INEC."))
        r = self.client.patch(reverse("my-news-detail", kwargs={"pk": a.pk}), {"title": "Polls open on time in Ikeja"}, format="json")
        self.assertEqual((r.data["status"], r.data["decision_reason"]), ("pending", ""))
        rc.post(reverse("admin-news-approve", kwargs={"pk": a.pk}))
        self.assertEqual(Article.objects.get(pk=a.pk).status, S.SUCCESSFUL)
        self.assertEqual(list(Notification.objects.filter(recipient=self.member).order_by("created_at").values_list("decision", flat=True)),
                         ["rejected", "approved"])
        self.assertIn("resubmitted", list(a.events.values_list("event", flat=True)))

    def test_no_self_review_and_double_decision(self):
        own = self.submit(client=self.as_(self.reviewer))
        rc = self.as_(self.reviewer)
        r = rc.post(reverse("admin-news-approve", kwargs={"pk": own.pk}))
        self.assertEqual((r.status_code, r.data["error"]["code"]), (403, "self_review"))
        self.assertTrue(rc.get(reverse("admin-news-detail", kwargs={"pk": own.pk})).data["is_own"])
        a = self.submit()
        rc.post(reverse("admin-news-approve", kwargs={"pk": a.pk}))
        r = self.as_(self.admin).post(reverse("admin-news-reject", kwargs={"pk": a.pk}), {"reason": "x"}, format="json")
        self.assertEqual(r.status_code, 409)

    def test_permissions(self):
        a = self.submit()
        mod = mk_role("user_mod", Role.MODERATOR)
        for who in (self.member, mod):
            self.assertEqual(self.as_(who).get(reverse("admin-news")).status_code, 403)
            self.assertEqual(self.as_(who).post(reverse("admin-news-approve", kwargs={"pk": a.pk})).status_code, 403)
        self.assertEqual(self.as_(self.admin).get(reverse("admin-news")).status_code, 200)  # system admin implies reviewer
        self.assertEqual(self.as_(self.reviewer).post(reverse("admin-announcements"), {}, format="json").status_code, 403)

    def test_remove_restore_and_withdraw(self):
        a = self.submit()
        rc = self.as_(self.reviewer)
        rc.post(reverse("admin-news-approve", kwargs={"pk": a.pk}))
        self.assertEqual(rc.post(reverse("admin-news-remove", kwargs={"pk": a.pk}), {}, format="json").status_code, 400)
        r = rc.post(reverse("admin-news-remove", kwargs={"pk": a.pk}), {"reason": "Shares private information"}, format="json")
        self.assertEqual((r.status_code, r.data["error"]["code"]), (400, "rule_required"))  # spec 13: every removal cites a rule
        rc.post(reverse("admin-news-remove", kwargs={"pk": a.pk}), {"reason": "Shares private information", "rule": "R5"}, format="json")
        gone = APIClient().get(reverse("news-detail", kwargs={"pk": a.pk}))
        self.assertEqual((gone.status_code, gone.data["error"]["rule"]), (410, "R5"))
        self.assertNotIn("title", gone.data["error"])
        self.assertEqual(self.client.get(reverse("my-news-detail", kwargs={"pk": a.pk})).data["removed_reason"], "Shares private information")
        self.assertEqual(self.client.post(reverse("my-news-withdraw", kwargs={"pk": a.pk})).status_code, 409)
        rc.post(reverse("admin-news-restore", kwargs={"pk": a.pk}))
        self.assertEqual(APIClient().get(reverse("news-detail", kwargs={"pk": a.pk})).status_code, 200)
        self.client.post(reverse("my-news-withdraw", kwargs={"pk": a.pk}))
        self.assertEqual(APIClient().get(reverse("news-detail", kwargs={"pk": a.pk})).status_code, 404)
        self.assertEqual(self.client.patch(reverse("my-news-detail", kwargs={"pk": a.pk}), {"title": "x"}, format="json").status_code, 409)

    def test_byline_choice(self):
        a = self.submit(show_author_name=True)
        self.as_(self.reviewer).post(reverse("admin-news-approve", kwargs={"pk": a.pk}))
        # spec 20 §5.2: first name and last initial by default; the full name if the author chooses it
        self.assertEqual(APIClient().get(reverse("news-detail", kwargs={"pk": a.pk})).data["byline"], "Ada O.")
        self.client.patch(reverse("my-settings-profile"), {"name_style": "full"}, format="json")
        self.assertEqual(APIClient().get(reverse("news-detail", kwargs={"pk": a.pk})).data["byline"], "Ada Obi")
        self.client.patch(reverse("my-news-detail", kwargs={"pk": a.pk}), {"show_author_name": False}, format="json")
        self.assertIsNone(APIClient().get(reverse("news-detail", kwargs={"pk": a.pk})).data["byline"])
        self.assertIn("byline_changed", list(a.events.values_list("event", flat=True)))

    def test_events_append_only(self):
        a = self.submit()
        e = ArticleEvent.objects.filter(article=a).first()
        with self.assertRaises(RuntimeError):
            e.note = "x"
            e.save()


# =========================================================== announcements
class AnnouncementTests(Base):
    def test_publish_directly_and_feed(self):
        a = self.announce()
        self.assertEqual(a.status, S.SUCCESSFUL)
        feed = APIClient().get(reverse("news-list")).data
        self.assertEqual(feed["results"][0]["id"], str(a.pk))
        self.assertEqual(feed["results"][0]["category_label"], "Party Announcements")
        self.assertTrue(AuditLog.objects.filter(action="announcement.published").exists())

    def test_election_updates_need_source(self):
        r = self.as_(self.admin).post(reverse("admin-announcements"), {"category": "election_updates", "title": "Results",
                                                                       "body": "x"}, format="json")
        self.assertEqual((r.status_code, r.data["error"]["code"]), (400, "source_required"))
        self.announce(category="election_updates", source_name="INEC", source_url="https://inecelectionresults.ng/x")

    def test_edit_shows_updated_without_notifying(self):
        a = self.announce()
        self.as_(self.admin).patch(reverse("admin-announcement-detail", kwargs={"pk": a.pk}), {"title": "New title", "notify": True},
                                   format="json")
        a.refresh_from_db()
        self.assertEqual(a.title, "New title")
        self.assertIsNotNone(a.edited_at)
        self.assertEqual(Notification.objects.count(), 0)

    def test_pin_limit_and_feed_order(self):
        ac = self.as_(self.admin)
        items = [self.announce(title=f"A{i}") for i in range(4)]
        for a in items[:3]:
            self.assertEqual(ac.post(reverse("admin-announcement-pin", kwargs={"pk": a.pk})).status_code, 200)
        r = ac.post(reverse("admin-announcement-pin", kwargs={"pk": items[3].pk}))
        self.assertEqual(r.data["error"]["code"], "too_many_pinned")
        feed = APIClient().get(reverse("news-list")).data
        self.assertEqual(len(feed["pinned"]), 3)
        self.assertEqual([x["id"] for x in feed["results"]], [str(items[3].pk)])  # pinned not repeated
        self.assertEqual(APIClient().get(reverse("news-list"), {"source": "community"}).data["pinned"], [])

    def test_unpublish_restore(self):
        a = self.announce()
        ac = self.as_(self.admin)
        ac.post(reverse("admin-announcement-unpublish", kwargs={"pk": a.pk}), {"reason": "Wrong date"}, format="json")
        self.assertEqual(APIClient().get(reverse("news-detail", kwargs={"pk": a.pk})).status_code, 404)
        ac.post(reverse("admin-announcement-restore", kwargs={"pk": a.pk}))
        self.assertEqual(APIClient().get(reverse("news-detail", kwargs={"pk": a.pk})).status_code, 200)
        self.assertTrue(AuditLog.objects.filter(action="announcement.unpublished").exists())

    def test_filters(self):
        self.announce(title="National")
        self.announce(title="Kano only", state="kano")
        c = self.submit(state="lagos")
        self.as_(self.reviewer).post(reverse("admin-news-approve", kwargs={"pk": c.pk}))
        titles = lambda **q: sorted(x["title"] for x in APIClient().get(reverse("news-list"), q).data["results"])  # noqa: E731
        self.assertEqual(titles(source="official"), ["Kano only", "National"])
        self.assertEqual(titles(source="community"), ["Polls open early in Ikeja"])
        self.assertEqual(titles(state="lagos"), ["National", "Polls open early in Ikeja"])


# ============================================================ notifications
class NotificationTests(Base):
    def test_announcement_delivery_rules(self):
        off = mk_user("user_off")
        NotificationPreference.objects.create(user=off, announcements=False)
        gone = mk_user("user_gone", is_active=False)
        with self.captureOnCommitCallbacks(execute=True):
            a = self.announce(notify=True)
        recipients = set(Notification.objects.filter(article=a).values_list("recipient__clerk_user_id", flat=True))
        self.assertIn("user_member", recipients)
        self.assertNotIn(off.clerk_user_id, recipients)
        self.assertNotIn(gone.clerk_user_id, recipients)
        from apps.notifications.services import deliver_announcement

        self.assertEqual(deliver_announcement(a), 0)  # idempotent

    def test_no_delivery_without_notify_or_before_commit(self):
        self.announce()
        self.assertEqual(Notification.objects.count(), 0)

    def test_api(self):
        with self.captureOnCommitCallbacks(execute=True):
            a = self.announce(notify=True)
        self.assertEqual(self.client.get(reverse("my-notifications-unread")).data["unread"], 1)
        items = self.client.get(reverse("my-notifications")).data["results"]
        r = self.client.get(reverse("my-notifications"))
        self.assertIn("no-store", r["Cache-Control"])
        self.assertEqual(items[0]["kind"], "announcement")
        nid = items[0]["id"]
        self.assertEqual(self.as_(mk_user("user_x")).post(reverse("my-notification-read", kwargs={"pk": nid})).status_code, 404)
        self.client.post(reverse("my-notification-read", kwargs={"pk": nid}))
        self.assertEqual(self.client.get(reverse("my-notifications-unread")).data["unread"], 0)
        self.as_(self.admin).post(reverse("admin-announcement-unpublish", kwargs={"pk": a.pk}), {"reason": "x"}, format="json")
        self.assertEqual(self.client.get(reverse("my-notifications")).data["count"], 0)  # hidden once not public
        self.assertEqual(self.client.patch(reverse("my-notifications-preferences"), {"announcements": False}, format="json").data,
                         {"announcements": False, "community_replies": True, "event_reminders": True,
                          "candidate_events": True, "community_posts": True, "volunteer_updates": True,
                          "candidate_bells_paused": False})
        self.client.post(reverse("my-notifications-read-all"))
        self.assertFalse(AuditLog.objects.filter(action__startswith="notification").exists())

    def test_decision_visible_even_when_rejected(self):
        a = self.submit()
        self.as_(self.reviewer).post(reverse("admin-news-reject", kwargs={"pk": a.pk}), {"reason": "Duplicate"}, format="json")
        items = self.client.get(reverse("my-notifications")).data["results"]
        self.assertEqual((items[0]["decision"], items[0]["article"]["public"]), ("rejected", False))


# ================================================================= reports
class ReportTests(Base):
    def setUp(self):
        super().setUp()
        self.a = self.submit()
        self.as_(self.reviewer).post(reverse("admin-news-approve", kwargs={"pk": self.a.pk}))

    def test_guest_and_user_reports(self):
        url = reverse("news-report", kwargs={"pk": self.a.pk})
        r = APIClient().post(url, {"reason": "false", "details": "Not true", "contact_email": "me@example.com"}, format="json",
                             REMOTE_ADDR="10.0.0.9")
        self.assertEqual(r.status_code, 201)
        self.assertTrue(r.data["reference"].startswith("RPT-"))
        rep = Report.objects.get()
        self.assertNotIn("10.0.0.9", rep.ip_hash)
        self.assertEqual(len(rep.ip_hash), 64)
        u = self.as_(mk_user("user_rep"))
        u.post(url, {"reason": "spam"}, format="json")
        u.post(url, {"reason": "abuse"}, format="json")
        self.assertEqual(Report.objects.filter(reporter__clerk_user_id="user_rep").count(), 1)
        self.assertEqual(APIClient().post(url, {"reason": "nope"}, format="json").status_code, 400)
        counts = self.as_(self.reviewer).get(reverse("admin-news"), {"reported": 1}).data
        self.assertEqual((counts["count"], counts["counts"]["reported"]), (1, 1))
        self.assertIsNotNone(counts["counts"]["oldest_open_report"])

    def test_remove_resolves_and_dismiss(self):
        url = reverse("news-report", kwargs={"pk": self.a.pk})
        APIClient().post(url, {"reason": "privacy"}, format="json")
        rep = Report.objects.get()
        rc = self.as_(self.reviewer)
        r = rc.post(reverse("admin-news-report-dismiss", kwargs={"pk": rep.pk}), {"note": "Fine"}, format="json")
        self.assertEqual((r.data["case"]["status"], r.data["case"]["decision"]), ("closed", "kept"))  # last report dismissed → Keep
        self.assertEqual(Report.objects.get().status, "dismissed")
        APIClient().post(url, {"reason": "privacy"}, format="json")
        rc.post(reverse("admin-news-remove", kwargs={"pk": self.a.pk}), {"reason": "Private information", "rule": "R5"}, format="json")
        self.assertEqual(Report.objects.filter(status="actioned").count(), 1)
        self.assertEqual(APIClient().post(url, {"reason": "spam"}, format="json").status_code, 404)

    def test_reports_private(self):
        APIClient().post(reverse("news-report", kwargs={"pk": self.a.pk}), {"reason": "spam"}, format="json")
        self.assertNotIn("reports", APIClient().get(reverse("news-detail", kwargs={"pk": self.a.pk})).data)
        self.assertNotIn("reports", self.client.get(reverse("my-news-detail", kwargs={"pk": self.a.pk})).data)


# ================================================================ blackout
class BlackoutTests(Base):
    @override_settings(CAMPAIGN_BLACKOUTS=WINDOW)
    def test_pauses(self):
        a = self.submit()  # submitting still works
        rc = self.as_(self.reviewer)
        r = rc.post(reverse("admin-news-approve", kwargs={"pk": a.pk}))
        self.assertEqual((r.status_code, r.data["error"]["code"]), (409, "publishing_paused"))
        self.assertIn("until", r.data["error"])
        self.assertEqual(rc.post(reverse("admin-news-reject", kwargs={"pk": a.pk}), {"reason": "Later"}, format="json").status_code, 200)
        ac = self.as_(self.admin)
        r = ac.post(reverse("admin-announcements"), {"category": "campaign", "title": "Vote NDC", "body": "x"}, format="json")
        self.assertEqual(r.data["error"]["code"], "publishing_paused")
        self.announce(category="election_updates", source_name="INEC", source_url="https://inec.example/x")  # not paused

    def test_window_scoping(self):
        from apps.core import blackouts

        with override_settings(CAMPAIGN_BLACKOUTS=WINDOW):
            self.assertIsNotNone(blackouts.active_window())
            self.assertIsNotNone(blackouts.active_window(positions=["president"]))
            self.assertIsNone(blackouts.active_window(positions=["governor"]))
        self.assertIsNone(blackouts.active_window(at=NOW))  # default 2027 windows aren't active now


# ======================================================== account deletion
class DeletionTests(Base):
    def test_erasure(self):
        uid = "user_test123"
        u = mk_user(uid)
        c = self.as_(u)
        pending = self.submit(client=c)
        pub = self.submit(client=c, show_author_name=True, title="Published one")
        self.as_(self.reviewer).post(reverse("admin-news-approve", kwargs={"pk": pub.pk}))
        APIClient().post(reverse("news-report", kwargs={"pk": pub.pk}), {"reason": "spam"}, format="json")
        c.post(reverse("news-report", kwargs={"pk": pub.pk}), {"reason": "other"}, format="json")
        NotificationPreference.objects.create(user=u, announcements=True)
        body, headers = signed_webhook("user.deleted", {"id": uid, "deleted": True, "object": "user"})
        APIClient().post(reverse("clerk-webhook"), data=body, content_type="application/json", **headers)
        pending.refresh_from_db()
        pub.refresh_from_db()
        self.assertEqual((pending.status, pending.title, pending.body), (S.WITHDRAWN, "", ""))
        self.assertEqual((pub.status, pub.show_author_name), (S.SUCCESSFUL, False))
        self.assertIsNone(APIClient().get(reverse("news-detail", kwargs={"pk": pub.pk})).data["byline"])
        self.assertFalse(Notification.objects.filter(recipient=u).exists())
        self.assertFalse(NotificationPreference.objects.filter(user=u).exists())
        self.assertFalse(Report.objects.filter(reporter=u).exists())
        log = AuditLog.objects.filter(action="user.deleted").latest("created_at")
        self.assertEqual(log.metadata["news_erased"]["drafts_cleared"], 1)


class PruneTests(Base):
    def test_prune(self):
        from django.core.management import call_command

        old = self.submit()
        Article.objects.filter(pk=old.pk).update(status=S.REJECTED, updated_at=timezone.now() - timedelta(days=200))
        keep = self.submit()
        n = Notification.objects.create(recipient=self.member, kind="news_decision", article=keep, decision="rejected",
                                        read_at=timezone.now())
        Notification.objects.filter(pk=n.pk).update(created_at=timezone.now() - timedelta(days=200))
        call_command("prune_news", stdout=io.StringIO())
        call_command("prune_notifications", stdout=io.StringIO())
        self.assertFalse(Article.objects.filter(pk=old.pk).exists())
        self.assertTrue(Article.objects.filter(pk=keep.pk).exists())
        self.assertFalse(Notification.objects.filter(pk=n.pk).exists())
