"""
Cloudinary over the shared HTTP client (spec 15 §7, §12.2). The official SDK does the signing; every HTTP call goes
through `integrations.http`, so Cloudinary gets the same timeouts, circuit breaker and health as other providers.
The API secret never leaves this module.
"""
import base64
import json
import time
import urllib.parse

import cloudinary.utils
from django.conf import settings

from apps.integrations import http

PROVIDER = "cloudinary"


def cfg():
    return {"cloud": settings.CLOUDINARY_CLOUD_NAME, "key": settings.CLOUDINARY_API_KEY, "secret": settings.CLOUDINARY_API_SECRET,
            "api": settings.CLOUDINARY_API_BASE.rstrip("/"), "delivery": settings.CLOUDINARY_DELIVERY_BASE.rstrip("/"),
            "preset": settings.CLOUDINARY_UPLOAD_PRESET, "folder": settings.CLOUDINARY_FOLDER.strip("/")}


def configured() -> bool:
    c = cfg()
    return bool(c["cloud"] and c["key"] and c["secret"])


def sign(params: dict) -> str:
    """Cloudinary's upload-API signature (sorted params, `&`-joined, secret appended, SHA-1)."""
    return cloudinary.utils.api_sign_request({k: v for k, v in params.items() if v not in (None, "")}, cfg()["secret"])


def verify_response(public_id: str, version, signature: str) -> bool:
    expected = cloudinary.utils.api_sign_request({"public_id": public_id, "version": str(version)}, cfg()["secret"])
    import hmac

    return bool(signature) and hmac.compare_digest(expected, signature)


def upload_url() -> str:
    c = cfg()
    return f"{c['api']}/v1_1/{c['cloud']}/image/upload"


def _basic():
    c = cfg()
    return {"Authorization": "Basic " + base64.b64encode(f"{c['key']}:{c['secret']}".encode()).decode()}


def resource(public_id: str, *, type_="upload") -> dict | None:
    c = cfg()
    url = f"{c['api']}/v1_1/{c['cloud']}/resources/image/{type_}/{urllib.parse.quote(public_id, safe='/')}"
    r = http.call(PROVIDER, "GET", url, operation="resource", headers=_basic(), idempotent=True)
    if r.status == 404:
        return None
    if r.status >= 400:
        raise http.ProviderUnavailable(PROVIDER, f"HTTP {r.status}")
    return r.json()


def list_resources(prefix: str, cursor: str = "") -> dict:
    c = cfg()
    q = urllib.parse.urlencode({"prefix": prefix, "max_results": 500, **({"next_cursor": cursor} if cursor else {})})
    r = http.call(PROVIDER, "GET", f"{c['api']}/v1_1/{c['cloud']}/resources/image/upload?{q}", operation="list", headers=_basic(),
                  idempotent=True)
    if r.status >= 400:
        raise http.ProviderUnavailable(PROVIDER, f"HTTP {r.status}")
    return r.json() or {}


def _signed_post(action: str, params: dict, *, files=None, timeout_key=None) -> dict:
    c = cfg()
    params = {**params, "timestamp": int(time.time())}
    params["signature"] = sign(params)
    params["api_key"] = c["key"]
    url = f"{c['api']}/v1_1/{c['cloud']}/image/{action}"
    r = http.call(PROVIDER, "POST", url, operation=action, data=params, files=files, timeout_key=timeout_key)
    body = r.json() or {}
    if r.status >= 400:
        msg = (body.get("error") or {}).get("message", "") if isinstance(body, dict) else ""
        raise CloudinaryRefused(f"{action} refused ({r.status})", msg)
    return body


class CloudinaryRefused(Exception):
    def __init__(self, what, detail=""):
        self.detail = detail
        super().__init__(what)


def destroy(public_id: str, *, type_="upload") -> dict:
    return _signed_post("destroy", {"public_id": public_id, "type": type_, "invalidate": "true"})


def set_type(public_id: str, *, to_type: str, from_type: str) -> dict:
    """Moves an asset between delivery types (`upload` = public, `authenticated` = needs a signed URL)."""
    return _signed_post("rename", {"from_public_id": public_id, "to_public_id": public_id, "type": from_type, "to_type": to_type,
                                   "invalidate": "true", "overwrite": "true"})


def server_upload(public_id: str, content: bytes, filename: str, content_type: str) -> dict:
    """Server-side upload with the same preset (used by the migration command)."""
    return _signed_post("upload", {"public_id": public_id, "upload_preset": cfg()["preset"]},
                        files={"file": (filename, content, content_type)}, timeout_key="cloudinary_upload")


def setup_preset(name: str) -> dict:
    """Create or update the signed upload preset (spec 15 §7.2): formats, 2000 px cap, re-encoding strips metadata."""
    c = cfg()
    fields = {"name": name, "unsigned": "false", "allowed_formats": "jpg,png,webp", "transformation": "c_limit,w_2000,h_2000",
              "overwrite": "false", "unique_filename": "false", "use_filename": "false", "type": "upload"}
    r = http.call(PROVIDER, "PUT", f"{c['api']}/v1_1/{c['cloud']}/upload_presets/{name}", operation="preset", headers=_basic(), data=fields)
    if r.status == 404:
        r = http.call(PROVIDER, "POST", f"{c['api']}/v1_1/{c['cloud']}/upload_presets", operation="preset", headers=_basic(), data=fields)
    if r.status >= 400:
        raise CloudinaryRefused(f"preset refused ({r.status})")
    return r.json() or {}


def delivery_url(public_id: str, version, transformation: str) -> str:
    c = cfg()
    v = f"v{version}/" if version else ""
    return f"{c['delivery']}/{c['cloud']}/image/upload/{transformation}/{v}{public_id}"


def dumps(x):  # pragma: no cover - debugging helper
    return json.dumps(x, default=str)
