"""Spec 15 §9 — one notification service, the two new kinds, and fan-out through the outbox."""
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core import outbox
from apps.core.models import OutboxJob
from apps.membership.tests.test_membership import OPEN
from apps.membership.tests.test_membership import Base as MembershipBase
from apps.notifications import services
from apps.notifications.models import Notification

NOW = timezone.now()


def mk(cid, **kw):
    return User.objects.create_user(cid, email=f"{cid}@example.com", full_name=f"Person {cid}", phone_number="+2348031234567",
                                    profile_completed_at=NOW, adult_confirmed_at=NOW, **kw)


class NotifyTests(TestCase):
    def test_skips_inactive_and_deleted(self):
        ok, off, gone = mk("u_ok"), mk("u_off", is_active=False), mk("u_gone", clerk_deleted_at=NOW)
        self.assertIsNotNone(services.notify(ok, "pvc_decision", decision="verified"))
        self.assertIsNone(services.notify(off, "pvc_decision", decision="verified"))
        self.assertIsNone(services.notify(gone, "pvc_decision", decision="verified"))
        self.assertIsNone(services.notify(None, "pvc_decision"))
        with self.assertRaises(ValueError):
            services.notify(ok, "made_up_kind")
        self.assertEqual(services.notify_many([ok, off, gone], "security_notice"), 1)

    def test_rows_hold_a_link_and_a_word_only(self):
        u = mk("u_w")
        n = services.notify(u, "pvc_decision", decision="a-very-long-decision-text")
        self.assertEqual(len(n.decision), 10)
        fields = {f.name for f in Notification._meta.fields}
        self.assertFalse({"amount_kobo", "vin", "nin", "body", "text", "moderator"} & fields)


@override_settings(**OPEN)
class MembershipDecisionTests(MembershipBase):
    def test_approved_rejected_cancelled(self):
        from apps.membership import services as ms
        from apps.membership.models import Membership

        self.ready()
        self.pay()
        m = Membership.objects.get(user=self.user)
        ms.approve(m, officer=self.officer)
        n = Notification.objects.get(recipient=self.user, kind="membership_decision")
        self.assertEqual((n.decision, n.membership_id), ("approved", m.pk))
        ms.cancel(m, actor=self.officer, reason="Left the party")
        self.assertEqual(list(Notification.objects.filter(kind="membership_decision").order_by("created_at")
                              .values_list("decision", flat=True)), ["approved", "cancelled"])
        r = self.client.get(reverse("my-notifications"))
        item = next(i for i in r.data["results"] if i["kind"] == "membership_decision")
        self.assertEqual(set(item["membership"]), {"status"})
        self.assertNotIn("nin", str(item).lower())


@override_settings(DONATIONS_ENABLED=True, DONATIONS_COMPLIANCE_APPROVED=True, PAYMENT_PROVIDER="fake")
class DonationReceiptTests(TestCase):
    def setUp(self):
        from django.core.cache import caches

        caches["fake_payments"].clear()

    def start(self, c):
        from apps.donations.tests.test_donations import payload

        r = c.post(reverse("donations"), payload(), format="json")
        assert r.status_code == 201, r.data
        return r.data["reference"]

    def settle(self, ref):
        from apps.donations.tests.test_donations import webhook
        from apps.payments.providers.fake import FakeProvider

        FakeProvider.settle(ref, "success")
        webhook(APIClient(), "charge.success", ref)

    def test_signed_in_donor_gets_one_receipt_guests_none(self):
        u = mk("u_donor")
        c = APIClient()
        c.force_authenticate(u)
        ref = self.start(c)
        self.settle(ref)
        self.settle(ref)
        rows = Notification.objects.filter(recipient=u, kind="donation_receipt")
        self.assertEqual(rows.count(), 1)
        item = next(i for i in c.get(reverse("my-notifications")).data["results"] if i["kind"] == "donation_receipt")
        self.assertEqual(item["donation"], {"reference": ref})
        guest_ref = self.start(APIClient())
        self.settle(guest_ref)
        self.assertEqual(Notification.objects.filter(kind="donation_receipt").count(), 1)


class FanoutTests(TestCase):
    def test_announcement_fanout_is_an_outbox_job_completed_once(self):
        from apps.news.models import Article

        users = [mk(f"u_f{i}") for i in range(5)]
        a = Article.objects.create(source="official", status="successful", title="Polling units confirmed", body="b", published_at=NOW,
                                   category="party")
        with self.captureOnCommitCallbacks(execute=False) as cbs:
            services.deliver_announcement_on_commit(a)
        job = OutboxJob.objects.get(kind="notify_fanout")
        self.assertEqual(job.payload, {"kind": "announcement", "article": str(a.pk)})
        # interrupted mid-way: a few rows already written, then the worker died
        for u in users[:2]:
            Notification.objects.create(recipient=u, kind="announcement", article=a)
        OutboxJob.objects.filter(pk=job.pk).update(status="running", next_attempt_at=NOW - outbox.STUCK_AFTER * 2)
        outbox.process()
        self.assertEqual(Notification.objects.filter(kind="announcement").count(), 5)
        for cb in cbs:  # the original after-commit kick arriving late changes nothing
            cb()
        outbox.process()
        self.assertEqual(Notification.objects.filter(kind="announcement").count(), 5)
        job.refresh_from_db()
        self.assertEqual(job.status, "done")

    def test_withdrawn_before_the_job_runs_sends_nothing(self):
        from apps.news.models import Article

        mk("u_x")
        a = Article.objects.create(source="official", status="successful", title="T", body="b", published_at=NOW, category="party")
        services.deliver_announcement_on_commit(a)
        Article.objects.filter(pk=a.pk).update(status="withdrawn")
        outbox.process()
        self.assertFalse(Notification.objects.exists())
