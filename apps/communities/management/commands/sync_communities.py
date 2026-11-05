from apps.core.jobs import TrackedCommand
from apps.communities import sync


class Command(TrackedCommand):
    help = ("Create national, state, LGA, ward and polling-unit communities from geography (spec 17 §12.2). Idempotent; run after "
            "load_geography and nightly. --placements re-runs verified placement for consented voters.")
    job = "sync_communities"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--placements", action="store_true")

    def run(self, *args, dry_run=False, placements=False, **opts):
        r = sync.sync(dry_run=dry_run)
        c = r.created
        text = (f"created {c['national']} national, {c['state']} state, {c['lga']} LGA, {c['ward']} ward and {c['polling_unit']} "
                f"polling-unit communities; updated {r.updated}; deactivated {r.deactivated}")
        if placements and not dry_run:
            text += f"; placement checked for {sync.run_placements()} people"
        return text + (" (dry run)" if dry_run else "")
