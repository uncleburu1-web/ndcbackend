"""Spec 04 §12 — backend tests for candidate following."""
import threading

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient, APITestCase

from apps.access.models import Role
from apps.access.services import grant_role
from apps.accounts.models import User
from apps.accounts.tests.helpers import signed_webhook
from apps.audit.models import AuditLog
from apps.candidates import services as cand
from apps.candidates.models import Candidate
from apps.follows import services
from apps.follows.models import CandidateFollow

BASE = {"position": "senator", "election_year": 2027, "state": "kano", "area_name": "Kano Central Senatorial District"}


def mk_admin(clerk_id="user_admin"):
    u = User.objects.create_user(clerk_id, email=f"{clerk_id}@example.com", full_name="Admin Person")
    grant_role(user=u, role=Role.SYSTEM_ADMIN)
    return u


def mk_user(clerk_id="user_fan", **kw):
    # Profile deliberately incomplete (no phone): following must not need it (§3 rule 2).
    return User.objects.create_user(clerk_id, email=f"{clerk_id}@example.com", **kw)


def make(admin, full_name="Amina Bello", *, publish=True, **kw):
    c = Candidate(**{**BASE, "full_name": full_name, **kw})
    c.full_clean(exclude=["slug"])
    c.assign_slug()
    c.save()
    if publish:
        cand.verify(candidate=c, notes="ok", actor=admin)
        c = cand.publish(candidate=c, actor=admin)
    return c


def url(slug):
    return reverse("my-follow", kwargs={"slug": slug})


class FollowTests(APITestCase):
    def setUp(self):
        self.admin = mk_admin()
        self.c = make(self.admin)
        self.user = mk_user()
        self.client.force_authenticate(self.user)

    def test_follow_creates_then_repeat_is_noop(self):
        r1 = self.client.put(url(self.c.slug))
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r1.data["candidate"]["slug"], self.c.slug)
        self.assertFalse(r1.data["notify"], "notifications start off")
        r2 = self.client.put(url(self.c.slug))
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(CandidateFollow.objects.count(), 1)

    def test_db_prevents_duplicates(self):
        CandidateFollow.objects.create(user=self.user, candidate=self.c)
        with self.assertRaises(IntegrityError), transaction.atomic():
            CandidateFollow.objects.create(user=self.user, candidate=self.c)

    def test_only_published_can_be_followed(self):
        draft = make(self.admin, "Draft Person", publish=False)
        unpub = make(self.admin, "Gone Person")
        cand.unpublish(candidate=unpub, reason="x", actor=self.admin)
        arch = make(self.admin, "Old Person")
        cand.archive(candidate=arch, reason="x", actor=self.admin)
        for slug in (draft.slug, unpub.slug, arch.slug, "never-existed", "BAD_Slug", "a" * 81):
            r = self.client.put(url(slug))
            self.assertEqual(r.status_code, 404, slug)
            self.assertEqual(r.data["error"]["message"], "We couldn't find that candidate.", "same answer for every case")
        self.assertEqual(CandidateFollow.objects.count(), 0)

    def test_guest_gets_401_everywhere(self):
        guest = APIClient()
        for method, path in (("put", url(self.c.slug)), ("patch", url(self.c.slug)), ("delete", url(self.c.slug)),
                             ("get", reverse("my-follows"))):
            self.assertEqual(getattr(guest, method)(path, {}, format="json").status_code, 401, f"{method} {path}")

    def test_deactivated_user_blocked(self):
        self.user.is_active = False
        self.user.save()
        self.assertEqual(self.client.put(url(self.c.slug)).status_code, 403)

    def test_unfollow_is_idempotent_and_reveals_nothing(self):
        self.client.put(url(self.c.slug))
        self.assertEqual(self.client.delete(url(self.c.slug)).status_code, 204)
        self.assertFalse(CandidateFollow.objects.exists())
        draft = make(self.admin, "Draft Person", publish=False)
        for slug in (self.c.slug, draft.slug, "never-existed", "BAD_Slug"):
            self.assertEqual(self.client.delete(url(slug)).status_code, 204, slug)

    def test_stale_tab_can_unfollow_unpublished_candidate(self):
        self.client.put(url(self.c.slug))
        cand.unpublish(candidate=self.c, reason="x", actor=self.admin)
        self.assertEqual(self.client.delete(url(self.c.slug)).status_code, 204)
        self.assertFalse(CandidateFollow.objects.exists())


class NotifyTests(APITestCase):
    def setUp(self):
        self.admin = mk_admin()
        self.c = make(self.admin)
        self.user = mk_user()
        self.client.force_authenticate(self.user)

    def test_toggle_and_changed_at(self):
        self.client.put(url(self.c.slug))
        f = CandidateFollow.objects.get()
        self.assertIsNone(f.notify_changed_at)
        r = self.client.patch(url(self.c.slug), {"notify": True}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["notify"])
        f.refresh_from_db()
        first = f.notify_changed_at
        self.assertIsNotNone(first)
        self.client.patch(url(self.c.slug), {"notify": True}, format="json")
        f.refresh_from_db()
        self.assertEqual(f.notify_changed_at, first, "unchanged value leaves the timestamp alone")
        self.client.patch(url(self.c.slug), {"notify": False}, format="json")
        f.refresh_from_db()
        self.assertFalse(f.notify)
        self.assertGreater(f.notify_changed_at, first)

    def test_not_following_404(self):
        self.assertEqual(self.client.patch(url(self.c.slug), {"notify": True}, format="json").status_code, 404)

    def test_bad_body_400(self):
        self.client.put(url(self.c.slug))
        for body in ({}, {"notify": "yes"}, {"notify": 1}, {"notify": None}):
            r = self.client.patch(url(self.c.slug), body, format="json")
            self.assertEqual(r.status_code, 400, body)
            self.assertEqual(r.data["error"]["code"], "validation_error")
            self.assertIn("notify", r.data["error"]["fields"])

    def test_refollow_resets_to_off(self):
        self.client.put(url(self.c.slug))
        self.client.patch(url(self.c.slug), {"notify": True}, format="json")
        self.client.delete(url(self.c.slug))
        self.assertFalse(self.client.put(url(self.c.slug)).data["notify"])


class ListTests(APITestCase):
    def setUp(self):
        self.admin = mk_admin()
        self.user = mk_user()
        self.client.force_authenticate(self.user)

    def test_following_page_published_only_newest_first_with_hidden_count(self):
        a = make(self.admin, "Ada One")
        b = make(self.admin, "Bola Two")
        gone = make(self.admin, "Gone Three")
        for c in (a, b, gone):
            services.follow(self.user, c)
        cand.unpublish(candidate=gone, reason="x", actor=self.admin)
        r = self.client.get(reverse("my-follows"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual([x["candidate"]["slug"] for x in r.data["results"]], [b.slug, a.slug])
        self.assertEqual(r.data["hidden_count"], 1)
        self.assertEqual(r.data["count"], 2)
        # Republished → back on the page (§4.4).
        cand.verify(candidate=gone, notes="ok", actor=self.admin)
        cand.publish(candidate=gone, actor=self.admin)
        r = self.client.get(reverse("my-follows"))
        self.assertEqual(r.data["count"], 3)
        self.assertEqual(r.data["hidden_count"], 0)

    def test_pagination(self):
        for i in range(23):
            services.follow(self.user, make(self.admin, f"Person {i:02d}"))
        r = self.client.get(reverse("my-follows"))
        self.assertEqual(len(r.data["results"]), 20)
        self.assertEqual(r.data["count"], 23)
        self.assertEqual(len(self.client.get(reverse("my-follows"), {"page": 2}).data["results"]), 3)

    def test_slug_state_lookup(self):
        a = make(self.admin, "Ada One")
        b = make(self.admin, "Bola Two")
        services.follow(self.user, a)
        services.set_notify(self.user, a, True)
        r = self.client.get(reverse("my-follows"), {"slugs": f"{a.slug},{b.slug},never,BAD_x"})
        self.assertEqual(r.data, {"results": [{"slug": a.slug, "notify": True}]})
        too_many = ",".join(f"s-{i}" for i in range(51))
        r = self.client.get(reverse("my-follows"), {"slugs": too_many})
        self.assertEqual(r.status_code, 400)
        self.assertIn("slugs", r.data["error"]["fields"])
        self.assertEqual(self.client.get(reverse("my-follows"), {"slugs": ""}).data, {"results": []})

    def test_follow_payload_has_public_candidate_fields_only(self):
        c = make(self.admin, verification_notes="SECRET NOTE")
        r = self.client.put(url(c.slug))
        self.assertNotIn("SECRET NOTE", str(r.data))
        self.assertFalse({"id", "status", "verification_notes", "verified_by"} & set(r.data["candidate"]))
        self.assertEqual(set(r.data), {"candidate", "notify", "followed_at"})

    def test_private_no_store_on_every_response(self):
        c = make(self.admin)
        for res in (self.client.put(url(c.slug)), self.client.patch(url(c.slug), {"notify": True}, format="json"),
                    self.client.get(reverse("my-follows")), self.client.get(reverse("my-follows"), {"slugs": c.slug}),
                    self.client.delete(url(c.slug)), self.client.put(url("never-existed"))):
            cc = res.get("Cache-Control", "")
            self.assertIn("private", cc, res.status_code)
            self.assertIn("no-store", cc, res.status_code)


class IsolationAndPrivacyTests(APITestCase):
    def setUp(self):
        self.admin = mk_admin()
        self.c = make(self.admin)
        self.a = mk_user("fan_alpha")
        self.b = mk_user("fan_bravo")
        services.follow(self.a, self.c)
        services.set_notify(self.a, self.c, True)

    def test_users_cannot_see_or_change_each_other(self):
        self.client.force_authenticate(self.b)
        self.assertEqual(self.client.get(reverse("my-follows")).data["count"], 0)
        self.assertEqual(self.client.get(reverse("my-follows"), {"slugs": self.c.slug}).data["results"], [])
        self.assertEqual(self.client.patch(url(self.c.slug), {"notify": False}, format="json").status_code, 404)
        self.client.delete(url(self.c.slug))  # B unfollowing does nothing to A
        self.client.get(reverse("my-follows"), {"user": str(self.a.id), "user_id": str(self.a.id)})
        f = CandidateFollow.objects.get(user=self.a)
        self.assertTrue(f.notify)

    def test_no_audit_entries_for_follows(self):
        before = AuditLog.objects.count()
        self.client.force_authenticate(self.b)
        self.client.put(url(self.c.slug))
        self.client.patch(url(self.c.slug), {"notify": True}, format="json")
        self.client.delete(url(self.c.slug))
        self.assertEqual(AuditLog.objects.count(), before)

    def test_admin_sees_counts_only(self):
        services.follow(self.b, self.c)
        self.client.force_authenticate(self.admin)
        r = self.client.get(reverse("admin-candidate", kwargs={"pk": self.c.pk}))
        self.assertEqual(r.data["followers"], {"total": 2, "notify_on": 1})
        self.assertNotIn(str(self.a.id), str(r.data))
        self.assertNotIn("fan_alpha", str(r.data))
        self.assertNotIn("fan_bravo", str(r.data))
        # Actions also return counts, so the admin page stays current.
        r = self.client.post(reverse("admin-candidate-unpublish", kwargs={"pk": self.c.pk}), {"reason": "x"}, format="json")
        self.assertEqual(r.data["followers"]["total"], 2)

    def test_non_admin_cannot_get_counts(self):
        self.client.force_authenticate(self.a)
        self.assertEqual(self.client.get(reverse("admin-candidate", kwargs={"pk": self.c.pk})).status_code, 403)

    def test_public_endpoints_never_mention_followers(self):
        for res in (self.client.get(reverse("candidates")),
                    self.client.get(reverse("candidate-detail", kwargs={"slug": self.c.slug}))):
            self.assertNotIn("follow", str(res.data).lower())


class LifecycleTests(APITestCase):
    def setUp(self):
        self.admin = mk_admin()
        self.c = make(self.admin)

    def post_webhook(self, body, headers):
        return self.client.post(reverse("clerk-webhook"), data=body, content_type="application/json", **headers)

    def test_clerk_user_deleted_removes_follows(self):
        u = mk_user("user_test123")
        other = mk_user("user_other")
        services.follow(u, self.c)
        services.follow(other, self.c)
        self.post_webhook(*signed_webhook("user.deleted", {"id": "user_test123", "deleted": True, "object": "user"}))
        self.assertFalse(CandidateFollow.objects.filter(user=u).exists())
        self.assertTrue(CandidateFollow.objects.filter(user=other).exists(), "only that user's follows")
        entry = AuditLog.objects.get(action="user.deleted", target_id=str(u.id))
        self.assertEqual(entry.metadata.get("follows_removed"), 1)

    def test_deactivation_keeps_follows(self):
        u = mk_user()
        services.follow(u, self.c)
        u.is_active = False
        u.save()
        self.assertTrue(CandidateFollow.objects.filter(user=u).exists())

    def test_archive_keeps_follows_hidden(self):
        u = mk_user()
        services.follow(u, self.c)
        cand.archive(candidate=self.c, reason="x", actor=self.admin)
        self.assertEqual(CandidateFollow.objects.filter(user=u).count(), 1)
        self.client.force_authenticate(u)
        r = self.client.get(reverse("my-follows"))
        self.assertEqual((r.data["count"], r.data["hidden_count"]), (0, 1))


class IndependenceTests(APITestCase):
    """Following needs no donation, PVC or completed profile (§3 rule 2, AC 6)."""

    def test_incomplete_profile_can_follow_and_set_notify(self):
        admin = mk_admin()
        c = make(admin)
        u = mk_user("user_new")  # no name, no phone
        self.assertFalse(u.profile_complete)
        self.client.force_authenticate(u)
        self.assertEqual(self.client.put(url(c.slug)).status_code, 201)
        self.assertEqual(self.client.patch(url(c.slug), {"notify": True}, format="json").status_code, 200)


@override_settings(REST_FRAMEWORK={
    **settings.REST_FRAMEWORK,
    "DEFAULT_THROTTLE_RATES": {"follows": "3/min", "candidates_public": "10000/min", "profile_write": "10000/min",
                               "clerk_webhook": "10000/min"},
})
class ThrottleTests(APITestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    def tearDown(self):
        from django.core.cache import cache

        cache.clear()

    def test_writes_throttled_reads_not(self):
        from rest_framework.settings import api_settings

        from apps.follows.views import FollowWriteThrottle

        FollowWriteThrottle.THROTTLE_RATES = api_settings.DEFAULT_THROTTLE_RATES
        try:
            admin = mk_admin()
            c = make(admin)
            self.client.force_authenticate(mk_user())
            codes = [self.client.put(url(c.slug)).status_code for _ in range(4)]
            self.assertEqual(codes[:3], [201, 200, 200])
            self.assertEqual(codes[3], 429)
            r = self.client.delete(url(c.slug))
            self.assertEqual(r.data["error"]["code"], "throttled")
            self.assertEqual(self.client.get(reverse("my-follows")).status_code, 200)
        finally:
            FollowWriteThrottle.THROTTLE_RATES = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]


class ConcurrencyTests(TransactionTestCase):
    """Two taps at once must still produce one follow (§3 rule 5, AC 2)."""

    def test_concurrent_follows_make_one_row(self):
        admin = mk_admin()
        c = make(admin)
        u = mk_user()
        barrier = threading.Barrier(4)
        errors = []

        def worker():
            try:
                barrier.wait()
                services.follow(u, c)
            except Exception as exc:  # pragma: no cover — reported below
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(CandidateFollow.objects.filter(user=u, candidate=c).count(), 1)
