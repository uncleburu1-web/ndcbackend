"""Spec 14 §12 — security and privacy (backend)."""
import io
import json
import logging
import os
import zipfile
from datetime import timedelta
from unittest import mock

from django.apps import apps as django_apps
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import get_resolver, reverse
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.access.models import Role, RoleAssignment, ScopeType
from apps.access.services import grant_role
from apps.accounts.models import User
from apps.accounts.tests.helpers import PUBLIC_PEM, bearer, session_token
from apps.audit.models import AuditLog
from apps.audit.services import record, verify_chain
from apps.candidates.models import Candidate
from apps.core import jobs
from apps.core.logging import _scrub
from apps.follows.models import CandidateFollow
from apps.notifications.models import Notification
from apps.privacy import consent, incidents as I, register
from apps.privacy import requests as RQ
from apps.privacy.export import EXPORT_APPS, build_export
from apps.privacy.models import AccessReview, AuditAnchor, ConsentRecord, DataRequest, Incident
from apps.pvc.models import PvcVerification

NOW = timezone.now()


def mk(cid, *roles, complete=True, **kw):
    u = User.objects.create_user(cid, email=kw.pop("email", f"{cid}@example.com"), full_name=kw.pop("full_name", f"Person {cid}"),
                                 phone_number="+2348031234567", profile_completed_at=NOW if complete else None,
                                 adult_confirmed_at=NOW if complete else None, **kw)
    for r in roles:
        grant_role(user=u, role=r)
    return u


def client(user=None):
    c = APIClient()
    if user:
        c.force_authenticate(user)
    return c


def mk_candidate(slug="amina-bello"):
    return Candidate.objects.create(slug=slug, full_name="Amina Bello", position="president", election_year=2027, state="lagos",
                                    status="published", verified_at=NOW, published_at=NOW)


def tok(user, *, mfa=True, fva=(0, 0), **extra):
    claims = dict(extra)
    if mfa is not None:
        claims["mfa"] = mfa
    if fva is not None:
        claims["fva"] = list(fva)
    return bearer(session_token(user.clerk_user_id, **claims))


# --------------------------------------------------------------------------- register
class RegisterTests(TestCase):
    def test_every_model_is_registered_or_explained(self):
        listed = register.registered_models() | set(register.NOT_PERSONAL)
        ours = {f"{m._meta.app_label}.{m.__name__}" for m in django_apps.get_models() if m.__module__.startswith("apps.")}
        self.assertEqual(sorted(ours - listed), [], "add these to privacy/register.py (a category or NOT_PERSONAL with a reason)")
        self.assertEqual(sorted(listed - ours), [], "register names a model that doesn't exist")
        for name, why in register.NOT_PERSONAL.items():
            self.assertGreater(len(why), 10, name)

    def test_every_category_complete_and_jobs_exist(self):
        for c in register.CATEGORIES:
            for f in ("collected", "purpose", "lawful_basis", "visible_to", "retention", "on_deletion"):
                self.assertTrue(getattr(c, f).strip(), (c.key, f))
            if c.job:
                self.assertIn(c.job, jobs.KNOWN_JOBS, c.key)

    def test_notice_payload_has_every_public_category(self):
        r = client().get(reverse("privacy-notice"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("max-age=300", r["Cache-Control"])
        keys = [c["key"] for c in r.data["categories"]]
        self.assertEqual(keys, [c.key for c in register.CATEGORIES if c.public])
        self.assertNotIn("models", r.data["categories"][0])
        self.assertEqual({p["key"] for p in r.data["consents"]}, set(register.CONSENT_PURPOSES))
        self.assertEqual([p["name"] for p in r.data["processors"]], ["Clerk", "Paystack", "Hosting provider"])

    def test_every_app_with_user_data_has_an_exporter(self):
        for app in EXPORT_APPS:
            mod = __import__(f"apps.{app}.privacy", fromlist=["export_for"])
            self.assertTrue(callable(mod.export_for) and mod.DESCRIPTION, app)


# --------------------------------------------------------------------------- consent
@override_settings(PRIVACY_CONSENT_REQUIRED=True)
class ConsentTests(APITestCase):
    def setUp(self):
        self.u = mk("u_consent")
        self.c = mk_candidate()

    def follow(self):
        return client(self.u).put(reverse("my-follow", args=[self.c.slug]), {}, format="json")

    def test_follow_needs_consent_then_works(self):
        r = self.follow()
        self.assertEqual(r.status_code, 403, r.data)
        err = r.data["error"]
        self.assertEqual((err["code"], err["purpose"], err["version"]), ("consent_required", "political_following", 1))
        self.assertIn("political interest", err["text"])
        self.assertFalse(CandidateFollow.objects.filter(user=self.u).exists())
        self.assertEqual(client(self.u).post(reverse("my-consent", args=["political_following"])).status_code, 201)
        self.assertIn(self.follow().status_code, (200, 201))
        self.assertTrue(CandidateFollow.objects.filter(user=self.u).exists())

    def test_one_active_record_per_purpose(self):
        consent.grant(self.u, "political_following")
        consent.grant(self.u, "political_following")
        self.assertEqual(ConsentRecord.objects.filter(user=self.u, purpose="political_following", withdrawn_at__isnull=True).count(), 1)

    def test_withdraw_following_removes_follows_with_count(self):
        consent.grant(self.u, "political_following")
        self.follow()
        c2 = mk_candidate("tunde-ade")
        client(self.u).put(reverse("my-follow", args=[c2.slug]), {}, format="json")
        self.assertIn("2 follows", client(self.u).get(reverse("my-consent", args=["political_following"])).data["withdraw_effect"])
        r = client(self.u).delete(reverse("my-consent", args=["political_following"]))
        self.assertEqual(r.status_code, 200)
        self.assertIn("2", r.data["effect"])
        self.assertFalse(CandidateFollow.objects.filter(user=self.u).exists())
        self.assertTrue(AuditLog.objects.filter(action="privacy.consent_withdrawn").exists())
        self.assertEqual(self.follow().status_code, 403)  # asks again

    def test_pvc_and_badge(self):
        r = client(self.u).put(reverse("my-pvc"), {"vin": "90F5B12345678901234", "surname": "Obi", "state": "lagos", "lga": "ikeja", "consent": True},
                                format="json")
        self.assertEqual(r.data["error"]["code"], "consent_required")
        self.assertEqual(r.data["error"]["purpose"], "pvc_check")
        PvcVerification.objects.create(user=self.u, vin_last4="0123", surname="Obi", state="lagos", lga="ikeja", status="successful",
                                       consent_at=NOW)
        consent.grant(self.u, "pvc_check")
        r = client(self.u).patch(reverse("my-pvc-badge"), {"show_badge": True}, format="json")
        self.assertEqual(r.data["error"]["purpose"], "verified_badge")
        consent.grant(self.u, "verified_badge")
        PvcVerification.objects.filter(user=self.u).update(show_badge=True)
        consent.withdraw(self.u, "verified_badge")
        self.assertFalse(PvcVerification.objects.get(user=self.u).show_badge)
        consent.withdraw(self.u, "pvc_check")
        self.assertFalse(PvcVerification.objects.filter(user=self.u).exists())

    def test_membership_details_need_consent(self):
        r = client(self.u).put(reverse("my-membership"), {"full_name": "Person U"}, format="json")
        if r.status_code == 405:
            r = client(self.u).patch(reverse("my-membership"), {"full_name": "Person U"}, format="json")
        self.assertEqual((r.status_code, r.data["error"]["code"], r.data["error"]["purpose"]), (403, "consent_required", "membership"))

    def test_material_change_asks_again(self):
        consent.grant(self.u, "political_following")
        p = register.CONSENT_PURPOSES["political_following"]
        bumped = type(p)(**{**p.__dict__, "version": 2, "material_since": 2})
        with mock.patch.dict(register.CONSENT_PURPOSES, {"political_following": bumped}):
            self.assertFalse(consent.has_consent(self.u, "political_following"))
            self.assertIn("political_following", consent.consents_needed(self.u))
            self.assertEqual(self.follow().status_code, 403)

    def test_legacy_record_prompt_up_to_three_times(self):
        ConsentRecord.objects.create(user=self.u, purpose="political_following", version=1, method="before_records")
        self.assertTrue(consent.has_consent(self.u, "political_following"))
        for _ in range(3):
            self.assertEqual(consent.consents_needed(self.u), ["political_following"])
            client(self.u).post(reverse("my-consent-prompted", args=["political_following"]))
        self.assertEqual(consent.consents_needed(self.u), [])  # kept on the legacy record (§15.3)

    def test_legacy_migration_created_records(self):
        from importlib import import_module

        mig = import_module("apps.privacy.migrations.0002_legacy_consents")
        CandidateFollow.objects.create(user=self.u, candidate=self.c)
        mig.forwards(django_apps, None)
        rec = ConsentRecord.objects.get(user=self.u, purpose="political_following")
        self.assertEqual(rec.method, "before_records")
        mig.forwards(django_apps, None)  # idempotent
        self.assertEqual(ConsentRecord.objects.filter(user=self.u).count(), 1)


# --------------------------------------------------------------------------- age
class AgeTests(APITestCase):
    def test_profile_incomplete_without_confirmation(self):
        u = mk("u_age", complete=False)
        r = client(u).patch(reverse("me"), {"full_name": "Ada Obi", "phone_number": "08031234567"}, format="json")
        self.assertFalse(r.data["profile_complete"])
        self.assertFalse(r.data["adult_confirmed"])
        r = client(u).patch(reverse("me"), {"adult_confirmed": True}, format="json")
        self.assertTrue(r.data["profile_complete"])
        self.assertTrue(r.data["just_completed"])

    def test_existing_complete_users_need_to_tick_once(self):
        u = mk("u_old", complete=False)
        User.objects.filter(pk=u.pk).update(profile_completed_at=NOW)
        r = client(User.objects.get(pk=u.pk)).get(reverse("me"))
        self.assertFalse(r.data["profile_complete"])

    def test_false_is_refused(self):
        u = mk("u_minor", complete=False)
        r = client(u).patch(reverse("me"), {"adult_confirmed": False}, format="json")
        self.assertEqual(r.status_code, 400)


# --------------------------------------------------------------------------- export
@override_settings(CLERK_JWT_KEY=PUBLIC_PEM)
class ExportTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.u = mk("u_export", full_name="Ada Exporter")
        self.reporter = mk("u_reporter", full_name="Rita Reporter", email="rita@example.com")
        self.mod = mk("u_mod", Role.MODERATOR, full_name="Moses Moderator", email="moses@example.com")
        self.rev = mk("u_rev", Role.NEWS_REVIEWER, full_name="Remi Reviewer", email="remi@example.com")
        self.officer = mk("u_dpo", Role.PRIVACY_OFFICER, full_name="Dora Officer", email="dora@example.com")
        self.c = mk_candidate()
        self._rich_fixture()

    def _rich_fixture(self):
        from apps.membership.models import Membership
        from apps.moderation import services as MS
        from apps.moderation.models import Report
        from apps.news.models import Article, ArticleEvent

        CandidateFollow.objects.create(user=self.u, candidate=self.c)
        consent.grant(self.u, "political_following")
        PvcVerification.objects.create(user=self.u, vin_last4="4321", surname="Exporter", state="lagos", lga="ikeja", status="successful",
                                       consent_at=NOW, reviewed_by=self.rev)
        Membership.objects.create(user=self.u, full_name="Ada Exporter", nin_last4="9876", state="lagos", status="draft")
        a = Article.objects.create(source="community", status="successful", title="Ikeja polling unit opens early", body="x " * 200,
                                   author=self.u, state="lagos", decided_by=self.rev, decision_reason="Fine", published_at=NOW,
                                   submitted_at=NOW)
        ArticleEvent.objects.create(article=a, event="approved", actor=self.rev, note="ok")
        other = Article.objects.create(source="community", status="successful", title="Another story from Surulere", body="y " * 200,
                                       author=self.reporter, state="lagos", published_at=NOW, submitted_at=NOW)
        self.theirs = MS.report("article", a, "spam", "Rita thinks this is spam", user=self.reporter)["reference"]
        self.mine = MS.report("article", other, "false", "I think this is false", user=self.u)["reference"]
        self.assertTrue(Report.objects.filter(reference=self.theirs).exists())
        RQ.open_request(self.u, "access", "Please send me everything you hold.")

    def test_every_category_masked_and_no_other_identity(self):
        data = build_export(self.u)
        z = zipfile.ZipFile(io.BytesIO(data))
        names = set(z.namelist())
        self.assertEqual(names, {f"{a}.json" for a in EXPORT_APPS} | {"README.txt", "manifest.json"})
        blob = "".join(z.read(n).decode() for n in names)
        for f in names - {"README.txt"}:
            self.assertEqual(json.loads(z.read(f))["schema_version"], 1)
        self.assertIn("Ada Exporter", blob)
        self.assertIn("*******9876", blob)
        self.assertIn("4321", blob)
        for other in (self.reporter, self.mod, self.rev, self.officer):
            for value in (other.email, other.full_name, str(other.pk), other.clerk_user_id):
                self.assertNotIn(value, blob, value)
        self.assertNotIn(self.theirs, blob)  # someone else's report about them
        self.assertNotIn("Rita thinks", blob)
        self.assertIn(self.mine, blob)  # their own report

    def test_endpoint_streams_no_store_counts_and_limits(self):
        before = set(os.listdir("/tmp"))
        with override_settings(REST_FRAMEWORK={**__import__("django.conf").conf.settings.REST_FRAMEWORK,
                                               "DEFAULT_THROTTLE_RATES": {**__import__("django.conf").conf.settings.REST_FRAMEWORK[
                                                   "DEFAULT_THROTTLE_RATES"], "data_export": "3/day"}}):
            from rest_framework.settings import api_settings

            api_settings.reload()
            from apps.privacy.views import ExportThrottle

            ExportThrottle.THROTTLE_RATES = api_settings.DEFAULT_THROTTLE_RATES
            for _ in range(3):
                r = self.client.post(reverse("my-data-export"), **tok(self.u))
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r["Content-Type"], "application/zip")
                self.assertIn("attachment", r["Content-Disposition"])
                self.assertIn("no-store", r["Cache-Control"])
            r = self.client.post(reverse("my-data-export"), **tok(self.u))
            self.assertEqual((r.status_code, r.data["error"]["code"]), (429, "export_limit"))
        api_settings.reload()
        ExportThrottle.THROTTLE_RATES = api_settings.DEFAULT_THROTTLE_RATES
        self.assertEqual(set(os.listdir("/tmp")) - before, set())
        self.assertEqual(DataRequest.objects.filter(user=self.u, self_served=True).count(), 3)

    def test_reverification_required(self):
        r = self.client.post(reverse("my-data-export"), **tok(self.u, fva=(30, -1)))
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.data["error"]["code"], "reverification_required")
        self.assertEqual(r.data["error"]["clerk_error"]["reason"], "reverification-error")
        self.assertEqual(r.data["error"]["clerk_error"]["metadata"]["reverification"]["level"], "first_factor")
        self.assertEqual(self.client.post(reverse("my-data-export"), **tok(self.u, fva=(2, -1))).status_code, 200)

    def test_officer_export_audited_with_reference(self):
        req = DataRequest.objects.get(user=self.u, self_served=False)
        r = client(self.officer).post(reverse("admin-privacy-request-export", args=[req.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(AuditLog.objects.filter(action="privacy.data_exported_by_officer", metadata__reference=req.reference).exists())


# --------------------------------------------------------------------------- self-deletion
@override_settings(CLERK_JWT_KEY=PUBLIC_PEM)
class SelfDeletionTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.u = mk("u_del")
        self.c = mk_candidate()
        CandidateFollow.objects.create(user=self.u, candidate=self.c)
        PvcVerification.objects.create(user=self.u, vin_last4="1111", surname="Del", state="lagos", lga="ikeja", status="successful",
                                       consent_at=NOW)

    def post(self, **kw):
        return self.client.post(reverse("my-delete"), {"confirm": "delete"}, format="json", **tok(self.u, **kw))

    def test_reverification_and_typed_confirm(self):
        self.assertEqual(self.post(fva=(15, -1)).data["error"]["code"], "reverification_required")
        r = self.client.post(reverse("my-delete"), {"confirm": "yes"}, format="json", **tok(self.u))
        self.assertEqual(r.status_code, 400)

    def test_clerk_failure_leaves_everything(self):
        with mock.patch("apps.accounts.clerk.delete_user", return_value=False):
            r = self.post()
        self.assertEqual((r.status_code, r.data["error"]["code"]), (503, "auth_unavailable"))
        self.u.refresh_from_db()
        self.assertTrue(self.u.is_active)
        self.assertTrue(CandidateFollow.objects.filter(user=self.u).exists())

    def test_success_erases_at_once_and_webhook_is_noop(self):
        from apps.accounts.services import mark_deleted
        from apps.donations.models import Donation

        Donation.objects.create(reference="DON-XYZ123", amount_kobo=500000, designation="party", donor_user=self.u, donor_name="Person",
                                donor_email="u_del@example.com", status="successful", public_label="Person")
        with mock.patch("apps.accounts.clerk.delete_user", return_value=True):
            r = self.post()
        self.assertEqual(r.status_code, 200, r.data)
        self.u.refresh_from_db()
        self.assertFalse(self.u.is_active)
        self.assertEqual(self.u.email, "")
        self.assertFalse(CandidateFollow.objects.filter(user=self.u).exists())
        self.assertFalse(PvcVerification.objects.filter(user=self.u).exists())
        self.assertTrue(Donation.objects.filter(reference="DON-XYZ123").exists())  # kept for returns
        self.assertTrue(AuditLog.objects.filter(action="privacy.account_deleted_self").exists())
        n = AuditLog.objects.count()
        mark_deleted(self.u.clerk_user_id)  # the later webhook
        self.assertEqual(AuditLog.objects.count(), n)


# --------------------------------------------------------------------------- requests
class RequestTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.u = mk("u_req")
        self.dpo = mk("u_dpo2", Role.PRIVACY_OFFICER)

    def test_reference_due_pause_resume_close_and_notifications(self):
        r = client(self.u).post(reverse("my-privacy-requests"), {"kind": "rectification", "details": "My LGA is wrong on the register."},
                                format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(r.data["reference"].startswith("PRV-"))
        req = DataRequest.objects.get(reference=r.data["reference"])
        self.assertAlmostEqual((req.due_at - req.received_at).days, 30)
        c = client(self.dpo)
        self.assertEqual(c.post(reverse("admin-privacy-request-ask-info", args=[req.pk]), {"text": "Which LGA is right?"}).status_code, 200)
        DataRequest.objects.filter(pk=req.pk).update(paused_at=timezone.now() - timedelta(days=2))
        r = client(self.u).post(reverse("my-privacy-request-answer", args=[req.pk]), {"text": "Ikeja, not Alimosho."})
        self.assertEqual(r.status_code, 200, r.data)
        req.refresh_from_db()
        self.assertEqual(req.status, "open")
        self.assertGreaterEqual((req.due_at - req.received_at).days, 32)
        c.post(reverse("admin-privacy-request-reply", args=[req.pk]), {"text": "Thanks, fixing it now."})
        r = c.post(reverse("admin-privacy-request-close", args=[req.pk]), {"outcome": "done", "text": "Register corrected to Ikeja."})
        self.assertEqual(r.data["status"], "done")
        kinds = list(Notification.objects.filter(recipient=self.u, kind="privacy_request_update").values_list("decision", flat=True))
        self.assertEqual(sorted(kinds), sorted(["opened", "waiting", "reply", "done"]))
        self.assertTrue(AuditLog.objects.filter(action="privacy.request_closed").exists())
        hist = client(self.u).get(reverse("my-privacy")).data["requests"][0]
        self.assertTrue(all(e["event"] != "" for e in hist["events"]))
        self.assertNotIn("by", hist["events"][0])  # never names the officer

    def test_refused_and_invalid_transition(self):
        req = RQ.open_request(self.u, "erasure", "Delete my donation record please.")
        RQ.close(req.pk, self.dpo, "refused", "Kept for the party's returns (Electoral Act s. 89).")
        r = client(self.dpo).post(reverse("admin-privacy-request-reply", args=[req.pk]), {"text": "More words here."})
        self.assertEqual((r.status_code, r.data["error"]["code"]), (409, "invalid_transition"))

    def test_officer_view_audited_once_a_day_and_access(self):
        req = RQ.open_request(self.u, "access", "Everything please, thank you.")
        for _ in range(3):
            self.assertEqual(client(self.dpo).get(reverse("admin-privacy-request", args=[req.pk])).status_code, 200)
        self.assertEqual(AuditLog.objects.filter(action="privacy.request_viewed").count(), 1)
        self.assertEqual(client(mk("u_mod2", Role.MODERATOR)).get(reverse("admin-privacy-requests")).status_code, 403)
        sa = mk("u_sa2", Role.SYSTEM_ADMIN)
        self.assertEqual(client(sa).get(reverse("admin-privacy-requests")).status_code, 200)  # implied (§15.2)

    def test_due_words(self):
        req = RQ.open_request(self.u, "access", "Everything please, thank you.")
        self.assertEqual(RQ.due_words(req, req.received_at), "Due in 30 days")
        self.assertEqual(RQ.due_words(req, req.due_at + timedelta(days=2, hours=1)), "Overdue by 2 days")


# --------------------------------------------------------------------------- admin MFA and reverification
def admin_url_names():
    out = []

    def walk(r, prefix=""):
        for p in r.url_patterns:
            if hasattr(p, "url_patterns"):
                walk(p, prefix + str(p.pattern))
            elif p.name and (prefix + str(p.pattern)).startswith("api/v1/admin/"):
                out.append((p.name, prefix + str(p.pattern)))

    walk(get_resolver())
    return out


def sample_path(pattern):
    import re
    import uuid

    path = "/" + pattern
    path = re.sub(r"<uuid:\w+>", str(uuid.uuid4()), path)
    path = re.sub(r"<int:\w+>", "1", path)
    path = re.sub(r"<(?:slug|str):\w+>", "x", path)
    return path.replace("^", "").replace("$", "")


@override_settings(CLERK_JWT_KEY=PUBLIC_PEM, ADMIN_MFA_REQUIRED=True)
class AdminMfaTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.sa = mk("u_mfa_sa", Role.SYSTEM_ADMIN)

    def test_every_admin_endpoint_refuses_without_mfa(self):
        names = admin_url_names()
        self.assertGreater(len(names), 40)
        for name, pattern in names:
            r = self.client.get(sample_path(pattern), **tok(self.sa, mfa=False))
            self.assertEqual(r.status_code, 403, (name, r.status_code))
            self.assertEqual(r.data["error"]["code"], "mfa_required", name)
        r = self.client.get(reverse("admin-overview"), **tok(self.sa, mfa=None))
        self.assertTrue(r.data["error"]["claim_missing"])
        self.assertEqual(self.client.get(reverse("admin-overview"), **tok(self.sa, mfa=True)).status_code, 200)

    def test_candidate_only_accounts_unaffected(self):
        cand = mk("u_cand_only")
        c = mk_candidate()
        grant_role(user=cand, role=Role.CANDIDATE, scope_type=ScopeType.CANDIDATE, scope_id=c.pk)
        r = self.client.get(reverse("admin-overview"), **tok(cand, mfa=False))
        self.assertEqual(r.data["error"]["code"], "permission_denied")
        self.assertEqual(self.client.get(reverse("me"), **tok(cand, mfa=False)).status_code, 200)

    @override_settings(ADMIN_MFA_REQUIRED=False)
    def test_dev_mode_allowed(self):
        self.assertEqual(self.client.get(reverse("admin-overview"), **tok(self.sa, mfa=False, fva=None)).status_code, 200)

    def test_me_reports_mfa_and_last_seen(self):
        r = self.client.get(reverse("me"), **tok(self.sa, mfa=True))
        self.assertTrue(r.data["mfa_enabled"])
        self.sa.refresh_from_db()
        self.assertIsNotNone(self.sa.last_seen_at)
        self.assertTrue(self.sa.mfa_enabled)


@override_settings(CLERK_JWT_KEY=PUBLIC_PEM, ADMIN_MFA_REQUIRED=True)
class ReverificationTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.sa = mk("u_rv_sa", Role.SYSTEM_ADMIN)

    def test_every_listed_endpoint_refuses_stale_and_accepts_fresh(self):
        from apps.access.security import SECOND_FACTOR

        patterns = dict(admin_url_names())
        for name, methods in SECOND_FACTOR.items():
            path = sample_path(patterns[name])
            for m in methods:
                method, _, q = m.partition(":")
                url = path + ("?format=csv" if q == "csv" else "")
                call = getattr(self.client, method.lower())
                stale = call(url, **tok(self.sa, fva=(1, 11)))
                self.assertEqual(stale.status_code, 403, (name, m))
                err = stale.data["error"]
                self.assertEqual(err["code"], "reverification_required", (name, m))
                self.assertEqual(err["clerk_error"], {"type": "forbidden", "reason": "reverification-error",
                                                      "metadata": {"reverification": {"level": "second_factor", "afterMinutes": 10}}})
                ok = call(url, **tok(self.sa, fva=(1, 3)))
                self.assertNotEqual((ok.status_code, (ok.data or {}).get("error", {}).get("code") if hasattr(ok, "data") and
                                     isinstance(ok.data, dict) else None), (403, "reverification_required"), (name, m))

    def test_csv_only_when_csv(self):
        r = self.client.get(reverse("admin-privacy-incidents"), **tok(self.sa, fva=(1, 30)))
        self.assertEqual(r.status_code, 200)

    def test_nin_reveal_needs_fresh_second_factor(self):
        from apps.membership.models import Membership

        m = Membership.objects.create(user=mk("u_member"), full_name="Mem Ber", nin_last4="1234", state="lagos", status="draft")
        url = reverse("admin-membership-reveal-nin", args=[m.pk])
        self.assertEqual(self.client.post(url, **tok(self.sa, fva=(0, 20))).data["error"]["code"], "reverification_required")


@override_settings(CLERK_JWT_KEY=PUBLIC_PEM, ADMIN_MFA_REQUIRED=False)
class DeniedProbeTests(APITestCase):
    def test_more_than_20_admin_403s_audited_once(self):
        cache.clear()
        u = mk("u_prober")
        for _ in range(25):
            self.client.get(reverse("admin-overview"), **tok(u))
        self.assertEqual(AuditLog.objects.filter(action="security.admin_access_denied").count(), 1)


# --------------------------------------------------------------------------- access review
@override_settings(CLERK_JWT_KEY=PUBLIC_PEM, ADMIN_MFA_REQUIRED=False)
class AccessReviewTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.sa = mk("u_ar_sa", Role.SYSTEM_ADMIN)
        self.idle = mk("u_idle", Role.MODERATOR)
        User.objects.filter(pk=self.idle.pk).update(last_seen_at=timezone.now() - timedelta(days=61))
        RoleAssignment.objects.update(created_at=timezone.now() - timedelta(days=91))

    def test_due_flags_finish_audited(self):
        from apps.dashboard.services import overview
        from apps.privacy.dashboard import access_review_due

        self.assertTrue(access_review_due(timezone.now()))
        self.assertIn("access_review_due", [c["key"] for c in overview(self.sa)["attention"]])
        r = client(self.sa).get(reverse("admin-team-review"))
        self.assertEqual(r.status_code, 200)
        row = next(x for x in r.data["results"] if x["user"]["id"] == str(self.idle.pk))
        self.assertTrue(row["inactive"])
        r = client(self.sa).post(reverse("admin-team-review"), {"notes": "Quarterly"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(AuditLog.objects.filter(action="security.access_review_completed").exists())
        self.assertFalse(access_review_due(timezone.now()))
        self.assertEqual(AccessReview.objects.count(), 1)

    def test_finish_needs_reverification(self):
        r = self.client.post(reverse("admin-team-review"), {}, format="json", **tok(self.sa, fva=(30, 30)))
        self.assertEqual(r.data["error"]["code"], "reverification_required")


# --------------------------------------------------------------------------- incidents
class IncidentTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.dpo = mk("u_inc_dpo", Role.PRIVACY_OFFICER)
        self.a, self.b = mk("u_a", email="a@example.com"), mk("u_b")

    def log(self, **kw):
        body = {"title": "Leaked admin session", "is_personal_data_breach": True, "is_cyber_incident": True, "risk": "high",
                "data_categories": ["account"], **kw}
        r = client(self.dpo).post(reverse("admin-privacy-incidents"), body, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    def test_clocks_per_flags_and_risk(self):
        d = self.log()
        self.assertEqual([c["key"] for c in d["clocks"]], ["ndpc", "ngcert", "people"])
        self.assertTrue(d["clocks"][0]["words"].startswith("NDPC: due in 71 hours") or d["clocks"][0]["words"].startswith("NDPC: due in 2 days"))
        self.assertEqual(d["clocks"][2]["words"], "People affected: tell them now")
        inc = Incident.objects.get(pk=d["id"])
        inc.risk = "none"
        self.assertEqual([c["key"] for c in I.clocks(inc)], ["ngcert"])
        inc.is_cyber_incident, inc.risk = False, "low"
        self.assertEqual([c["key"] for c in I.clocks(inc)], ["ndpc"])
        later = inc.aware_at + timedelta(hours=75)
        self.assertEqual(I.clocks(inc, later)[0]["state"], "overdue")
        self.assertIn("overdue by 3 hours", I.clocks(inc, later)[0]["words"])

    def test_notifications_clear_clocks(self):
        d = self.log()
        for kind in ("ndpc", "ngcert"):
            r = client(self.dpo).post(reverse("admin-privacy-incident-notifications", args=[d["id"]]),
                                      {"kind": kind, "how": "Portal submission", "reference": "REF-1"}, format="json")
            self.assertEqual(r.status_code, 200, r.data)
        states = {c["key"]: c["state"] for c in r.data["clocks"]}
        self.assertEqual((states["ndpc"], states["ngcert"]), ("done", "done"))

    def test_notice_audience_matching_once_per_version(self):
        d = self.log()
        lines = f"{self.a.email}\n{self.b.pk}\nnobody@example.com\nnot-an-id"
        url = reverse("admin-privacy-incident-notice-preview", args=[d["id"]])
        p = client(self.dpo).post(url, {"audience": "list", "lines": lines}, format="json").data
        self.assertEqual((p["count"], p["unmatched_count"]), (2, 2))
        body = {"audience": "list", "lines": lines, "text": "We fixed a sign-in problem. Please review /account/privacy for details."}
        url = reverse("admin-privacy-incident-notice", args=[d["id"]])
        r = client(self.dpo).post(url, body, format="json")
        self.assertEqual((r.status_code, r.data["sent"]), (200, 2), r.data)
        self.assertEqual(Notification.objects.filter(kind="security_notice").count(), 2)
        self.assertEqual(client(self.dpo).post(url, body, format="json").data["error"]["code"], "notice_already_sent")
        n = client(self.a).get(reverse("my-privacy")).data["security_notices"]
        self.assertEqual(len(n), 1)
        self.assertIn("sign-in problem", n[0]["text"])
        bad = {**body, "text": "Visit https://evil.example to reset your password right now."}
        client(self.dpo).patch(reverse("admin-privacy-incident", args=[d["id"]]), {"risk": "low"}, format="json")  # new version
        self.assertEqual(client(self.dpo).post(url, bad, format="json").status_code, 400)

    def test_close_csv_audited_and_free_of_personal_data(self):
        d = self.log(description="Internal: a@example.com session token was copied")
        url = reverse("admin-privacy-incident-close", args=[d["id"]])
        self.assertEqual(client(self.dpo).post(url, {"root_cause": "x"}, format="json").status_code, 400)
        r = client(self.dpo).post(url, {"root_cause": "A reused password on a shared laptop.", "lessons": "Require MFA."}, format="json")
        self.assertEqual(r.data["status"], "closed")
        r = client(self.dpo).get(reverse("admin-privacy-incidents") + "?format=csv")
        text = r.content.decode()
        self.assertIn("Leaked admin session", text)
        self.assertNotIn("a@example.com", text)
        self.assertTrue(AuditLog.objects.filter(action="security.incidents_exported").exists())

    def test_access_restricted(self):
        self.assertEqual(client(mk("u_fin", Role.FINANCE_OFFICER)).get(reverse("admin-privacy-incidents")).status_code, 403)


# --------------------------------------------------------------------------- audit chain
class AuditChainTests(TestCase):
    def test_sequential_and_tamper_detected(self):
        for i in range(5):
            record(AuditLog.Action.USER_VIEWED, actor_label="system:test", n=i)
        r = verify_chain()
        self.assertTrue(r["ok"], r)
        rows = list(AuditLog.objects.order_by("seq"))
        self.assertEqual([x.seq for x in rows], list(range(rows[0].seq, rows[0].seq + len(rows))))
        self.assertEqual(rows[1].prev_hash, rows[0].hash)
        AuditLog.objects.filter(pk=rows[2].pk).update(metadata={"n": 99})
        self.assertEqual(verify_chain()["broken_at"], rows[2].seq)

    def test_deleted_row_detected(self):
        for i in range(4):
            record(AuditLog.Action.USER_VIEWED, actor_label="system:test", n=i)
        rows = list(AuditLog.objects.order_by("seq"))
        AuditLog.objects.filter(pk=rows[1].pk).delete()
        self.assertFalse(verify_chain()["ok"])

    def test_prune_with_anchor_still_verifies(self):
        for i in range(6):
            record(AuditLog.Action.USER_VIEWED, actor_label="system:test", n=i)
        rows = list(AuditLog.objects.order_by("seq"))
        AuditLog.objects.filter(pk__in=[r.pk for r in rows[:3]]).update(created_at=timezone.now() - timedelta(days=7 * 365))
        call_command("prune_audit", stdout=io.StringIO())
        self.assertEqual(AuditAnchor.objects.get().seq, rows[2].seq)
        self.assertTrue(verify_chain()["ok"])
        record(AuditLog.Action.USER_VIEWED, actor_label="system:test", n=7)
        self.assertTrue(verify_chain()["ok"])

    def test_failed_hash_leaves_action_committed(self):
        with mock.patch("apps.audit.services.chain", side_effect=RuntimeError("boom")), self.assertLogs("apps.audit.services", "ERROR"):
            row = record(AuditLog.Action.USER_VIEWED, actor_label="system:test")
        self.assertIsNotNone(row)
        self.assertTrue(AuditLog.objects.filter(seq__isnull=True).exists())
        self.assertFalse(verify_chain()["ok"])

    def test_verify_job_and_readiness(self):
        from apps.privacy import checks

        record(AuditLog.Action.USER_VIEWED, actor_label="system:test")
        call_command("verify_audit", stdout=io.StringIO())
        self.assertEqual(checks.audit_state()[0], "PASS")
        AuditLog.objects.update(action="user.updated")
        with self.assertRaises(Exception):
            call_command("verify_audit", stdout=io.StringIO())
        state, words = checks.audit_state()
        self.assertEqual(state, "FAIL")
        self.assertIn("chain broken at #", words)


# --------------------------------------------------------------------------- headers, checks, logs
class HeadersAndChecksTests(TestCase):
    def test_api_headers(self):
        r = APIClient().get(reverse("privacy-notice"))
        self.assertEqual(r["Content-Security-Policy"], "default-src 'none'; frame-ancestors 'none'")
        self.assertIn("camera=()", r["Permissions-Policy"])
        self.assertEqual(r["Cross-Origin-Resource-Policy"], "same-site")
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")

    @override_settings(DEBUG=False, SECRET_KEY="k" * 60, ALLOWED_HOSTS=["api.ndcconnect.ng"], SECURE_SSL_REDIRECT=True,
                       SECURE_HSTS_SECONDS=31536000, CORS_ALLOWED_ORIGINS=["https://ndcconnect.ng"], CLERK_SECRET_KEY="sk_test_dummy",
                       CLERK_JWT_KEY="dummy", ADMIN_MFA_REQUIRED=True, SECURITY_CONTACT_EMAIL="security@ndcconnect.ng",
                       PRIVACY_CONTACT_EMAIL="dpo@ndcconnect.ng", PAYSTACK_SECRET_KEY="sk_test_dummy",
                       PRIVACY_CONSENT_REQUIRED=True, REVERIFICATION_REQUIRED=True)
    def test_check_security_fails_by_name_without_values(self):
        from django.conf import settings

        db = {**settings.DATABASES["default"], "OPTIONS": {"sslmode": "require"}}
        with override_settings(DATABASES={"default": db}):
            out = io.StringIO()
            call_command("check_security", stdout=out)  # only warnings left (restore test, audit, claim): exit 0
            text = out.getvalue()
            self.assertIn("0 failed", text)
        for secret in ("sk_test_dummy", "k" * 60, "security@ndcconnect.ng", "dpo@ndcconnect.ng"):
            self.assertNotIn(secret, text)
        self.assertIn("PASS DEBUG off", text)
        with override_settings(DEBUG=True, DATABASES={"default": db}):
            out = io.StringIO()
            with self.assertRaises(SystemExit):
                call_command("check_security", stdout=out)
            self.assertIn("FAIL DEBUG off", out.getvalue())

    def test_each_check_fails_when_wrong(self):
        from apps.privacy import checks

        bad = {"DEBUG off": {"DEBUG": True}, "SECRET_KEY strong": {"SECRET_KEY": "insecure-dev-key-change-me"},
               "HTTPS redirect on": {"SECURE_SSL_REDIRECT": False}, "CORS origins use https": {"CORS_ALLOWED_ORIGINS": ["http://x.ng"]},
               "Admin two-step verification required": {"ADMIN_MFA_REQUIRED": False}, "Security contact set": {"SECURITY_CONTACT_EMAIL": ""},
               "Privacy contact set": {"PRIVACY_CONTACT_EMAIL": ""}}
        for name, s in bad.items():
            with override_settings(**s):
                self.assertEqual(dict(checks.run())[name], "FAIL", name)

    def test_logs_redact(self):
        text = _scrub("ada@example.com called 08031234567 and +2348031234567 with Bearer abc.def.ghi and sk_live_" + "A" * 20)
        for v in ("ada@example.com", "08031234567", "+2348031234567", "abc.def.ghi", "sk_live_AAAA"):
            self.assertNotIn(v, text)

    def test_logs_redacted_through_handlers(self):
        from django.conf import settings

        handlers = [h for h in logging.getLogger().handlers] + [h for n in settings.LOGGING.get("loggers", {}) for h in
                                                                  logging.getLogger(n).handlers]
        self.assertTrue(any(f.__class__.__name__ == "RedactPII" for h in handlers for f in h.filters) or handlers == [])

    def test_upload_views_check_content(self):
        """Every multipart view validates image bytes, not the file name (spec 14 §4.9)."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.candidates import photos
        from apps.membership import services as membership_services
        from apps.news import images as news_images

        self.assertIs(news_images.load_validated_rgb, photos.load_validated_rgb)
        self.assertIs(membership_services.load_validated_rgb, photos.load_validated_rgb)
        bad = SimpleUploadedFile("photo.jpg", b"<?php echo 1; ?>" + b"x" * 200, content_type="image/jpeg")
        with self.assertRaises(photos.PhotoError):
            photos.load_validated_rgb(bad)

    def test_upload_limit(self):
        from django.conf import settings

        self.assertEqual(settings.DATA_UPLOAD_MAX_MEMORY_SIZE, 12 * 1024 * 1024)
