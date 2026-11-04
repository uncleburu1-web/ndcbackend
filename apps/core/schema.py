"""
OpenAPI generation (spec 15 §14). drf-spectacular with three NDCConnect extensions on every operation:

- `x-permissions`: who may call it, in words ("signed in", "system admin", …);
- `x-error-codes`: the catalogued codes it can return (apps/core/errors.py);
- `x-rate-limit`: its throttle scopes and rates (apps/core/throttling.py).

Views that build their answer by hand (most of NDCConnect's APIViews) have no serializer to describe it. Their
response is published as an open object marked `x-untyped: true`, and docs/openapi-untyped.txt lists them: a
test fails when a NEW untyped operation appears (a ratchet), so new endpoints must declare their response
schema with `@extend_schema`. Existing ones are typed area by area as each area is next changed.
"""
import inspect
import re

from django.conf import settings
from drf_spectacular.openapi import AutoSchema

PERMISSION_WORDS = {
    "AllowAny": "anyone",
    "IsSignedIn": "signed in",
    "IsProfileComplete": "signed in with a complete profile",
    "IsSystemAdmin": "system admin",
    "IsPrivacyOfficer": "privacy officer (system admin)",
}
COMMON = {
    "signed in": ["not_authenticated", "account_deactivated", "auth_unavailable"],
    "admin": ["permission_denied", "mfa_required"],
}
CODE_RE = re.compile(r"""code\s*=\s*["']([a-z_]+)["']|ServiceError\([^)]*?,\s*["']([a-z_]+)["']""")
_SOURCE_CODES = {}


def _module_codes(view_cls):
    mod = inspect.getmodule(view_cls)
    if mod is None:
        return set()
    key = mod.__name__
    if key not in _SOURCE_CODES:
        try:
            src = inspect.getsource(mod)
        except (OSError, TypeError):
            src = ""
        _SOURCE_CODES[key] = {a or b for a, b in CODE_RE.findall(src)}
    return _SOURCE_CODES[key]


def _words(perm_cls):
    name = perm_cls.__name__
    if name in PERMISSION_WORDS:
        return PERMISSION_WORDS[name]
    doc = (perm_cls.__doc__ or "").strip().splitlines()
    return doc[0].rstrip(".") if doc else re.sub(r"(?<!^)(?=[A-Z])", " ", name).lower()


class NDCAutoSchema(AutoSchema):
    untyped = set()

    def _view_cls(self):
        return getattr(self.view, "cls", None) or type(self.view)

    def _own_doc(self):
        fn = getattr(self._view_cls(), self.method.lower(), None)
        doc = inspect.getdoc(fn) if fn is not None and fn.__doc__ else None
        if not doc:
            cls = self._view_cls()
            doc = inspect.cleandoc(cls.__dict__["__doc__"]) if cls.__dict__.get("__doc__") else ""
        return doc or ""

    def get_summary(self):
        s = super().get_summary()
        if s:
            return s
        first = self._own_doc().split("\n\n")[0].replace("\n", " ").strip()
        if not first:
            name = _route_names().get(self.path, "") or self.path
            first = f"{self.method} {name.replace('-', ' ')}"
        return (first[:117] + "…") if len(first) > 120 else first

    def get_description(self):
        doc = self._own_doc()
        parts = doc.split("\n\n", 1)
        return parts[1].strip() if len(parts) > 1 else ""

    def get_tags(self):
        tags = super().get_tags()
        parts = [p for p in self.path.split("/") if p and not p.startswith("{")]
        parts = parts[2:] if parts[:2] == ["api", "v1"] else parts
        if not parts:
            return tags
        if parts[0] in ("admin", "campaign", "me", "webhooks") and len(parts) > 1:
            return [f"{parts[0]}:{parts[1]}"]
        return [parts[0]]

    def get_operation_id(self):
        name = _route_names().get(self.path)
        if name:
            return f"{name.replace('-', '_')}_{self.method.lower()}"
        return super().get_operation_id()

    def _permissions(self):
        words = []
        for p in self.view.get_permissions():
            w = _words(type(p))
            if w not in words:
                words.append(w)
        from apps.access.security import ANY_FACTOR, SECOND_FACTOR

        url_name = _route_names().get(self.path, "")
        if url_name in SECOND_FACTOR and any(m.split(":")[0] == self.method for m in SECOND_FACTOR[url_name]):
            words.append("two-step verification, recently confirmed (reverification)")
        if url_name in ANY_FACTOR and self.method in ANY_FACTOR[url_name]:
            words.append("recent sign-in confirmation (reverification)")
        return words or ["anyone"]

    def _throttles(self):
        rates = settings.REST_FRAMEWORK.get("DEFAULT_THROTTLE_RATES", {})
        try:
            throttles = self.view.get_throttles()
        except Exception:
            throttles = []
        out = []
        for t in throttles:
            scope = getattr(t, "scope", None)
            if scope:
                from .throttling import CATALOGUE

                out.append({"scope": scope, "rate": rates.get(scope, ""), "key": CATALOGUE.get(scope, ("", ""))[0]})
        return out

    def _error_codes(self, perms):
        from .errors import CATALOGUE

        codes = {"server_error"}
        if self.method in ("POST", "PUT", "PATCH"):
            codes.add("validation_error")
        if any(p != "anyone" for p in perms):
            codes.update(COMMON["signed in"])
        if any("admin" in p or "officer" in p or "role" in p for p in perms):
            codes.update(COMMON["admin"])
        if any("reverification" in p for p in perms):
            codes.add("reverification_required")
        if "{" in self.path:
            codes.add("not_found")
        if self._throttles():
            codes.add("throttled")
        codes.update(_module_codes(self._view_cls()))
        return sorted(c for c in codes if c in CATALOGUE)

    def _get_serializer(self):
        view = self.view
        if hasattr(view, "get_serializer_class") or hasattr(view, "serializer_class"):
            try:
                return super()._get_serializer()
            except Exception:
                return None
        return None

    def get_operation(self, path, path_regex, path_prefix, method, registry):
        op = super().get_operation(path, path_regex, path_prefix, method, registry)
        if op is None:
            return op
        perms = self._permissions()
        op["x-permissions"] = perms
        op["x-error-codes"] = self._error_codes(perms)
        rl = self._throttles()
        if rl:
            op["x-rate-limit"] = rl
        if "anyone" in perms and "security" not in op:
            op["security"] = [{}]
        elif "anyone" not in perms:
            op["security"] = [{"bearerAuth": []}]
        for status, resp in list(op.get("responses", {}).items()):
            if status.startswith("2") and "content" not in resp and status != "204":
                resp["content"] = {"application/json": {"schema": {"type": "object", "additionalProperties": True}}}
                op["x-untyped"] = True
        if op.get("x-untyped"):
            NDCAutoSchema.untyped.add(f"{method.upper()} {path}")
        return op

    def _get_response_for_code(self, serializer, status_code, media_types=None, direction="response"):
        if serializer is None:
            return {"description": "Response (untyped; see docs/api.md)"}
        return super()._get_response_for_code(serializer, status_code, media_types, direction)


_ROUTES = {}


def _route_names() -> dict:
    """OpenAPI path ("/api/v1/candidates/{slug}/") → URL name, from the URLconf."""
    if not _ROUTES:
        from django.urls import get_resolver

        def walk(resolver, prefix):
            for p in resolver.url_patterns:
                route = prefix + str(p.pattern)
                if hasattr(p, "url_patterns"):
                    walk(p, route)
                elif p.name:
                    path = "/" + re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", route)
                    _ROUTES[path] = p.name
                    _ROUTES[path.replace("{pk}", "{id}")] = p.name  # drf-spectacular coerces pk to id

        walk(get_resolver(), "")
    return _ROUTES


def postprocess(result, generator, request, public):
    """Add the error and rate-limit catalogues as top-level extensions (docs/api.md repeats them)."""
    from .errors import CATALOGUE
    from .throttling import CATALOGUE as RL

    rates = settings.REST_FRAMEWORK.get("DEFAULT_THROTTLE_RATES", {})
    result["x-error-codes"] = {c: {"status": s, "meaning": m, "web_handling": w} for c, (s, m, w) in sorted(CATALOGUE.items())}
    result["x-rate-limits"] = {k: {"key": key, "purpose": why, "rate": rates.get(k, "")} for k, (key, why) in sorted(RL.items())}
    result.setdefault("components", {}).setdefault("schemas", {})["Error"] = {
        "type": "object", "required": ["error"],
        "properties": {"error": {"type": "object", "required": ["status", "code", "message", "fields", "request_id"], "properties": {
            "status": {"type": "integer"}, "code": {"type": "string"}, "message": {"type": "string"},
            "fields": {"type": "object", "additionalProperties": {"type": "array", "items": {"type": "string"}}},
            "request_id": {"type": "string"}, "retry_after": {"type": "integer"}}}}}
    return result
