"""
Public search and state pages (spec 12 §7). Anonymous on purpose: no session is read, so a
search can't be tied to an account (rule 3.3). Cacheable for 60 s; per-IP throttle.
"""
from django.db.models import Count, Q
from django.http import Http404
from django.utils.cache import patch_cache_control
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from apps.candidates.reference import POSITION_ORDER, Position, State
from apps.candidates.serializers import PublicCandidateListSerializer
from apps.news.serializers import PublicArticleSerializer
from apps.posts.serializers import FeedPostSerializer

from . import services
from .index import _public_articles, _public_candidates, _public_posts
from .models import Kind
from .services import snippet, words_for

ORDER_SIZE = 20


class SearchThrottle(AnonRateThrottle):
    scope = "search_public"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class _Public(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [SearchThrottle]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        if response.status_code == 200:
            patch_cache_control(response, public=True, max_age=60)
        return response


def _by_ids(qs, ids):
    objs = {o.pk: o for o in qs.filter(pk__in=ids)}
    return [objs[i] for i in ids if i in objs]


def _candidates(entries, request):
    objs = _by_ids(_public_candidates(), [e.object_id for e in entries])
    return PublicCandidateListSerializer(objs, many=True, context={"request": request}).data


def _articles(entries, words, request):
    objs = _by_ids(_public_articles().select_related("author"), [e.object_id for e in entries])
    out = []
    for a, data in zip(objs, PublicArticleSerializer(objs, many=True, context={"request": request}).data):
        snip, ms = snippet(a.body, words)
        out.append({**data, "snippet": snip, "matches": ms, "title_matches": services._matches(a.title, words),
                    "url": f"/news/{a.source}/{a.id}"})
    return out


def _posts(entries, words, request):
    objs = _by_ids(_public_posts(), [e.object_id for e in entries])
    out = []
    for p, data in zip(objs, FeedPostSerializer(objs, many=True, context={"request": request}).data):
        snip, ms = snippet(p.body, words)
        out.append({**data, "snippet": snip, "matches": ms, "title_matches": services._matches(p.title, words),
                    "url": f"/candidates/{p.candidate.slug}/posts/{p.id}"})
    return out


def _render(kind, entries, words, request):
    if kind == Kind.CANDIDATE:
        return _candidates(entries, request)
    if kind == Kind.ARTICLE:
        return _articles(entries, words, request)
    return _posts(entries, words, request)


class SearchView(_Public):
    def get(self, request):
        qp = request.query_params
        type_ = qp.get("type", "all") or "all"
        year = services.validate_filters(qp.get("state"), qp.get("position"), qp.get("year"), qp.get("source"), qp.get("since"), type_)
        filters = {"state": qp.get("state") or None, "position": qp.get("position") or None,
                   "year": year or services.current_year(), "source": qp.get("source") or None, "since": qp.get("since") or None}
        q = services.clean_query(qp.get("q", ""))
        words = words_for(q)
        if type_ == "all":
            out, dym = {}, None
            for name, kind in services.TYPES.items():
                count, rows, d = services.search(q, kind, page=1, size=5, **filters)
                out[name] = {"count": count, "results": _render(kind, rows, words, request)}
                dym = dym or d
            return Response({**out, "did_you_mean": dym, "election_year": filters["year"]})
        try:
            page = max(1, int(qp.get("page", 1)))
        except ValueError:
            page = 1
        kind = services.TYPES[type_]
        count, rows, dym = services.search(q, kind, page=page, size=ORDER_SIZE, **filters)
        if page > 1 and not rows:
            raise Http404
        more = page * ORDER_SIZE < count
        return Response({"count": count, "page": page, "next": page + 1 if more else None, "previous": page - 1 if page > 1 else None,
                         "results": _render(kind, rows, words, request), "did_you_mean": dym, "election_year": filters["year"]})


class SuggestView(_Public):
    def get(self, request):
        cands, titles = services.suggest(request.query_params.get("q", ""))
        objs = _by_ids(_public_candidates(), [e.object_id for e in cands])
        out_titles = []
        for e in titles:
            if e.kind == Kind.ARTICLE:
                a = _public_articles().filter(pk=e.object_id).first()
                if a:
                    out_titles.append({"type": "news", "title": a.title, "url": f"/news/{a.source}/{a.id}"})
            else:
                p = _public_posts().filter(pk=e.object_id).first()
                if p:
                    out_titles.append({"type": "post", "title": p.title, "candidate": p.candidate.full_name,
                                       "url": f"/candidates/{p.candidate.slug}/posts/{p.id}"})
        return Response({"candidates": PublicCandidateListSerializer(objs, many=True, context={"request": request}).data,
                         "titles": out_titles})


def _state_list():
    return [s for s in State]


class StatesView(_Public):
    def get(self, request):
        year = services.current_year()
        cands = dict(_public_candidates().filter(election_year=year).exclude(state="").values_list("state").annotate(n=Count("id")))
        arts = dict(_public_articles().exclude(state="").values_list("state").annotate(n=Count("id")))
        posts = dict(_public_posts().exclude(candidate__state="").values_list("candidate__state").annotate(n=Count("id")))
        return Response({"election_year": year, "results": [
            {"state": s.value, "label": s.label, "candidates": cands.get(s.value, 0), "news": arts.get(s.value, 0), "posts": posts.get(s.value, 0)}
            for s in sorted(_state_list(), key=lambda s: s.label)]})


class StateDetailView(_Public):
    GROUP = 6

    def get(self, request, state):
        if state not in State.values:
            raise Http404
        year = services.current_year()
        cands = _public_candidates().filter(election_year=year)
        groups = []
        national = cands.filter(position=Position.PRESIDENT).order_by("full_name")
        if national.exists():
            groups.append({"position": "president", "label": "National", "total": national.count(),
                           "results": PublicCandidateListSerializer(national[: self.GROUP], many=True, context={"request": request}).data})
        local = cands.filter(state=state)
        for p in POSITION_ORDER:
            if p == Position.PRESIDENT:
                continue
            qs = local.filter(position=p).order_by("area_name", "ward_name", "full_name")
            n = qs.count()
            if n:
                groups.append({"position": p, "label": Position(p).label, "total": n,
                               "results": PublicCandidateListSerializer(qs[: self.GROUP], many=True, context={"request": request}).data})
        news = _public_articles().filter(Q(state=state) | Q(state="")).select_related("author").order_by("-published_at")[:5]
        posts = _public_posts().filter(candidate__state=state).order_by("-published_at")[:5]
        return Response({
            "state": state, "label": State(state).label, "election_year": year, "groups": groups,
            "news": PublicArticleSerializer(news, many=True, context={"request": request}).data,
            "posts": FeedPostSerializer(posts, many=True, context={"request": request}).data,
        })
