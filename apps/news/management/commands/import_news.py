"""
python manage.py import_news --as admin@example.com [--file data/news/2026-11-election-news.json] [--dry-run] [--list]
python manage.py import_news --as admin@example.com --announcements [--file data/news/ndcconnect-announcements.json]

Publishes researched election news (or NDCConnect's own announcements, with images) as official articles without
notifying anyone. Safe to run again: anything already there — matched by source link, or by title for announcements —
is left exactly as it is. See apps/news/importing.py.
"""
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.news.importing import ImportFileError, load, run

DATA = settings.BASE_DIR / "data" / "news"
DEFAULTS = {"news": DATA / "2026-11-election-news.json", "announcements": DATA / "ndcconnect-announcements.json"}


class Command(BaseCommand):
    help = "Publish researched election news, or NDCConnect announcements, from a JSON file (no notifications)."

    def add_arguments(self, parser):
        parser.add_argument("--as", dest="admin", required=True, help="Email of the system admin to publish as.")
        parser.add_argument("--announcements", action="store_true", help="Import announcements (with images) instead of news.")
        parser.add_argument("--file", help="JSON file (default: the news or announcements file in data/news/).")
        parser.add_argument("--dry-run", action="store_true", help="Check and report without saving anything.")
        parser.add_argument("--list", action="store_true", help="List every item by outcome.")

    def handle(self, *args, **opts):
        kind = "announcements" if opts["announcements"] else "news"
        path = Path(opts["file"] or DEFAULTS[kind])
        admin = get_user_model().objects.filter(email__iexact=opts["admin"].strip()).first()
        if admin is None:
            raise CommandError(f"No account with the email {opts['admin']}.")
        try:
            data = load(path, kind=kind)
            res = run(data, admin=admin, kind=kind, base=path.parent, dry_run=opts["dry_run"])
        except ImportFileError as exc:
            raise CommandError("Import stopped, nothing saved:\n- " + "\n- ".join(exc.errors)) from exc
        if opts["list"]:
            for title, rows in (("Published" if not opts["dry_run"] else "Would publish", res.created),
                                ("Already there", res.existing)):
                if rows:
                    self.stdout.write(f"{title}:")
                    for r in rows:
                        self.stdout.write(f"  - {r}")
        self.stdout.write(res.summary(dry_run=opts["dry_run"]))
