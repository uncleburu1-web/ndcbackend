"""Spec 12 §12 — search and discovery (backend)."""
import logging
import time
from io import StringIO

from django.core.cache import cache
from django.core.management import call_command
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.access.models import Role
from apps.access.services import grant_role
from apps.accounts.models import User
from apps.candidates import services as cand_services
from apps.candidates.models import Candidate
from apps.core.logging import RedactSearchQuery
from apps.core.models import JobRun
from apps.follows.models import CandidateFollow
from apps.news import announcements
from apps.news import services as news_services
from apps.news.models import Article
from apps.posts import services as post_services
from apps.posts.models import CandidatePost
from apps.search import index, services
from apps.search.models import Kind, SearchEntry
from apps.search.text import fold, fold_with_map, tokens

NOW = timezone.now


def mk_user(cid, **kw):
    return User.objects.create_user(cid, email=f"{cid}@example.com", full_name=kw.pop("full_name", "Ada Obi"),
                                    phone_number="+2348031234567", profile_completed_at=timezone.now(), adult_confirmed_at=timezone.now(), **kw)


def mk_candidate(slug, name, position="governor", state="kano", year=2027, published=True, **kw):
    return Candidate.objects.create(slug=slug, full_name=name, position=position, election_year=year, state=state,
                                    status="published" if published else "draft", verified_at=timezone.now() if published else None,
                                    published_at=timezone.now() if published else None, **kw)


def mk_article(title, body="Polling units opened on time across the state this morning.", state="", source="official", status="successful",
               author=None, published_at=None):
    return Article.objects.create(source=source, status=status, title=title, body=body, state=state, author=author,
                                  category="party" if source == "official" else "", submitted_at=timezone.now(),
                                  published_at=published_at or (timezone.now() if status == "successful" else None),
                                  removed_at=timezone.now() if status == "removed" else None)


def mk_post(c, title, body="Join us at the town hall on Saturday."):
    return CandidatePost.objects.create(candidate=c, kind="news", title=title, body=body, published_at=timezone.now())


def get(path, **params):
    cache.clear()
    return APIClient().get(path, params)


class FoldTests(TestCase):
    def test_fold(self):
        self.assertEqual(fold("Ọ̀ṣun"), "osun")
        self.assertEqual(fold("Ìbàdàn"), "ibadan")
        self.assertEqual(fold("Ndị Igbo"), "ndi igbo")
        self.assertEqual(fold("Ahmadu  BELLO"), "ahmadu bello")
        self.assertEqual(fold("Akwa-Ibom O'Neil"), "akwa ibom o neil")
        self.assertEqual(fold("Straße"), "strasse")

    def test_map_points_back_to_the_original(self):
        text = "Gov. Ọ̀ṣun"
        folded, idx = fold_with_map(text)
        i = folded.index("osun")
        self.assertEqual(text[idx[i]: idx[i + 3] + 1], "Ọ̀ṣun"[: idx[i + 3] + 1 - idx[i]])
        self.assertEqual(text[idx[i]], "Ọ")

    def test_tokens(self):
        self.assertEqual(tokens('"Amina" bello -kano <script>'), (["amina", "bello", "script"], ["kano"]))


class MatchingTests(TestCase):
    def setUp(self):
        self.osun = mk_candidate("adeyemi-osun", "Adéyẹmí Ọ̀ṣúnlọ́lá", state="osun")
        self.muh = mk_candidate("muhammad-bello", "Muhammad Bello", state="kano")
        self.ade = mk_candidate("adebayo-ola", "Adebayo Ola", position="senator", state="lagos", area_name="Lagos Central")
        self.fct = mk_candidate("fct-chair", "Zainab Musa", position="lga_chairman", state="fct", area_name="Abaji")
        self.year = 2027

    def titles(self, q, kind=Kind.CANDIDATE, **f):
        f.setdefault("year", self.year if kind == Kind.CANDIDATE else None)
        return [e.title for e in services.search(q, kind, **f)[1]]

    def test_diacritics_typos_aliases_prefix(self):
        self.assertIn(self.osun.full_name, self.titles("osunlola"))
        self.assertIn("Muhammad Bello", self.titles("mohammed bello"))  # trigram
        self.assertEqual(self.titles("guber kano"), ["Muhammad Bello"])
        self.assertIn("Zainab Musa", self.titles("abuja"))
        self.assertEqual(self.titles("adeb"), ["Adebayo Ola"])  # prefix while typing
        self.assertIn("Adebayo Ola", self.titles("reps") + self.titles("senate lagos"))

    def test_exact_name_ranks_first(self):
        mk_candidate("bello-musa", "Bello Musa", state="kano", position="senator")
        self.assertEqual(self.titles("muhammad bello")[0], "Muhammad Bello")

    def test_did_you_mean(self):
        count, rows, dym = services.search("muhamad bellow", Kind.CANDIDATE, year=self.year)
        self.assertTrue(count >= 1 or dym == "Muhammad Bello")
        count, rows, dym = services.search("zzzz qqqq", Kind.CANDIDATE, year=self.year)
        self.assertEqual((count, dym), (0, None))

    def test_query_limits(self):
        for q, code in (("a", "query_too_short"), ("x" * 101, "query_too_long"), ("--", "query_too_short")):
            with self.assertRaises(services.ServiceError) as e:
                services.clean_query(q)
            self.assertEqual(e.exception.get_codes(), code)


class PublicOnlyTests(TestCase):
    def setUp(self):
        self.admin = mk_user("u_sa", full_name="Sam Admin")
        grant_role(user=self.admin, role=Role.SYSTEM_ADMIN)

    def kinds(self, q):
        return {k: [e.title for e in services.search(q, k, year=2027 if k == Kind.CANDIDATE else None)[1]] for k in Kind}

    def test_non_public_items_never_appear(self):
        mk_candidate("draft-zebra", "Zebra Draft", published=False)
        mk_article("Zebra pending", source="community", status="pending")
        mk_article("Zebra rejected", source="community", status="rejected")
        mk_article("Zebra removed", status="removed")
        hidden = mk_candidate("hidden-c", "Hidden Person")
        p = mk_post(hidden, "Zebra post")
        mk_post(hidden, "Zebra deleted post").__class__.objects.filter(title="Zebra deleted post").update(status="deleted",
                                                                                                         deleted_at=timezone.now())
        Candidate.objects.filter(pk=hidden.pk).update(status="unpublished")  # bypasses signals: the query-time check catches it
        res = self.kinds("zebra")
        self.assertEqual(res, {Kind.CANDIDATE: [], Kind.ARTICLE: [], Kind.POST: [], Kind.EVENT: []})
        self.assertTrue(SearchEntry.objects.filter(kind=Kind.POST, object_id=p.pk).exists())  # planted entry still there…
        # …and a hand-planted entry for a draft is ignored too
        draft = Candidate.objects.get(slug="draft-zebra")
        SearchEntry.objects.create(kind=Kind.CANDIDATE, object_id=draft.pk, title=draft.full_name, folded_title="zebra draft")
        self.assertEqual(self.kinds("zebra")[Kind.CANDIDATE], [])

    def test_lifecycle_follows_services(self):
        c = mk_candidate("amina", "Amina Lifecycle")
        self.assertTrue(SearchEntry.objects.filter(kind=Kind.CANDIDATE, object_id=c.pk).exists())
        post = post_services.create_post(c, self._linked(c), {"kind": "news", "title": "Lifecycle rally", "body": "Come."})
        self.assertTrue(SearchEntry.objects.filter(kind=Kind.POST, object_id=post.pk).exists())
        post_services.remove_post(post, self.admin, "Abusive or inciting language", rule="R2")
        self.assertFalse(SearchEntry.objects.filter(kind=Kind.POST, object_id=post.pk).exists())
        post_services.restore_post(CandidatePost.objects.get(pk=post.pk), self.admin)
        self.assertTrue(SearchEntry.objects.filter(kind=Kind.POST, object_id=post.pk).exists())
        cand_services.unpublish(candidate=c, reason="Paused", actor=self.admin)
        self.assertFalse(SearchEntry.objects.filter(kind=Kind.CANDIDATE, object_id=c.pk).exists())
        self.assertFalse(SearchEntry.objects.filter(kind=Kind.POST, object_id=post.pk).exists())  # posts follow the candidate

    def test_articles_follow_review_and_announcements(self):
        author = mk_user("u_author")
        reviewer = mk_user("u_rev")
        grant_role(user=reviewer, role=Role.NEWS_REVIEWER)
        a = mk_article("Lifecycle community story", source="community", status="pending", author=author)
        self.assertFalse(SearchEntry.objects.filter(object_id=a.pk).exists())
        news_services.approve(a, reviewer)
        self.assertTrue(SearchEntry.objects.filter(object_id=a.pk).exists())
        news_services.remove(Article.objects.get(pk=a.pk), self.admin, "False", rule="R4")
        self.assertFalse(SearchEntry.objects.filter(object_id=a.pk).exists())
        o = announcements.publish(self.admin, {"category": "party", "title": "Lifecycle announcement", "body": "Rally on Saturday."})
        self.assertTrue(SearchEntry.objects.filter(object_id=o.pk).exists())

    def test_rollback_leaves_no_entry(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                mk_candidate("rollback", "Rollback Person")
                raise RuntimeError
        self.assertFalse(SearchEntry.objects.filter(title="Rollback Person").exists())

    def _linked(self, c):
        from apps.access.models import ScopeType

        u = mk_user(f"u_camp_{c.slug}")
        grant_role(user=u, role=Role.CANDIDATE, scope_type=ScopeType.CANDIDATE, scope_id=c.pk)
        return u


class RankingFilterTests(TestCase):
    def test_ties_by_office_then_name_never_by_follows(self):
        a = mk_candidate("zed", "Zed Okafor", position="senator", state="enugu")
        b = mk_candidate("abe", "Abe Okafor", position="senator", state="enugu")
        g = mk_candidate("gov", "Gift Okafor", position="governor", state="enugu")
        for i in range(5):
            CandidateFollow.objects.create(user=mk_user(f"fan{i}"), candidate=a, notify=True)
        titles = [e.title for e in services.search("okafor", Kind.CANDIDATE, year=2027)[1]]
        self.assertEqual(titles, ["Gift Okafor", "Abe Okafor", "Zed Okafor"])
        self.assertEqual({a.pk, b.pk, g.pk}, set(SearchEntry.objects.filter(kind=Kind.CANDIDATE).values_list("object_id", flat=True)))

    def test_news_ties_by_date_and_no_official_boost(self):
        old = mk_article("Kano budget hearing", source="community", published_at=NOW() - timezone.timedelta(days=3))
        new = mk_article("Kano budget hearing", source="official", published_at=NOW())
        old.refresh_from_db()
        index.index_article(old)
        titles = [e.object_id for e in services.search("kano budget", Kind.ARTICLE)[1]]
        self.assertEqual(titles, [new.pk, old.pk])

    def test_filters(self):
        mk_candidate("pres", "Peter Nation", position="president", state="")
        mk_candidate("lag", "Peter Lagos", position="senator", state="lagos")
        mk_candidate("kan", "Peter Kano", position="senator", state="kano")
        mk_candidate("old", "Peter Past", position="senator", state="lagos", year=2023)
        t = lambda **f: sorted(e.title for e in services.search("peter", Kind.CANDIDATE, **f)[1])  # noqa: E731
        self.assertEqual(t(year=2027, state="lagos"), ["Peter Lagos", "Peter Nation"])  # presidential count as national
        self.assertEqual(t(year=2027, position="senator"), ["Peter Kano", "Peter Lagos"])
        self.assertEqual(t(year=2023), ["Peter Past"])
        mk_article("Peter national story")
        mk_article("Peter Lagos story", state="lagos", source="community")
        mk_article("Peter Kano story", state="kano")
        mk_article("Peter old story", published_at=NOW() - timezone.timedelta(days=60))
        a = lambda **f: sorted(e.title for e in services.search("peter", Kind.ARTICLE, **f)[1])  # noqa: E731
        self.assertEqual(a(state="lagos", since="month"), ["Peter Lagos story", "Peter national story"])
        self.assertEqual(a(source="community"), ["Peter Lagos story"])

    def test_snippet_offsets_map_to_original_text(self):
        body = "Supporters gathered in Ọ̀ṣogbo on Friday. The rally in Ọ̀ṣogbo was calm."
        text, ms = services.snippet(body, ["osogbo"])
        self.assertEqual([text[s:e] for s, e in ms], ["Ọ̀ṣogbo", "Ọ̀ṣogbo"])
        self.assertNotIn("<", text)


class ApiTests(APITestCase):
    def setUp(self):
        self.c = mk_candidate("amina-bello", "Amina Bello", state="kano")
        self.a = mk_article("Kano rally <script>alert(1)</script>", body="The rally in Kano was calm and orderly.", state="kano")
        self.p = mk_post(self.c, "Kano town hall", "Amina meets Kano traders.")

    def test_all_and_single_types(self):
        r = get(reverse("search"), q="kano")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn("public", r["Cache-Control"])
        self.assertEqual((r.data["candidates"]["count"], r.data["news"]["count"], r.data["posts"]["count"]), (1, 1, 1))
        news = r.data["news"]["results"][0]
        self.assertEqual(news["url"], f"/news/official/{self.a.pk}")
        self.assertIn("<script>", news["title"])  # plain text; the browser renders it as text
        self.assertTrue(news["matches"])
        post = r.data["posts"]["results"][0]
        self.assertEqual(post["candidate"]["slug"], "amina-bello")
        r = get(reverse("search"), q="kano", type="news")
        self.assertEqual(r.data["count"], 1)
        self.assertEqual(get(reverse("search"), q="kano", type="news", page=2).status_code, 404)

    def test_errors_and_anonymous(self):
        self.assertEqual(get(reverse("search"), q="k").data["error"]["code"], "query_too_short")
        self.assertEqual(get(reverse("search"), q="kano", state="atlantis").data["error"]["code"], "invalid_filter")
        self.assertEqual(get(reverse("search"), q="kano", type="people").data["error"]["code"], "invalid_filter")
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION="Bearer not-even-a-token")
        cache.clear()
        self.assertEqual(c.get(reverse("search"), {"q": "kano"}).status_code, 200)  # the header is never read

    def test_response_has_no_private_fields(self):
        text = str(get(reverse("search"), q="kano").data)
        for leak in ("@example.com", "status", "created_by", "verification_notes"):
            self.assertNotIn(leak, text)

    def test_suggest(self):
        r = get(reverse("search-suggest"), q="ami")
        self.assertEqual([c["full_name"] for c in r.data["candidates"]], ["Amina Bello"])
        r = get(reverse("search-suggest"), q="kano")
        self.assertTrue(any(t["url"].startswith("/candidates/amina-bello/posts/") for t in r.data["titles"]))

    def test_candidates_list_uses_the_engine(self):
        mk_candidate("muhammad-x", "Muhammad Sani", state="kano", position="senator")
        r = get(reverse("candidates"), q="mohammed")
        self.assertEqual([c["full_name"] for c in r.data["results"]], ["Muhammad Sani"])
        r = get(reverse("candidates"))
        self.assertEqual([c["full_name"] for c in r.data["results"]], ["Amina Bello", "Muhammad Sani"])  # browse order unchanged

    def test_states(self):
        mk_candidate("pres-x", "Peter Nation", position="president", state="")
        r = get(reverse("states"))
        kano = next(s for s in r.data["results"] if s["state"] == "kano")
        self.assertEqual((kano["candidates"], kano["news"], kano["posts"]), (1, 1, 1))
        self.assertEqual(len(r.data["results"]), 37)
        r = get(reverse("state-detail", args=["kano"]))
        self.assertEqual([g["label"] for g in r.data["groups"]], ["National", "Governor"])
        self.assertEqual(r.data["posts"][0]["title"], "Kano town hall")
        self.assertEqual(get(reverse("state-detail", args=["atlantis"])).status_code, 404)

    def test_throttled(self):
        from unittest import mock

        from apps.search.views import SearchThrottle

        with mock.patch.object(SearchThrottle, "THROTTLE_RATES", {"search_public": "2/min"}):
            self._throttled()

    def _throttled(self):
        cache.clear()
        c = APIClient()
        codes = [c.get(reverse("search"), {"q": f"kano{i}"}).status_code for i in range(3)]
        self.assertEqual(codes[-1], 429)


class HomeStateTests(APITestCase):
    def test_set_clear_validate_and_private(self):
        from apps.accounts.tests.helpers import PUBLIC_PEM, bearer, session_token

        with override_settings(CLERK_JWT_KEY=PUBLIC_PEM):
            h = bearer(session_token("user_home"))
            self.client.get(reverse("me"), **h)
            r = self.client.patch(reverse("me"), {"home_state": "lagos"}, format="json", **h)
            self.assertEqual(r.data["home_state"], "lagos")
            r = self.client.patch(reverse("me"), {"home_state": "atlantis"}, format="json", **h)
            self.assertEqual(r.status_code, 400)
            r = self.client.patch(reverse("me"), {"home_state": ""}, format="json", **h)
            self.assertEqual(r.data["home_state"], "")
        from apps.accounts.services import mark_deleted

        u = User.objects.get(clerk_user_id="user_home")
        User.objects.filter(pk=u.pk).update(home_state="kano")
        mark_deleted("user_home")
        self.assertEqual(User.objects.get(pk=u.pk).home_state, "")


class JobAndLogTests(TestCase):
    def test_rebuild_tracks_and_removes_orphans(self):
        c = mk_candidate("rebuild", "Rebuild Person")
        SearchEntry.objects.all().delete()
        SearchEntry.objects.create(kind=Kind.ARTICLE, object_id=c.pk, title="orphan", folded_title="orphan")
        call_command("rebuild_search", stdout=StringIO())
        self.assertTrue(SearchEntry.objects.filter(kind=Kind.CANDIDATE, object_id=c.pk).exists())
        self.assertFalse(SearchEntry.objects.filter(title="orphan").exists())
        self.assertTrue(JobRun.objects.filter(job="rebuild_search", ok=True).exists())
        self.assertTrue(index.health()["ok"])
        SearchEntry.objects.all().delete()
        self.assertFalse(index.health()["ok"])

    def test_query_redacted_in_logs(self):
        rec = logging.LogRecord("django.server", logging.INFO, __file__, 1, '"%s" %s %s', ("GET /api/v1/search/?q=osun+bello&type=all HTTP/1.1", 200, 10), None)
        RedactSearchQuery().filter(rec)
        self.assertEqual(rec.getMessage(), '"GET /api/v1/search/?q=[redacted]&type=all HTTP/1.1" 200 10')


class PerformanceTests(TestCase):
    def test_all_types_fast_on_a_larger_set(self):
        cands = [Candidate(slug=f"c{i}", full_name=f"Person{i} Surname{i % 97}", position="councillor", election_year=2027, state="kano",
                           area_name="Fagge", ward_name=f"Ward {i}", status="published", verified_at=timezone.now(), published_at=timezone.now())
                 for i in range(2000)]
        Candidate.objects.bulk_create(cands)  # bulk_create skips signals: index once
        Article.objects.bulk_create([Article(source="official", status="successful", category="party", title=f"Story {i} about Kano",
                                             body="Polling units in Kano opened on time.", published_at=timezone.now()) for i in range(2000)])
        index.rebuild()
        start = time.perf_counter()
        for kind in Kind:
            services.search("surname42", kind, year=2027 if kind == Kind.CANDIDATE else None, size=5)
        self.assertLess(time.perf_counter() - start, 1.5)
