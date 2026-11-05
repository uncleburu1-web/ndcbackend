from apps.core.jobs import TrackedCommand
from apps.media.services import audit_orphans


class Command(TrackedCommand):
    help = "Destroy Cloudinary assets in NDCConnect's folder with no MediaAsset, older than 24 h (spec 15 §7.6). Weekly."
    job = "media_audit"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def run(self, *args, dry_run=False, **opts):
        r = audit_orphans(dry_run=dry_run)
        return f"{r['checked']} checked, {r['orphans']} orphans" + (" (dry run)" if dry_run else " queued for deletion")
