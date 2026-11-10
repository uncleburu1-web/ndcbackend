"""Spec 16 §8.1, 19 §4.3 and §12.2: preview a geography file (the default), or apply it with `--apply`."""
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from apps.core.exceptions import ServiceError
from apps.geo import loading
from apps.geo.models import Dataset


class Command(BaseCommand):
    help = ("Preview (default) or apply (--apply) an INEC geography file. Polling units: " + ", ".join(loading.COLUMNS) +
            ". Constituencies (--kind senatorial_districts|federal_constituencies|state_constituencies): " +
            ", ".join(loading.CONSTITUENCY_COLUMNS) + " (codes separated by ';', wards as LGA-ward). Places are never deleted: "
            "missing ones become inactive. A polling-unit file with fewer than 95% of the active units needs --confirm-by.")

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--kind", default=Dataset.Kind.POLLING_UNITS, choices=Dataset.Kind.values)
        parser.add_argument("--apply", action="store_true", help="Apply the file (the command line counts as one admin).")
        parser.add_argument("--confirm-by", default="", help="Email of the second system admin (the 95%% rule).")
        parser.add_argument("--source", default="", help="Where the file came from (URL or description).")
        parser.add_argument("--published-on", type=date.fromisoformat, default=None, help="INEC's publication date, YYYY-MM-DD")
        parser.add_argument("--notes", default="", help="Who checked the file, and how (spec 19 §4.1).")
        parser.add_argument("--dry-run", action="store_true", help="Same as the default: preview only.")

    def handle(self, path, kind=Dataset.Kind.POLLING_UNITS, apply=False, confirm_by="", source="", published_on=None, notes="",
               dry_run=False, **opts):
        second = None
        if confirm_by:
            from apps.access.services import is_system_admin
            from apps.accounts.models import User

            second = User.objects.filter(email__iexact=confirm_by.strip()).first()
            if second is None or not is_system_admin(second):
                raise CommandError(f"{confirm_by} isn't a system admin.")
        try:
            with open(path, "rb") as f:
                content = f.read()
            ds = loading.preview(content, kind=kind, source=source or path, published_on=published_on, notes=notes)
        except OSError as e:
            raise CommandError(str(e)) from None
        except loading.GeographyFileError as e:
            raise CommandError("; ".join(e.errors[:10])) from None
        rep = ds.preview
        for line in rep["errors"][:20]:
            self.stderr.write(line)
        self.stdout.write(self._summary(rep))
        if not apply or dry_run:
            self.stdout.write(f"Previewed as dataset {ds.pk} (nothing applied). Run again with --apply to apply it.")
            return
        if rep["error_count"]:
            raise CommandError(f"{rep['error_count']} problems found; the file can't be applied.")
        try:
            ds = loading.apply(ds, None, second_admin=second)
        except ServiceError as e:
            raise CommandError(str(e.detail if hasattr(e, "detail") else e)) from None
        self.stdout.write(f"Applied dataset {ds.pk}: {ds.states} states, {ds.lgas} LGAs, {ds.wards} wards, {ds.polling_units} polling "
                          f"units, {ds.constituencies} constituencies active; {ds.deactivated} retired or moved.")

    @staticmethod
    def _summary(rep):
        c = rep["counts"]
        fmt = lambda d: ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in d.items()) or "none"  # noqa: E731
        return (f"{rep['rows']} rows. Added: {fmt(c['added'])}. Renamed: {fmt(c['renamed'])}. Back in the list: {fmt(c['restored'])}. "
                f"Retired: {fmt(c['retired'])}. Moved: {c['moved']}. Records pointing at retired or moved units: "
                f"{rep['affected'].get('memberships', 0)} memberships, {rep['affected'].get('pvc_records', 0)} PVC checks.")
