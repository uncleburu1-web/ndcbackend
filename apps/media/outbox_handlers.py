from django.utils import timezone

from apps.core.outbox import handler

from . import cloudinary_api as cld
from .models import MediaAsset


@handler("media_destroy")
def media_destroy(payload):
    public_id = payload["public_id"]
    cld.destroy(public_id, type_=payload.get("type", "upload"))
    MediaAsset.objects.filter(provider=MediaAsset.Provider.CLOUDINARY, public_id=public_id).update(
        delivery=MediaAsset.Delivery.DELETED, deleted_at=timezone.now())


def _move(payload, hidden):
    a = MediaAsset.objects.get(pk=payload["asset"])
    if a.delivery == MediaAsset.Delivery.DELETED:
        return
    want = MediaAsset.Delivery.HIDDEN if hidden else MediaAsset.Delivery.PUBLIC
    if a.delivery == want:
        return
    cld.set_type(a.public_id, to_type="authenticated" if hidden else "upload", from_type="upload" if hidden else "authenticated")
    MediaAsset.objects.filter(pk=a.pk).update(delivery=want)


@handler("media_hide")
def media_hide(payload):
    _move(payload, True)


@handler("media_show")
def media_show(payload):
    _move(payload, False)
