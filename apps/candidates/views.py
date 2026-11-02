"""Public and admin candidate API — spec 03 §7."""
from django.db.models import Case, Count, IntegerField, Max, Q, Value, When
from django.shortcuts import get_object_or_404
from django.utils.cache import patch_cache_control
from rest_framework import generics, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from apps.access.permissions import IsSystemAdmin
from apps.core.exceptions import ServiceError

from . import services
from .models import Candidate, CandidateStatus
from .photos import PhotoError
from .reference import POSITION_ORDER, Position
from .serializers import (
    AdminCandidateSerializer,
    CandidateWriteSerializer,
    LinkAccountSerializer,
    LinkedAccountSerializer,
    NotesSerializer,
    PublicCandidateListSerializer,
    PublicCandidateSerializer,
    ReasonSerializer,
)

POSITION_RANK = Case(*[When(position=p, then=Value(i)) for i, p in enumerate(POSITION_ORDER)], output_field=IntegerField())


class PublicThrottle(AnonRateThrottle):
    """Per-IP for everyone (signed in or not) — the public pages are the scraping target."""

    scope = "candidates_public"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class Page20(PageNumberPagination):
    page_size = 20


def _published():
    """Enforced in the queryset, not just the serializer (§9)."""
    return Candidate.objects.filter(status=CandidateStatus.PUBLISHED, verified_at__isnull=False)


def _latest_year():
    return _published().aggregate(y=Max("election_year"))["y"]


def _public_cache(response):
    patch_cache_control(response, public=True, max_age=60)
    return response


def _year_param(request):
    raw = request.query_params.get("election_year")
    if raw:
        try:
            return int(raw)
        except ValueError:
            return None
    return _latest_year()


# --------------------------------------------------------------------- public
class PublicCandidateList(generics.ListAPIView):
    """GET /candidates/ — published only; filters position/state/election_year/q (§7.1)."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [PublicThrottle]
    serializer_class = PublicCandidateListSerializer
    pagination_class = Page20

    def get_queryset(self):
        qp = self.request.query_params
        qs = _published().annotate(rank=POSITION_RANK)
        year = _year_param(self.request)
        qs = qs.filter(election_year=year) if year else qs.none()
        if qp.get("position") in POSITION_ORDER:
            qs = qs.filter(position=qp["position"])
        if qp.get("state"):
            qs = qs.filter(state=qp["state"])
        q = (qp.get("q") or "").strip()[:100]
        if q:
            # Spec 12 §4.3: same engine as /search (diacritics, aliases, name typos); relevance order with fixed tie-breaks.
            from apps.search.services import ranked_candidate_ids

            try:
                ids = ranked_candidate_ids(q, year=year)
            except ServiceError:  # a single character: keep the old prefix-style match
                return qs.filter(Q(full_name__icontains=q) | Q(area_name__icontains=q)).order_by("rank", "state", "full_name", "id")
            order = Case(*[When(pk=pk, then=Value(i)) for i, pk in enumerate(ids)], output_field=IntegerField())
            return qs.filter(pk__in=ids).annotate(search_order=order).order_by("search_order")
        return qs.order_by("rank", "state", "full_name", "id")

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        response.data["election_year"] = _year_param(request)
        return _public_cache(response)


class PublicPositions(APIView):
    """GET /candidates/positions/ — positions with published candidates, with counts (§7.1)."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [PublicThrottle]

    def get(self, request):
        year = _year_param(request)
        qs = _published().filter(election_year=year) if year else Candidate.objects.none()
        if request.query_params.get("state"):
            qs = qs.filter(state=request.query_params["state"])
        counts = dict(qs.values_list("position").annotate(n=Count("id")))
        data = [{"position": p, "label": Position(p).label, "count": counts[p]} for p in POSITION_ORDER if counts.get(p)]
        years = sorted(set(_published().values_list("election_year", flat=True)), reverse=True)
        return _public_cache(Response({"election_year": year, "election_years": years, "positions": data}))


class PublicCandidateDetail(generics.RetrieveAPIView):
    """GET /candidates/{slug}/ — published only, else the same 404 for every other case (§3.3)."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [PublicThrottle]
    serializer_class = PublicCandidateSerializer
    lookup_field = "slug"

    def get_queryset(self):
        return _published()

    def retrieve(self, request, *args, **kwargs):
        return _public_cache(super().retrieve(request, *args, **kwargs))


# ---------------------------------------------------------------------- admin
def _admin_data(c, request):
    """Single-record admin responses also carry follower counts — counts only, never who (spec 04 §7.4)."""
    from apps.follows.services import follower_counts

    data = AdminCandidateSerializer(c, context={"request": request}).data
    data["followers"] = follower_counts(c)
    return data


class AdminCandidateList(APIView):
    permission_classes = [IsSystemAdmin]

    def get(self, request):
        qp = request.query_params
        qs = Candidate.objects.annotate(rank=POSITION_RANK).select_related("verified_by")
        for f in ("status", "position", "state"):
            if qp.get(f):
                qs = qs.filter(**{f: qp[f]})
        q = (qp.get("q") or "").strip()[:100]
        if q:
            qs = qs.filter(Q(full_name__icontains=q) | Q(area_name__icontains=q) | Q(slug__icontains=q))
        qs = qs.order_by("-updated_at")
        paginator = Page20()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(AdminCandidateSerializer(page, many=True, context={"request": request}).data)

    def post(self, request):
        ser = CandidateWriteSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        c = services.create_candidate(data=ser.validated_data, actor=request.user, request=request)
        return Response(_admin_data(c, request), status=status.HTTP_201_CREATED)


class AdminCandidateDetail(APIView):
    permission_classes = [IsSystemAdmin]

    def get(self, request, pk):
        return Response(_admin_data(get_object_or_404(Candidate, pk=pk), request))

    def patch(self, request, pk):
        c = get_object_or_404(Candidate, pk=pk)
        ser = CandidateWriteSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        c, unpublished = services.update_candidate(candidate=c, data=ser.validated_data, actor=request.user, request=request)
        data = _admin_data(c, request)
        data["unpublished_by_edit"] = unpublished
        return Response(data)


class AdminCandidateAction(APIView):
    """POST /admin/candidates/{id}/<action>/ — verify, unverify, publish, unpublish, archive, restore."""

    permission_classes = [IsSystemAdmin]
    action_name = ""

    def post(self, request, pk):
        c = get_object_or_404(Candidate, pk=pk)
        kw = {"candidate": c, "actor": request.user, "request": request}
        if self.action_name == "verify":
            notes = NotesSerializer(data=request.data)
            notes.is_valid(raise_exception=True)
            c = services.verify(notes=notes.validated_data.get("notes", ""), **kw)
        elif self.action_name in ("unpublish", "archive"):
            reason = ReasonSerializer(data=request.data)
            reason.is_valid(raise_exception=True)
            c = getattr(services, self.action_name)(reason=reason.validated_data.get("reason", ""), **kw)
        else:
            c = getattr(services, self.action_name)(**kw)
        return Response(_admin_data(c, request))


class AdminCandidatePhoto(APIView):
    permission_classes = [IsSystemAdmin]
    parser_classes = [MultiPartParser, FormParser]

    def put(self, request, pk):
        c = get_object_or_404(Candidate, pk=pk)
        upload = request.FILES.get("photo")
        if upload is None:
            return Response({"error": {"status": 400, "code": "validation_error", "message": "Choose a photo to upload.",
                                       "fields": {"photo": ["Choose a photo to upload."]}}}, status=400)
        confirmed = str(request.data.get("rights_confirmed", "")).lower() in ("true", "1", "on", "yes")
        try:
            c = services.set_photo(candidate=c, upload=upload, rights_confirmed=confirmed, actor=request.user, request=request)
        except PhotoError as exc:
            return Response({"error": {"status": 400, "code": "invalid_photo", "message": str(exc),
                                       "fields": {"photo": [str(exc)]}}}, status=400)
        except (OSError, IOError) as exc:  # storage unavailable (§11)
            raise ServiceError("Photo storage is unavailable right now. The candidate was saved without the new photo; try again later.",
                               code="storage_unavailable", status_code=503) from exc
        return Response(_admin_data(c, request))

    def delete(self, request, pk):
        c = services.remove_photo(candidate=get_object_or_404(Candidate, pk=pk), actor=request.user, request=request)
        return Response(_admin_data(c, request))


class AdminCandidateAccounts(APIView):
    permission_classes = [IsSystemAdmin]

    def get(self, request, pk):
        c = get_object_or_404(Candidate, pk=pk)
        return Response(LinkedAccountSerializer(services.linked_accounts(c), many=True).data)

    def post(self, request, pk):
        c = get_object_or_404(Candidate, pk=pk)
        ser = LinkAccountSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        a = services.link_account(candidate=c, user=ser.validated_data["email"], actor=request.user)
        return Response(LinkedAccountSerializer(a).data, status=status.HTTP_201_CREATED)


class AdminCandidateAccountDetail(APIView):
    permission_classes = [IsSystemAdmin]

    def delete(self, request, pk, user_id):
        services.unlink_account(candidate=get_object_or_404(Candidate, pk=pk), user_id=user_id, actor=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)
