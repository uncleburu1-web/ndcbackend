from apps.core.jobs import TrackedCommand
from apps.moderation import services


class Command(TrackedCommand):
    job = "prune_moderation"
    help = ("Retention (spec 13 §10): delete closed cases with their reports, events and appeals 1 year after closing, except "
            "legal holds. Freezes the yearly compliance figures first. Run daily.")

    def run(self, *args, **opts):
        r = services.prune()
        text = f"Deleted {r['cases']} cases and {r['reports']} reports; froze {r['years_frozen']} years"
        self.stdout.write(text + ".")
        return text
