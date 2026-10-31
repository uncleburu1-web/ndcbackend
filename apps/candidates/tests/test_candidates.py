"""Spec 03 §12 — backend tests."""
import io
import shutil
import tempfile
import uuid

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image
from rest_framework.test import APITestCase

from apps.access.models import Role, RoleAssignment, ScopeType
from apps.access.services import grant_role, has_role
from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.candidates.models import Candidate, CandidateStatus as S
from apps.candidates.reference import area_display

TMP_MEDIA = tempfile.mkdtemp()

BASE = {
    "full_name": "Amina Bello",
    "position": "senator",
    "election_year": 2027,
    "state": "kano",
    "area_name": "Kano Central Senatorial District",
}

PRIVATE_FIELDS = {"verification_notes", "verified_by", "verified_by_email", "id", "status", "created_by", "updated_by",
                  "accounts", "linked_accounts", "area_name", "ward_name"}


def mk_admin(clerk_id="user_admin"):
    u = User.objects.create_user(clerk_id, email=f"{clerk_id}@example.com", full_name="Admin Person")
    grant_role(user=u, role=Role.SYSTEM_ADMIN)
    return u


def make(**overrides) -> Candidate:
    c = Candidate(**{**BASE, **overrides})
    c.full_clean(exclude=["slug"])
    c.assign_slug()
    c.save()
    return c


def jpeg(size=(900, 700), *, gps=True, fmt="JPEG") -> bytes:
    img = Image.new("RGB", size, (46, 43, 125))
    out = io.BytesIO()
    kwargs = {}
    if gps and fmt == "JPEG":
        exif = Image.Exif()
        exif[0x010F] = "PhoneMaker"  # Make
        exif[0x8825] = {1: "N", 2: (6.0, 27.0, 0.0), 3: "E", 4: (3.0, 23.0, 0.0)}  # GPS IFD: Lagos
        kwargs["exif"] = exif
    img.save(out, format=fmt, **kwargs)
    return out.getvalue()


# ======================================================================= model
class ValidationTests(TestCase):
    def check_invalid(self, field, **overrides):
        with self.assertRaises(ValidationError) as ctx:
            Candidate(**{**BASE, **overrides}).full_clean(exclude=["slug"])
        self.assertIn(field, ctx.exception.message_dict, overrides)

    def test_area_rules_per_position(self):
        self.check_invalid("state", position="senator", state="")
        self.check_invalid("area_name", position="senator", area_name="")
        self.check_invalid("state", position="president", state="kano", area_name="")
        self.check_invalid("area_name", position="governor", area_name="Kano Central")
        self.check_invalid("ward_name", position="councillor", area_name="Nassarawa", ward_name="")
        self.check_invalid("ward_name", position="senator", ward_name="Ward 05")
        Candidate(**{**BASE, "position": "president", "state": "", "area_name": ""}).full_clean(exclude=["slug"])
        Candidate(**{**BASE, "position": "councillor", "area_name": "Nassarawa", "ward_name": "Ward 05"}).full_clean(exclude=["slug"])

    def test_fct_has_no_governor_or_state_assembly(self):
        self.check_invalid("state", position="governor", state="fct", area_name="")
        self.check_invalid("state", position="state_assembly", state="fct", area_name="AMAC")
        Candidate(**{**BASE, "position": "senator", "state": "fct", "area_name": "FCT Senatorial District"}).full_clean(exclude=["slug"])

    def test_priorities_and_links_limits(self):
        self.check_invalid("priorities", priorities=["x"] * 9)
        self.check_invalid("priorities", priorities=["y" * 121])
        self.check_invalid("priorities", priorities=["  "])
        self.check_invalid("links", links=[{"label": "Site", "url": "http://insecure.example"}])
        self.check_invalid("links", links=[{"label": "Site", "url": "javascript:alert(1)"}])
        self.check_invalid("links", links=[{"label": "", "url": "https://ok.example"}])
        self.check_invalid("links", links=[{"label": f"L{i}", "url": "https://ok.example"} for i in range(6)])
        Candidate(**{**BASE, "priorities": ["Jobs"], "links": [{"label": "Site", "url": "https://ok.example/x"}]}).full_clean(exclude=["slug"])

    def test_election_year_range(self):
        self.check_invalid("election_year", election_year=27)

    def test_slugs_unique_and_fixed_after_publish(self):
        a, b = make(), make()
        self.assertEqual((a.slug, b.slug), ("amina-bello", "amina-bello-2"))
        b.full_name = "Amina B. Bello"
        b.assign_slug()
        self.assertEqual(b.slug, "amina-b-bello")
        b.published_at = b.created_at
        b.full_name = "Someone Else"
        b.assign_slug()
        self.assertEqual(b.slug, "amina-b-bello", "fixed once published")

    def test_db_requires_verification_to_publish(self):
        c = make()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Candidate.objects.filter(pk=c.pk).update(status=S.PUBLISHED)

    def test_area_display(self):
        self.assertEqual(area_display("president", ""), "Nigeria")
        self.assertEqual(area_display("governor", "kano"), "Kano State")
        self.assertEqual(area_display("senator", "kano", "Kano Central Senatorial District"), "Kano Central Senatorial District, Kano State")
        self.assertEqual(area_display("councillor", "kano", "Nassarawa", "Ward 05"), "Ward 05, Nassarawa LGA, Kano State")
        self.assertEqual(area_display("lga_chairman", "fct", "Bwari"), "Bwari Area Council, Federal Capital Territory")


# ==================================================================== workflow
class WorkflowTests(APITestCase):
    def setUp(self):
        self.admin = mk_admin()
        self.client.force_authenticate(self.admin)

    def url(self, name, c, **kw):
        return reverse(name, kwargs={"pk": c.pk, **kw})

    def act(self, c, action, **body):
        return self.client.post(self.url(f"admin-candidate-{action}", c), body, format="json")

    def published(self):
        c = make()
        self.act(c, "verify", notes="Checked INEC list")
        self.act(c, "publish")
        c.refresh_from_db()
        return c

    def test_full_happy_path_is_audited(self):
        res = self.client.post(reverse("admin-candidates"), {**BASE, "priorities": ["Jobs for youth"]}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        c = Candidate.objects.get(pk=res.data["id"])
        self.assertEqual(c.status, S.DRAFT)
        self.assertEqual(self.client.patch(self.url("admin-candidate", c), {"biography": "Teacher."}, format="json").status_code, 200)
        self.assertEqual(self.act(c, "verify", notes="Matched INEC nomination list").status_code, 200)
        res = self.act(c, "publish")
        self.assertEqual(res.data["status"], "published")
        self.assertEqual(self.act(c, "unpublish", reason="Withdrawn").data["status"], "unpublished")
        self.assertEqual(self.act(c, "publish").data["status"], "published")
        self.assertEqual(self.act(c, "archive", reason="Lost primaries").data["status"], "archived")
        res = self.act(c, "restore")
        self.assertEqual((res.data["status"], res.data["is_verified"]), ("draft", False))
        actions = set(AuditLog.objects.filter(target_id=str(c.pk)).values_list("action", flat=True))
        self.assertTrue({"candidate.created", "candidate.updated", "candidate.verified", "candidate.published",
                         "candidate.unpublished", "candidate.archived", "candidate.restored"} <= actions)
        self.assertEqual(AuditLog.objects.get(action="candidate.unpublished", target_id=str(c.pk)).metadata["reason"], "Withdrawn")

    def test_cannot_publish_without_verification(self):
        c = make()
        res = self.act(c, "publish")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["error"]["code"], "invalid_transition")
        self.assertIn("Verify this candidate before publishing", res.data["error"]["message"])

    def test_disallowed_transitions_are_409(self):
        c = make()
        self.assertEqual(self.act(c, "unpublish", reason="x").status_code, 409)
        self.assertEqual(self.act(c, "restore").status_code, 409)
        self.assertEqual(self.act(c, "unverify").status_code, 409)
        p = self.published()
        self.assertEqual(self.act(p, "verify", notes="again").status_code, 409)
        self.assertEqual(self.act(p, "unverify").status_code, 409)
        self.assertEqual(self.act(p, "publish").status_code, 409)
        self.act(p, "archive", reason="x")
        self.assertEqual(self.act(p, "archive", reason="x").status_code, 409)
        self.assertEqual(self.client.patch(self.url("admin-candidate", p), {"biography": "x"}, format="json").status_code, 409)

    def test_required_notes_and_reasons(self):
        c = make()
        self.assertEqual(self.act(c, "verify").status_code, 400)
        p = self.published()
        self.assertEqual(self.act(p, "unpublish").status_code, 400)
        self.assertEqual(self.act(p, "archive", reason="  ").status_code, 400)

    def test_identity_edit_on_published_unpublishes_and_clears_verification(self):
        p = self.published()
        res = self.client.patch(self.url("admin-candidate", p), {"area_name": "Kano South Senatorial District"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["unpublished_by_edit"])
        self.assertEqual((res.data["status"], res.data["is_verified"]), ("unpublished", False))
        self.assertTrue(AuditLog.objects.filter(action="candidate.unpublished", target_id=str(p.pk)).exists())
        self.assertEqual(self.client.get(reverse("candidate-detail", kwargs={"slug": p.slug})).status_code, 404)

    def test_non_identity_edit_keeps_published(self):
        p = self.published()
        res = self.client.patch(self.url("admin-candidate", p), {"biography": "Updated bio", "priorities": ["Roads"]}, format="json")
        self.assertEqual((res.data["status"], res.data["unpublished_by_edit"]), ("published", False))

    def test_identity_edit_on_verified_draft_clears_verification(self):
        c = make()
        self.act(c, "verify", notes="ok")
        res = self.client.patch(self.url("admin-candidate", c), {"full_name": "Amina Bala"}, format="json")
        self.assertFalse(res.data["is_verified"])
        self.assertTrue(AuditLog.objects.filter(action="candidate.unverified", target_id=str(c.pk)).exists())

    def test_validation_errors_from_model_reach_the_envelope(self):
        res = self.client.post(reverse("admin-candidates"), {**BASE, "area_name": ""}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("area_name", res.data["error"]["fields"])

    def test_account_linking_and_archive_revokes(self):
        c = make()
        member = User.objects.create_user("user_cand", email="cand@example.com", full_name="Amina Bello")
        res = self.client.post(self.url("admin-candidate-accounts", c), {"email": "CAND@example.com"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(has_role(member, Role.CANDIDATE, ScopeType.CANDIDATE, c.pk))
        self.assertEqual(self.client.post(self.url("admin-candidate-accounts", c), {"email": "cand@example.com"}, format="json").status_code, 409)
        self.assertEqual(len(self.client.get(self.url("admin-candidate-accounts", c)).data), 1)
        self.assertEqual(self.client.post(self.url("admin-candidate-accounts", c), {"email": "nobody@example.com"}, format="json").status_code, 400)

        self.act(c, "archive", reason="Withdrew")
        self.assertFalse(has_role(member, Role.CANDIDATE, ScopeType.CANDIDATE, c.pk))
        self.act(c, "restore")
        self.assertFalse(has_role(member, Role.CANDIDATE, ScopeType.CANDIDATE, c.pk), "restore does not bring links back")
        self.assertTrue(AuditLog.objects.filter(action="role.granted").exists())
        self.assertTrue(AuditLog.objects.filter(action="role.revoked").exists())

    def test_unlink(self):
        c = make()
        member = User.objects.create_user("user_c2", email="c2@example.com")
        self.client.post(self.url("admin-candidate-accounts", c), {"email": "c2@example.com"}, format="json")
        res = self.client.delete(self.url("admin-candidate-account", c, user_id=member.pk))
        self.assertEqual(res.status_code, 204)
        self.assertFalse(RoleAssignment.objects.filter(user=member, is_active=True).exists())
        self.assertEqual(self.client.delete(self.url("admin-candidate-account", c, user_id=member.pk)).status_code, 404)


# ================================================================= permissions
class PermissionTests(APITestCase):
    def test_non_admins_blocked_everywhere(self):
        c = make()
        endpoints = [
            ("get", reverse("admin-candidates")),
            ("post", reverse("admin-candidates")),
            ("get", reverse("admin-candidate", kwargs={"pk": c.pk})),
            ("patch", reverse("admin-candidate", kwargs={"pk": c.pk})),
            ("post", reverse("admin-candidate-verify", kwargs={"pk": c.pk})),
            ("post", reverse("admin-candidate-publish", kwargs={"pk": c.pk})),
            ("put", reverse("admin-candidate-photo", kwargs={"pk": c.pk})),
            ("get", reverse("admin-candidate-accounts", kwargs={"pk": c.pk})),
        ]
        for method, url in endpoints:
            self.assertEqual(getattr(self.client, method)(url, {}, format="json").status_code, 401, f"guest {method} {url}")

        regular = User.objects.create_user("user_reg", email="r@example.com")
        linked = User.objects.create_user("user_lnk", email="l@example.com")
        grant_role(user=linked, role=Role.CANDIDATE, scope_type=ScopeType.CANDIDATE, scope_id=c.pk)
        for user in (regular, linked):
            self.client.force_authenticate(user)
            for method, url in endpoints:
                self.assertEqual(getattr(self.client, method)(url, {}, format="json").status_code, 403, f"{user} {method} {url}")


# ================================================================== public API
class PublicApiTests(APITestCase):
    def setUp(self):
        self.admin = mk_admin()

    def publish(self, **kw):
        from apps.candidates import services

        c = make(**kw)
        services.verify(candidate=c, notes="ok", actor=self.admin)
        return services.publish(candidate=c, actor=self.admin)

    def test_only_published_listed_and_retrievable(self):
        live = self.publish(full_name="Live Person")
        draft = make(full_name="Draft Person")
        unpub = self.publish(full_name="Gone Person")
        from apps.candidates import services

        services.unpublish(candidate=unpub, reason="x", actor=self.admin)
        arch = self.publish(full_name="Old Person")
        services.archive(candidate=arch, reason="x", actor=self.admin)

        res = self.client.get(reverse("candidates"))
        self.assertEqual([r["slug"] for r in res.data["results"]], [live.slug])
        for hidden in (draft, unpub, arch):
            r = self.client.get(reverse("candidate-detail", kwargs={"slug": hidden.slug}))
            self.assertEqual(r.status_code, 404, hidden.slug)
        self.assertEqual(self.client.get(reverse("candidate-detail", kwargs={"slug": "never-existed"})).status_code, 404)

    def test_public_fields_never_include_private_data(self):
        c = self.publish(biography="Bio", verification_notes="SECRET NOTE")
        detail = self.client.get(reverse("candidate-detail", kwargs={"slug": c.slug})).data
        self.assertFalse(PRIVATE_FIELDS & set(detail), set(detail))
        self.assertNotIn("SECRET NOTE", str(detail))
        self.assertTrue(detail["verified"])
        self.assertEqual(detail["area_display"], "Kano Central Senatorial District, Kano State")
        listing = self.client.get(reverse("candidates")).data["results"][0]
        self.assertFalse(PRIVATE_FIELDS & set(listing))

    def test_ordering_filters_search_and_positions(self):
        self.publish(full_name="Zed Senator")
        self.publish(full_name="Pres Person", position="president", state="", area_name="")
        self.publish(full_name="Gov Lagos", position="governor", state="lagos", area_name="")
        self.publish(full_name="Councillor One", position="councillor", area_name="Nassarawa", ward_name="Ward 05")
        names = [r["full_name"] for r in self.client.get(reverse("candidates")).data["results"]]
        self.assertEqual(names, ["Pres Person", "Gov Lagos", "Zed Senator", "Councillor One"])
        self.assertEqual(len(self.client.get(reverse("candidates"), {"position": "governor"}).data["results"]), 1)
        self.assertEqual([r["full_name"] for r in self.client.get(reverse("candidates"), {"state": "lagos"}).data["results"]], ["Gov Lagos"])
        self.assertEqual([r["full_name"] for r in self.client.get(reverse("candidates"), {"q": "nassarawa"}).data["results"]], ["Councillor One"])
        pos = self.client.get(reverse("candidate-positions")).data
        self.assertEqual([p["position"] for p in pos["positions"]], ["president", "governor", "senator", "councillor"])
        self.assertEqual(pos["positions"][0]["label"], "President")

    def test_defaults_to_latest_election_year(self):
        self.publish(full_name="Old Cycle", election_year=2023)
        self.publish(full_name="New Cycle", election_year=2027)
        res = self.client.get(reverse("candidates"))
        self.assertEqual(res.data["election_year"], 2027)
        self.assertEqual([r["full_name"] for r in res.data["results"]], ["New Cycle"])
        self.assertEqual([r["full_name"] for r in self.client.get(reverse("candidates"), {"election_year": 2023}).data["results"]], ["Old Cycle"])
        self.assertEqual(self.client.get(reverse("candidate-positions")).data["election_years"], [2027, 2023])

    def test_pagination_and_cache_headers(self):
        for i in range(23):
            self.publish(full_name=f"Person {i:02d}")
        res = self.client.get(reverse("candidates"))
        self.assertEqual((res.data["count"], len(res.data["results"])), (23, 20))
        self.assertIn("public", res["Cache-Control"])
        self.assertIn("max-age=60", res["Cache-Control"])

    def test_empty_when_nothing_published(self):
        res = self.client.get(reverse("candidates"))
        self.assertEqual((res.status_code, res.data["count"]), (200, 0))


# ===================================================================== photos
@override_settings(MEDIA_ROOT=TMP_MEDIA)
class PhotoTests(APITestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP_MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.client.force_authenticate(mk_admin())
        self.c = make()
        self.url = reverse("admin-candidate-photo", kwargs={"pk": self.c.pk})

    def upload(self, data, name="p.jpg", rights="true"):
        return self.client.put(self.url, {"photo": SimpleUploadedFile(name, data), "rights_confirmed": rights}, format="multipart")

    def test_strips_metadata_and_stores_webp_800_and_200(self):
        raw = jpeg()
        self.assertIn(0x8825, Image.open(io.BytesIO(raw)).getexif(), "fixture has GPS")
        res = self.upload(raw)
        self.assertEqual(res.status_code, 200, res.data)
        self.c.refresh_from_db()
        from django.core.files.storage import default_storage

        for name, size in ((self.c.photo_800, 800), (self.c.photo_200, 200)):
            with default_storage.open(name) as f:
                img = Image.open(f)
                img.load()
                self.assertEqual((img.format, img.size), ("WEBP", (size, size)))
                self.assertEqual(len(img.getexif()), 0, "no EXIF/GPS left")
                self.assertNotIn("exif", img.info)
        self.assertTrue(res.data["photo"]["url_800"].endswith("_800.webp"))
        self.assertTrue(AuditLog.objects.filter(action="candidate.photo_updated", metadata__rights_confirmed=True).exists())

    def test_rejections(self):
        self.assertEqual(self.upload(jpeg((300, 300))).data["error"]["code"], "invalid_photo")  # too small
        self.assertEqual(self.upload(b"%PDF-1.4 not an image", name="x.jpg").data["error"]["code"], "invalid_photo")
        gif = io.BytesIO()
        Image.new("RGB", (500, 500)).save(gif, format="GIF")
        self.assertEqual(self.upload(gif.getvalue(), name="x.png").status_code, 400)
        with override_settings(CANDIDATE_PHOTO_MAX_BYTES=1000):
            self.assertIn("5 MB", self.upload(jpeg()).data["error"]["message"])
        self.assertEqual(self.upload(jpeg(), rights="false").status_code, 400)
        self.assertEqual(self.client.put(self.url, {"rights_confirmed": "true"}, format="multipart").status_code, 400)
        self.c.refresh_from_db()
        self.assertEqual(self.c.photo_800, "")

    def test_png_and_webp_accepted_and_replace_removes_old_files(self):
        from django.core.files.storage import default_storage

        self.assertEqual(self.upload(jpeg(gps=False, fmt="PNG"), name="a.png").status_code, 200)
        self.c.refresh_from_db()
        old = self.c.photo_800
        self.assertEqual(self.upload(jpeg(gps=False, fmt="WEBP"), name="b.webp").status_code, 200)
        self.assertFalse(default_storage.exists(old))
        res = self.client.delete(self.url)
        self.assertIsNone(res.data["photo"])

    def test_storage_failure_is_503_and_record_unchanged(self):
        from unittest import mock

        with mock.patch("django.core.files.storage.default_storage.save", side_effect=OSError("disk gone")):
            res = self.upload(jpeg())
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.data["error"]["code"], "storage_unavailable")
        self.c.refresh_from_db()
        self.assertEqual(self.c.photo_800, "")
