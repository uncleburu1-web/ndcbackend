"""Spec 21 — languages and translation (backend)."""
import csv
import gettext as pygettext
import shutil
import tempfile
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APITestCase

from apps.access.models import Role
from apps.accounts.models import LanguageReview, SettingsDefaults, UserPreferences
from apps.audit.models import AuditLog
from apps.core import i18n
from apps.news.models import Article, ArticleTranslation
from apps.news.tests.test_news import mk_role, mk_user
from apps.privacy.models import ConsentRecord
from apps.search.text import fold


def write_catalogue(root: Path, lang: str, translate):
    """A catalogue for `lang` where every message the backend has is translated by `translate(english)`."""
    entries = [i18n.Entry(m, translate(m), [w], "reviewed") for m, w in i18n.all_msgids().items()]
    po = root / lang / "LC_MESSAGES" / "django.po"
    i18n.write_po(po, lang, entries)
    po.with_suffix(".mo").write_bytes(i18n.mo_bytes(i18n.read_po(po), lang))


class TempLocale:
    """LOCALE_PATHS pointing at a fresh folder (Django resets its translation cache when the setting changes)."""

    def setUp(self):
        super().setUp()
        from apps.accounts.language import forget

        forget()  # the enabled-languages cache outlives a test's database
        self.locale = Path(tempfile.mkdtemp())
        self._override = override_settings(LOCALE_PATHS=[self.locale])
        self._override.enable()

    def tearDown(self):
        self._override.disable()
        shutil.rmtree(self.locale, ignore_errors=True)
        super().tearDown()


def enable(*codes):
    d, _ = SettingsDefaults.objects.get_or_create(pk=1)
    d.enabled_languages = list(codes)
    d.save()
    from apps.accounts.language import forget

    forget()


HA = lambda s: f"[ha] {s}"  # noqa: E731


# --------------------------------------------------------------------------- catalogue files
class CatalogueTests(TempLocale, TestCase):
    def test_po_round_trip_and_mo_readable_by_gettext(self):
        po = self.locale / "yo" / "LC_MESSAGES" / "django.po"
        i18n.write_po(po, "yo", [i18n.Entry('Say "hi"\nnow', "Ẹ kú \"àárọ̀\"\nnísisìyí", ["x.py:1"], "machine-draft"), i18n.Entry("Empty")])
        back = i18n.read_po(po)
        self.assertEqual(back['Say "hi"\nnow'].msgstr, "Ẹ kú \"àárọ̀\"\nnísisìyí")
        self.assertEqual(back['Say "hi"\nnow'].status, "machine-draft")
        mo = i18n.compile_language("yo")
        with open(mo, "rb") as fh:
            t = pygettext.GNUTranslations(fh)
        self.assertEqual(t.gettext('Say "hi"\nnow'), "Ẹ kú \"àárọ̀\"\nnísisìyí")
        self.assertEqual(t.gettext("Empty"), "Empty")  # untranslated: English

    def test_messages_are_collected_from_errors_rules_and_privacy(self):
        ids = i18n.all_msgids()
        self.assertIn("Sign in to continue.", ids)
        self.assertIn("Only the author can do this.", ids)  # a ServiceError/_err in communities
        from apps.moderation.rules import RULES

        self.assertIn(RULES[0].summary, ids)

    def test_committed_catalogues_are_compiled(self):
        with override_settings(LOCALE_PATHS=[settings.BASE_DIR / "locale"]):
            call_command("i18n_compile", "--check", stdout=StringIO())

    def test_import_checks_placeholders_and_keys(self):
        write_catalogue(self.locale, "ha", lambda s: "")
        bad = self.locale / "bad.csv"
        with open(bad, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["key", "english", "context", "translation", "status"])
            w.writerow(["Not a real message", "", "", "x", "translated"])
        with self.assertRaisesMessage(CommandError, "unknown key"):
            call_command("i18n_import", "--lang", "ha", "--file", str(bad), stdout=StringIO())
        good = self.locale / "good.csv"
        with open(good, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["key", "english", "context", "translation", "status"])
            w.writerow(["Sign in to continue.", "", "", "Shiga don ci gaba.", ""])
        out = StringIO()
        call_command("i18n_import", "--lang", "ha", "--file", str(good), "--status", "machine-draft", stdout=out)
        self.assertIn("imported 1", out.getvalue())
        e = i18n.read_po(i18n.po_path("ha"))["Sign in to continue."]
        self.assertEqual((e.msgstr, e.status), ("Shiga don ci gaba.", "machine-draft"))
        self.assertEqual(i18n.stats("ha")["reviewed"], 0)  # a machine draft never counts as reviewed


# --------------------------------------------------------------------------- answering in a language
class AnswerLanguageTests(TempLocale, APITestCase):
    def setUp(self):
        super().setUp()
        write_catalogue(self.locale, "ha", HA)

    def test_errors_in_the_request_language_only_when_enabled(self):
        url = reverse("my-settings")
        r = self.client.get(url, HTTP_ACCEPT_LANGUAGE="ha")
        self.assertEqual((r.data["error"]["message"], r["Content-Language"]), ("Sign in to continue.", "en"))  # ha not enabled
        enable("ha")
        r = self.client.get(url, HTTP_ACCEPT_LANGUAGE="ha")
        self.assertEqual((r.data["error"]["message"], r["Content-Language"]), ("[ha] Sign in to continue.", "ha"))
        self.assertEqual(r.data["error"]["code"], "not_authenticated")  # codes never change

    def test_legal_texts_need_a_legal_review_and_use_lang_param_not_the_header(self):
        enable("ha")
        r = self.client.get(reverse("rules"), {"lang": "ha"})
        self.assertEqual(r.data["translation"], {"language": "en", "complete": False, "requested": "ha", "authoritative": "en"})
        LanguageReview.objects.create(language="ha", scope="legal", catalogue_version=i18n.legal_version(), reviewer_name="Barr. A.")
        r = self.client.get(reverse("rules"), {"lang": "ha"})
        self.assertEqual(r.data["translation"]["language"], "ha")
        self.assertTrue(r.data["rules"][0]["summary"].startswith("[ha] "))
        self.assertEqual(r["Content-Language"], "ha")
        r = self.client.get(reverse("rules"), HTTP_ACCEPT_LANGUAGE="ha")  # cached public text: ?lang= only (rule 7)
        self.assertEqual(r.data["translation"]["language"], "en")
        r = self.client.get(reverse("privacy-notice"), {"lang": "ha"})
        self.assertEqual(r.data["translation"]["language"], "ha")

    def test_consent_records_the_language_shown(self):
        enable("ha")
        u = mk_user("user_c1")
        self.client.force_authenticate(u)
        r = self.client.post(reverse("my-consent", args=["political_following"]), {"language": "ha"}, format="json")
        self.assertEqual(r.data["language"], "en")  # no legal review yet: the English text was shown
        ConsentRecord.objects.all().delete()
        LanguageReview.objects.create(language="ha", scope="legal", catalogue_version=i18n.legal_version(), reviewer_name="Barr. A.")
        r = self.client.post(reverse("my-consent", args=["political_following"]), {"language": "ha"}, format="json")
        self.assertEqual(r.data["language"], "ha")
        self.assertEqual(ConsentRecord.objects.get(user=u).language, "ha")
        r = self.client.get(reverse("my-privacy"))
        self.assertEqual(r.data["consent_history"][0]["language"], "ha")


# --------------------------------------------------------------------------- enabling and reviews
class EnableAndReviewTests(TempLocale, APITestCase):
    def setUp(self):
        super().setUp()
        self.admin = mk_role("user_lang_admin", Role.SYSTEM_ADMIN)
        self.client.force_authenticate(self.admin)

    def patch(self, langs):
        return self.client.patch(reverse("admin-settings-defaults"), {"enabled_languages": langs}, format="json")

    def test_complete_and_reviewed_before_enabling(self):
        r = self.patch(["yo"])
        self.assertEqual((r.status_code, r.data["error"]["code"]), (400, "language_not_ready"))
        write_catalogue(self.locale, "yo", lambda s: f"[yo] {s}")
        r = self.client.post(reverse("admin-language-reviews", args=["yo"]),
                             {"scope": "interface", "reviewer_name": "Adébáyọ̀ O.", "catalogue_version": "abc123", "interface_percent": 99},
                             format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.patch(["yo"]).data["error"]["code"], "language_not_ready")  # the web app wasn't complete
        self.client.post(reverse("admin-language-reviews", args=["yo"]),
                         {"scope": "interface", "reviewer_name": "Adébáyọ̀ O.", "catalogue_version": "abc124", "interface_percent": 100},
                         format="json")
        r = self.patch(["yo"])
        self.assertEqual(r.status_code, 200)
        self.assertIn("yo", r.data["enabled_languages"])
        self.assertEqual(AuditLog.objects.filter(action=AuditLog.Action.LANGUAGE_REVIEW_RECORDED).count(), 2)
        r = self.client.get(reverse("languages"))
        self.assertEqual([x["code"] for x in r.data["languages"]], ["en", "yo"])
        self.assertEqual(r.data["languages"][1]["native"], "Yorùbá")

    def test_admin_screen_and_permissions(self):
        r = self.client.get(reverse("admin-languages"))
        ha = next(x for x in r.data["languages"] if x["code"] == "ha")
        self.assertEqual((ha["enabled"], ha["ready"], ha["backend"]["percent"]), (False, False, 0))
        self.assertEqual(self.client.post(reverse("admin-language-reviews", args=["en"]), {}, format="json").data["error"]["code"],
                         "language_invalid")
        self.client.force_authenticate(mk_user("user_lang_member"))
        self.assertEqual(self.client.get(reverse("admin-languages")).status_code, 403)

    def test_staff_languages(self):
        self.client.force_authenticate(mk_role("user_lang_mod", Role.MODERATOR))
        r = self.client.put(reverse("admin-my-languages"), {"languages": ["ha", "yo"]}, format="json")
        self.assertEqual(r.data["languages"], ["ha", "yo"])
        self.assertEqual(self.client.put(reverse("admin-my-languages"), {"languages": ["fr"]}, format="json").status_code, 400)
        self.client.force_authenticate(mk_user("user_lang_member2"))
        self.assertEqual(self.client.get(reverse("admin-my-languages")).status_code, 403)


# --------------------------------------------------------------------------- announcements in several languages
class AnnouncementTranslationTests(APITestCase):
    def setUp(self):
        self.a1 = mk_role("user_tr_a1", Role.SYSTEM_ADMIN)
        self.a2 = mk_role("user_tr_a2", Role.SYSTEM_ADMIN)
        self.client.force_authenticate(self.a1)
        r = self.client.post(reverse("admin-announcements"), {"category": "party", "title": "Town hall in Kano", "body": "Come along.",
                                                              "language": "en"}, format="json")
        self.art = Article.objects.get(pk=r.data["id"])

    def url(self, code, publish=False):
        return reverse("admin-announcement-translation-publish" if publish else "admin-announcement-translation", args=[self.art.pk, code])

    def test_write_review_publish_and_read(self):
        self.assertEqual(self.client.put(self.url("en"), {"title": "x", "body": "y"}, format="json").data["error"]["code"],
                         "translation_same_language")
        r = self.client.put(self.url("ha"), {"title": "Taron jama'a a Kano", "body": "Ku zo."}, format="json")
        self.assertEqual(r.data["status"], "draft")
        self.assertEqual(self.client.post(self.url("ha", True)).data["error"]["code"], "translation_self_review")
        self.client.force_authenticate(self.a2)
        r = self.client.post(self.url("ha", True))
        self.assertEqual((r.data["status"], r.data["out_of_date"]), ("published", False))
        pub = self.client.get(reverse("news-detail", args=[self.art.pk]), {"lang": "ha"})
        self.assertEqual((pub.data["title"], pub.data["version_language"]), ("Taron jama'a a Kano", "ha"))
        self.assertEqual(pub.data["translations"][0]["language"], "ha")
        en = self.client.get(reverse("news-detail", args=[self.art.pk]))
        self.assertEqual((en.data["title"], en.data["language"]), ("Town hall in Kano", "en"))
        found = self.client.get(reverse("search"), {"q": "taron"})
        self.assertIn(str(self.art.pk), [str(x["id"]) for x in found.data["news"]["results"]])
        # editing the original marks the version out of date (it stays visible)
        self.client.patch(reverse("admin-announcement-detail", args=[self.art.pk]), {"body": "Come along at 4pm."}, format="json")
        pub = self.client.get(reverse("news-detail", args=[self.art.pk]), {"lang": "ha"})
        self.assertTrue(pub.data["version_out_of_date"])
        self.assertEqual(self.client.delete(self.url("ha")).status_code, 204)
        self.assertFalse(ArticleTranslation.objects.exists())
        self.assertEqual(AuditLog.objects.filter(action__startswith="news.translation").count(), 3)


# --------------------------------------------------------------------------- the language of what people write
class ContentLanguageTests(APITestCase):
    def test_default_is_the_writers_language_and_choices_are_checked(self):
        u = mk_user("user_cl1")
        UserPreferences.objects.create(user=u, language="yo")
        self.client.force_authenticate(u)
        body = {"title": "Ọjà tuntun ní Ìbàdàn", "body": "Ọjà tuntun kan ṣí ní Ìbàdàn lónìí. " * 8,
                "declarations": {"accurate": True, "no_private_info": True, "rules": True}}
        r = self.client.post(reverse("my-news"), body, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Article.objects.get(pk=r.data["id"]).language, "yo")
        r = self.client.post(reverse("my-news"), {**body, "title": "Other", "language": "fr"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_feed_filter_and_moderation_language(self):
        from apps.moderation import targets
        from apps.moderation.models import TargetKind

        admin = mk_role("user_cl_admin", Role.SYSTEM_ADMIN)
        self.client.force_authenticate(admin)
        for lang, title in (("ha", "Labari"), ("en", "News")):
            self.client.post(reverse("admin-announcements"), {"category": "party", "title": title, "body": "x", "language": lang},
                             format="json")
        r = self.client.get(reverse("news-list"), {"language": "ha"})
        self.assertEqual([x["title"] for x in r.data["results"]], ["Labari"])
        self.assertEqual(self.client.get(reverse("news-list"), {"language": "xx"}).status_code, 400)
        a = Article.objects.get(title="Labari")
        self.assertEqual(targets.language(TargetKind.ARTICLE, a), "ha")

    def test_nfc_on_save_and_hausa_search_folding(self):
        a = Article.objects.create(source="official", status="successful", category="party", title="Ọ̀yọ́",
                                   body="ɗan ƙasa", published_at="2026-11-13T08:00:00Z")
        a.refresh_from_db()
        self.assertEqual(a.title, "Ọ̀yọ́")  # composed (NFC)
        self.assertEqual(fold("Ɓaƙi ɗan ƙasa ʼya"), "baki dan kasa ya")
        self.assertEqual(fold("Ọ̀yọ́ Ṣàngó ụlọ ɛ ɔ"), "oyo sango ulo e o")
