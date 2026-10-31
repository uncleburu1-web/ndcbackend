"""Append-only audit log — spec §5.3, AC 10."""
from django.test import RequestFactory, TestCase

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.audit.services import client_ip, record


class AuditTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("user_a", email="a@example.com")

    def test_entries_cannot_be_edited_or_deleted(self):
        entry = record(AuditLog.Action.PROFILE_UPDATED, target=self.user, actor=self.user)
        entry.action = "tampered"
        with self.assertRaises(PermissionError):
            entry.save()
        with self.assertRaises(PermissionError):
            entry.delete()

    def test_secrets_and_personal_data_redacted(self):
        entry = record(AuditLog.Action.PROFILE_UPDATED, target=self.user, token="t", secret="s", phone_number="+234",
                       email="x@y.z", fields=["full_name"])
        self.assertEqual(entry.metadata, {"fields": ["full_name"]})

    def test_forwarded_for_ignored_without_proxies(self):
        req = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="6.6.6.6", REMOTE_ADDR="10.0.0.1")
        self.assertEqual(client_ip(req), "10.0.0.1")
