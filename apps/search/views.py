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
from .index import _public_articles, _public_candidates, _public_events, _public_posts
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


def _events(entries, words, request):
    from apps.events.serializers import card

    objs = _by_ids(_public_events(), [e.object_id for e in entries])
    out = []
    for ev in objs:
        snip, ms = snippet(ev.description, words)
        out.append({**card(ev), "snippet": snip, "matches": ms, "title_matches": services._matches(ev.title, words), "url": f"/events/{ev.id}"})
    return out


def _places(entries):
    """Spec 19 §9: "Ikeja, Lagos State" → /states/lagos/ikeja. Rebuilt from active rows, never from the entry alone."""
    from apps.geo.models import Lga
    from apps.geo.models import State as GeoState

    from .index import geo_id

    states = {geo_id(Kind.STATE, s.pk): s for s in GeoState.objects.all()}
    lgas = {geo_id(Kind.LGA, l_.pk): l_ for l_ in Lga.objects.filter(active=True).select_related("state")}
    out = []
    for e in entries:
        if e.kind == Kind.STATE and e.object_id in states:
            out.append({"kind": "state", "title": e.title, "state": e.state, "url": f"/states/{states[e.object_id].slug}"})
        elif e.kind == Kind.LGA and e.object_id in lgas:
            l_ = lgas[e.object_id]
            out.append({"kind": "lga", "title": e.title, "state": e.state, "url": f"/states/{l_.state.slug}/{l_.slug}"})
    return out


def _render(kind, entries, words, request):
    if kind == Kind.CANDIDATE:
        return _candidates(entries, request)
    if kind == Kind.ARTICLE:
        return _articles(entries, words, request)
    if kind == Kind.EVENT:
        return _events(entries, words, request)
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
            n, places = services.search_places(q, state=filters["state"])  # spec 19 §9
            out["places"] = {"count": n, "results": _places(places)}
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
            elif e.kind == Kind.EVENT:
                ev = _public_events().filter(pk=e.object_id).first()
                if ev:
                    out_titles.append({"type": "event", "title": ev.title, "url": f"/events/{ev.id}"})
            else:
                p = _public_posts().filter(pk=e.object_id).first()
                if p:
                    out_titles.append({"type": "post", "title": p.title, "candidate": p.candidate.full_name,
                                       "url": f"/candidates/{p.candidate.slug}/posts/{p.id}"})
        _, places = services.search_places(request.query_params.get("q", ""), limit=3)  # spec 19 §9
        return Response({"candidates": PublicCandidateListSerializer(objs, many=True, context={"request": request}).data,
                         "titles": out_titles, "places": _places(places)})


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
        from apps.events.serializers import card
        from apps.events.services import upcoming

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
            "events": [card(e) for e in upcoming(state=state, limit=5)],  # spec 18 §5
            **_geography(state, cands),  # spec 19 §6.1
        })


def _geography(state, cands):
    """Spec 19 §6.1: the state's LGAs with counts, its constituencies, the capital and "About this data"."""
    from django.utils import timezone

    from apps.events.models import EventStatus
    from apps.events.services import public_events
    from apps.geo import services as geo
    from apps.geo.models import Constituency, Lga
    from apps.geo.models import State as GeoState

    st = GeoState.objects.filter(slug=state).first()
    if st is None:
        return {"capital": "", "area_councils": state == "fct", "lgas": [], "constituencies": [], "about": None}
    lgas = list(Lga.objects.filter(state=st, active=True).order_by("name"))
    cons = list(Constituency.objects.filter(state=st, active=True).prefetch_related("areas__ward").order_by("kind", "name"))
    covers = {}  # constituency → the LGA IDs it touches
    for c in cons:
        covers[c.pk] = {a.lga_id or a.ward.lga_id for a in c.areas.all()}
    local = dict(cands.filter(geo_lga__in=lgas).values_list("geo_lga").annotate(n=Count("id")))
    by_con = dict(cands.filter(geo_constituency__in=cons).values_list("geo_constituency").annotate(n=Count("id")))
    events = dict(public_events().filter(status=EventStatus.PUBLISHED, ends_at__gt=timezone.now(), geo_lga__in=lgas)
                  .values_list("geo_lga").annotate(n=Count("id")))
    out_lgas = []
    for l_ in lgas:
        n = local.get(l_.pk, 0) + sum(by_con.get(c.pk, 0) for c in cons if l_.pk in covers[c.pk])
        out_lgas.append({"id": l_.pk, "code": l_.code, "name": l_.name, "slug": l_.slug, "candidates": n, "events": events.get(l_.pk, 0)})
    names = {l_.pk: l_.name for l_ in lgas}
    about = next((d for d in geo.current_datasets() if d.kind == "polling_units"), None)
    return {
        "capital": st.capital, "area_councils": state == "fct", "lgas": out_lgas,
        "constituencies": [{"id": c.pk, "kind": c.kind, "code": c.code, "name": c.name, "candidates": by_con.get(c.pk, 0),
                            "lgas": sorted(names[i] for i in covers[c.pk] if i in names)} for c in cons],
        "about": {"source": about.source, "published_on": about.published_on, "polling_units": about.polling_units} if about else None,
    }
