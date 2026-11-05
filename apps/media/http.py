"""Helpers for the image endpoints of 03, 07 and 09 (spec 15 §7.3): accept a multipart file (local backend) or a
verified Cloudinary asset (`{"asset": {public_id, version, signature}}`), and map media errors to the envelope."""
from apps.candidates.photos import PhotoError
from apps.core.exceptions import ServiceError

from . import services


def upload_or_asset(request, *, field: str, purpose: str, target_id):
    """Returns (file-or-Accepted, rights_confirmed)."""
    confirmed = str(request.data.get("rights_confirmed", "")).lower() in ("true", "1", "on", "yes")
    asset = request.data.get("asset") if hasattr(request.data, "get") else None
    if asset:
        try:
            return services.accept(request.user, purpose, target_id, asset), confirmed
        except services.MediaError as exc:
            raise PhotoError(str(exc)) from None
        except services.MediaUnavailable:
            raise ServiceError("Image upload isn't working right now. Your changes are saved without the new image.",
                               code="media_unavailable", status_code=503) from None
    upload = request.FILES.get(field)
    if upload is None:
        raise ServiceError("Choose an image to upload." if field == "image" else "Choose a photo to upload.", code="validation_error",
                           status_code=400)
    return upload, confirmed
