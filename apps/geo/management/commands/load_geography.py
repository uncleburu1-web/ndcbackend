from datetime import date

from django.core.management.base import BaseCommand, CommandError

from apps.geo.loading import COLUMNS, GeographyFileError, load


class Command(BaseCommand):
    help = ("Load INEC's polling-unit list (spec 16 §8.1). CSV columns: " + ", ".join(COLUMNS) +
            ". Idempotent; units missing from a newer file become inactive.")

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--source", default="", help="Where the file came from (URL or description).")
        parser.add_argument("--published-on", type=date.fromisoformat, default=None, help="YYYY-MM-DD")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, path, source="", published_on=None, dry_run=False, **opts):
        try:
            with open(path, "rb") as f:
                content = f.read()
            r = load(content, source=source or path, published_on=published_on, dry_run=dry_run)
        except (OSError, GeographyFileError) as e:
            raise CommandError(str(e)) from None
        c = r.created
        self.stdout.write(f"{r.rows} rows: created {c['states']} states, {c['lgas']} LGAs, {c['wards']} wards, "
                          f"{c['polling_units']} polling units; updated {r.updated}; deactivated {r.deactivated}"
                          + (" (dry run, nothing saved)" if dry_run else ""))
