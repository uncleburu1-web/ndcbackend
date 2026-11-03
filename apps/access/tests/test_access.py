"""Roles, permissions, commands — spec §5.2, §6.4, §6.5, AC 5, 8, 10."""
import uuid
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIRequestFactory, APITestCase

from apps.access.models import Role, RoleAssignment, ScopeType
from apps.access.permissions import HasRole, IsProfileComplete, IsSystemAdmin
from apps.access.services import RoleError, grant_role, has_role, is_system_admin
from apps.accounts.models import User
from apps.accounts.tests.helpers import PUBLIC_PEM, bearer, session_token
from apps.audit.models import AuditLog


def mk(clerk_id, email=None, **kw):
    return User.objects.create_user(clerk_id, email=email or f"{clerk_id}@example.com", **kw)


class RoleRulesTests(TestCase):
    def setUp(self):
        self.u = mk("user_r")
        self.c1, self.c2 = uuid.uuid4(), uuid.uuid4()

    def test_invalid_scope_pairs_rejected(self):
        bad = [
            (Role.SYSTEM_ADMIN, ScopeType.COMMUNITY, self.c1),
            (Role.CANDIDATE, ScopeType.GLOBAL, None),
            (Role.CANDIDATE, ScopeType.CANDIDATE, None),
            (Role.NEWS_REVIEWER, ScopeType.COMMUNITY, self.c1),
            (Role.MEMBERSHIP_OFFICER, ScopeType.CANDIDATE, self.c1),
            (Role.MODERATOR, ScopeType.GLOBAL, self.c1),
        ]
        for role, st, sid in bad:
            with self.assertRaises(RoleError, msg=(role, st)):
                grant_role(user=self.u, role=role, scope_type=st, scope_id=sid)
        self.assertFalse(RoleAssignment.objects.exists())

    def test_duplicate_active_grant_rejected_then_allowed_after_revoke(self):
        a = grant_role(user=self.u, role=Role.MODERATOR, scope_type=ScopeType.COMMUNITY, scope_id=self.c1)
        with self.assertRaises(RoleError):
            grant_role(user=self.u, role=Role.MODERATOR, scope_type=ScopeType.COMMUNITY, scope_id=self.c1)
        from apps.access.services import revoke_role

        revoke_role(assignment=a)
        grant_role(user=self.u, role=Role.MODERATOR, scope_type=ScopeType.COMMUNITY, scope_id=self.c1)
        self.assertEqual(RoleAssignment.objects.filter(user=self.u).count(), 2)

    def test_db_constraints_hold_even_bypassing_services(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            RoleAssignment.objects.create(user=self.u, role=Role.CANDIDATE, scope_type=ScopeType.CANDIDATE, scope_id=None)
        RoleAssignment.objects.create(user=self.u, role=Role.SYSTEM_ADMIN)
        with self.assertRaises(IntegrityError), transaction.atomic():
            RoleAssignment.objects.create(user=self.u, role=Role.SYSTEM_ADMIN)

    def test_system_admin_implications(self):
        grant_role(user=self.u, role=Role.SYSTEM_ADMIN)
        self.assertTrue(is_system_admin(self.u))
        self.assertTrue(has_role(self.u, Role.MODERATOR, ScopeType.COMMUNITY, self.c1))
        self.assertTrue(has_role(self.u, Role.NEWS_REVIEWER))
        self.assertTrue(has_role(self.u, Role.MEMBERSHIP_OFFICER))
        self.assertFalse(has_role(self.u, Role.CANDIDATE, ScopeType.CANDIDATE, self.c1))

    def test_scoped_and_global_moderator(self):
        grant_role(user=self.u, role=Role.MODERATOR, scope_type=ScopeType.COMMUNITY, scope_id=self.c1)
        self.assertTrue(has_role(self.u, Role.MODERATOR, ScopeType.COMMUNITY, self.c1))
        self.assertFalse(has_role(self.u, Role.MODERATOR, ScopeType.COMMUNITY, self.c2))
        g = mk("user_g")
        grant_role(user=g, role=Role.MODERATOR)
        self.assertTrue(has_role(g, Role.MODERATOR, ScopeType.COMMUNITY, self.c2))

    def test_candidate_scope_is_per_record(self):
        grant_role(user=self.u, role=Role.CANDIDATE, scope_type=ScopeType.CANDIDATE, scope_id=self.c1)
        self.assertTrue(has_role(self.u, Role.CANDIDATE, ScopeType.CANDIDATE, self.c1))
        self.assertFalse(has_role(self.u, Role.CANDIDATE, ScopeType.CANDIDATE, self.c2))

    def test_inactive_users_hold_no_roles(self):
        grant_role(user=self.u, role=Role.SYSTEM_ADMIN)
        self.u.is_active = False
        self.u.save()
        self.assertFalse(is_system_admin(self.u))

    def test_cannot_grant_to_inactive_user(self):
        off = mk("user_off2", is_active=False)
        with self.assertRaises(RoleError):
            grant_role(user=off, role=Role.MODERATOR)


class PermissionClassTests(TestCase):
    def check(self, perm_cls, user, kwargs=None):
        req = APIRequestFactory().get("/")
        req.user = user
        view = mock.Mock(kwargs=kwargs or {})
        return perm_cls().has_permission(req, view)

    def test_is_profile_complete(self):
        u = mk("user_p")
        self.assertFalse(self.check(IsProfileComplete, u))
        from django.utils import timezone

        u.profile_completed_at = timezone.now()
        self.assertFalse(self.check(IsProfileComplete, u))  # spec 14 §4.3
        u.adult_confirmed_at = timezone.now()
        self.assertTrue(self.check(IsProfileComplete, u))

    def test_has_role_reads_scope_from_url(self):
        u = mk("user_h")
        cid = uuid.uuid4()
        grant_role(user=u, role=Role.CANDIDATE, scope_type=ScopeType.CANDIDATE, scope_id=cid)
        perm = HasRole(Role.CANDIDATE, ScopeType.CANDIDATE, "candidate_id")
        self.assertTrue(self.check(perm, u, {"candidate_id": cid}))
        self.assertFalse(self.check(perm, u, {"candidate_id": uuid.uuid4()}))

    def test_is_system_admin(self):
        u = mk("user_sa")
        self.assertFalse(self.check(IsSystemAdmin, u))
        grant_role(user=u, role=Role.SYSTEM_ADMIN)
        self.assertTrue(self.check(IsSystemAdmin, u))


@override_settings(CLERK_JWT_KEY=PUBLIC_PEM)
class CommandTests(APITestCase):
    def run_cmd(self, *args):
        out = StringIO()
        call_command(*args, stdout=out)
        return out.getvalue()

    def test_grant_and_revoke_are_audited_as_cli(self):
        u = mk("user_c", "boss@example.com")
        self.run_cmd("grant_role", "BOSS@example.com", "system_admin")
        a = RoleAssignment.objects.get(user=u)
        self.assertEqual(a.granted_by_label, "system:cli")
        self.assertTrue(AuditLog.objects.filter(action="role.granted", actor_label="system:cli", target_id=str(a.id)).exists())
        with self.assertRaisesMessage(CommandError, "at least one system administrator"):  # spec 11 §3.5: never zero admins, CLI too
            self.run_cmd("revoke_role", "boss@example.com", "system_admin")
        mk("user_c2", "boss2@example.com")
        self.run_cmd("grant_role", "boss2@example.com", "system_admin")
        self.run_cmd("revoke_role", "boss@example.com", "system_admin", "--reason", "rotation")
        self.assertTrue(AuditLog.objects.filter(action="role.revoked", actor_label="system:cli").exists())

    def test_grant_scoped_role_by_clerk_id(self):
        u = mk("user_cand")
        cid = str(uuid.uuid4())
        self.run_cmd("grant_role", "--clerk-id", "user_cand", "candidate", "--scope-type", "candidate", "--scope-id", cid)
        self.assertTrue(has_role(u, Role.CANDIDATE, ScopeType.CANDIDATE, uuid.UUID(cid)))

    def test_command_errors(self):
        with self.assertRaises(CommandError):
            self.run_cmd("grant_role", "nobody@example.com", "moderator")
        mk("user_bad")
        with self.assertRaises(CommandError):
            self.run_cmd("grant_role", "--clerk-id", "user_bad", "candidate")  # missing scope

    def test_deactivate_blocks_api_and_bans_in_clerk(self):
        """AC 5."""
        mk("user_d", "d@example.com")
        auth = bearer(session_token("user_d"))
        self.assertEqual(self.client.get(reverse("me"), **auth).status_code, 200)
        with mock.patch("apps.accounts.clerk.ban_user", return_value=True) as ban:
            out = self.run_cmd("deactivate_user", "d@example.com", "--reason", "spam")
        ban.assert_called_once_with("user_d")
        self.assertIn("Clerk ban confirmed", out)
        res = self.client.get(reverse("me"), **auth)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["error"]["code"], "account_deactivated")
        self.assertTrue(AuditLog.objects.filter(action="user.deactivated", metadata__clerk_ban="ok").exists())

    def test_failed_clerk_ban_keeps_local_block_and_can_be_retried(self):
        u = mk("user_f", "f@example.com")
        with mock.patch("apps.accounts.clerk.ban_user", return_value=False):
            out = self.run_cmd("deactivate_user", "f@example.com")
        self.assertIn("FAILED", out)
        u.refresh_from_db()
        self.assertFalse(u.is_active)
        with mock.patch("apps.accounts.clerk.ban_user", return_value=True) as ban:
            self.run_cmd("deactivate_user", "f@example.com")
        ban.assert_called_once()
        self.assertTrue(AuditLog.objects.filter(action="user.deactivated", metadata__retry=True, metadata__clerk_ban="ok").exists())

    def test_reactivate(self):
        u = mk("user_re", "re@example.com", is_active=False)
        with mock.patch("apps.accounts.clerk.unban_user", return_value=True):
            self.run_cmd("reactivate_user", "re@example.com")
        u.refresh_from_db()
        self.assertTrue(u.is_active)
        self.assertTrue(AuditLog.objects.filter(action="user.reactivated").exists())
