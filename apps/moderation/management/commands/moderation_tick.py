from apps.core.jobs import TrackedCommand
from apps.moderation import services


class Command(TrackedCommand):
    job = "moderation_tick"
    help = "Moderation upkeep (spec 13 §5.4): release claims older than 4 hours, recompute burst flags, mark copyright removals restorable. Run every 15 minutes."

    def run(self, *args, **opts):
        r = services.tick()
        text = f"Released {r['claims_released']} claims, flagged {r['bursts']} bursts, {r['restorable']} restorable"
        self.stdout.write(text + ".")
        return text
