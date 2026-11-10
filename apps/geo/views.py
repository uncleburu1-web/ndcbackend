"""
Geography API (spec 19 §13).

Public endpoints are anonymous on purpose (rule 6): no session is read, nothing records who looked something up, and
responses never vary by user. Cacheable for an hour with an ETag from the current datasets; per-IP `geo_lookup`.
"""
import hashlib
import json

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponseNotModified
from django.utils.cache import add_never_cache_headers, patch_cache_control
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle
from rest_framework.views import APIView

from apps.access.permissions import IsSignedIn, IsSystemAdmin
from apps.core.exceptions import ServiceError

from . import records, serializers as G, services
from .models import Alias, Change, Constituency, Dataset, Lga

CONSTITUENCY_POSITION = {Constituency.Kind.SENATORIAL_DISTRICT: "senator", Constituency.Kind.FEDERAL_CONSTITUENCY: "house_of_reps",
                         Constituency.Kind.STATE_CONSTITUENCY: "state_assembly"}
Q_PARAM = OpenApiParameter("q", str, description="Type-ahead over names and aliases (20 results)")


class GeoThrottle(AnonRateThrottle):
    scope = "geo_lookup"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class _Public(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [GeoThrottle]
    max_age = 3600
    content_etag = False  # the LGA page also carries candidates and events, so its ETag hashes the response

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        if response.status_code == 200:
            if self.content_etag:
                tag = '"lga-' + hashlib.sha256(json.dumps(response.data, sort_keys=True, default=str).encode()).hexdigest()[:16] + '"'
            else:
                tag = services.etag()
            if request.META.get("HTTP_IF_NONE_MATCH") == tag:
                nm = HttpResponseNotModified()
                nm["ETag"] = tag
                patch_cache_control(nm, public=True, max_age=self.max_age)
                return nm
            response["ETag"] = tag
            patch_cache_control(response, public=True, max_age=self.max_age)
        return response


class _Private(APIView):
    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        add_never_cache_headers(response)
        patch_cache_control(response, private=True, no_store=True)
        return response


def _lga(l_):
    return {"id": l_.pk, "code": l_.code, "name": l_.name, "slug": l_.slug, "active": l_.active}


def _ward(w):
    return {"id": w.pk, "code": w.code, "name": w.name, "polling_units": getattr(w, "units", None) or 0}


def _pu(p):
    return {"id": p.pk, "code": p.code, "delimitation": p.delimitation, "name": p.name}


def _pu_detail(p):
    w, l_ = p.ward, p.ward.lga
    return {**_pu(p), "active": p.active, "ward": {"id": w.pk, "code": w.code, "name": w.name},
            "lga": {"id": l_.pk, "code": l_.code, "name": l_.name, "slug": l_.slug, "active": l_.active},
            "state": {"slug": l_.state.slug, "code": l_.state.code, "name": l_.state.name}, "inec_url": settings.INEC_PU_LOCATOR_URL}


def _dataset_public(d):
    return {"kind": d.kind, "source": d.source, "published_on": d.published_on, "applied_at": d.applied_at, "states": d.states,
            "lgas": d.lgas, "wards": d.wards, "polling_units": d.polling_units, "constituencies": d.constituencies}


def _about_units():
    d = next((d for d in services.current_datasets() if d.kind == Dataset.Kind.POLLING_UNITS), None)
    return _dataset_public(d) if d else None


def _constituency(c):
    areas = list(c.areas.all())
    return {"id": c.pk, "kind": c.kind, "code": c.code, "name": c.name, "slug": c.slug, "state": c.state.slug,
            "lga_ids": sorted(a.lga_id for a in areas if a.lga_id), "ward_ids": sorted(a.ward_id for a in areas if a.ward_id)}


# ------------------------------------------------------------------ public (§13.1)
class GeoStates(_Public):
    @extend_schema(summary="The 36 states and the FCT, with capitals (spec 19 §13.1)", responses=G.StateList)
    def get(self, request):
        counts = dict(Lga.objects.filter(active=True).values_list("state_id").annotate(n=Count("pk")))
        return Response({"results": [{"slug": s.slug, "code": s.code, "name": s.name, "capital": s.capital, "lgas": counts.get(s.pk, 0)}
                                     for s in services.states()]})


class GeoStateLgas(_Public):
    @extend_schema(summary="A state's LGAs (area councils in the FCT), active only", parameters=[Q_PARAM], responses=G.LgaList)
    def get(self, request, state):
        return Response({"results": [_lga(x) for x in services.children("lga", state, request.query_params.get("q"))]})


class GeoLgaWards(_Public):
    @extend_schema(summary="An LGA's wards (INEC registration areas), with how many polling units each has", parameters=[Q_PARAM],
                   responses=G.WardList)
    def get(self, request, pk):
        return Response({"results": [_ward(x) for x in services.children("ward", pk, request.query_params.get("q"))]})


class GeoWardUnits(_Public):
    @extend_schema(summary="A ward's polling units (INEC's names and codes)", parameters=[Q_PARAM], responses=G.PollingUnitList)
    def get(self, request, pk):
        return Response({"results": [_pu(x) for x in services.children("polling_unit", pk, request.query_params.get("q"))]})


class GeoUnitByCode(_Public):
    @extend_schema(summary="One polling unit by INEC code (e.g. 24-07-05-004), with its ward, LGA and state", responses=G.PollingUnitDetailSchema)
    def get(self, request, code):
        return Response(_pu_detail(services.by_code(code)))


class GeoUnitFinder(_Public):
    @extend_schema(summary="Polling units whose code starts with what was typed (the finder, §6.4); at least the state and LGA parts",
                   parameters=[OpenApiParameter("code", str, required=True)], responses=G.PollingUnitFound)
    def get(self, request):
        code = (request.query_params.get("code") or "").strip()
        if len(code.replace("-", "")) < 4 or not code.replace("-", "").isdigit():
            raise ServiceError("Type at least the state and LGA parts of the code, like 24-07.", code="validation_error", status_code=400)
        return Response({"results": [_pu_detail(p) for p in services.search_units(code)]})


class GeoConstituencies(_Public):
    @extend_schema(summary="Constituencies with the LGAs and wards they cover", responses=G.ConstituencyList,
                   parameters=[OpenApiParameter("state", str), OpenApiParameter("kind", str, enum=Constituency.Kind.values)])
    def get(self, request):
        kind = request.query_params.get("kind") or None
        if kind and kind not in Constituency.Kind.values:
            raise ServiceError("Unknown kind.", code="validation_error", status_code=400)
        return Response({"results": [_constituency(c) for c in services.constituencies(request.query_params.get("state") or None, kind)]})


class GeoAbout(_Public):
    @extend_schema(summary="Which INEC files are loaded: source, publication date and counts", responses=G.AboutSchema)
    def get(self, request):
        return Response({"datasets": [_dataset_public(d) for d in services.current_datasets()], "inec_url": settings.INEC_PU_LOCATOR_URL})


class LgaPage(_Public):
    """The LGA page (§6.2). The lowest public page (§6.3): wards are listed, never shown on their own."""

    GROUP = 6
    max_age = 300
    content_etag = True

    @extend_schema(summary="The LGA page: candidates standing here, upcoming events, community, wards", responses=G.LgaPageSchema)
    def get(self, request, state, lga):
        from apps.candidates.serializers import PublicCandidateListSerializer
        from apps.communities.models import Community
        from apps.communities.services import band
        from apps.events.serializers import card
        from apps.events.services import upcoming
        from apps.search.index import _public_candidates
        from apps.search.services import current_year

        l_ = services.lga_page(state, lga)
        year = current_year()
        cands = _public_candidates().filter(election_year=year) if year else _public_candidates().none()
        groups = []

        def add(key, label, position, qs, see_all, code=None):
            qs = qs.order_by("ward_name", "full_name")
            n = qs.count()
            if n:
                groups.append({"key": key, "label": label, "position": position, "constituency": code, "total": n,
                               "results": PublicCandidateListSerializer(qs[: self.GROUP], many=True, context={"request": request}).data,
                               "see_all": see_all})

        fct = l_.state.slug == "fct"
        add("lga_chairman", "Area Council Chairman" if fct else "LGA Chairman", "lga_chairman",
            cands.filter(position="lga_chairman", geo_lga=l_), f"/candidates?position=lga_chairman&lga={l_.pk}")
        add("councillor", "Councillors", "councillor", cands.filter(position="councillor", geo_lga=l_),
            f"/candidates?position=councillor&lga={l_.pk}")
        for c in services.covering(l_):
            add(c.slug, c.name, CONSTITUENCY_POSITION[c.kind], cands.filter(geo_constituency=c), f"/candidates?constituency={c.pk}",
                code=c.code)
        com = Community.objects.filter(level="lga", geo_lga=l_, active=True).first()
        return Response({
            "state": l_.state.slug, "state_label": l_.state.name, "lga": _lga(l_), "area_council": fct, "election_year": year,
            "groups": groups, "events": [card(e) for e in upcoming(lga=l_, limit=5)],
            "community": {"id": com.pk, "path": com.path, "name": com.name, "members": band(com.member_count)} if com else None,
            "wards": [_ward(w) for w in services.unit_counts(l_)], "about": _about_units(),
        })


# ------------------------------------------------------------------ signed in (§13.2)
class MyGeoConfirm(_Private):
    permission_classes = [IsSignedIn]

    @extend_schema(summary="Your membership and PVC records that need you to confirm where you vote, with suggestions",
                   responses=G.ConfirmList)
    def get(self, request):
        return Response({"results": records.confirmations(request.user)})


class ConfirmThrottle(UserRateThrottle):
    scope = "profile_write"


class MyGeoConfirmKind(_Private):
    permission_classes = [IsSignedIn]
    throttle_classes = [ConfirmThrottle]

    @extend_schema(summary="Confirm or choose where you vote for one record (membership or pvc)", request=G.ConfirmRequest,
                   responses=G.ConfirmResult)
    def post(self, request, kind):
        rec = records.confirm(request.user, kind, request.data, request=request)
        pending = kind == "pvc" and bool(getattr(rec, "pending_geo_lga_id", None))
        return Response({"kind": kind, "geo_source": rec.geo_source, "waits_for_officer": pending,
                         "place": records.place(rec.geo_lga, rec.geo_ward, rec.geo_polling_unit)})


# ------------------------------------------------------------------ admin (§13.3)
class AdminWriteThrottle(UserRateThrottle):
    scope = "admin_write"


def _who(u):
    return u.email if u else None


def _dataset_admin(d, detail=False):
    rep = d.preview or {}
    out = {"id": d.pk, "kind": d.kind, "status": d.status, "source": d.source, "published_on": d.published_on, "notes": d.notes,
           "checksum": d.checksum, "loaded_at": d.loaded_at, "applied_at": d.applied_at, "applied_by": _who(d.applied_by),
           "first_approval_by": _who(d.first_approval_by), "states": d.states, "lgas": d.lgas, "wards": d.wards,
           "polling_units": d.polling_units, "constituencies": d.constituencies, "deactivated": d.deactivated,
           "changes": rep.get("changes", 0), "error_count": rep.get("error_count", 0),
           "needs_second_admin": bool(rep.get("needs_second_admin")) and d.status == Dataset.Status.PREVIEWED}
    if detail:
        out["preview"] = rep
    return out


class AdminDatasets(_Private):
    permission_classes = [IsSystemAdmin]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_throttles(self):
        return [AdminWriteThrottle()] if self.request.method == "POST" else []

    @extend_schema(summary="Geography files: previewed and applied, newest first", responses=G.DatasetList)
    def get(self, request):
        qs = Dataset.objects.select_related("applied_by", "first_approval_by").order_by("-loaded_at", "-pk")[:100]
        return Response({"results": [_dataset_admin(d) for d in qs]})

    @extend_schema(summary="Upload a geography file (CSV, ≤ 50 MB, private storage) and preview it", request=G.DatasetUpload,
                   responses={201: G.DatasetDetailSchema})
    def post(self, request):
        ser = G.DatasetUpload(data=request.data)
        ser.is_valid(raise_exception=True)
        v = ser.validated_data
        f = v["file"]
        if f.size > 50 * 1024 * 1024:
            raise ServiceError("The file is larger than 50 MB.", code="geo_file_invalid", status_code=400)
        ds = services.preview(f.read(), kind=v["kind"], source=v.get("source") or f.name, published_on=v.get("published_on"),
                              notes=v.get("notes", ""), user=request.user, request=request, file_name=f.name)
        return Response(_dataset_admin(ds, detail=True), status=201)


class AdminDatasetDetail(_Private):
    permission_classes = [IsSystemAdmin]

    @extend_schema(summary="One geography file with its preview report", responses=G.DatasetDetailSchema)
    def get(self, request, pk):
        d = Dataset.objects.select_related("applied_by", "first_approval_by").filter(pk=pk).first()
        if d is None:
            raise ServiceError("Dataset not found.", code="not_found", status_code=404)
        return Response(_dataset_admin(d, detail=True))


class AdminDatasetApply(_Private):
    permission_classes = [IsSystemAdmin]
    throttle_classes = [AdminWriteThrottle]

    @extend_schema(summary="Apply a previewed file in one transaction; the 95% rule needs a second admin (`confirm_second`)",
                   request=G.ApplyRequest, responses=G.DatasetDetailSchema)
    def post(self, request, pk):
        ser = G.ApplyRequest(data=request.data)
        ser.is_valid(raise_exception=True)
        ds = services.apply(pk, request.user, request=request, confirm_second=bool(ser.validated_data.get("confirm_second")))
        ds = Dataset.objects.select_related("applied_by", "first_approval_by").get(pk=ds.pk)
        return Response(_dataset_admin(ds, detail=True))


class AdminDatasetChanges(_Private):
    permission_classes = [IsSystemAdmin]

    @extend_schema(summary="What applying a file changed (100 a page)", responses=G.ChangePage,
                   parameters=[OpenApiParameter("level", str), OpenApiParameter("action", str), OpenApiParameter("page", int)])
    def get(self, request, pk):
        qs = Change.objects.filter(dataset_id=pk).order_by("level", "code", "pk")
        for f in ("level", "action"):
            v = request.query_params.get(f)
            if v:
                qs = qs.filter(**{f: v})
        page = Paginator(qs, 100).get_page(request.query_params.get("page") or 1)
        return Response({"count": page.paginator.count, "next": page.next_page_number() if page.has_next() else None,
                         "results": [{"level": c.level, "action": c.action, "code": c.code, "old_value": c.old_value,
                                      "new_value": c.new_value} for c in page]})


class GeoReviewer(IsSignedIn):
    """Membership officers (memberships), PVC officers (PVC checks) and system admins (all)."""

    message = "The geography review queue is for membership officers, PVC officers and system administrators."

    def has_permission(self, request, view):
        return super().has_permission(request, view) and bool(records.review_kinds(request.user))


def _review_item(kind, rec):
    pending = records.place(rec.pending_geo_lga, rec.pending_geo_ward, rec.pending_geo_polling_unit) \
        if kind == "pvc" and rec.pending_geo_lga_id else None
    return {"kind": kind, "id": rec.pk, "state": rec.state,
            "typed": {"lga": rec.lga, "ward": getattr(rec, "ward", ""), "polling_unit": rec.polling_unit},
            "why": records.why(kind, rec), "current": records.place(rec.geo_lga, rec.geo_ward, rec.geo_polling_unit),
            "pending": pending, "suggestion": services.suggest(rec.state, rec.lga, getattr(rec, "ward", ""), rec.polling_unit),
            "record_url": f"/admin/membership/{rec.pk}" if kind == "membership" else f"/admin/pvc?record={rec.pk}"}


class AdminReview(_Private):
    permission_classes = [GeoReviewer]

    @extend_schema(summary="Records with free text but no link, \"not listed\", a retired unit, or a person's choice waiting",
                   responses=G.ReviewPage,
                   parameters=[OpenApiParameter("kind", str, enum=["membership", "pvc"]), OpenApiParameter("state", str),
                               OpenApiParameter("why", str, enum=["unlinked", "not_listed", "retired", "person_choice"]),
                               OpenApiParameter("page", int)])
    def get(self, request):
        kinds = records.review_kinds(request.user)
        want = request.query_params.get("kind")
        if want:
            if want not in kinds:
                raise ServiceError("You can't review these records.", code="permission_denied", status_code=403)
            kinds = [want]
        state = request.query_params.get("state") or None
        why = request.query_params.get("why") or None
        items, groups = [], {}
        for kind in kinds:
            qs = records.review_queue(kind, state=state)
            for row in qs.values("state", "lga").annotate(n=Count("pk")):
                key = (row["state"], row["lga"])
                groups[key] = groups.get(key, 0) + row["n"]
            items += [(kind, r) for r in qs[:2000]]
        if why:
            items = [(k, r) for k, r in items if records.why(k, r) == why]
        page = Paginator(items, 20).get_page(request.query_params.get("page") or 1)
        return Response({"kinds": kinds, "count": page.paginator.count, "next": page.next_page_number() if page.has_next() else None,
                         "groups": [{"state": s, "lga": l_, "count": n} for (s, l_), n in sorted(groups.items())],
                         "results": [_review_item(k, r) for k, r in page]})


class AdminReviewAction(_Private):
    permission_classes = [GeoReviewer]
    throttle_classes = [AdminWriteThrottle]

    @extend_schema(summary="Link a record to a place, accept a person's choice (PVC), or ask the person (§8.1 notice)",
                   request=G.ReviewAction, responses=G.ReviewItemSchema)
    def post(self, request, kind, pk):
        if kind not in records.KINDS:
            raise ServiceError("Not found.", code="not_found", status_code=404)
        rec = records.review_action(request.user, kind, pk, request.data, request=request)
        rec = type(rec).objects.select_related("geo_lga", "geo_ward", "geo_polling_unit").get(pk=rec.pk)
        return Response(_review_item(kind, rec))


def _alias(a):
    level = "lga" if a.lga_id else "ward" if a.ward_id else "polling_unit"
    t = a.lga or a.ward or a.polling_unit
    code = t.delimitation if level == "polling_unit" else t.code
    return {"id": a.pk, "level": level, "target_id": t.pk, "target_name": t.name, "target_code": code, "name": a.name,
            "source": a.source, "created_at": a.created_at}


class AdminAliases(_Private):
    permission_classes = [IsSystemAdmin]

    def get_throttles(self):
        return [AdminWriteThrottle()] if self.request.method != "GET" else []

    @extend_schema(summary="Other names for places (renames and admin-added), used by type-ahead and suggestions only",
                   parameters=[OpenApiParameter("q", str), OpenApiParameter("state", str)], responses=G.AliasList)
    def get(self, request):
        qs = Alias.objects.select_related("lga", "ward", "polling_unit").order_by("-created_at")
        q = services.k(request.query_params.get("q"))
        if q:
            qs = qs.filter(Q(folded_name__icontains=q) | Q(lga__folded_name__icontains=q) | Q(ward__folded_name__icontains=q)
                           | Q(polling_unit__folded_name__icontains=q))
        st = request.query_params.get("state")
        if st:
            qs = qs.filter(Q(lga__state__slug=st) | Q(ward__lga__state__slug=st) | Q(polling_unit__ward__lga__state__slug=st))
        return Response({"results": [_alias(a) for a in qs[:200]]})

    @extend_schema(summary="Add another name for an LGA, ward or polling unit", request=G.AliasCreate, responses={201: G.AliasSchema})
    def post(self, request):
        ser = G.AliasCreate(data=request.data)
        ser.is_valid(raise_exception=True)
        v = ser.validated_data
        a = services.add_alias(level=v["level"], target_id=v["target_id"], name=v["name"], user=request.user, request=request)
        return Response(_alias(Alias.objects.select_related("lga", "ward", "polling_unit").get(pk=a.pk)), status=201)


class AdminAlias(_Private):
    permission_classes = [IsSystemAdmin]
    throttle_classes = [AdminWriteThrottle]

    @extend_schema(summary="Remove an alias", responses={204: None})
    def delete(self, request, pk):
        services.remove_alias(pk, user=request.user, request=request)
        return Response(status=204)

