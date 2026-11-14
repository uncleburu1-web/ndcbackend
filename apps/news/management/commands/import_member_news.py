"""
python manage.py import_member_news --authors m1=a@x.com,m2=b@x.com,... [--dry-run] [--list]
python manage.py import_member_news --authors ... --pending      # submit only; reviewers approve in the app
python manage.py import_member_news --authors ... --review-as m7 --second-reviewer m6 --show-names

The file names its authors by handle (m1, m2, ...) so no email address lives in the repository; --authors maps them.

Posts community news on behalf of the members named in the file, through the same submit and approve steps as the
app. Nobody is notified. Safe to run again: an item already posted by that author (same title, any status) is left
alone. See apps/news/member_import.py.
"""
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.news.importing import ImportFileError
from apps.news.member_import import load, parse_authors, run

DEFAULT = settings.BASE_DIR / "data" / "news" / "2026-11-member-news.json"


class Command(BaseCommand):
    help = "Post researched community news as the members named in a JSON file, approved by a second person."

    def add_arguments(self, parser):
        parser.add_argument("--file", help="JSON file (default: data/news/2026-11-member-news.json).")
        parser.add_argument("--authors", default="", help="Map the file's handles to emails: m1=a@x.com,m2=b@x.com,...")
        parser.add_argument("--review-as", dest="reviewer", default="",
                            help="Handle or email of the news reviewer who approves (default: the file's).")
        parser.add_argument("--second-reviewer", dest="second", default="",
                            help="Handle or email of who approves the reviewer's own items (default: the file's).")
        parser.add_argument("--pending", action="store_true", help="Submit only; leave items waiting for review in the app.")
        parser.add_argument("--show-names", action="store_true", help="Show each author's name on their articles.")
        parser.add_argument("--dry-run", action="store_true", help="Check and report without saving anything.")
        parser.add_argument("--list", action="store_true", help="List every item by outcome.")

    def handle(self, *args, **opts):
        path = Path(opts["file"] or DEFAULT)
        try:
            data = load(path)
            res = run(data, authors=parse_authors(opts["authors"]), reviewer=opts["reviewer"], second=opts["second"],
                      pending=opts["pending"],
                      show_names=opts["show_names"], dry_run=opts["dry_run"])
        except ImportFileError as exc:
            raise CommandError("Import stopped, nothing saved:\n- " + "\n- ".join(exc.errors)) from exc
        if opts["list"]:
            verb = "Would post" if opts["dry_run"] else "Posted"
            for title, rows in ((verb, res.created), ("Already there", res.existing), ("Not submitted", res.not_submitted)):
                if rows:
                    self.stdout.write(f"{title}:")
                    for r in rows:
                        self.stdout.write(f"  - {r}")
        for email, count in sorted(res.per_author.items()):
            self.stdout.write(f"  {email}: {count}")
        self.stdout.write(res.summary(dry_run=opts["dry_run"], pending=opts["pending"]))
