"""Query-parameter filters (spec 15 §4.7–4.8). Unknown values for a choice filter answer 400 `invalid_filter` instead of
being silently ignored; unknown parameter NAMES are ignored."""
from .exceptions import ServiceError


def invalid(name):
    return ServiceError(f"That {name.replace('_', ' ')} filter isn't valid.", code="invalid_filter", status_code=400)


def choice(request, name, values, default=""):
    raw = request.query_params.get(name, "")
    if raw in ("", None):
        return default
    if raw not in values:
        raise invalid(name)
    return raw


def integer(request, name, default=None, minimum=None, maximum=None):
    raw = request.query_params.get(name, "")
    if raw in ("", None):
        return default
    try:
        n = int(raw)
    except ValueError:
        raise invalid(name) from None
    if (minimum is not None and n < minimum) or (maximum is not None and n > maximum):
        raise invalid(name)
    return n


def ordering(request, allowed, default):
    """`?ordering=field` or `-field` from an allow-list; returns the order_by tuple with an id tie-break."""
    raw = request.query_params.get("ordering", "")
    if not raw:
        return default
    if raw.lstrip("-") not in allowed:
        raise invalid("ordering")
    return (raw, "-id" if raw.startswith("-") else "id")
