"""Spec 15 §7, §16 — Cloudinary (mocked at the HTTP boundary) and the local backend."""
import io
import json
import shutil
import tempfile
import urllib.parse
from datetime import timedelta
from unittest import mock

import cloudinary.utils
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from apps.access.models import Role, ScopeType
from apps.access.services import grant_role
from apps.accounts.models import User
from apps.candidates.models import Candidate
from apps.core import outbox
from apps.core.models import OutboxJob
from apps.integrations.http import HttpResult, ProviderUnavailable
from apps.media import services as M
from apps.media.models import MediaAsset

NOW = timezone.now()
SECRET = "test_cloudinary_secret_value"
CLD = dict(MEDIA_BACKEND="cloudinary", CLOUDINARY_CLOUD_NAME="ndc-test", CLOUDINARY_API_KEY="123456789012345",
           CLOUDINARY_API_SECRET=SECRET, CLOUDINARY_FOLDER="ndcconnect/test", CLOUDINARY_UPLOAD_PRESET="ndcconnect_signed",
           CLOUDINARY_API_BASE="https://api.cloudinary.com", CLOUDINARY_DELIVERY_BASE="https://res.cloudinary.com")
TMP_MEDIA = tempfile.mkdtemp(prefix="ndc-media-test-")


def mk(cid, *roles):
    u = User.objects.create_user(cid, email=f"{cid}@example.com", full_name=f"Person {cid}", phone_number="+2348031234567",
                                 profile_completed_at=NOW, adult_confirmed_at=NOW)
    for r in roles:
        grant_role(user=u, role=r)
    return u


def client(user=None):
    c = APIClient()
    if user:
        c.force_authenticate(user)
    return c


def jpeg(size=(900, 700)):
    out = io.BytesIO()
    Image.new("RGB", size, (46, 43, 125)).save(out, format="JPEG")
    return out.getvalue()


class FakeCloud:
    """Cloudinary's Admin and Upload APIs, enough for these tests."""

    def __init__(self):
        self.resources, self.destroyed, self.renamed, self.uploaded = {}, [], [], []
        self.down = False

    def add(self, public_id, **info):
        self.resources[public_id] = {"public_id": public_id, "version": 1730000000, "format": "jpg", "bytes": 200_000, "width": 900,
                                     "height": 700, "secure_url": f"https://res.cloudinary.com/ndc-test/image/upload/v1/{public_id}.jpg",
                                     "created_at": (NOW - timedelta(days=2)).isoformat(), "type": "upload", **info}

    def __call__(self, provider, method, url, **kw):
        if self.down:
            raise ProviderUnavailable("cloudinary", "TimeoutError")
        path = urllib.parse.urlsplit(url).path
        if method == "GET" and "/resources/image/upload/" in path:
            pid = urllib.parse.unquote(path.split("/resources/image/upload/", 1)[1])
            r = self.resources.get(pid)
            return HttpResult(200 if r else 404, json.dumps(r or {}).encode(), {})
        if method == "GET" and path.endswith("/resources/image/upload"):
            return HttpResult(200, json.dumps({"resources": list(self.resources.values())}).encode(), {})
        data = kw.get("data") or {}
        if method == "POST" and path.endswith("/destroy"):
            self.destroyed.append(data["public_id"])
            self.resources.pop(data["public_id"], None)
            return HttpResult(200, b'{"result": "ok"}', {})
        if method == "POST" and path.endswith("/rename"):
            self.renamed.append((data["from_public_id"], data["to_type"]))
            return HttpResult(200, b"{}", {})
        if method == "POST" and path.endswith("/upload"):
            self.uploaded.append(data["public_id"])
            self.add(data["public_id"], format="webp")
            return HttpResult(200, json.dumps(self.resources[data["public_id"]]).encode(), {})
        raise AssertionError(f"unexpected {method} {url}")


def browser_upload(cloud, sig, **info):
    """What Cloudinary returns to the browser after a signed upload: public_id, version and its response signature."""
    cloud.add(sig["public_id"], **info)
    version = cloud.resources[sig["public_id"]]["version"]
    return {"public_id": sig["public_id"], "version": version,
            "signature": cloudinary.utils.api_sign_request({"public_id": sig["public_id"], "version": str(version)}, SECRET)}


@override_settings(**CLD, MEDIA_ROOT=TMP_MEDIA)
class CloudinaryFlowTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP_MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.cloud = FakeCloud()
        p = mock.patch("apps.integrations.http.call", side_effect=self.cloud)
        p.start()
        self.addCleanup(p.stop)
        self.sa = mk("u_sa", Role.SYSTEM_ADMIN)
        self.c = Candidate.objects.create(slug="amina", full_name="Amina Bello", position="president", election_year=2027,
                                          status="published", verified_at=NOW, published_at=NOW)

    def sign(self, user=None, purpose="candidate_photo", target=None):
        return client(user or self.sa).post(reverse("media-upload-signature"), {"purpose": purpose, "target_id": str(target or self.c.pk)},
                                            format="json")

    def put_photo(self, asset):
        return client(self.sa).put(reverse("admin-candidate-photo", kwargs={"pk": self.c.pk}), {"asset": asset, "rights_confirmed": True},
                                   format="json")

    def test_signature_only_for_allowed_purpose_and_target_and_never_the_secret(self):
        r = self.sign()
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual((d["mode"], d["cloud_name"], d["upload_preset"]), ("direct", "ndc-test", "ndcconnect_signed"))
        self.assertTrue(d["public_id"].startswith("ndcconnect/test/candidate_photo/"))
        self.assertNotIn(SECRET, json.dumps(d))
        self.assertIn("no-store", r["Cache-Control"])
        self.assertEqual(self.sign(user=mk("u_plain")).status_code, 403)
        self.assertEqual(self.sign(purpose="membership_photo").status_code, 400)
        self.assertEqual(client().post(reverse("media-upload-signature"), {}, format="json").status_code, 401)

    def test_verified_asset_is_linked_with_responsive_urls(self):
        asset = browser_upload(self.cloud, self.sign().json())
        r = self.put_photo(asset)
        self.assertEqual(r.status_code, 200, r.content)
        self.c.refresh_from_db()
        self.assertTrue(self.c.photo_800.startswith("cld:ndcconnect/test/candidate_photo/"))
        a = self.c.photo_asset
        self.assertEqual((a.provider, a.public_id, a.width, a.format, a.uploaded_by), ("cloudinary", asset["public_id"], 900, "jpg", self.sa))
        self.assertIn("secure_url", [f.name for f in MediaAsset._meta.fields])
        pub = client().get(reverse("candidate-detail", kwargs={"slug": self.c.slug})).json()["photo"]
        self.assertIn("/image/upload/c_fill,g_auto,ar_1:1,w_800,f_auto,q_auto/", pub["url_800"])
        self.assertEqual(pub["srcset"].count("w,"), 2)
        self.assertIn(" 800w", pub["srcset"])

    def test_bad_assets_are_destroyed_and_refused(self):
        cases = [({"format": "pdf"}, "JPEG, PNG or WebP"), ({"bytes": 6 * 1024 * 1024}, "5 MB"), ({"width": 300, "height": 300}, "too small")]
        for info, words in cases:
            asset = browser_upload(self.cloud, self.sign().json(), **info)
            with self.captureOnCommitCallbacks(execute=True):
                r = self.put_photo(asset)
            self.assertEqual(r.status_code, 400, info)
            self.assertIn(words, r.json()["error"]["message"])
            self.assertIn(asset["public_id"], self.cloud.destroyed)
        # outside our folder, or a signature we didn't issue
        sig = self.sign().json()
        forged = {**browser_upload(self.cloud, sig), "signature": "0" * 40}
        self.assertEqual(self.put_photo(forged).status_code, 400)
        other = {"public_id": "someone/else/x", "version": 1, "signature": "x"}
        self.assertEqual(self.put_photo(other).status_code, 400)
        self.c.refresh_from_db()
        self.assertEqual(self.c.photo_800, "")

    def test_replace_and_delete_destroy_the_old_asset_after_commit(self):
        first = browser_upload(self.cloud, self.sign().json())
        self.put_photo(first)
        second = browser_upload(self.cloud, self.sign().json())
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.put_photo(second).status_code, 200)
        self.assertEqual(self.cloud.destroyed, [first["public_id"]])
        self.assertEqual(MediaAsset.objects.get(public_id=first["public_id"]).delivery, "deleted")
        with self.captureOnCommitCallbacks(execute=True):
            client(self.sa).delete(reverse("admin-candidate-photo", kwargs={"pk": self.c.pk}))
        self.assertEqual(self.cloud.destroyed, [first["public_id"], second["public_id"]])

    def test_destroy_failure_retried_by_the_outbox(self):
        first = browser_upload(self.cloud, self.sign().json())
        self.put_photo(first)
        self.cloud.down = True
        with self.assertLogs("apps.core.outbox", "WARNING"), self.captureOnCommitCallbacks(execute=True):
            client(self.sa).delete(reverse("admin-candidate-photo", kwargs={"pk": self.c.pk}))
        job = OutboxJob.objects.get(kind="media_destroy")
        self.assertEqual((job.status, job.attempts), ("pending", 1))
        self.cloud.down = False
        OutboxJob.objects.filter(pk=job.pk).update(next_attempt_at=timezone.now())
        outbox.process()
        self.assertEqual(self.cloud.destroyed, [first["public_id"]])

    def test_cloudinary_down_is_503_and_the_record_unchanged(self):
        sig = self.sign().json()
        asset = browser_upload(self.cloud, sig)
        self.cloud.down = True
        r = self.put_photo(asset)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (503, "media_unavailable"))
        self.c.refresh_from_db()
        self.assertEqual(self.c.photo_800, "")

    def test_moderation_removal_hides_and_restore_unhides(self):
        from apps.posts.models import CandidatePost
        from apps.posts.services import remove_post, restore_post

        u = mk("u_camp")
        grant_role(user=u, role=Role.CANDIDATE, scope_type=ScopeType.CANDIDATE, scope_id=self.c.pk)
        p = CandidatePost.objects.create(candidate=self.c, kind="news", title="Town hall", body="b", status="published", published_at=NOW,
                                         created_by=u)
        sig = client(u).post(reverse("media-upload-signature"), {"purpose": "post_image", "target_id": str(p.pk)}, format="json").json()
        self.assertEqual(sig["mode"], "direct")
        asset = browser_upload(self.cloud, sig, width=1600, height=900)
        r = client(u).put(reverse("campaign-post-image", kwargs={"candidate_id": self.c.pk, "post_id": p.pk}), {"asset": asset, "rights_confirmed": True},
                          format="json")
        self.assertEqual(r.status_code, 200, r.content)
        p.refresh_from_db()
        self.assertTrue(p.image.startswith("cld:") and p.image_asset_id)
        with self.captureOnCommitCallbacks(execute=True):
            remove_post(p, self.sa, "Spam", rule="R2")
        self.assertEqual(self.cloud.renamed, [(asset["public_id"], "authenticated")])
        self.assertEqual(MediaAsset.objects.get(public_id=asset["public_id"]).delivery, "hidden")
        p.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            restore_post(p, self.sa)
        self.assertEqual(self.cloud.renamed[-1], (asset["public_id"], "upload"))
        self.assertEqual(MediaAsset.objects.get(public_id=asset["public_id"]).delivery, "public")

    def test_media_audit_destroys_only_old_orphans(self):
        known = browser_upload(self.cloud, self.sign().json())
        self.put_photo(known)
        self.cloud.add("ndcconnect/test/post_image/orphan-old")
        self.cloud.add("ndcconnect/test/post_image/orphan-new", created_at=timezone.now().isoformat())
        out = io.StringIO()
        call_command("media_audit", stdout=out)
        self.assertEqual(list(OutboxJob.objects.filter(kind="media_destroy").values_list("payload__public_id", flat=True)),
                         ["ndcconnect/test/post_image/orphan-old"])

    def test_migration_command_is_idempotent(self):
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage

        a = default_storage.save("candidates/legacy_800.webp", ContentFile(b"x" * 10))
        b = default_storage.save("candidates/legacy_200.webp", ContentFile(b"y" * 10))
        Candidate.objects.filter(pk=self.c.pk).update(photo_800=a, photo_200=b)
        out = io.StringIO()
        call_command("migrate_media_to_cloudinary", "--dry-run", stdout=out)
        self.assertIn("would move 1", out.getvalue())
        self.assertEqual(self.cloud.uploaded, [])
        call_command("migrate_media_to_cloudinary", stdout=out)
        self.c.refresh_from_db()
        self.assertTrue(self.c.photo_800.startswith("cld:") and self.c.photo_asset.provider == "cloudinary")
        out = io.StringIO()
        call_command("migrate_media_to_cloudinary", stdout=out)
        self.assertIn("moved 0, already on Cloudinary 1", out.getvalue())
        self.assertEqual(len(self.cloud.uploaded), 1)

    def test_setup_preset_needs_config(self):
        from django.core.management.base import CommandError

        with override_settings(CLOUDINARY_API_SECRET=""), self.assertRaises(CommandError):
            call_command("cloudinary_setup", stdout=io.StringIO())


@override_settings(MEDIA_BACKEND="local", MEDIA_ROOT=TMP_MEDIA)
class LocalBackendTests(TestCase):
    def setUp(self):
        self.sa = mk("u_sa", Role.SYSTEM_ADMIN)
        self.c = Candidate.objects.create(slug="amina", full_name="Amina Bello", position="president", election_year=2027,
                                          status="published", verified_at=NOW, published_at=NOW)

    def test_server_mode_end_to_end(self):
        r = client(self.sa).post(reverse("media-upload-signature"), {"purpose": "candidate_photo", "target_id": str(self.c.pk)}, format="json")
        self.assertEqual(r.json(), {"mode": "server"})
        up = SimpleUploadedFile("p.jpg", jpeg(), content_type="image/jpeg")
        r = client(self.sa).put(reverse("admin-candidate-photo", kwargs={"pk": self.c.pk}), {"photo": up, "rights_confirmed": "true"},
                                format="multipart")
        self.assertEqual(r.status_code, 200)
        self.c.refresh_from_db()
        a = self.c.photo_asset
        self.assertEqual((a.provider, set(a.local_files)), ("local", {"800", "200"}))
        photo = r.json()["photo"]
        self.assertEqual(set(photo), {"url_800", "url_200", "srcset"})  # shape unchanged, srcset added
        self.assertIn(" 200w", photo["srcset"])
        with self.captureOnCommitCallbacks(execute=True):
            client(self.sa).delete(reverse("admin-candidate-photo", kwargs={"pk": self.c.pk}))
        a.refresh_from_db()
        self.assertEqual(a.delivery, "deleted")

    def test_asset_json_refused_in_local_mode(self):
        r = client(self.sa).put(reverse("admin-candidate-photo", kwargs={"pk": self.c.pk}),
                                {"asset": {"public_id": "x", "version": 1, "signature": "s"}}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_encoding(self):
        a = MediaAsset.objects.create(provider="cloudinary", public_id="ndcconnect/test/news_image/abc", version="17", purpose="news_image")
        name = M.encode(a, "thumb")
        self.assertEqual(name, "cld:ndcconnect/test/news_image/abc#17@thumb")
        self.assertEqual(M.decode(name), ("ndcconnect/test/news_image/abc", "17", "thumb"))
        self.assertIsNone(M.decode("news/abc_400.webp"))
