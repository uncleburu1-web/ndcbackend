"""GET/PATCH /me — spec §6.3, AC 2."""
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.accounts.validators import normalize_ng_phone
from apps.audit.models import AuditLog

from .helpers import PUBLIC_PEM, bearer, session_token

ME = reverse("me")


@override_settings(CLERK_JWT_KEY=PUBLIC_PEM)
class MeTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("user_me", email="ada@example.com", full_name="Ada Obi")
        self.auth = bearer(session_token("user_me"))

    def patch(self, data):
        return self.client.patch(ME, data, format="json", **self.auth)

    def test_get_returns_only_own_profile_shape(self):
        res = self.client.get(ME, **self.auth)
        self.assertEqual(
            set(res.data), {"id", "email", "full_name", "phone_number", "profile_complete", "roles", "is_system_admin"}
        )

    def test_just_completed_is_returned_exactly_once(self):
        first = self.patch({"full_name": "Ada Obi", "phone_number": "0803 123 4567"})
        self.assertEqual(first.status_code, 200, first.content)
        self.assertTrue(first.data["just_completed"])
        self.assertTrue(first.data["profile_complete"])
        self.assertEqual(first.data["phone_number"], "+2348031234567")

        second = self.patch({"phone_number": "+234 909 000 1111"})
        self.assertNotIn("just_completed", second.data)
        self.assertEqual(second.data["phone_number"], "+2349090001111")
        self.assertEqual(AuditLog.objects.filter(action="profile.completed").count(), 1)
        self.assertTrue(AuditLog.objects.filter(action="profile.updated").exists())

    def test_completion_needs_both_fields(self):
        User.objects.filter(pk=self.user.pk).update(full_name="")
        res = self.patch({"phone_number": "08031234567"})
        self.assertFalse(res.data["profile_complete"])
        self.assertNotIn("just_completed", res.data)
        res = self.patch({"full_name": "Ada Obi"})
        self.assertTrue(res.data["just_completed"])

    def test_validation_errors_use_envelope(self):
        res = self.patch({"full_name": "Mononym", "phone_number": "12345"})
        self.assertEqual(res.status_code, 400)
        err = res.data["error"]
        self.assertEqual(err["code"], "validation_error")
        self.assertIn("full_name", err["fields"])
        self.assertIn("phone_number", err["fields"])

    def test_name_too_long(self):
        res = self.patch({"full_name": "A " + "b" * 160})
        self.assertEqual(res.status_code, 400)

    def test_cannot_change_email_roles_or_flags(self):
        self.patch({"email": "evil@example.com", "roles": [{"role": "system_admin"}], "is_active": False, "clerk_user_id": "user_x"})
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "ada@example.com")
        self.assertTrue(self.user.is_active)
        self.assertEqual(self.user.clerk_user_id, "user_me")
        self.assertFalse(self.user.role_assignments.exists())

    def test_put_and_post_not_allowed(self):
        self.assertEqual(self.client.put(ME, {}, format="json", **self.auth).status_code, 405)
        self.assertEqual(self.client.post(ME, {}, format="json", **self.auth).status_code, 405)


class PhoneTests(APITestCase):
    def test_accepts_nigerian_formats(self):
        for raw in ["08031234567", "+2348031234567", "2348031234567", "0803 123 4567", "0803-123-4567", "(0803) 123 4567"]:
            self.assertEqual(normalize_ng_phone(raw), "+2348031234567", raw)

    def test_rejects_others(self):
        from django.core.exceptions import ValidationError

        for raw in ["", "12345", "08631234567", "+14155552671", "0803123456"]:
            with self.assertRaises(ValidationError, msg=raw):
                normalize_ng_phone(raw)
