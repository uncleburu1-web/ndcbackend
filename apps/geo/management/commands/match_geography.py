"""Spec 16 §8.2 and 19 §12.2: link free-text LGA/ward/polling unit to the reference tables where the text matches exactly
(after folding). Never changes what a person typed, and never touches links someone chose, an officer confirmed, or a
record marked "not listed". New links are `geo_source = matched`. Counts only in the output."""
from apps.core.jobs import TrackedCommand
from apps.geo.matching import match_all


class Command(TrackedCommand):
    help = "Match free-text LGA, ward and polling unit on memberships, PVC records and candidates to INEC's list (nightly)."
    job = "match_geography"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report counts and write nothing.")

    def run(self, *args, dry_run=False, **opts):
        return "; ".join(f"{name}: {r['checked']} checked, {r['lga']} LGA, {r['ward']} ward, {r['polling_unit']} polling unit "
                         f"matches{' (dry run)' if dry_run else ' linked'}" for name, r in match_all(dry_run=dry_run).items())
