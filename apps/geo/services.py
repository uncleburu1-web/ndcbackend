"""
Geography services (spec 19 §12.1). Reference data only: nothing here reads or returns anything about a person,
except `confirmations`/`suggest_for`, which the signed-in person's own `/me` view and officers' existing views call.
"""
import hashlib

from django.contrib.postgres.search import TrigramSimilarity
from django.db.models import Count, Q

from apps.core.exceptions import ServiceError
from apps.search.text import fold

from . import loading
from .models import Alias, Constituency, ConstituencyArea, Dataset, Lga, PollingUnit, State, Ward

LIMIT = 20
SUGGEST_MIN = 0.6  # §8.3
CODE_RE = r"^\d{2}-\d{2}-\d{2}-\d{3}$"


def k(text):
    return fold(text or "").lower().strip()


# ------------------------------------------------------------------ lookups
def states():
    return State.objects.order_by("name")


def get_state(slug):
    st = State.objects.filter(slug=slug).first()
    if st is None:
        raise ServiceError("State not found.", code="not_found", status_code=404)
    return st


def _typeahead(qs, q, alias_field, limit):
    """Names first, then aliases, then trigram similarity. Always inside a parent (§22: never across 177k units)."""
    f = k(q)
    if not f:
        return list(qs.order_by("name")[:limit])
    alias_ids = Alias.objects.filter(**{f"{alias_field}__in": qs.values("pk")}, folded_name__icontains=f).values(alias_field)
    by_code = Q(code=f) if f.isdigit() else Q(pk__in=[])
    hits = (qs.filter(Q(folded_name__icontains=f) | Q(pk__in=alias_ids) | by_code).distinct()
            .annotate(sim=TrigramSimilarity("folded_name", f)).order_by("-sim", "name"))
    out = list(hits[:limit])
    if len(out) < limit and len(f) >= 3:  # near misses ("anifowose" for "anifowoshe")
        seen = [o.pk for o in out]
        more = (qs.exclude(pk__in=seen).annotate(sim=TrigramSimilarity("folded_name", f)).filter(sim__gte=0.3)
                .order_by("-sim", "name")[: limit - len(out)])
        out += list(more)
    return out


def children(level, parent, q=None, limit=LIMIT, include_inactive=False):
    """Active rows under a parent (§12.1). `parent` is a state slug for LGAs, else the parent's ID."""
    limit = max(1, min(int(limit or LIMIT), 100))
    if level == "lga":
        st = State.objects.filter(slug=parent).first()
        if st is None:
            # A real state whose INEC list isn't loaded yet has no LGAs (the forms fall back to typing); only an unknown
            # slug is "not found".
            from apps.candidates.reference import State as StateChoice
            if parent in StateChoice.values:
                return []
            raise ServiceError("State not found.", code="not_found", status_code=404)
        qs = Lga.objects.filter(state=st)
        qs = qs if include_inactive else qs.filter(active=True)
        return _typeahead(qs, q, "lga", limit if q else 1000)
    if level == "ward":
        lga = Lga.objects.filter(pk=parent).first()
        if lga is None:
            raise ServiceError("LGA not found.", code="not_found", status_code=404)
        qs = Ward.objects.filter(lga=lga, active=True).annotate(units=Count("polling_units", filter=Q(polling_units__active=True)))
        return _typeahead(qs, q, "ward", limit if q else 500)
    if level == "polling_unit":
        ward = Ward.objects.filter(pk=parent).first()
        if ward is None:
            raise ServiceError("Ward not found.", code="not_found", status_code=404)
        return _typeahead(PollingUnit.objects.filter(ward=ward, active=True), q, "polling_unit", limit if q else 1000)
    raise ValueError(level)


def by_code(code):
    """A polling unit (and its parents) from `NN-NN-NN-NNN`. Inactive units are returned too, flagged (§3 rule 2)."""
    code = (code or "").strip()
    pu = PollingUnit.objects.select_related("ward__lga__state").filter(delimitation=code).first()
    if pu is None:
        raise ServiceError("No polling unit has that code.", code="not_found", status_code=404)
    return pu


def search_units(q, limit=LIMIT):
    """The finder (§6.4): a full or partial code, or part of a name inside a chosen ward (the view requires the ward)."""
    q = (q or "").strip()
    if q.replace("-", "").isdigit():
        return list(PollingUnit.objects.select_related("ward__lga__state").filter(delimitation__startswith=q, active=True)
                    .order_by("delimitation")[:limit])
    return []


def constituencies(state=None, kind=None):
    qs = Constituency.objects.filter(active=True).select_related("state").prefetch_related("areas").order_by("kind", "code")
    if state:
        qs = qs.filter(state__slug=state)
    if kind:
        qs = qs.filter(kind=kind)
    return qs


def covering(lga):
    """Constituencies that cover an LGA: by the whole LGA, or by one of its wards (§5.1)."""
    ids = ConstituencyArea.objects.filter(Q(lga=lga) | Q(ward__lga=lga)).values("constituency")
    return Constituency.objects.filter(pk__in=ids, active=True).order_by("kind", "name")


def current_datasets():
    """The latest applied dataset of each kind (§6.4 "About this data", readiness)."""
    out = []
    for kind in Dataset.Kind.values:
        ds = Dataset.objects.filter(kind=kind, status=Dataset.Status.APPLIED).order_by("-applied_at", "-pk").first()
        if ds:
            out.append(ds)
    return out


def etag():
    """Changes whenever a dataset is applied or an alias changes, so cached pickers refresh (§13.1)."""
    last = Dataset.objects.filter(status=Dataset.Status.APPLIED).order_by("-pk").values_list("pk", "applied_at").first()
    alias = Alias.objects.order_by("-pk").values_list("pk", flat=True).first()
    return '"geo-' + hashlib.sha256(f"{last}|{alias}|{Alias.objects.count()}".encode()).hexdigest()[:16] + '"'


def is_sample_only():
    """Readiness (§15.3): only the committed sample (or nothing) is loaded."""
    ds = Dataset.objects.filter(kind=Dataset.Kind.POLLING_UNITS, status=Dataset.Status.APPLIED)
    return not ds.exists() or not ds.exclude(source__icontains="sample").exists()


# ------------------------------------------------------------------ chain validation (§7.3)
def _fail(code, field, message):
    e = ServiceError(message, code=code, status_code=400)
    e.extra = {"field": field}
    return e


def validate_chain(state=None, lga=None, ward=None, polling_unit=None, *, allow_inactive=()):
    """Check chosen IDs belong together and are active. Returns (state_slug, lga, ward, pu); the parents are filled from
    the deepest choice. `allow_inactive` names fields whose current value may stay inactive (an unchanged old link)."""
    pu = w = l_ = None
    if polling_unit:
        pu = PollingUnit.objects.select_related("ward__lga__state").filter(pk=polling_unit).first()
        if pu is None:
            raise _fail("geo_mismatch", "polling_unit", "That polling unit doesn't exist.")
        if ward and pu.ward_id != int(ward):
            raise _fail("geo_mismatch", "polling_unit", "That polling unit isn't in the chosen ward.")
        w = pu.ward
    if ward and w is None:
        w = Ward.objects.select_related("lga__state").filter(pk=ward).first()
        if w is None:
            raise _fail("geo_mismatch", "ward", "That ward doesn't exist.")
    if w is not None:
        if lga and w.lga_id != int(lga):
            raise _fail("geo_mismatch", "ward", "That ward isn't in the chosen LGA.")
        l_ = w.lga
    if lga and l_ is None:
        l_ = Lga.objects.select_related("state").filter(pk=lga).first()
        if l_ is None:
            raise _fail("geo_mismatch", "lga", "That LGA doesn't exist.")
    if l_ is not None and state and l_.state.slug != state:
        raise _fail("geo_mismatch", "lga", "That LGA isn't in the chosen state.")
    for name, obj in (("lga", l_), ("ward", w), ("polling_unit", pu)):
        if obj is not None and not obj.active and name not in allow_inactive:
            raise _fail("geo_inactive", name, "That place is no longer in INEC's current list.")
    return (l_.state.slug if l_ else state), l_, w, pu


def chain_ids(data, prefix=""):
    """Read `lga_id`, `ward_id`, `polling_unit_id` (or with a prefix) from request data as ints or None."""
    out = {}
    for f in ("lga", "ward", "polling_unit"):
        v = data.get(f"{prefix}{f}_id")
        if v in (None, "", 0, "0"):
            out[f] = None
            continue
        try:
            out[f] = int(v)
        except (TypeError, ValueError):
            raise _fail("geo_mismatch", f"{f}_id", "Choose a place from the list.") from None
    return out


# ------------------------------------------------------------------ suggestions (§8.3): never linked automatically
def _best(qs, text, alias_field):
    f = k(text)
    if not f:
        return None, 0.0
    exact = list(qs.filter(folded_name=f)[:2])
    if len(exact) == 1:
        return exact[0], 1.0
    via_alias = list(qs.filter(pk__in=Alias.objects.filter(folded_name=f).values(alias_field))[:2])
    if len(via_alias) == 1:
        return via_alias[0], 0.9
    hit = (qs.annotate(sim=TrigramSimilarity("folded_name", f))
           .filter(sim__gte=SUGGEST_MIN).order_by("-sim").first())
    return (hit, round(float(hit.sim), 2)) if hit else (None, 0.0)


def suggest(state, lga_text="", ward_text="", pu_text=""):
    """Best guesses for typed text, each with a score (1.0 exact, 0.9 alias, else trigram ≥ 0.6)."""
    out = {"lga": None, "ward": None, "polling_unit": None}
    if not state:
        return out
    lga, s1 = _best(Lga.objects.filter(state__slug=state, active=True), lga_text, "lga")
    if lga is None:
        return out
    out["lga"] = {"id": lga.pk, "name": lga.name, "code": lga.code, "score": s1}
    ward, s2 = _best(Ward.objects.filter(lga=lga, active=True), ward_text, "ward")
    if ward:
        out["ward"] = {"id": ward.pk, "name": ward.name, "code": ward.code, "score": s2}
    pus = PollingUnit.objects.filter(ward=ward, active=True) if ward else PollingUnit.objects.filter(ward__lga=lga, active=True)
    pu, s3 = _best(pus, pu_text, "polling_unit")
    if pu:
        out["polling_unit"] = {"id": pu.pk, "name": pu.name, "code": pu.delimitation, "score": s3}
        if not ward:
            out["ward"] = {"id": pu.ward_id, "name": pu.ward.name, "code": pu.ward.code, "score": s3}
    return out


# ------------------------------------------------------------------ public pages
def lga_page(state_slug, lga_slug):
    lga = Lga.objects.select_related("state").filter(state__slug=state_slug, slug=lga_slug).first()
    if lga is None:
        raise ServiceError("LGA not found.", code="not_found", status_code=404)
    return lga


def unit_counts(lga):
    return (Ward.objects.filter(lga=lga, active=True)
            .annotate(units=Count("polling_units", filter=Q(polling_units__active=True))).order_by("name"))


# ------------------------------------------------------------------ admin (§13.3)
def preview(content, *, kind, source="", published_on=None, notes="", user=None, request=None, file_name=""):
    if file_name and not file_name.lower().endswith(".csv"):
        raise loading.GeographyFileError("Upload a .csv file.")
    return loading.preview(content, kind=kind, source=source, published_on=published_on, notes=notes, user=user, request=request)


def apply(dataset_id, user, *, request=None, confirm_second=False):
    """§4.3's 95% rule from the admin page: the first admin's apply is recorded and refused; a different system admin
    then applies with `confirm_second`, and the two together count as two admins."""
    ds = Dataset.objects.select_related("first_approval_by").filter(pk=dataset_id).first()
    if ds is None:
        raise ServiceError("Dataset not found.", code="not_found", status_code=404)
    second = None
    if confirm_second and ds.first_approval_by_id and ds.first_approval_by_id != getattr(user, "pk", None):
        second = ds.first_approval_by
    return loading.apply(ds, user, request=request, second_admin=second)


def add_alias(*, level, target_id, name, user, request=None):
    from apps.audit.models import AuditLog
    from apps.audit.services import record

    model = {"lga": Lga, "ward": Ward, "polling_unit": PollingUnit}.get(level)
    if model is None:
        raise ServiceError("Choose an LGA, ward or polling unit.", code="validation_error", status_code=400)
    target = model.objects.filter(pk=target_id).first()
    name = (name or "").strip()
    if target is None or not name or len(name) > 200:
        raise ServiceError("Choose a place and give a name (up to 200 characters).", code="validation_error", status_code=400)
    if Alias.objects.filter(**{level: target}, folded_name=k(name)).exists() or k(name) == target.folded_name:
        raise ServiceError("That place already has this name.", code="validation_error", status_code=400)
    a = Alias.objects.create(**{level: target}, name=name, folded_name=k(name), source=Alias.Source.ADMIN)
    if level == "lga":
        from apps.search.index import sync_lga

        sync_lga(target)
    record(AuditLog.Action.GEO_ALIAS_CHANGED, target=a, actor=user, request=request, change="added", level=level)
    return a


def remove_alias(alias_id, *, user, request=None):
    from apps.audit.models import AuditLog
    from apps.audit.services import record

    a = Alias.objects.filter(pk=alias_id).first()
    if a is None:
        raise ServiceError("Alias not found.", code="not_found", status_code=404)
    level = "lga" if a.lga_id else "ward" if a.ward_id else "polling_unit"
    record(AuditLog.Action.GEO_ALIAS_CHANGED, target=a, actor=user, request=request, change="removed", level=level)
    lga = a.lga
    a.delete()
    if lga is not None:
        from apps.search.index import sync_lga

        sync_lga(lga)
