"""import_member_news — community news posted as named members, approved by someone else (data/news/)."""
import json
import tempfile
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import CommandError, call_command
from django.test import TestCase

from apps.access.models import Role
from apps.audit.models import AuditLog
from apps.news.importing import ImportFileError
from apps.news.member_import import check, load
from apps.news.models import Article, Source
from apps.news.models import ArticleStatus as S
from apps.notifications.models import Notification

from .test_news import mk_role, mk_user

SHIPPED = settings.BASE_DIR / "data" / "news" / "2026-11-member-news.json"
BODY = "A community member's summary of a news story, written in their own words with the source linked below. " * 2


def item(n=1, author="ada@example.com", **over):
    return {"key": f"story-{n}", "author": author, "title": f"Story {n}", "body": BODY, "state": "lagos",
            "source_url": f"https://example.com/{n}", "published_at": "2026-11-10", "language": "en", **over}


class ShippedFile(TestCase):
    def test_file_is_valid(self):
        data = load(SHIPPED)
        items = data["items"]
        self.assertEqual(len(items), 108)
        counts = {}
        for it in items:
            counts[it["author"]] = counts.get(it["author"], 0) + 1
        self.assertEqual(len(counts), 7)
        self.assertTrue(all(10 <= c <= 20 for c in counts.values()), counts)
        self.assertIn(data["reviewer"], counts)
        self.assertNotEqual(data["reviewer"], data["second_reviewer"])
        self.assertNotIn("@", SHIPPED.read_text(encoding="utf-8"))  # handles only: no member email in the repository

    def test_no_overlap_with_official_news_links(self):
        official = {it["source_url"] for it in json.loads((SHIPPED.parent / "2026-11-election-news.json").read_text(encoding="utf-8"))["items"]}
        self.assertFalse(official & {it["source_url"] for it in load(SHIPPED)["items"]})


class FileChecks(TestCase):
    def errs(self, items):
        with self.assertRaises(ImportFileError) as ctx:
            check({"items": items})
        return " ".join(ctx.exception.errors)

    def test_rules(self):
        self.assertIn("characters", self.errs([item(body="too short")]))
        self.assertIn("https", self.errs([item(source_url="http://x.com")]))
        self.assertIn("duplicate title", self.errs([item(1), item(2, title="Story 1")]))
        self.assertIn("unknown state", self.errs([item(state="lagos-state")]))
        self.assertIn("author", self.errs([item(author="Ada Obi")]))
        self.assertIn("future", self.errs([item(published_at="2999-01-01")]))
        self.assertIn("non-empty", self.errs([]))


class ImportMemberNews(TestCase):
    def setUp(self):
        self.rev = mk_role("rev", Role.NEWS_REVIEWER)
        self.second = mk_role("sec", Role.SYSTEM_ADMIN)
        self.ada = mk_user("ada")
        self.tmp = Path(tempfile.mkdtemp())
        self.file = self.tmp / "member.json"
        self.write([item(1), item(2), item(3, author="rev@example.com", published_at="2026-11-01"),
                    item(4), item(5)])

    def write(self, items, **top):
        self.file.write_text(json.dumps({"reviewer": "rev@example.com", "second_reviewer": "sec@example.com",
                                         "items": items, **top}))

    def cmd(self, *args):
        out = StringIO()
        call_command("import_member_news", "--file", str(self.file), *args, stdout=out)
        return out.getvalue()

    def test_dry_run_saves_nothing(self):
        out = self.cmd("--dry-run")
        self.assertIn("5 would be published", out)
        self.assertFalse(Article.objects.exists())

    def test_publishes_as_author_approved_by_someone_else_without_notifying(self):
        out = self.cmd()
        self.assertIn("5 published", out)
        arts = Article.objects.filter(source=Source.COMMUNITY)
        self.assertEqual(arts.count(), 5)
        self.assertTrue(all(a.status == S.SUCCESSFUL for a in arts))
        self.assertEqual(set(arts.filter(author=self.ada).values_list("decided_by", flat=True)), {self.rev.pk})
        own = arts.get(author=self.rev)
        self.assertEqual(own.decided_by, self.second)  # no self-review
        self.assertEqual(own.published_at.date().isoformat(), "2026-11-01")
        self.assertEqual(own.language, "en")
        self.assertFalse(own.show_author_name)
        self.assertTrue(own.declarations["accurate"])
        self.assertFalse(Notification.objects.exists())
        self.assertEqual(AuditLog.objects.filter(action=AuditLog.Action.NEWS_APPROVED).count(), 5)

    def test_rerun_leaves_everything_alone(self):
        self.cmd()
        a = Article.objects.get(title="Story 1")
        a.status = S.WITHDRAWN
        a.save()
        out = self.cmd()
        self.assertIn("0 published, 5 already there", out)
        a.refresh_from_db()
        self.assertEqual(a.status, S.WITHDRAWN)

    def test_pending_respects_the_three_waiting_limit(self):
        out = self.cmd("--pending", "--list")
        self.assertIn("4 submitted for review", out)  # ada's 4th waits; rev's 1 is fine
        self.assertIn("1 not submitted", out)
        self.assertEqual(Article.objects.filter(status=S.PENDING).count(), 4)

    def test_handles_are_mapped_with_authors(self):
        self.write([item(1, author="m1"), item(2, author="m2")], reviewer="m2", second_reviewer="sec@example.com")
        with self.assertRaises(CommandError) as ctx:
            self.cmd()
        self.assertIn("No email given for m1, m2", str(ctx.exception))
        out = self.cmd("--authors", "m1=ada@example.com, m2=rev@example.com")
        self.assertIn("2 published", out)
        self.assertEqual(Article.objects.get(title="Story 1").decided_by, self.rev)
        self.assertEqual(Article.objects.get(title="Story 2").decided_by, self.second)
        with self.assertRaises(CommandError):
            self.cmd("--authors", "m1")

    def test_show_names(self):
        self.cmd("--show-names")
        self.assertTrue(all(Article.objects.values_list("show_author_name", flat=True)))

    def test_accounts_checked_before_anything_is_saved(self):
        mk_user("half", complete=False)
        self.write([item(1), item(2, author="half@example.com"), item(3, author="ghost@example.com")])
        with self.assertRaises(CommandError) as ctx:
            self.cmd()
        msg = str(ctx.exception)
        self.assertIn("ghost@example.com has no account", msg)
        self.assertIn("half@example.com hasn't finished", msg)
        self.assertFalse(Article.objects.exists())

    def test_reviewer_must_be_a_reviewer_and_different(self):
        mk_user("plain")
        self.write([item(1)], reviewer="plain@example.com")
        with self.assertRaises(CommandError) as ctx:
            self.cmd()
        self.assertIn("not an active news reviewer", str(ctx.exception))
        with self.assertRaises(CommandError) as ctx:
            self.cmd("--review-as", "rev@example.com", "--second-reviewer", "rev@example.com")
        self.assertIn("must be different", str(ctx.exception))
