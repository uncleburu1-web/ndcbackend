from django.core.management.base import BaseCommand, CommandError

from apps.media import cloudinary_api as cld


class Command(BaseCommand):
    help = "Create or update the signed Cloudinary upload preset (spec 15 §7.2). Prints names only."

    def handle(self, *args, **opts):
        if not cld.configured():
            raise CommandError("Set CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET first.")
        name = cld.cfg()["preset"]
        cld.setup_preset(name)
        self.stdout.write(f"Upload preset {name}: formats jpg/png/webp, originals capped at 2000 px, metadata stripped on upload.")
