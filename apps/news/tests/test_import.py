"""import_news — researched election news and NDCConnect announcements (data/news/)."""
import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from PIL import Image

from apps.access.models import Role
from apps.audit.models import AuditLog
from apps.news.importing import ImportFileError, check, load
from apps.news.models import Article, Category
from apps.news.models import ArticleStatus as S
from apps.notifications.models import Notification

from .test_news import mk_role, mk_user

TMP_MEDIA = tempfile.mkdtemp(prefix="ndc-news-import-")
DATA = settings.BASE_DIR / "data" / "news"
NEWS = DATA / "2026-11-election-news.json"
ANN = DATA / "ndcconnect-announcements.json"


def item(**over):
    it = {"key": "inec-mock-vote", "category": "election_updates", "title": "INEC plans a mock vote",
          "body": "INEC says it will hold a nationwide mock accreditation exercise.", "state": "",
          "source_name": "Vanguard", "source_url": "https://example.com/a", "published_at": "2026-11-12"}
    return {**it, **over}


def run_cmd(*args):
    out = StringIO()
    call_command("import_news", *args, stdout=out)
    return out.getvalue()


class ShippedFiles(TestCase):
    def test_news_file_is_valid(self):
        data = load(NEWS, kind="news")
        self.assertGreaterEqual(len(data["items"]), 50)
        for it in data["items"]:
            self.assertTrue(it["source_url"].startswith("https://"))

    def test_announcements_file_is_valid_with_images(self):
        data = load(ANN, kind="announcements")
        self.assertEqual(len(data["items"]), 3)
        for it in data["items"]:
            with Image.open(DATA / it["image"]) as im:
                self.assertEqual(im.size, (1600, 900))


class FileChecks(TestCase):
    def errs(self, items, kind="news"):
        with self.assertRaises(ImportFileError) as ctx:
            check({"items": items}, kind=kind, base=DATA)
        return " ".join(ctx.exception.errors)

    def test_rules(self):
        self.assertIn("source_url", self.errs([item(source_url="http://x.com")]))
        self.assertIn("duplicate source_url", self.errs([item(), item(key="other", title="Other")]))
        self.assertIn("unknown state", self.errs([item(state="lagos-state")]))
        self.assertIn("category", self.errs([item(category="party")]))
        self.assertIn("future", self.errs([item(published_at="2999-01-01")]))
        self.assertIn("source_name", self.errs([item(source_name="")]))
        self.assertIn("not found", self.errs([{**item(category="party"), "image": "../../manage.py"}], kind="announcements"))
        self.assertIn("non-empty", self.errs([]))


class ImportNews(TestCase):
    def setUp(self):
        self.admin = mk_role("user_sysadmin", Role.SYSTEM_ADMIN, full_name="Sam Admin")
        self.tmp = Path(tempfile.mkdtemp())
        self.file = self.tmp / "news.json"
        self.file.write_text(json.dumps({"items": [item(), item(key="second", title="Second", category="election_news",
                                                                 source_url="https://example.com/b", state="lagos",
                                                                 published_at="2026-10-01")]}))

    def test_publishes_without_notifying_and_keeps_source_dates(self):
        out = run_cmd("--as", self.admin.email, "--file", str(self.file))
        self.assertIn("2 published", out)
        a = Article.objects.get(source_url="https://example.com/b")
        self.assertEqual((a.source, a.status, a.category, a.state), ("official", S.SUCCESSFUL, Category.ELECTION_NEWS, "lagos"))
        self.assertEqual(a.published_at.date().isoformat(), "2026-10-01")
        self.assertFalse(a.important)
        self.assertEqual(Notification.objects.count(), 0)
        self.assertEqual(AuditLog.objects.filter(action=AuditLog.Action.ANNOUNCEMENT_PUBLISHED).count(), 2)

    def test_idempotent_and_never_undoes_admin_changes(self):
        run_cmd("--as", self.admin.email, "--file", str(self.file))
        Article.objects.filter(source_url="https://example.com/a").update(title="Edited by an admin", status=S.REMOVED,
                                                                          removed_at="2026-11-12T10:00:00Z")
        out = run_cmd("--as", self.admin.email, "--file", str(self.file), "--list")
        self.assertIn("0 published, 2 already there", out)
        self.assertEqual(Article.objects.count(), 2)
        self.assertEqual(Article.objects.get(source_url="https://example.com/a").title, "Edited by an admin")

    def test_dry_run_saves_nothing(self):
        out = run_cmd("--as", self.admin.email, "--file", str(self.file), "--dry-run")
        self.assertIn("Dry run", out)
        self.assertEqual(Article.objects.count(), 0)

    def test_needs_a_system_admin(self):
        member = mk_user("user_member")
        with self.assertRaisesMessage(CommandError, "not an active system admin"):
            run_cmd("--as", member.email, "--file", str(self.file))
        with self.assertRaisesMessage(CommandError, "No account"):
            run_cmd("--as", "nobody@example.com", "--file", str(self.file))
        self.assertEqual(Article.objects.count(), 0)

    def test_bad_file_saves_nothing(self):
        self.file.write_text(json.dumps({"items": [item(), item(key="bad", title="Bad", state="nowhere")]}))
        with self.assertRaisesMessage(CommandError, "nothing saved"):
            run_cmd("--as", self.admin.email, "--file", str(self.file))
        self.assertEqual(Article.objects.count(), 0)


class ImportAnnouncements(TestCase):
    def setUp(self):
        self.admin = mk_role("user_sysadmin", Role.SYSTEM_ADMIN, full_name="Sam Admin")

    @override_settings(MEDIA_ROOT=TMP_MEDIA, MEDIA_BACKEND="local")
    def test_local_images_and_order(self):
        out = run_cmd("--as", self.admin.email, "--announcements")
        self.assertIn("3 published, 3 with an image", out)
        feed = list(Article.objects.order_by("-published_at").values_list("title", flat=True))
        self.assertEqual(feed[0], "Welcome to NDCConnect")  # first in the file is newest in the feed
        for a in Article.objects.all():
            self.assertTrue(a.image.endswith(".webp") and a.image_thumb)
            self.assertIsNotNone(a.image_asset_id)
            self.assertIsNone(a.edited_at)
            self.assertEqual(a.category, Category.PARTY)
        self.assertIn("0 published, 3 already there", run_cmd("--as", self.admin.email, "--announcements"))

    @override_settings(MEDIA_ROOT=TMP_MEDIA)
    def test_cloudinary_server_upload(self):
        def fake_upload(public_id, content, filename, ctype):
            return {"public_id": public_id, "version": 7, "secure_url": f"https://res.cloudinary.com/{public_id}.webp",
                    "format": "webp", "bytes": len(content), "width": 1600, "height": 900}
        with mock.patch("apps.media.services.backend", return_value="cloudinary"), \
             mock.patch("apps.media.cloudinary_api.cfg", return_value={"folder": "ndc", "preset": "p"}), \
             mock.patch("apps.media.cloudinary_api.server_upload", side_effect=fake_upload) as up:
            run_cmd("--as", self.admin.email, "--announcements")
        self.assertEqual(up.call_count, 3)
        self.assertTrue(up.call_args.args[0].startswith("ndc/announcement_image/"))
        a = Article.objects.get(title="Welcome to NDCConnect")
        self.assertEqual(a.image_asset.provider, "cloudinary")
        self.assertEqual(a.image_asset.purpose, "announcement_image")
        self.assertTrue(a.image)
