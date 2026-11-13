"""
Bulk import of researched election news and NDCConnect's own announcements (data/news/*.json).

Each item is published as an official article through `announcements.publish(..., notify=False)`, so the same
validation, audit entry and source rule (Official Election Updates need a source) apply as when an admin types it in.
No one is notified. News items keep the date their source published them, so the feed stays in real order.

Idempotent:
- a news item is matched by its `source_url` against every official article (any status);
- an announcement is matched by its exact title against every official article.
A match is left exactly as it is, so a later edit or removal by an admin is never undone.

Images (announcements only) are PNG/JPEG files next to the JSON file. With the local media backend they go through
`services.set_image`; with Cloudinary they are uploaded server-side, as `migrate_media_to_cloudinary` does.
"""
import io
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.utils import timezone
from PIL import Image

from apps.access.services import is_system_admin
from apps.candidates.reference import State

from . import announcements
from .models import Article, Category, Source

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,80}$")
NEWS_CATEGORIES = {Category.ELECTION_UPDATES, Category.ELECTION_NEWS}
LAGOS = ZoneInfo("Africa/Lagos")
MAX_TITLE = Article._meta.get_field("title").max_length
MAX_SOURCE_NAME = Article._meta.get_field("source_name").max_length
MAX_URL = Article._meta.get_field("source_url").max_length


class ImportFileError(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class Result:
    created: list = field(default_factory=list)
    existing: list = field(default_factory=list)
    images: int = 0

    def summary(self, *, dry_run=False) -> str:
        head = "Dry run — nothing saved. " if dry_run else ""
        img = f", {self.images} with an image" if self.images else ""
        verb = "would be published" if dry_run else "published"
        return f"{head}{len(self.created)} {verb}{img}, {len(self.existing)} already there (left alone)."


def load(path, *, kind: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ImportFileError([f"Can't read {path}: {exc}"]) from exc
    check(data, kind=kind, base=Path(path).parent)
    return data


def check(data: dict, *, kind: str, base: Path):
    """Validate the whole file before anything is saved."""
    errors = []
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise ImportFileError(["The file needs a non-empty \"items\" list."])
    seen_keys, seen_urls, seen_titles = set(), set(), set()
    for n, it in enumerate(items, 1):
        where = f"item {n} ({it.get('key', '?') if isinstance(it, dict) else '?'})"
        if not isinstance(it, dict):
            errors.append(f"{where}: not an object")
            continue
        key, title, body = it.get("key", ""), it.get("title", ""), it.get("body", "")
        if not KEY_RE.fullmatch(str(key)):
            errors.append(f"{where}: key must be lower-case letters, digits and dashes")
        elif key in seen_keys:
            errors.append(f"{where}: duplicate key")
        seen_keys.add(key)
        if not str(title).strip() or len(title) > MAX_TITLE:
            errors.append(f"{where}: title must be 1–{MAX_TITLE} characters")
        elif title.strip() in seen_titles:
            errors.append(f"{where}: duplicate title")
        seen_titles.add(str(title).strip())
        if not str(body).strip() or len(body) > settings.NEWS_OFFICIAL_MAX_BODY:
            errors.append(f"{where}: body must be 1–{settings.NEWS_OFFICIAL_MAX_BODY:,} characters")
        if it.get("state", "") and it["state"] not in State.values:
            errors.append(f"{where}: unknown state {it['state']!r}")
        if kind == "news":
            if it.get("category") not in NEWS_CATEGORIES:
                errors.append(f"{where}: category must be election_updates or election_news")
            url, name = it.get("source_url", ""), it.get("source_name", "")
            if not str(url).startswith("https://") or len(url) > MAX_URL:
                errors.append(f"{where}: source_url must be an https link")
            elif url in seen_urls:
                errors.append(f"{where}: duplicate source_url")
            seen_urls.add(url)
            if not str(name).strip() or len(name) > MAX_SOURCE_NAME:
                errors.append(f"{where}: source_name is required")
            try:
                d = date.fromisoformat(str(it.get("published_at", "")))
                if d > timezone.localdate():
                    errors.append(f"{where}: published_at is in the future")
            except ValueError:
                errors.append(f"{where}: published_at must be YYYY-MM-DD")
            if it.get("image"):
                errors.append(f"{where}: news items don't carry images")
        else:
            if it.get("category") not in Category.values or it.get("category") == Category.COMMUNITY:
                errors.append(f"{where}: category must be an official category other than community")
            if it.get("category") == Category.ELECTION_UPDATES and not (it.get("source_name") and it.get("source_url")):
                errors.append(f"{where}: Official Election Updates need source_name and source_url")
            img = it.get("image")
            if img:
                p = (base / img).resolve()
                if base.resolve() not in p.parents or not p.is_file():
                    errors.append(f"{where}: image {img!r} not found next to the file")
    if errors:
        raise ImportFileError(errors)


def _existing(it: dict, kind: str):
    qs = Article.objects.filter(source=Source.OFFICIAL)
    if kind == "news":
        return qs.filter(source_url=it["source_url"]).first()
    return qs.filter(title=it["title"].strip()).first()


def _image_upload(path: Path) -> tuple[bytes, SimpleUploadedFile]:
    raw = path.read_bytes()
    with Image.open(io.BytesIO(raw)) as im:
        fmt = (im.format or "PNG").lower()
    ctype = "image/jpeg" if fmt in ("jpeg", "jpg") else f"image/{fmt}"
    return raw, SimpleUploadedFile(path.name, raw, content_type=ctype)


def _attach_cloudinary(article, path: Path):
    """Server-side upload with the signed preset, then link the asset (same steps as migrate_media_to_cloudinary)."""
    from apps.media import cloudinary_api as cld
    from apps.media.models import MediaAsset
    from apps.media.services import Accepted

    raw = path.read_bytes()
    with Image.open(io.BytesIO(raw)) as im:
        rgb = im.convert("RGB")
        out = io.BytesIO()
        rgb.save(out, format="WEBP", quality=88, method=6)
    public_id = f"{cld.cfg()['folder']}/announcement_image/{secrets.token_urlsafe(16)[:22]}"
    info = cld.server_upload(public_id, out.getvalue(), path.stem + ".webp", "image/webp")
    if not info.get("width") or not info.get("public_id"):
        raise RuntimeError(f"Cloudinary didn't confirm the upload of {path.name}")
    asset = MediaAsset.objects.create(
        provider="cloudinary", public_id=info["public_id"], version=str(info.get("version", "")),
        secure_url=info.get("secure_url", "")[:500], format=str(info.get("format", "")), bytes=int(info.get("bytes") or 0),
        width=info.get("width"), height=info.get("height"), purpose="announcement_image")
    c = Accepted(asset).as_content()
    Article.objects.filter(pk=article.pk).update(image_asset=asset, **c)


def _attach(article, admin, path: Path):
    from apps.media.services import backend

    from .services import set_image

    if backend() == "cloudinary":
        _attach_cloudinary(article, path)
    else:
        _, upload = _image_upload(path)
        set_image(article, admin, upload, rights_confirmed=True)
    # set_image marks a published article as edited; a fresh import isn't an edit
    Article.objects.filter(pk=article.pk).update(edited_at=None)


def _published_at(day: str):
    return datetime.combine(date.fromisoformat(day), time(8, 0), tzinfo=LAGOS)


def run(data: dict, *, admin, kind: str, base: Path, dry_run=False) -> Result:
    if not is_system_admin(admin):
        raise ImportFileError([f"{admin} is not an active system admin."])
    res = Result()
    # announcements are listed top-first; publishing the last one first leaves the first one newest in the feed
    items = data["items"] if kind == "news" else list(reversed(data["items"]))
    for it in items:
        label = f"{it['key']}: {it['title']}"
        if _existing(it, kind):
            res.existing.append(label)
            continue
        res.created.append(label)
        if it.get("image"):
            res.images += 1
        if dry_run:
            continue
        payload = {k: it.get(k, "") for k in ("category", "title", "body", "state", "source_name", "source_url")}
        with transaction.atomic():
            a = announcements.publish(admin, payload, notify=False)
            if kind == "news":
                Article.objects.filter(pk=a.pk).update(published_at=_published_at(it["published_at"]))
        if it.get("image"):
            _attach(a, admin, (base / it["image"]).resolve())
    return res
