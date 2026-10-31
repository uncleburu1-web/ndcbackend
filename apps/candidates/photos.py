"""
Candidate photo processing — spec 03 §5.3, §9.

- Real type checked by opening the image with Pillow (not the filename or Content-Type).
- Size limit checked BEFORE reading the body.
- EXIF orientation applied, then ALL metadata dropped (photos can carry GPS location):
  the image is re-encoded from pixels only, which also neutralises malformed/polyglot files.
- Centre-cropped square, stored as WebP at 800 px and 200 px under random names.
"""
import io
import uuid
from dataclasses import dataclass

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from PIL import Image, ImageOps, UnidentifiedImageError

ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}
SIZES = (800, 200)
MAX_PIXELS = 40_000_000  # refuse decompression bombs well below Pillow's default


class PhotoError(Exception):
    """User-facing message for a rejected photo."""


@dataclass
class StoredPhoto:
    name_800: str
    name_200: str


def process_and_store(upload) -> StoredPhoto:
    if upload.size > settings.CANDIDATE_PHOTO_MAX_BYTES:
        raise PhotoError("That photo is larger than 5 MB. Choose a smaller file.")

    data = upload.read()
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt = probe.format
            probe.verify()  # structural check
        if fmt not in ALLOWED_FORMATS:
            raise PhotoError("Use a JPEG, PNG or WebP photo.")
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            img = ImageOps.exif_transpose(img)  # honour camera rotation before metadata is dropped
            minimum = settings.CANDIDATE_PHOTO_MIN_SIDE
            if min(img.size) < minimum:
                raise PhotoError(f"That photo is too small. Use one at least {minimum} × {minimum} pixels.")
            rgb = img.convert("RGB")  # pixels only — no EXIF/ICC/XMP carried over
    except PhotoError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError, ValueError):
        raise PhotoError("We couldn't read that file as a photo. Use a JPEG, PNG or WebP image.") from None

    stem = uuid.uuid4().hex
    names = []
    for size in SIZES:
        square = ImageOps.fit(rgb, (size, size), method=Image.Resampling.LANCZOS, centering=(0.5, 0.4))
        out = io.BytesIO()
        square.save(out, format="WEBP", quality=82, method=6)  # no exif= argument → none written
        names.append(default_storage.save(f"candidates/{stem}_{size}.webp", ContentFile(out.getvalue())))
    return StoredPhoto(name_800=names[0], name_200=names[1])


def delete_stored(*names: str):
    for name in names:
        if name:
            try:
                default_storage.delete(name)
            except Exception:  # pragma: no cover — a missing old file must not block the update
                pass


def public_url(request, name: str) -> str | None:
    if not name:
        return None
    url = default_storage.url(name)
    return request.build_absolute_uri(url) if request is not None and url.startswith("/") else url
