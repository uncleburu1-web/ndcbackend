"""Spec 15 §7.8: move existing local images to Cloudinary. Idempotent, resumable, counts only."""
import secrets

from django.core.files.storage import default_storage
from django.core.management.base import CommandError
from django.db import transaction

from apps.core.jobs import TrackedCommand
from apps.media import cloudinary_api as cld
from apps.media.models import MediaAsset
from apps.media.services import Accepted, asset_for_name, is_cloudinary

TARGETS = {
    "candidate_photo": ("candidates", "Candidate", "photo_800", ("photo_800", "photo_200"), "square"),
    "post_image": ("posts", "CandidatePost", "image", ("image", "image_thumb"), "content"),
    "news_image": ("news", "Article", "image", ("image", "image_thumb"), "content"),
}


class Command(TrackedCommand):
    help = "Upload local candidate photos and article/post images to Cloudinary and relink them."
    job = "migrate_media_to_cloudinary"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--purpose", choices=list(TARGETS), action="append")
        parser.add_argument("--delete-local", action="store_true", help="Delete local files after a verified upload.")

    def run(self, *args, dry_run=False, purpose=None, delete_local=False, **opts):
        from django.apps import apps

        if not cld.configured():
            raise CommandError("Cloudinary isn't configured.")
        moved = skipped = failed = 0
        for key in purpose or list(TARGETS):
            app, model, main, fields, kind = TARGETS[key]
            M = apps.get_model(app, model)
            for obj in M.objects.exclude(**{main: ""}).iterator():
                name = getattr(obj, main)
                if is_cloudinary(name):
                    skipped += 1
                    continue
                p = key
                if model == "Article" and obj.source == "official":
                    p = "announcement_image"
                if dry_run:
                    moved += 1
                    continue
                try:
                    with default_storage.open(name, "rb") as fh:
                        content = fh.read()
                    public_id = f"{cld.cfg()['folder']}/{p}/{secrets.token_urlsafe(16)[:22]}"
                    info = cld.server_upload(public_id, content, name.rsplit("/", 1)[-1], "image/webp")
                    if not info.get("width") or not info.get("public_id"):
                        raise RuntimeError("upload not verified")
                    asset = MediaAsset.objects.create(provider="cloudinary", public_id=info["public_id"], version=str(info.get("version", "")),
                                                      secure_url=info.get("secure_url", "")[:500], format=str(info.get("format", "")),
                                                      bytes=int(info.get("bytes") or 0), width=info.get("width"), height=info.get("height"),
                                                      purpose=p)
                    acc = Accepted(asset)
                    old_names = [getattr(obj, f) for f in fields]
                    with transaction.atomic():
                        if kind == "square":
                            st = acc.as_photo()
                            M.objects.filter(pk=obj.pk).update(photo_800=st.name_800, photo_200=st.name_200, photo_asset=asset)
                        else:
                            c = acc.as_content()
                            M.objects.filter(pk=obj.pk).update(image=c["image"], image_thumb=c["image_thumb"], image_asset=asset)
                        local = asset_for_name(old_names[0])
                        if local:
                            MediaAsset.objects.filter(pk=local.pk).update(delivery=MediaAsset.Delivery.DELETED)
                    if delete_local:
                        for n in old_names:
                            try:
                                default_storage.delete(n)
                            except Exception:
                                pass
                    moved += 1
                except Exception:
                    self.stderr.write(f"Could not move one {key} (see logs)")
                    failed += 1
        verb = "would move" if dry_run else "moved"
        return f"{verb} {moved}, already on Cloudinary {skipped}, failed {failed}"
