"""Clerk webhook — spec §6.2, §11, AC 6."""
from datetime import datetime, timedelta, timezone

from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APITestCase

from apps.access.models import Role
from apps.access.services import grant_role
from apps.accounts.models import User
from apps.integrations.models import WebhookReceipt as ProcessedWebhook
from apps.audit.models import AuditLog

from .helpers import PUBLIC_PEM, bearer, clerk_user, session_token, signed_webhook

URL = reverse("clerk-webhook")


class WebhookTests(APITestCase):
    def post(self, body, headers):
        return self.client.generic("POST", URL, body, content_type="application/json", **headers)

    def test_user_created_upserts_with_primary_email_and_name(self):
        res = self.post(*signed_webhook("user.created", clerk_user()))
        self.assertEqual(res.status_code, 200)
        u = User.objects.get(clerk_user_id="user_test123")
        self.assertEqual((u.email, u.full_name), ("ada@example.com", "Ada Obi"))
        self.assertFalse(u.has_usable_password())
        self.assertTrue(AuditLog.objects.filter(action="user.created", actor_label="clerk:webhook").exists())

    def test_bad_signature_writes_nothing(self):
        body, headers = signed_webhook("user.created", clerk_user(), secret="whsec_" + "d3Jvbmcta2V5LXdyb25nLWtleS13cm9uZw==")
        res = self.post(body, headers)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["error"]["code"], "invalid_signature")
        self.assertEqual(User.objects.count(), 0)
        self.assertEqual(ProcessedWebhook.objects.count(), 0)

    def test_tampered_body_rejected(self):
        body, headers = signed_webhook("user.created", clerk_user())
        res = self.post(body.replace(b"ada@example.com", b"evil@example.com"), headers)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(User.objects.count(), 0)

    def test_missing_headers_rejected(self):
        body, _ = signed_webhook("user.created", clerk_user())
        self.assertEqual(self.post(body, {}).status_code, 400)

    def test_stale_timestamp_rejected(self):
        old = datetime.now(tz=timezone.utc) - timedelta(hours=1)
        self.assertEqual(self.post(*signed_webhook("user.created", clerk_user(), when=old)).status_code, 400)

    def test_replay_is_ignored(self):
        body, headers = signed_webhook("user.created", clerk_user(), msg_id="msg_replay")
        self.post(body, headers)
        res = self.post(body, headers)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["duplicate"])
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(AuditLog.objects.filter(action="user.created").count(), 1)

    def test_user_updated_changes_email_but_never_overwrites_name(self):
        self.post(*signed_webhook("user.created", clerk_user()))
        u = User.objects.get()
        u.full_name = "Ada Nneka Obi"
        u.save()
        self.post(*signed_webhook("user.updated", clerk_user(email="new@example.com", first="Someone", last="Else")))
        u.refresh_from_db()
        self.assertEqual(u.email, "new@example.com")
        self.assertEqual(u.full_name, "Ada Nneka Obi")
        self.assertTrue(AuditLog.objects.filter(action="user.updated", target_id=str(u.id)).exists())

    def test_user_deleted_deactivates_anonymises_and_revokes_roles(self):
        self.post(*signed_webhook("user.created", clerk_user()))
        u = User.objects.get()
        u.phone_number = "+2348031234567"
        u.save()
        grant_role(user=u, role=Role.MODERATOR, actor_label="test")
        self.post(*signed_webhook("user.deleted", {"id": "user_test123", "deleted": True, "object": "user"}))
        u.refresh_from_db()
        self.assertFalse(u.is_active)
        self.assertEqual((u.email, u.full_name, u.phone_number), ("", "", ""))
        self.assertIsNotNone(u.clerk_deleted_at)
        self.assertFalse(u.role_assignments.filter(is_active=True).exists())
        self.assertTrue(AuditLog.objects.filter(action="user.created", target_id=str(u.id)).exists(), "history kept")

    def test_late_created_after_deleted_does_not_resurrect(self):
        self.post(*signed_webhook("user.deleted", {"id": "user_late", "deleted": True}))
        self.post(*signed_webhook("user.created", clerk_user("user_late")))
        u = User.objects.get(clerk_user_id="user_late")
        self.assertFalse(u.is_active)
        self.assertEqual(u.email, "")

    def test_unhandled_event_acknowledged(self):
        res = self.post(*signed_webhook("session.created", {"id": "sess_1", "user_id": "user_x"}))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(User.objects.count(), 0)

    @override_settings(CLERK_JWT_KEY=PUBLIC_PEM)
    def test_late_webhook_after_jit_merges_harmlessly(self):
        """AC 6 — a late webhook doesn't break the user's first request."""
        self.assertEqual(self.client.get(reverse("me"), **bearer(session_token("user_test123"))).status_code, 200)
        res = self.post(*signed_webhook("user.created", clerk_user()))
        self.assertEqual(res.status_code, 200)
        u = User.objects.get(clerk_user_id="user_test123")
        self.assertEqual(u.email, "ada@example.com")
        self.assertEqual(User.objects.count(), 1)

    @override_settings(CLERK_WEBHOOK_SIGNING_SECRET="")
    def test_unconfigured_secret_refuses(self):
        body, headers = signed_webhook("user.created", clerk_user(), secret="whsec_dGVzdC1zZWNyZXQtZm9yLW5kY2Nvbm5lY3QtdGVzdHM=")
        self.assertEqual(self.post(body, headers).status_code, 503)
        self.assertEqual(User.objects.count(), 0)
