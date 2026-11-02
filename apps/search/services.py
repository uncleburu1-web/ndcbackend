"""
Search (spec 12 §6.4). Public items only, neutral ranking (text relevance, then fixed tie-breaks),
plain-text snippets with match offsets. Nothing here stores or logs the query.
"""
import re
from datetime import timedelta

from django.contrib.postgres.search import SearchQuery, SearchRank, TrigramStrictWordSimilarity
from django.db import OperationalError, connection, transaction
from django.db.models import Case, F, FloatField, Max, Q, Value, When
from django.db.models.functions import Greatest
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.candidates.reference import Position, State
from apps.core.exceptions import ServiceError

from .index import public_ids
from .models import Kind, SearchEntry
from .text import fold, fold_with_map, sound_key, tokens

Q_MIN, Q_MAX = 2, 100
SIMILAR = 0.35  # strict word similarity; calibrated on Nigerian name variants (spec 12 §6.4)
SUGGEST = 0.2
SNIPPET = 200
TIMEOUT_MS = 2000
TYPES = {"candidates": Kind.CANDIDATE, "news": Kind.ARTICLE, "posts": Kind.POST}
SINCE = {"week": 7, "month": 30, "year": 365}
SOURCES = {"official", "community"}


def clean_query(q: str) -> str:
    q = (q or "").strip()
    if len(q) > Q_MAX:
        raise ServiceError(f"Keep searches under {Q_MAX} characters.", code="query_too_long", status_code=400)
    want, _ = tokens(q)
    if len(q) < Q_MIN or not want or len("".join(want)) < Q_MIN:
        raise ServiceError(f"Type at least {Q_MIN} characters.", code="query_too_short", status_code=400)
    return q


def validate_filters(state=None, position=None, year=None, source=None, since=None, type_=None):
    bad = (
        (state and state not in State.values) or (position and position not in Position.values)
        or (source and source not in SOURCES) or (since and since not in SINCE)
        or (type_ and type_ not in ("all", *TYPES))
    )
    if bad:
        raise ServiceError("One of the filters isn't valid.", code="invalid_filter", status_code=400)
    if year not in (None, ""):
        try:
            return int(year)
        except (TypeError, ValueError):
            raise ServiceError("One of the filters isn't valid.", code="invalid_filter", status_code=400) from None
    return None


def current_year():
    from apps.search.index import _public_candidates

    return _public_candidates().aggregate(y=Max("election_year"))["y"]


def _tsquery(q: str):
    want, drop = tokens(q)
    parts = list(want[:-1]) + [f"{want[-1]}:*"]  # prefix on the last word, as people type
    raw = " & ".join(parts) + "".join(f" & !{d}" for d in drop)
    return SearchQuery(raw, search_type="raw", config="simple")


def _base(kind, q, *, state=None, position=None, year=None, source=None, since=None):
    folded = " ".join(tokens(q)[0])
    query = _tsquery(q)
    qs = SearchEntry.objects.filter(kind=kind, object_id__in=public_ids(kind))  # re-checked at query time (spec 12 §9)
    rank = SearchRank(F("document"), query, cover_density=True)
    if kind == Kind.CANDIDATE:
        # Strict word similarity: how well the query matches whole words of the name, so name length never changes order.
        # Sound keys catch spelling variants trigrams miss on one word ("mohammed" → Muhammad).
        keys = [k for k in (sound_key(w) for w in tokens(q)[0] if len(w) >= 4) if len(k) >= 3]
        key_q = Q()
        for k in keys:
            key_q &= Q(title_key__regex=rf"(^| ){k}( |$)")
        qs = qs.annotate(trgm=TrigramStrictWordSimilarity(folded, "folded_title"),
                         key=Case(When(key_q, then=Value(0.4)), default=Value(0.0), output_field=FloatField()) if keys
                         else Value(0.0, output_field=FloatField()))
        qs = qs.annotate(sim=Greatest("trgm", "key"))
        qs = qs.filter(Q(document=query) | Q(sim__gt=SIMILAR))
        qs = qs.annotate(score=Coalesce(rank, Value(0.0), output_field=FloatField()) + F("sim") * 0.5)
        qs = qs.filter(election_year=year) if year else qs
        if position:
            qs = qs.filter(position=position)
        order = ["-score", "rank_order", "title"]
    else:
        qs = qs.filter(document=query).annotate(score=rank)
        if since:
            qs = qs.filter(published_at__gte=timezone.now() - timedelta(days=SINCE[since]))
        if source and kind == Kind.ARTICLE:
            qs = qs.filter(source=source)
        order = ["-score", "-published_at"]
    if state:
        qs = qs.filter(Q(state=state) | Q(state=""))  # national items (and presidential candidates) belong to every state
    return qs.order_by(*order)


def _matches(text: str, words: list[str]) -> list[list[int]]:
    """Offsets of word-prefix matches in the ORIGINAL text, found on its folded form."""
    folded, idx = fold_with_map(text)
    out = []
    for w in words:
        for m in re.finditer(r"(?<![a-z0-9])" + re.escape(w), folded):
            start, end = idx[m.start()], idx[m.end() - 1] + 1
            # extend to the end of the original word so the whole word is marked
            while end < len(text) and text[end].isalnum():
                end += 1
            out.append([start, end])
    out.sort()
    merged = []
    for s, e in out:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return merged


def snippet(body: str, words: list[str]) -> tuple[str, list[list[int]]]:
    body = re.sub(r"\s+", " ", body or "").strip()
    if not body:
        return "", []
    ms = _matches(body, words)
    if not ms:
        cut = body[:SNIPPET]
        return (cut + ("…" if len(body) > SNIPPET else "")), []
    start = max(0, ms[0][0] - 60)
    if start:
        sp = body.rfind(" ", 0, start)
        start = sp + 1 if sp != -1 else start
    end = min(len(body), start + SNIPPET)
    text = ("…" if start else "") + body[start:end] + ("…" if end < len(body) else "")
    shift = (1 if start else 0) - start
    return text, [[s + shift, min(e, end) + shift] for s, e in ms if s >= start and s < end]


def _did_you_mean(q, year):
    folded = " ".join(tokens(q)[0])
    qs = SearchEntry.objects.filter(kind=Kind.CANDIDATE, object_id__in=public_ids(Kind.CANDIDATE))
    if year:
        qs = qs.filter(election_year=year)
    best = qs.annotate(sim=TrigramStrictWordSimilarity(folded, "folded_title")).filter(sim__gt=SUGGEST).order_by("-sim", "title").first()
    return best.title if best else None


def _run(fn):
    """2-second statement timeout on search queries (spec 12 §11)."""
    try:
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = %s", [TIMEOUT_MS])
            return fn()
    except OperationalError as exc:
        if "statement timeout" in str(exc) or "canceling statement" in str(exc):
            raise ServiceError("Search is busy. Try again in a moment.", code="search_unavailable", status_code=503) from exc
        raise


def search(q, kind, *, page=1, size=20, **filters):
    """Returns (count, entries for the page, did_you_mean)."""
    q = clean_query(q)

    def go():
        qs = _base(kind, q, **filters)
        count = qs.count()
        rows = list(qs[(page - 1) * size: page * size])
        dym = _did_you_mean(q, filters.get("year")) if kind == Kind.CANDIDATE and count == 0 else None
        return count, rows, dym

    return _run(go)


def words_for(q):
    return tokens(q)[0]


def suggest(q):
    q = clean_query(q)
    year = current_year()

    def go():
        cands = list(_base(Kind.CANDIDATE, q, year=year)[:5])
        titles = list(_base(Kind.ARTICLE, q)[:3]) + list(_base(Kind.POST, q)[:3])
        titles.sort(key=lambda e: -float(e.score or 0))
        return cands, titles[:3]

    return _run(go)


def ranked_candidate_ids(q, *, state=None, position=None, year=None, limit=500):
    """For /candidates?q= (spec 12 §4.3): matching candidate IDs in search order."""
    q = clean_query(q)
    return _run(lambda: list(_base(Kind.CANDIDATE, q, state=state, position=position, year=year)
                             .values_list("object_id", flat=True)[:limit]))
