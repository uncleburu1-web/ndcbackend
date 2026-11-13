"""Article images — spec 07 §5.1: 03 rules (type check, size limit, metadata dropped), aspect ratio kept, not cropped."""
import io
import uuid

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from PIL import Image

from apps.candidates.photos import (  # noqa: F401  (PhotoError re-exported)
    PhotoError,
    load_validated_rgb,
)

SIZES = {"full": 1600, "thumb": 400}


def _fit(rgb, longest: int) -> bytes:
    img = rgb.copy()
    img.thumbnail((longest, longest), Image.Resampling.LANCZOS)  # never upscales; keeps aspect ratio
    out = io.BytesIO()
    img.save(out, format="WEBP", quality=82, method=6)  # no exif= → none written
    return out.getvalue()


def store(upload, *, purpose="news_image", user=None) -> dict:
    """A file (local media backend) or a verified Cloudinary asset (`media.services.Accepted`, spec 15 §7)."""
    from apps.media.services import Accepted, register_local

    if isinstance(upload, Accepted):
        return upload.as_content()
    rgb = load_validated_rgb(upload)
    stem = uuid.uuid4().hex
    full = default_storage.save(f"news/{stem}_1600.webp", ContentFile(_fit(rgb, SIZES["full"])))
    thumb = default_storage.save(f"news/{stem}_400.webp", ContentFile(_fit(rgb, SIZES["thumb"])))
    w, h = rgb.size
    scale = min(1.0, SIZES["full"] / max(w, h))
    register_local(purpose, {"image": full, "thumb": thumb}, width=round(w * scale), height=round(h * scale), user=user)
    return {"image": full, "image_thumb": thumb, "image_width": round(w * scale), "image_height": round(h * scale)}


def delete(*names):
    from apps.media.services import release

    release(*names)


def url(name: str, request=None) -> str | None:
    from apps.candidates.photos import public_url

    return public_url(request, name)
