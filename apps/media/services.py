"""
Media services (spec 15 §7). Two backends behind one interface:
- `local` (development, tests): today's Pillow pipeline writes files to MEDIA_ROOT; the signature endpoint answers
  {"mode": "server"} and the web app posts the file to Django.
- `cloudinary` (production): Django signs a direct browser upload, then verifies the uploaded asset before linking it.

Content models keep their existing name fields (photo_800/photo_200, image/image_thumb). For Cloudinary images those
hold an encoded reference `cld:<public_id>#<version>@<variant>`, so every existing "has an image?" check, URL helper and
deletion path keeps working; the `MediaAsset` row carries the asset ID, version, URL and metadata.
"""
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from apps.core import outbox
from apps.integrations.http import ProviderUnavailable

from . import cloudinary_api as cld
from .models import MediaAsset, Purpose

ALLOWED_FORMATS = ("jpg", "jpeg", "png", "webp")
PENDING_SECONDS = 2 * 3600
SQUARE = {"sq200": "c_fill,g_auto,ar_1:1,w_200", "sq400": "c_fill,g_auto,ar_1:1,w_400", "sq800": "c_fill,g_auto,ar_1:1,w_800"}
CONTENT = {"full": "c_limit,w_1200", "thumb": "c_fill,g_auto,ar_16:9,w_400", "w400": "c_limit,w_400", "w800": "c_limit,w_800",
           "w1200": "c_limit,w_1200"}
DELIVERY_FLAGS = "f_auto,q_auto"


class MediaError(Exception):
    """A user-facing refusal of an uploaded asset (maps to 400 invalid_photo)."""


class MediaUnavailable(Exception):
    """Cloudinary can't be reached (maps to 503 media_unavailable)."""


def backend() -> str:
    return getattr(settings, "MEDIA_BACKEND", "local")


def max_bytes() -> int:
    return settings.CANDIDATE_PHOTO_MAX_BYTES


def min_side() -> int:
    return settings.CANDIDATE_PHOTO_MIN_SIDE  # one rule for every purpose (03; 07's implementation choice)


# ------------------------------------------------------------------ references stored in the content models
def encode(asset: MediaAsset, variant: str) -> str:
    return f"cld:{asset.public_id}#{asset.version}@{variant}"


def decode(name: str):
    """'cld:<public_id>#<version>@<variant>' → (public_id, version, variant), or None for a local storage name."""
    if not name or not name.startswith("cld:"):
        return None
    rest = name[4:]
    rest, _, variant = rest.rpartition("@")
    public_id, _, version = rest.rpartition("#")
    return public_id, version, variant


def is_cloudinary(name: str) -> bool:
    return bool(decode(name))


def asset_for_name(name: str):
    d = decode(name)
    if d:
        return MediaAsset.objects.filter(provider=MediaAsset.Provider.CLOUDINARY, public_id=d[0]).first()
    return _local_by_value(name) if name else None


def _local_by_value(name):
    for a in MediaAsset.objects.filter(provider=MediaAsset.Provider.LOCAL, deleted_at__isnull=True, public_id=_stem(name)):
        if name in (a.local_files or {}).values():
            return a
    return None


def _stem(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    return base.split("_", 1)[0]


def delivery(name: str, variant: str | None = None) -> str | None:
    d = decode(name)
    if not d:
        return None
    public_id, version, stored_variant = d
    v = variant or stored_variant
    t = SQUARE.get(v) or CONTENT.get(v) or CONTENT["full"]
    return cld.delivery_url(public_id, version, f"{t},{DELIVERY_FLAGS}")


def srcset(name: str, kind: str, request=None, small: str | None = None, width: int | None = None) -> str | None:
    """Responsive candidates for <img srcset> (spec 15 §7.5). No queries: local files use the companion name."""
    if not name:
        return None
    if is_cloudinary(name):
        widths = (200, 400, 800) if kind == "square" else (400, 800, 1200)
        variants = ("sq200", "sq400", "sq800") if kind == "square" else ("w400", "w800", "w1200")
        return ", ".join(f"{delivery(name, v)} {w}w" for v, w in zip(variants, widths))
    from apps.candidates.photos import public_url

    big = 800 if kind == "square" else (width or 1600)
    pairs = [(small, 200 if kind == "square" else 400), (name, big)]
    out = [f"{public_url(request, n)} {w}w" for n, w in pairs if n]
    return ", ".join(out) or None


# ------------------------------------------------------------------ authorisation per purpose (§7.3)
def authorise(user, purpose: str, target_id):
    """The same rules as the image endpoints themselves. Returns the target, or raises a ServiceError."""
    from apps.access.services import is_system_admin
    from apps.core.exceptions import ServiceError

    def deny():
        return ServiceError("You can't upload an image for this.", code="permission_denied", status_code=403)

    if purpose not in Purpose.values:
        raise ServiceError("Choose what the image is for.", code="validation_error", status_code=400)
    try:
        if purpose == Purpose.CANDIDATE_PHOTO:
            from apps.candidates.models import Candidate

            if not is_system_admin(user):
                raise deny()
            return Candidate.objects.get(pk=target_id)
        if purpose == Purpose.ANNOUNCEMENT_IMAGE:
            from apps.news.models import Article

            if not is_system_admin(user):
                raise deny()
            return Article.objects.get(pk=target_id, source="official")
        if purpose == Purpose.EVENT_IMAGE:  # spec 18 §4.2: the event's organisers
            from apps.events.models import Event, Organiser
            from apps.events.services import can_organise

            e = Event.objects.select_related("candidate").get(pk=target_id)
            if not can_organise(user, None if e.organiser_kind == Organiser.PARTY else e.candidate):
                raise deny()
            return e
        if purpose == Purpose.NEWS_IMAGE:
            from apps.news.models import Article
            from apps.news.services import EDITABLE

            a = Article.objects.get(pk=target_id, source="community")
            if a.author_id != user.pk or a.status not in EDITABLE:
                raise deny()
            return a
        from apps.posts import services as posts
        from apps.posts.models import CandidatePost

        p = CandidatePost.objects.select_related("candidate").get(pk=target_id)
        if not posts.can_post(user, p.candidate):
            raise deny()
        return p
    except (ValueError, TypeError, Exception) as exc:  # DoesNotExist, bad UUID
        if isinstance(exc, ServiceError):
            raise
        raise ServiceError("Not found.", code="not_found", status_code=404) from None


def signature_for(user, purpose: str, target_id) -> dict:
    authorise(user, purpose, target_id)
    if backend() != "cloudinary":
        return {"mode": "server"}
    if not cld.configured():
        raise MediaUnavailable("Cloudinary isn't configured")
    c = cld.cfg()
    public_id = f"{c['folder']}/{purpose}/{secrets.token_urlsafe(16)[:22]}"
    params = {"timestamp": int(timezone.now().timestamp()), "public_id": public_id, "upload_preset": c["preset"],
              "allowed_formats": "jpg,png,webp"}
    signature = cld.sign(params)
    try:
        cache.set(f"media:pending:{public_id}", {"purpose": purpose, "target": str(target_id), "user": str(user.pk)}, PENDING_SECONDS)
    except Exception:
        raise MediaUnavailable("Upload authorisation can't be recorded right now") from None
    return {"mode": "direct", "upload_url": cld.upload_url(), "cloud_name": c["cloud"], "api_key": c["key"], **params,
            "signature": signature, "max_bytes": max_bytes()}


@dataclass
class Accepted:
    """A verified asset, ready to link to a record (passed to the owning app's service instead of an upload)."""

    asset: MediaAsset

    def as_photo(self):
        from apps.candidates.photos import StoredPhoto

        return StoredPhoto(name_800=encode(self.asset, "sq800"), name_200=encode(self.asset, "sq200"))

    def as_content(self) -> dict:
        w, h = self.asset.width or 0, self.asset.height or 0
        scale = min(1.0, 1200 / max(w, h)) if w and h else 1
        return {"image": encode(self.asset, "full"), "image_thumb": encode(self.asset, "thumb"),
                "image_width": round(w * scale) if w else None, "image_height": round(h * scale) if h else None}


def accept(user, purpose: str, target_id, data) -> Accepted:
    """Verify an asset the browser uploaded with our signature (spec 15 §7.3). Anything wrong → destroyed, MediaError."""
    authorise(user, purpose, target_id)
    if backend() != "cloudinary":
        raise MediaError("Upload the image file directly.")
    if not isinstance(data, dict):
        raise MediaError("The upload details are missing.")
    public_id, version, signature = str(data.get("public_id", "")), str(data.get("version", "")), str(data.get("signature", ""))
    try:
        pending = cache.get(f"media:pending:{public_id}")
    except Exception:
        pending = None
    if not pending or pending != {"purpose": purpose, "target": str(target_id), "user": str(user.pk)}:
        raise MediaError("That upload wasn't authorised for this item. Upload the image again.")
    if not cld.verify_response(public_id, version, signature):
        _destroy_quietly(public_id)
        raise MediaError("That upload couldn't be verified. Upload the image again.")
    try:
        info = cld.resource(public_id)
    except ProviderUnavailable:
        raise MediaUnavailable("Cloudinary is unavailable") from None
    if not info:
        raise MediaError("That upload wasn't found. Upload the image again.")
    problem = None
    fmt = str(info.get("format", "")).lower()
    if not public_id.startswith(f"{cld.cfg()['folder']}/{purpose}/"):
        problem = "That upload is in the wrong place."
    elif fmt not in ALLOWED_FORMATS:
        problem = "Use a JPEG, PNG or WebP image."
    elif int(info.get("bytes") or 0) > max_bytes():
        problem = "That image is larger than 5 MB. Choose a smaller file."
    elif min(int(info.get("width") or 0), int(info.get("height") or 0)) < min_side():
        problem = f"That image is too small. Use one at least {min_side()} × {min_side()} pixels."
    if problem:
        _destroy_quietly(public_id)
        raise MediaError(problem)
    asset, _ = MediaAsset.objects.update_or_create(
        provider=MediaAsset.Provider.CLOUDINARY, public_id=public_id,
        defaults={"version": str(info.get("version") or version), "secure_url": info.get("secure_url", "")[:500], "format": fmt,
                  "bytes": int(info.get("bytes") or 0), "width": info.get("width"), "height": info.get("height"), "purpose": purpose,
                  "uploaded_by": user, "delivery": MediaAsset.Delivery.PUBLIC, "deleted_at": None})
    try:
        cache.delete(f"media:pending:{public_id}")
    except Exception:
        pass
    return Accepted(asset)


def _destroy_quietly(public_id):
    with transaction.atomic():
        outbox.enqueue("media_destroy", {"public_id": public_id}, dedupe_key=f"media_destroy:{public_id}")


# ------------------------------------------------------------------ local files get a record too
def register_local(purpose, files: dict, *, width=None, height=None, user=None) -> MediaAsset:
    from django.core.files.storage import default_storage

    main = files.get("800") or files.get("image") or next(iter(files.values()))
    try:
        size = default_storage.size(main)
    except Exception:
        size = 0
    return MediaAsset.objects.create(provider=MediaAsset.Provider.LOCAL, public_id=_stem(main), purpose=purpose, local_files=files,
                                     secure_url=main, format="webp", bytes=size, width=width, height=height, uploaded_by=user)


# ------------------------------------------------------------------ after a record lets go of an image
def release(*names):
    """Called by the owning app after its commit: Cloudinary assets are destroyed by the outbox (retried), local files
    are deleted now. Names that are still used elsewhere are kept."""
    from django.core.files.storage import default_storage

    seen = set()
    for name in names:
        if not name:
            continue
        d = decode(name)
        if d:
            if d[0] in seen:
                continue
            seen.add(d[0])
            a = MediaAsset.objects.filter(provider=MediaAsset.Provider.CLOUDINARY, public_id=d[0]).first()
            with transaction.atomic():
                if a:
                    MediaAsset.objects.filter(pk=a.pk).update(deleted_at=timezone.now())
                outbox.enqueue("media_destroy", {"public_id": d[0], "type": "authenticated" if a and a.delivery == "hidden" else "upload"},
                               dedupe_key=f"media_destroy:{d[0]}")
            continue
        try:
            default_storage.delete(name)
        except Exception:  # a missing old file must not block the update
            pass
        a = _local_by_value(name)
        if a and a.deleted_at is None:
            MediaAsset.objects.filter(pk=a.pk).update(deleted_at=timezone.now(), delivery=MediaAsset.Delivery.DELETED)


def set_hidden(name: str, hidden: bool):
    """Spec 15 §7.7: a removed item's image stops loading at its old URL; restoring brings it back."""
    d = decode(name)
    if not d:
        return
    a = MediaAsset.objects.filter(provider=MediaAsset.Provider.CLOUDINARY, public_id=d[0]).first()
    if a is None:
        return
    target = MediaAsset.Delivery.HIDDEN if hidden else MediaAsset.Delivery.PUBLIC
    if a.delivery == target:
        return
    kind = "media_hide" if hidden else "media_show"
    outbox.enqueue(kind, {"asset": str(a.pk)}, dedupe_key=f"{kind}:{a.pk}")


def audit_orphans(now=None, dry_run=False) -> dict:
    """Weekly (spec 15 §7.6): Cloudinary assets under our folder with no MediaAsset, older than 24 h, are destroyed."""
    if backend() != "cloudinary" or not cld.configured():
        return {"checked": 0, "orphans": 0}
    from django.utils.dateparse import parse_datetime

    now = now or timezone.now()
    known = set(MediaAsset.objects.filter(provider=MediaAsset.Provider.CLOUDINARY, deleted_at__isnull=True).values_list("public_id", flat=True))
    cursor, checked, orphans = "", 0, 0
    while True:
        page = cld.list_resources(cld.cfg()["folder"] + "/", cursor)
        for r in page.get("resources", []):
            checked += 1
            created = parse_datetime(r.get("created_at", "") or "") or now
            if r["public_id"] not in known and now - created > timedelta(hours=24):
                orphans += 1
                if not dry_run:
                    outbox.enqueue("media_destroy", {"public_id": r["public_id"]}, dedupe_key=f"media_destroy:{r['public_id']}")
        cursor = page.get("next_cursor") or ""
        if not cursor:
            break
    return {"checked": checked, "orphans": orphans}
