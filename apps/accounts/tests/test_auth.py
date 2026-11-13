"""Token verification, JIT creation, deactivation — spec §6.1, §12, AC 4, 5, 7, 8."""
from unittest import mock

from clerk_backend_api.security.types import (
    TokenVerificationError,
    TokenVerificationErrorReason,
)
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.audit.models import AuditLog

from .helpers import OTHER_PRIVATE_PEM, PUBLIC_PEM, bearer, clerk_user, session_token

ME = reverse("me")


@override_settings(CLERK_JWT_KEY=PUBLIC_PEM)
class TokenVerificationTests(APITestCase):
    def test_valid_token_signs_in_and_creates_user_just_in_time(self):
        res = self.client.get(ME, **bearer(session_token("user_jit1")))
        self.assertEqual(res.status_code, 200, res.content)
        user = User.objects.get(clerk_user_id="user_jit1")
        self.assertEqual(res.data["id"], str(user.id))
        self.assertFalse(res.data["profile_complete"])
        self.assertTrue(AuditLog.objects.filter(action="user.created", target_id=str(user.id), actor_label="system:jit").exists())

    def test_jit_prefills_from_clerk_when_reachable(self):
        with mock.patch("apps.accounts.clerk.fetch_user", return_value=clerk_user("user_jit2", "jit@example.com")):
            res = self.client.get(ME, **bearer(session_token("user_jit2")))
        self.assertEqual(res.data["email"], "jit@example.com")
        self.assertEqual(res.data["full_name"], "Ada Obi")

    def test_repeat_requests_reuse_the_same_user(self):
        for _ in range(3):
            self.client.get(ME, **bearer(session_token("user_same")))
        self.assertEqual(User.objects.filter(clerk_user_id="user_same").count(), 1)

    def test_missing_token_is_401_with_envelope(self):
        res = self.client.get(ME)
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.data["error"]["code"], "not_authenticated")

    def test_expired_token_is_401(self):
        res = self.client.get(ME, **bearer(session_token(expires_in=-120)))
        self.assertEqual(res.status_code, 401)

    def test_token_for_another_origin_is_401(self):
        res = self.client.get(ME, **bearer(session_token(azp="https://evil.example")))
        self.assertEqual(res.status_code, 401)

    def test_token_without_azp_is_401(self):
        self.assertEqual(self.client.get(ME, **bearer(session_token(azp=None))).status_code, 401)

    def test_token_signed_by_unknown_key_is_401(self):
        self.assertEqual(self.client.get(ME, **bearer(session_token(key=OTHER_PRIVATE_PEM))).status_code, 401)

    def test_non_session_token_types_are_401(self):
        for fake in ["mt_abc123", "ak_abc123", "oat_abc123"]:
            self.assertEqual(self.client.get(ME, **bearer(fake)).status_code, 401, fake)

    def test_garbage_token_is_401(self):
        self.assertEqual(self.client.get(ME, **bearer("not.a.jwt")).status_code, 401)

    def test_clerk_session_cookie_is_ignored(self):
        """Only the Bearer header authenticates; a browser-sent cookie must not (CSRF)."""
        self.client.cookies["__session"] = session_token("user_cookie")
        self.assertEqual(self.client.get(ME).status_code, 401)
        self.assertFalse(User.objects.filter(clerk_user_id="user_cookie").exists())

    def test_deactivated_user_gets_403_even_with_valid_token(self):
        User.objects.create_user("user_off", is_active=False)
        res = self.client.get(ME, **bearer(session_token("user_off")))
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["error"]["code"], "account_deactivated")

    def test_clerk_metadata_grants_nothing(self):
        """AC 8 — roles come only from RoleAssignment, never token/Clerk metadata."""
        token = session_token("user_meta", metadata={"role": "system_admin"}, public_metadata={"roles": ["system_admin"]})
        res = self.client.get(ME, **bearer(token))
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data["is_system_admin"])
        self.assertEqual(res.data["roles"], [])


class ClerkUnavailableTests(APITestCase):
    @override_settings(CLERK_SECRET_KEY="sk_test_x", CLERK_JWT_KEY="")
    def test_jwks_fetch_failure_is_503_never_signed_in(self):
        boom = TokenVerificationError(TokenVerificationErrorReason.JWK_FAILED_TO_LOAD)
        with mock.patch("clerk_backend_api.security.verifytoken._fetch_jwks", side_effect=boom):
            res = self.client.get(ME, **bearer(session_token("user_503")))
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.data["error"]["code"], "auth_unavailable")
        self.assertFalse(User.objects.filter(clerk_user_id="user_503").exists())

    @override_settings(CLERK_SECRET_KEY="", CLERK_JWT_KEY="")
    def test_unconfigured_clerk_is_503_when_a_token_is_sent(self):
        res = self.client.get(ME, **bearer(session_token()))
        self.assertEqual(res.status_code, 503)

    @override_settings(CLERK_SECRET_KEY="", CLERK_JWT_KEY="")
    def test_public_endpoints_still_work_when_clerk_is_down(self):
        self.assertEqual(self.client.get(reverse("health")).status_code, 200)


class NoPasswordsTests(APITestCase):
    def test_platform_users_have_unusable_passwords(self):
        """AC 7 — no passwords or password hashes for platform users."""
        u = User.objects.create_user("user_pw")
        self.assertFalse(u.has_usable_password())
        self.assertTrue(u.password.startswith("!"))

    def test_create_superuser_is_refused(self):
        with self.assertRaises(NotImplementedError):
            User.objects.create_superuser("user_su")
