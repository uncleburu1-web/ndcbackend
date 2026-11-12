"""Spec 20 §19.1: settings and user preferences."""
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.access.models import Role, ScopeType
from apps.access.services import deactivate_user, grant_role
from apps.accounts import settings_services as svc
from apps.accounts.models import RetiredHandle, SettingsDefaults, User, UserPreferences
from apps.accounts.services import mark_deleted
from apps.accounts.tests.helpers import PUBLIC_PEM, bearer, session_token
from apps.audit.models import AuditLog
from apps.candidates.models import Candidate
from apps.notifications import services as N
from apps.notifications.models import Notification, NotificationPreference
from apps.privacy import consent, register
from apps.privacy.export import build_export
from apps.privacy.register import CONSENT_PURPOSES

NOW = timezone.now()
VERSION = CONSENT_PURPOSES["public_profile"].version


def mk(cid, *roles, name=None, **kw):
    then = timezone.now() - timedelta(days=3)
    u = User.objects.create_user(cid, email=f"{cid}@example.com", full_name=name or f"Ada {cid.title()}", phone_number="+2348031234567",
                                 profile_completed_at=then, adult_confirmed_at=then, **kw)
    for r in roles:
        grant_role(user=u, role=r)
    return u


def client(user=None):
    c = APIClient()
    if user:
        c.force_authenticate(user)
    return c


def tok(user, *, fva=(0, 0), **extra):
    return bearer(session_token(user.clerk_user_id, mfa=True, fva=list(fva), **extra))


def mk_candidate(slug="amina-bello", position="governor"):
    return Candidate.objects.create(slug=slug, full_name="Amina Bello", position=position, election_year=2027, state="lagos",
                                    status="published", verified_at=NOW, published_at=NOW)


def turn_on(user):
    return client(user).post(reverse("my-settings-public-profile"), {"consent": True, "version": VERSION}, format="json")


# --------------------------------------------------------------------------- preferences
class PreferenceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.u = mk("u_ada", name="Ada Obi")

    def test_defaults_when_absent_and_nothing_saved(self):
        r = client(self.u).get(reverse("my-settings"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("no-store", r["Cache-Control"])
        self.assertEqual(r.data["accessibility"], {"theme": "system", "text_size": "standard", "contrast": "system", "motion": "system"})
        self.assertEqual((r.data["language"]["language"], r.data["profile"]["name_style"], r.data["privacy"]["donation_name_default"]),
                         ("en", "first_initial", "anonymous"))
        self.assertEqual(r.data["profile"]["name_examples"], {"first_initial": "Ada O.", "full": "Ada Obi"})
        self.assertIsNone(r.data["saved_at"])
        self.assertFalse(UserPreferences.objects.filter(user=self.u).exists())  # reading never creates the row

    def test_patch_validation_per_field_and_saved_at(self):
        c = client(self.u)
        for path, body in [("my-settings-accessibility", {"theme": "neon"}), ("my-settings-accessibility", {"text_size": "huge"}),
                           ("my-settings-accessibility", {"contrast": "max"}), ("my-settings-accessibility", {"motion": "fast"}),
                           ("my-settings-accessibility", {}), ("my-settings-profile", {"name_style": "nickname"}),
                           ("my-settings-profile", {"bio": "x" * 301}), ("my-settings-profile", {"profile_photo": "yes"}),
                           ("my-settings-privacy", {"donation_name_default": "everyone"}), ("my-settings-language", {"language": "fr"})]:
            r = c.patch(reverse(path), body, format="json")
            self.assertEqual(r.status_code, 400, (path, body))
            self.assertEqual(r.data["error"]["code"], "validation_error", (path, body))
        self.assertFalse(UserPreferences.objects.filter(user=self.u).exists())
        r = c.patch(reverse("my-settings-accessibility"), {"theme": "dark", "text_size": "large"}, format="json")
        self.assertEqual((r.status_code, r.data["theme"], r.data["text_size"], r.data["contrast"]), (200, "dark", "large", "system"))
        p = UserPreferences.objects.get(user=self.u)
        first = p.saved_at
        self.assertIsNotNone(first)
        c.patch(reverse("my-settings-privacy"), {"donation_name_default": "name"}, format="json")
        p.refresh_from_db()
        self.assertEqual((p.donation_name_default, p.saved_at), ("name", first))  # saved_at marks the first save only
        self.assertEqual(c.get(reverse("me")).data["preferences"]["theme"], "dark")

    def test_bio_is_plain_text_and_trimmed(self):
        r = client(self.u).patch(reverse("my-settings-profile"), {"bio": "  I vote in Ikeja.  "}, format="json")
        self.assertEqual(r.data["bio"], "I vote in Ikeja.")

    def test_device_import_once(self):
        c = client(self.u)
        r = c.post(reverse("my-settings-import-device"), {"theme": "dark", "text_size": "larger", "language": "en"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.data["accessibility"]["theme"], r.data["accessibility"]["text_size"]), ("dark", "larger"))
        r = c.post(reverse("my-settings-import-device"), {"theme": "light"}, format="json")
        self.assertEqual((r.status_code, r.data["error"]["code"]), (409, "invalid_transition"))
        self.assertEqual(UserPreferences.objects.get(user=self.u).theme, "dark")

    def test_device_import_refused_after_explicit_save(self):
        client(self.u).patch(reverse("my-settings-accessibility"), {"motion": "reduce"}, format="json")
        r = client(self.u).post(reverse("my-settings-import-device"), {"theme": "dark"}, format="json")
        self.assertEqual(r.status_code, 409)

    def test_nobody_reads_another_persons_preferences(self):
        other = mk("u_bo")
        client(other).patch(reverse("my-settings-accessibility"), {"theme": "dark"}, format="json")
        self.assertEqual(client(self.u).get(reverse("my-settings")).data["accessibility"]["theme"], "system")
        self.assertEqual(client().get(reverse("my-settings")).status_code, 401)
        for name in ("my-settings", "my-settings-notifications"):
            self.assertEqual(client().get(reverse(name)).status_code, 401)


# --------------------------------------------------------------------------- language
class LanguageTests(TestCase):
    def setUp(self):
        cache.clear()
        self.u = mk("u_ada")

    def test_only_enabled_languages(self):
        r = client(self.u).patch(reverse("my-settings-language"), {"language": "ha"}, format="json")
        self.assertEqual((r.status_code, r.data["error"]["code"]), (400, "language_unavailable"))
        opts = {o["code"]: o for o in client(self.u).get(reverse("my-settings")).data["language"]["options"]}
        self.assertEqual(set(opts), {"en", "ha", "ig", "yo", "pcm"})
        self.assertTrue(opts["en"]["enabled"])
        self.assertFalse(any(opts[c]["enabled"] for c in ("ha", "ig", "yo", "pcm")))
        self.assertEqual((opts["yo"]["native"], opts["pcm"]["native"]), ("Yorùbá", "Naijá"))
        r = client(self.u).patch(reverse("my-settings-language"), {"language": "en"}, format="json")
        self.assertEqual((r.status_code, r.data["language"]), (200, "en"))

    def test_accept_language_for_guests_and_account_language_when_signed_in(self):
        from django.test import RequestFactory

        from apps.accounts import language

        with mock.patch.object(language, "enabled", return_value=["en", "yo"]):
            self.assertEqual(language.from_header("yo-NG,yo;q=0.9,en;q=0.8"), "yo")
            self.assertEqual(language.from_header("fr-FR,ha;q=0.5"), "en")
            self.assertEqual(language.from_header(""), "en")
            req = RequestFactory().get("/", HTTP_ACCEPT_LANGUAGE="yo")
            language.LanguageMiddleware(lambda r: None)(req)
            self.assertEqual(req.ndc_language, "yo")  # a guest: the browser's language
            UserPreferences.objects.create(user=self.u, language="en")
            drf = mock.Mock(_request=req)
            language.attach_account_language(drf, self.u)
            self.assertEqual(req.ndc_language, "en")  # signed in: the account's language


# --------------------------------------------------------------------------- notifications
class NotificationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.u = mk("u_ada")
        self.c = client(self.u)

    def test_categories_locked_and_switchable(self):
        r = self.c.get(reverse("my-settings-notifications"))
        cats = {x["key"]: x for x in r.data["categories"]}
        self.assertEqual({k for k, x in cats.items() if x["locked"]},
                         {"your_events", "your_applications", "payments", "moderation_privacy", "security"})
        self.assertTrue(all(x["on"] for x in cats.values()))
        self.assertFalse(r.data["candidate_bells_paused"])
        r = self.c.patch(reverse("my-settings-notifications"), {"payments": False}, format="json")
        self.assertEqual((r.status_code, r.data["error"]["code"]), (400, "validation_error"))
        r = self.c.patch(reverse("my-settings-notifications"), {"community_posts": "no"}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.c.patch(reverse("my-settings-notifications"), {"community_posts": False, "volunteer_updates": False,
                                                                 "candidate_bells_paused": True}, format="json")
        self.assertEqual(r.status_code, 200)
        np = NotificationPreference.objects.get(user=self.u)
        self.assertEqual((np.community_posts, np.volunteer_updates, np.candidate_bells_paused, np.announcements), (False, False, True, True))
        # the old endpoint stays as an alias over the same row
        self.assertFalse(client(self.u).get(reverse("my-notifications-preferences")).data["community_posts"])

    def test_bell_pause_stops_candidate_posts_and_keeps_bells(self):
        from apps.follows.models import CandidateFollow
        from apps.posts.models import CandidatePost

        cand = mk_candidate()
        other = mk("u_bo")
        then = timezone.now() - timedelta(hours=1)
        for u in (self.u, other):
            CandidateFollow.objects.create(user=u, candidate=cand, notify=True, notify_changed_at=then)
        self.c.patch(reverse("my-settings-notifications"), {"candidate_bells_paused": True}, format="json")
        post = CandidatePost.objects.create(candidate=cand, kind="update", title="Rally on Saturday", body="Come along.", published_at=NOW)
        N.deliver_post(post)
        self.assertFalse(Notification.objects.filter(recipient=self.u, kind="candidate_post").exists())
        self.assertTrue(Notification.objects.filter(recipient=other, kind="candidate_post").exists())
        self.assertTrue(CandidateFollow.objects.get(user=self.u).notify)  # bells stay on, to come back to

    def test_volunteer_updates_switch(self):
        from apps.volunteers.models import VolunteerApplication, VolunteerUpdate

        cand = mk_candidate()
        other = mk("u_bo")
        for u in (self.u, other):
            VolunteerApplication.objects.create(candidate=cand, user=u, roles=["canvassing"], state="lagos", status="accepted",
                                                decided_at=NOW)
        self.c.patch(reverse("my-settings-notifications"), {"volunteer_updates": False}, format="json")
        n = N.deliver_volunteer_update(VolunteerUpdate.objects.create(candidate=cand, text="Meet at 9am."))
        self.assertEqual(n, 1)
        self.assertFalse(Notification.objects.filter(recipient=self.u).exists())

    def test_new_accounts_take_admin_defaults_existing_unchanged(self):
        sa = mk("u_sa", Role.SYSTEM_ADMIN)
        r = client(sa).patch(reverse("admin-settings-defaults"), {"notifications": {"announcements": False}, "default_theme": "dark",
                                                                   "default_text_size": "large"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        from apps.accounts.services import upsert_from_clerk
        from apps.accounts.tests.helpers import clerk_user

        new = upsert_from_clerk(clerk_user("user_new", email="new@example.com"), source="test")
        self.assertFalse(NotificationPreference.objects.get(user=new).announcements)
        p = UserPreferences.objects.get(user=new)
        self.assertEqual((p.theme, p.text_size, p.saved_at), ("dark", "large", None))  # defaults, not a choice: device copy still works
        self.assertFalse(NotificationPreference.objects.filter(user=self.u).exists())
        self.assertFalse(UserPreferences.objects.filter(user=self.u).exists())


class CommunityNotificationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from apps.communities import sync
        from apps.geo import loading

        loading.load((Path(settings.BASE_DIR) / "data" / "geo" / "sample.csv").read_bytes(), source="sample")
        sync.sync()

    def test_community_posts_off_replies_still_on(self):
        from apps.communities import services as C
        from apps.communities.models import Community, CommunityMembership

        nat = Community.objects.get(path="national")
        lga = Community.objects.get(path="lagos/ikeja")
        a, b = mk("u_a", name="Amaka Eze"), mk("u_b", name="Bisi Ola")
        for u in (a, b):
            consent.grant(u, "community_membership")
        CommunityMembership.objects.create(user=a, community=lga, level="lga", how="verified_registration", notify_new_posts=True)
        client(a).patch(reverse("my-settings-notifications"), {"community_posts": False}, format="json")
        post = C.create_post(b, nat, "Who is going to the town hall?")
        from apps.communities.models import CommunityPost

        lga_post = CommunityPost.objects.create(community=lga, author=b, body="Ikeja town hall on Friday evening")
        self.assertEqual(N.deliver_community_post(lga_post), 0)
        own = C.create_post(a, nat, "My own question for everyone")
        C.create_comment(b, own, "A reply to Amaka")
        self.assertTrue(Notification.objects.filter(recipient=a, kind="community_reply").exists())
        self.assertFalse(Notification.objects.filter(recipient=a, kind="community_post").exists())
        self.assertIsNotNone(post)


# --------------------------------------------------------------------------- public profile
class PublicProfileTests(TestCase):
    def setUp(self):
        cache.clear()
        self.u = mk("u_ada", name="Ada Obi", home_state="lagos")
        self.c = client(self.u)

    def test_consent_required_then_handle_created_and_kept(self):
        r = self.c.post(reverse("my-settings-public-profile"), {"consent": True, "version": VERSION + 1}, format="json")
        self.assertEqual((r.status_code, r.data["error"]["code"]), (400, "profile_consent_required"))
        r = self.c.post(reverse("my-settings-public-profile"), {}, format="json")
        self.assertEqual(r.data["error"]["code"], "profile_consent_required")
        r = turn_on(self.u)
        self.assertEqual(r.status_code, 200, r.content)
        handle = r.data["handle"]
        self.assertRegex(handle, r"^[a-z0-9]{6}$")
        self.assertTrue(consent.active(self.u, "public_profile"))
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.PUBLIC_PROFILE_ON).exists())
        r = self.c.delete(reverse("my-settings-public-profile"))
        self.assertEqual((r.data["public_profile"], r.data["handle"]), (False, None))
        self.assertFalse(consent.active(self.u, "public_profile"))
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.PUBLIC_PROFILE_OFF).exists())
        self.assertEqual(client().get(reverse("public-profile", args=[handle])).status_code, 404)
        self.assertEqual(turn_on(self.u).data["handle"], handle)  # the same address again

    def test_withdrawing_consent_turns_it_off(self):
        handle = turn_on(self.u).data["handle"]
        consent.withdraw(self.u, "public_profile")
        self.assertEqual(client().get(reverse("public-profile", args=[handle])).status_code, 404)

    def test_fields_only_as_chosen_and_never_political_lists(self):
        from apps.geo import loading
        from apps.geo.models import Lga

        loading.load((Path(settings.BASE_DIR) / "data" / "geo" / "sample.csv").read_bytes(), source="sample")
        ikeja = Lga.objects.get(state__slug="lagos", name__iexact="Ikeja")
        handle = turn_on(self.u).data["handle"]
        self.u.image_url = "https://img.clerk.com/ada.png"
        self.u.save(update_fields=["image_url"])
        g = client()
        r = g.get(reverse("public-profile", args=[handle]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.data["name"], r.data["bio"], r.data["location"], r.data["photo_url"], r.data["verified_voter"]),
                         ("Ada O.", "", None, None, False))
        self.assertEqual(r["X-Robots-Tag"], "noindex")
        self.assertIn("max-age=300", r["Cache-Control"])
        self.assertEqual(set(r.data), {"handle", "name", "bio", "location", "verified_voter", "photo_url", "member_since", "news"})
        bad = self.c.patch(reverse("my-settings-profile"), {"profile_location": "lga", "profile_lga": 999999}, format="json")
        self.assertEqual(bad.data["error"]["code"], "profile_location_invalid")
        r = self.c.patch(reverse("my-settings-profile"), {"bio": "Teacher in Ikeja.", "profile_location": "lga", "profile_lga": ikeja.pk,
                                                         "profile_photo": True, "name_style": "full"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        cache.clear()
        r = g.get(reverse("public-profile", args=[handle]))
        self.assertEqual((r.data["name"], r.data["bio"], r.data["photo_url"]), ("Ada Obi", "Teacher in Ikeja.", "https://img.clerk.com/ada.png"))
        self.assertEqual((r.data["location"]["state"], r.data["location"]["lga"]), ("lagos", ikeja.name))
        body = r.content.decode().lower()
        for word in ("follow", "communit", "membership", "pvc", "donat", "count", "email", "phone"):
            self.assertNotIn(word, body)
        self.c.patch(reverse("my-settings-profile"), {"profile_location": "state"}, format="json")
        self.assertIsNone(UserPreferences.objects.get(user=self.u).profile_lga)
        # an LGA outside the home state is refused
        self.u.home_state = "kano"
        self.u.save(update_fields=["home_state"])
        r = client(self.u).patch(reverse("my-settings-profile"), {"profile_location": "lga", "profile_lga": ikeja.pk}, format="json")
        self.assertEqual(r.data["error"]["code"], "profile_location_invalid")

    def test_404_when_off_paused_deleted_or_unknown(self):
        handle = turn_on(self.u).data["handle"]
        g = client()
        self.assertEqual(g.get(reverse("public-profile", args=["zzzzzz"])).status_code, 404)
        self.assertEqual(g.get(reverse("public-profile", args=["toolonghandle"])).status_code, 404)
        r = g.get(reverse("public-profile", args=[handle.upper()]))
        self.assertEqual(r.status_code, 200)
        svc.pause(self.u, confirm="pause")
        r = g.get(reverse("public-profile", args=[handle]))
        self.assertEqual((r.status_code, r["X-Robots-Tag"]), (404, "noindex"))
        svc.reactivate(User.objects.get(pk=self.u.pk))
        self.assertEqual(g.get(reverse("public-profile", args=[handle])).status_code, 200)
        with mock.patch("apps.accounts.clerk.delete_user", return_value=True):
            mark_deleted(self.u.clerk_user_id)
        self.assertEqual(g.get(reverse("public-profile", args=[handle])).status_code, 404)
        self.assertFalse(UserPreferences.objects.filter(user=self.u).exists())
        self.assertTrue(RetiredHandle.objects.filter(handle=handle).exists())
        other = mk("u_bo")
        with mock.patch("apps.accounts.settings_services.secrets.choice", side_effect=list(handle) + list("b2c3d4")):
            self.assertEqual(turn_on(other).data["handle"], "b2c3d4")  # a retired handle is never handed out again

    def test_bio_and_photo_reportable_removable_restorable(self):
        from apps.moderation import services as M
        from apps.moderation.models import Case

        handle = turn_on(self.u).data["handle"]
        self.u.image_url = "https://img.clerk.com/ada.png"
        self.u.save(update_fields=["image_url"])
        self.c.patch(reverse("my-settings-profile"), {"bio": "Rude words here", "profile_photo": True}, format="json")
        r = client().post(reverse("public-profile-report", args=[handle]), {"reason": "abuse", "details": "Bio insults people"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        case = Case.objects.get(target_kind="profile", target_id=self.u.pk)
        mod = mk("u_mod", Role.MODERATOR)
        self.assertIn(case.pk, list(M.visible_cases(mod).values_list("pk", flat=True)))
        r = client(mod).get(reverse("admin-moderation-case", args=[case.pk]))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.data["item"]["body"], r.data["item"]["public_url"]), ("Rude words here", f"/people/{handle}"))
        r = client(self.u).post(reverse("admin-moderation-case-remove", args=[case.pk]), {"rule": "R2"}, format="json")
        self.assertEqual(r.status_code, 403)  # the person can't moderate their own profile
        r = client(mod).post(reverse("admin-moderation-case-remove", args=[case.pk]), {"rule": "R2", "parts": ["bio"]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        cache.clear()
        page = client().get(reverse("public-profile", args=[handle])).data
        self.assertEqual((page["bio"], page["photo_url"]), ("", "https://img.clerk.com/ada.png"))
        p = UserPreferences.objects.get(user=self.u)
        self.assertEqual(p.bio, "Rude words here")  # never edited
        self.assertTrue(client(self.u).get(reverse("my-settings")).data["profile"]["bio_removed"])
        self.assertTrue(Notification.objects.filter(recipient=self.u, kind="content_removed").exists())
        r = client(mod).post(reverse("admin-moderation-case-restore", args=[case.pk]), {"note": "On review it was fine"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        cache.clear()
        self.assertEqual(client().get(reverse("public-profile", args=[handle])).data["bio"], "Rude words here")
        # a new bio clears an earlier removal
        UserPreferences.objects.filter(pk=self.u.pk).update(bio_removed_at=timezone.now())
        self.c.patch(reverse("my-settings-profile"), {"bio": "Teacher in Ikeja."}, format="json")
        self.assertIsNone(UserPreferences.objects.get(pk=self.u.pk).bio_removed_at)
        self.assertEqual(client().post(reverse("public-profile-report", args=["zzzzzz"]), {"reason": "spam"}, format="json").status_code, 404)


# --------------------------------------------------------------------------- name style
class NameStyleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.u = mk("u_ada", name="Ada Obi")

    def test_news_byline_and_profile_link(self):
        from apps.news.models import Article
        from apps.news.serializers import PublicArticleSerializer

        a = Article.objects.create(source="community", title="Turnout in Ikeja", body="Turnout was high in Ikeja this year. " * 5,
                                   state="lagos", author=self.u, show_author_name=True, status="successful", published_at=NOW)
        out = PublicArticleSerializer(Article.objects.get(pk=a.pk)).data
        self.assertEqual((out["byline"], out["byline_profile"]), ("Ada O.", None))
        handle = turn_on(self.u).data["handle"]
        client(self.u).patch(reverse("my-settings-profile"), {"name_style": "full"}, format="json")
        out = PublicArticleSerializer(Article.objects.get(pk=a.pk)).data
        self.assertEqual((out["byline"], out["byline_profile"]), ("Ada Obi", handle))
        news = client().get(reverse("public-profile", args=[handle])).data["news"]
        self.assertEqual([n["title"] for n in news], ["Turnout in Ikeja"])
        Article.objects.filter(pk=a.pk).update(show_author_name=False)
        out = PublicArticleSerializer(Article.objects.get(pk=a.pk)).data
        self.assertEqual((out["byline"], out["byline_profile"]), (None, None))

    def test_community_byline(self):
        from apps.communities import sync
        from apps.communities.models import Community
        from apps.geo import loading

        loading.load((Path(settings.BASE_DIR) / "data" / "geo" / "sample.csv").read_bytes(), source="sample")
        sync.sync()
        consent.grant(self.u, "community_membership")
        from apps.communities import services as C

        post = C.create_post(self.u, Community.objects.get(path="national"), "Turnout question for all of us")
        self.assertEqual(client(self.u).get(reverse("community-post", kwargs={"pk": post.pk})).data["author"]["name"], "Ada O.")
        client(self.u).patch(reverse("my-settings-profile"), {"name_style": "full"}, format="json")
        self.assertEqual(client(self.u).get(reverse("community-post", kwargs={"pk": post.pk})).data["author"]["name"], "Ada Obi")


# --------------------------------------------------------------------------- pause
@override_settings(CLERK_JWT_KEY=PUBLIC_PEM, PRIVACY_CONSENT_REQUIRED=False)
class PauseTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.u = mk("u_ada", name="Ada Obi")

    def pause(self, **kw):
        return self.client.post(reverse("my-pause"), {"confirm": "pause"}, format="json", **tok(self.u, **kw))

    def test_reverification_and_typed_confirmation(self):
        r = self.pause(fva=(30, -1))
        self.assertEqual(r.data["error"]["code"], "reverification_required")
        r = self.client.post(reverse("my-pause"), {"confirm": "yes"}, format="json", **tok(self.u))
        self.assertEqual(r.status_code, 400)
        self.assertTrue(User.objects.get(pk=self.u.pk).is_active)

    def test_pause_cancels_withdraws_and_limits_endpoints(self):
        from apps.events import services as E
        from apps.events.models import EventRegistration
        from apps.volunteers import services as V
        from apps.volunteers.models import VolunteerApplication

        cand = mk_candidate()
        camp = mk("u_camp")
        grant_role(user=camp, role=Role.CANDIDATE, scope_type=ScopeType.CANDIDATE, scope_id=cand.pk)
        start = timezone.now() + timedelta(days=5)
        periods = [{"positions": ["governor"], "start": (NOW - timedelta(days=30)).isoformat(), "end": (NOW + timedelta(days=60)).isoformat()}]
        with self.settings(CAMPAIGN_PERIODS=periods, CAMPAIGN_BLACKOUTS=[]):
            e = E.create(camp, cand, {"type": "rally", "mode": "in_person", "title": "Ikeja rally for jobs",
                                      "description": "Come and hear the plan for jobs in Lagos. " * 2, "starts_at": start,
                                      "ends_at": start + timedelta(hours=3), "venue_name": "Ikeja Grammar School field",
                                      "venue_address": "Ikeja, Lagos", "venue_type": "open_ground", "state": "lagos"})
            e = E.publish(camp, e, {"no_inducements": True, "lawful_venue": True, "police_notified": True})
            E.register(self.u, e)
        from apps.volunteers.models import VolunteerOpening

        VolunteerOpening.objects.create(candidate=cand, open=True, opened_at=NOW, roles=["canvassing"], states=["lagos"])
        V.apply(self.u, cand, {"roles": ["canvassing"], "state": "lagos", "lga_text": "Ikeja", "availability": ["weekends"],
                               "note": "I know Ikeja well.", "share_phone": False})
        preview = self.client.get(reverse("my-settings"), **tok(self.u)).data["manage"]
        self.assertEqual(preview, {"upcoming_registrations": 1, "pending_volunteer_applications": 1})
        r = self.pause()
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.data["registrations_cancelled"], r.data["applications_withdrawn"]), (1, 1))
        u = User.objects.get(pk=self.u.pk)
        self.assertEqual((u.is_active, u.deactivation_kind), (False, "self"))
        self.assertFalse(EventRegistration.objects.filter(user=u).exists())
        self.assertEqual(VolunteerApplication.objects.get(user=u).status, "withdrawn")
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.ACCOUNT_PAUSED_SELF).exists())
        # only the paused screen's endpoints answer
        me = self.client.get(reverse("me"), **tok(u))
        self.assertEqual((me.status_code, me.data["account_status"]), (200, "paused"))
        for name in ("my-settings", "my-notifications-preferences"):
            r = self.client.get(reverse(name), **tok(u))
            self.assertEqual((r.status_code, r.data["error"]["code"]), (403, "account_paused"), name)
        self.assertIsNone(N.notify(u, "pvc_decision", decision="verified"))  # no notices while paused
        r = self.client.post(reverse("my-data-export"), **tok(u))
        self.assertEqual(r.status_code, 200)  # they can still download their data
        r = self.client.post(reverse("my-reactivate"), **tok(u))
        self.assertEqual((r.status_code, r.data["active"]), (200, True))
        u = User.objects.get(pk=self.u.pk)
        self.assertEqual((u.is_active, u.deactivation_kind), (True, ""))
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.ACCOUNT_REACTIVATED_SELF).exists())
        self.assertEqual(self.client.get(reverse("my-settings"), **tok(u)).status_code, 200)

    def test_admin_deactivated_cannot_self_reactivate(self):
        with mock.patch("apps.accounts.clerk.ban_user", return_value=True):
            deactivate_user(user=self.u, reason="abuse", actor_label="test")
        u = User.objects.get(pk=self.u.pk)
        self.assertEqual(u.deactivation_kind, "admin")
        r = self.client.post(reverse("my-reactivate"), **tok(u))
        self.assertEqual(r.status_code, 403)
        self.assertFalse(User.objects.get(pk=self.u.pk).is_active)

    def test_admin_takes_over_a_self_pause(self):
        svc.pause(self.u, confirm="pause")
        with mock.patch("apps.accounts.clerk.ban_user", return_value=True):
            deactivate_user(user=User.objects.get(pk=self.u.pk), reason="abuse", actor_label="test")
        self.assertEqual(User.objects.get(pk=self.u.pk).deactivation_kind, "admin")
        from apps.core.exceptions import ServiceError

        with self.assertRaises(ServiceError):
            svc.reactivate(User.objects.get(pk=self.u.pk))

    def test_last_system_admin_cannot_pause(self):
        sa = mk("u_sa", Role.SYSTEM_ADMIN)
        r = self.client.post(reverse("my-pause"), {"confirm": "pause"}, format="json", **tok(sa))
        self.assertEqual((r.status_code, r.data["error"]["code"]), (409, "last_system_admin"))
        mk("u_sa2", Role.SYSTEM_ADMIN)
        r = self.client.post(reverse("my-pause"), {"confirm": "pause"}, format="json", **tok(sa))
        self.assertEqual(r.status_code, 200, r.content)


# --------------------------------------------------------------------------- sign out others
@override_settings(CLERK_JWT_KEY=PUBLIC_PEM)
class SignOutOthersTests(APITestCase):
    def setUp(self):
        self.u = mk("u_ada")

    def fake(self, ids):
        clerk = mock.MagicMock()
        clerk.__enter__.return_value = clerk
        clerk.sessions.list.return_value = [mock.Mock(id=i) for i in ids]
        return clerk

    def test_revokes_every_other_session(self):
        clerk = self.fake(["sess_here", "sess_phone", "sess_laptop"])
        with mock.patch("apps.accounts.clerk._client", return_value=clerk):
            r = self.client.post(reverse("my-settings-sign-out-others"),
                                 **bearer(session_token(self.u.clerk_user_id, mfa=True, fva=[0, 0], sid="sess_here")))
        self.assertEqual((r.status_code, r.data["signed_out"]), (200, 2), r.content)
        revoked = [c.kwargs["session_id"] for c in clerk.sessions.revoke.call_args_list]
        self.assertEqual(revoked, ["sess_phone", "sess_laptop"])
        self.assertEqual(AuditLog.objects.get(action=AuditLog.Action.SETTINGS_SIGNED_OUT_OTHERS).metadata["sessions"], 2)

    def test_reverification_required(self):
        r = self.client.post(reverse("my-settings-sign-out-others"), **tok(self.u, fva=(30, -1)))
        self.assertEqual(r.data["error"]["code"], "reverification_required")

    def test_clerk_down_claims_nothing(self):
        clerk = self.fake(["sess_a"])
        clerk.sessions.list.side_effect = RuntimeError("boom")
        with mock.patch("apps.accounts.clerk._client", return_value=clerk):
            r = self.client.post(reverse("my-settings-sign-out-others"), **tok(self.u))
        self.assertEqual((r.status_code, r.data["error"]["code"]), (503, "auth_unavailable"))
        self.assertFalse(AuditLog.objects.filter(action=AuditLog.Action.SETTINGS_SIGNED_OUT_OTHERS).exists())


# --------------------------------------------------------------------------- admin defaults
class AdminDefaultsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.sa = mk("u_sa", Role.SYSTEM_ADMIN)

    def test_system_admin_only_and_audited(self):
        self.assertEqual(client(mk("u_mod", Role.MODERATOR)).get(reverse("admin-settings-defaults")).status_code, 403)
        self.assertEqual(client().get(reverse("admin-settings-defaults")).status_code, 401)
        c = client(self.sa)
        r = c.get(reverse("admin-settings-defaults"))
        self.assertEqual((r.status_code, r.data["enabled_languages"], r.data["translations_complete"]), (200, ["en"], ["en"]))
        r = c.patch(reverse("admin-settings-defaults"), {"enabled_languages": ["en", "yo"]}, format="json")
        self.assertEqual((r.status_code, r.data["error"]["code"]), (400, "language_unavailable"))
        r = c.patch(reverse("admin-settings-defaults"), {"notifications": {"payments": False}}, format="json")
        self.assertEqual(r.status_code, 400)
        r = c.patch(reverse("admin-settings-defaults"), {"default_theme": "dark"}, format="json")
        self.assertEqual((r.status_code, r.data["default_theme"]), (200, "dark"))
        log = AuditLog.objects.get(action=AuditLog.Action.SETTINGS_DEFAULTS_CHANGED)
        self.assertEqual(log.metadata["changes"], {"default_theme": {"from": "system", "to": "dark"}})
        self.assertEqual(SettingsDefaults.objects.get().updated_by, self.sa)

    def test_dashboard_counts(self):
        u = mk("u_ada")
        client(u).patch(reverse("my-settings-accessibility"), {"theme": "dark"}, format="json")
        from apps.accounts.dashboard import _settings_stats

        stats = {s.key: s.total for s in _settings_stats(User.objects.all())}
        self.assertEqual((stats["theme_dark"], stats["lang_en"], stats["public_profiles"], stats["paused"]), (1, 2, 0, 0))


# --------------------------------------------------------------------------- privacy
class PrivacyTests(TestCase):
    def setUp(self):
        cache.clear()
        self.u = mk("u_ada", name="Ada Obi")

    def test_register_covers_the_new_models_and_purpose(self):
        cats = {m for c in register.CATEGORIES for m in c.models}
        self.assertNotIn("accounts.UserPreferences", str(register.public_payload() if hasattr(register, "public_payload") else ""))
        self.assertTrue({"accounts.UserPreferences", "accounts.RetiredHandle"} <= cats)
        self.assertIn("accounts.SettingsDefaults", register.NOT_PERSONAL)
        self.assertIn("public_profile", CONSENT_PURPOSES)

    def test_download_includes_settings_and_deletion_removes_them(self):
        client(self.u).patch(reverse("my-settings-accessibility"), {"theme": "dark"}, format="json")
        turn_on(self.u)
        import io
        import zipfile

        z = zipfile.ZipFile(io.BytesIO(build_export(self.u)))
        text = "".join(z.read(n).decode() for n in z.namelist())
        self.assertIn("dark", text)
        self.assertIn("public_profile", text)
        with mock.patch("apps.accounts.clerk.delete_user", return_value=True):
            mark_deleted(self.u.clerk_user_id)
        self.assertFalse(UserPreferences.objects.filter(user=self.u).exists())
        u = User.objects.get(pk=self.u.pk)
        self.assertEqual((u.is_active, u.deactivation_kind, u.image_url), (False, "deleted", ""))
