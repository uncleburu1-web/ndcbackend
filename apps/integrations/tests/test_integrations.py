"""Spec 15 §16 — the shared HTTP client, Paystack and Clerk adapters, the webhook register and /admin/integrations."""
import http.server
import json
import threading
import time
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.access.models import Role
from apps.access.services import grant_role
from apps.accounts.models import User
from apps.accounts.tests.helpers import (
    PUBLIC_PEM,
    bearer,
    session_token,
    signed_webhook,
)
from apps.audit.models import AuditLog
from apps.core import outbox
from apps.core.models import OutboxJob
from apps.integrations import http as H
from apps.integrations import registry
from apps.integrations.models import WebhookReceipt
from apps.payments.providers.base import ProviderError

NOW = timezone.now()


def mk(cid, *roles):
    u = User.objects.create_user(cid, email=f"{cid}@example.com", full_name=f"Person {cid}", phone_number="+2348031234567",
                                 profile_completed_at=NOW, adult_confirmed_at=NOW)
    for r in roles:
        grant_role(user=u, role=r)
    return u


def client(user=None):
    c = APIClient()
    if user:
        c.force_authenticate(user)
    return c


class _Handler(http.server.BaseHTTPRequestHandler):
    hits = []
    mode = "ok"

    def _answer(self):
        _Handler.hits.append((self.command, self.path, dict(self.headers)))
        if _Handler.mode == "slow":
            threading.Event().wait(0.6)  # not time.sleep: the tests mock it
        code = 503 if _Handler.mode == "error" else 200
        body = json.dumps({"status": True, "data": {"ok": 1}}).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    do_GET = do_POST = _answer

    def log_message(self, *a):
        pass


class HttpClientTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        super().tearDownClass()

    def setUp(self):
        _Handler.hits, _Handler.mode = [], "ok"
        H.reset("testsvc")
        self.sleep = mock.patch("apps.integrations.http.time.sleep")  # no real backoff waits
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        H.reset("testsvc")

    def test_ok_sends_request_id_and_records_health(self):
        from apps.core.requestid import _current

        t = _current.set("rid-abcdef12")
        try:
            r = H.call("testsvc", "GET", self.base + "/v", operation="v", headers={"Authorization": "Bearer sk_test_secret123"})
        finally:
            _current.reset(t)
        self.assertEqual((r.status, r.json()["data"]["ok"]), (200, 1))
        self.assertEqual(_Handler.hits[0][2].get("X-Request-ID"), "rid-abcdef12")
        self.assertTrue(H.health("testsvc")["last_success"])

    def test_timeout_applies_and_retries_only_idempotent_calls(self):
        _Handler.mode = "slow"
        with mock.patch.dict(H.READ_TIMEOUTS, {"testsvc": 0.2}), self.assertLogs("apps.integrations", "INFO"):
            started = time.monotonic()
            with self.assertRaises(H.ProviderUnavailable):
                H.call("testsvc", "POST", self.base + "/init", json={"a": 1})
            self.assertLess(time.monotonic() - started, 0.6 + 0.5)
            self.assertEqual(len(_Handler.hits), 1, "POST (initialize) is never retried")
            _Handler.hits = []
            with self.assertRaises(H.ProviderUnavailable):
                H.call("testsvc", "GET", self.base + "/verify", idempotent=True)
            self.assertEqual(len(_Handler.hits), 3, "an idempotent GET: 1 try + 2 retries")

    def test_5xx_is_unavailable_4xx_is_returned(self):
        _Handler.mode = "error"
        with self.assertRaises(H.ProviderUnavailable):
            H.call("testsvc", "GET", self.base + "/x")
        self.assertEqual(H.health("testsvc")["last_reason"], "HTTP 503")

    def test_circuit_opens_after_5_failures_and_recovers(self):
        _Handler.mode = "error"
        for _ in range(5):
            with self.assertRaises(H.ProviderUnavailable):
                H.call("testsvc", "GET", self.base + "/x")
        self.assertTrue(H.is_open("testsvc"))
        _Handler.hits, _Handler.mode = [], "ok"
        with self.assertRaises(H.ProviderUnavailable) as e:
            H.call("testsvc", "GET", self.base + "/x")
        self.assertEqual((e.exception.reason, _Handler.hits), ("circuit open", []), "fails fast without calling")
        cache.delete(H._k("testsvc", "open"))  # the 30 s window passed: one trial call
        self.assertEqual(H.call("testsvc", "GET", self.base + "/x").status, 200)
        self.assertFalse(H.is_open("testsvc"))

    def test_nothing_secret_logged(self):
        with self.assertLogs("apps.integrations", "INFO") as logs:
            H.call("testsvc", "POST", self.base + "/init", headers={"Authorization": "Bearer sk_test_supersecret"},
                   json={"email": "donor@example.com"})
        text = "\n".join(logs.output)
        self.assertNotIn("sk_test_supersecret", text)
        self.assertNotIn("donor@example.com", text)
        self.assertIn("testsvc", text)

    def test_only_http_urls(self):
        with self.assertRaises(ValueError):
            H.call("testsvc", "GET", "file:///etc/passwd")

    def test_cache_outage_never_breaks_a_call(self):
        with mock.patch.object(H, "cache") as broken:
            broken.get.side_effect = ConnectionError
            broken.set_many.side_effect = ConnectionError
            broken.delete.side_effect = ConnectionError
            self.assertEqual(H.call("testsvc", "GET", self.base + "/x").status, 200)


# ================================================================ Paystack adapter on the client (§6)
@override_settings(PAYSTACK_SECRET_KEY="sk_test_dummy_key_1234")
class PaystackAdapterTests(TestCase):
    def test_calls_go_through_the_shared_client(self):
        from apps.payments.providers.paystack import PaystackProvider

        with mock.patch("apps.integrations.http.call", return_value=H.HttpResult(200, b'{"status": true, "data": {"x": 1}}', {})) as c:
            self.assertEqual(PaystackProvider()._call("GET", "/transaction/verify/NDC-1"), {"x": 1})
        args, kwargs = c.call_args
        self.assertEqual((args[0], args[1], kwargs["idempotent"]), ("paystack", "GET", True))
        with mock.patch("apps.integrations.http.call", return_value=H.HttpResult(200, b'{"status": true, "data": {}}', {})) as c:
            PaystackProvider()._call("POST", "/transaction/initialize", {"amount": 1})
        self.assertFalse(c.call_args.kwargs["idempotent"])

    def test_unreachable_and_refusals_are_provider_errors_without_the_key(self):
        from apps.payments.providers.paystack import PaystackProvider

        with mock.patch("apps.integrations.http.call", side_effect=H.ProviderUnavailable("paystack", "circuit open")):
            with self.assertRaises(ProviderError) as e:
                PaystackProvider()._call("POST", "/transaction/initialize", {})
        self.assertNotIn("sk_test", str(e.exception))
        with mock.patch("apps.integrations.http.call", return_value=H.HttpResult(401, b'{"status": false}', {})):
            with self.assertRaises(ProviderError):
                PaystackProvider()._call("GET", "/transaction/verify/NDC-1")

    def test_initialize_failure_leaves_pending_with_friendly_error(self):
        from apps.candidates.models import Candidate
        from apps.donations.models import Donation

        Candidate.objects.create(slug="amina", full_name="Amina Bello", position="president", election_year=2027, status="published",
                                 verified_at=NOW, published_at=NOW)
        payload = {"designation": "party", "amount_kobo": 500000, "donor_name": "Ada Obi", "donor_email": "ada@example.com",
                   "donor_phone": "08031234567", "donor_state": "lagos", "donor_address": "12 Allen Avenue, Ikeja",
                   "display_choice": "anonymous", "declarations": {"own_funds": True, "nigerian_source": True, "voluntary": True}}
        with override_settings(DONATIONS_ENABLED=True, DONATIONS_COMPLIANCE_APPROVED=True, PAYMENT_PROVIDER="paystack"), \
                mock.patch("apps.integrations.http.call", side_effect=H.ProviderUnavailable("paystack", "TimeoutError")), \
                self.assertLogs("apps.donations.services", "ERROR"):
            r = client().post(reverse("donations"), payload, format="json")
        self.assertIn(r.status_code, (502, 503), r.content)
        self.assertIn("No money was taken", r.json()["error"]["message"])  # 05's wording for "nothing was charged"
        d = Donation.objects.get()
        self.assertIn(d.status, ("initiated", "failed"))
        self.assertNotEqual(d.status, "successful")


# ================================================================ webhooks (§5.5, §6.5, §12.4)
def fake_webhook(c, event, reference, body=None):
    from apps.payments.providers.fake import FakeProvider, sign

    body = body or FakeProvider.webhook_body(event, reference)
    return c.post(reverse("payment-webhook", kwargs={"provider": "fake"}), data=body, content_type="application/json",
                  HTTP_X_FAKE_SIGNATURE=sign(body))


@override_settings(PAYMENT_PROVIDER="fake")
class WebhookRegisterTests(TestCase):
    def test_payment_webhook_once_no_payload_stored(self):
        with mock.patch("apps.donations.services.on_provider_event") as handler:
            self.assertEqual(fake_webhook(client(), "charge.success", "NDC-AAA111").status_code, 200)
            self.assertEqual(fake_webhook(client(), "charge.success", "NDC-AAA111").status_code, 200)  # a re-delivery
        self.assertEqual(handler.call_count, 1)
        r = WebhookReceipt.objects.get()
        self.assertEqual((r.provider, r.status, r.subject, r.event_type), ("fake", "processed", "NDC-AAA111", "charge.success"))
        self.assertEqual(len(r.body_sha256), 64)
        stored = json.dumps({f.name: str(getattr(r, f.name)) for f in WebhookReceipt._meta.fields})
        self.assertNotIn("reference", stored.replace("NDC-AAA111", ""))  # no body fields, only the hash

    def test_failed_processing_is_visible_and_retried(self):
        with mock.patch("apps.donations.services.on_provider_event", side_effect=RuntimeError("db hiccup")), \
                self.assertLogs("apps.payments.views", "ERROR"), self.assertLogs("django.request", "ERROR"):
            self.assertEqual(fake_webhook(client(), "charge.success", "NDC-BBB222").status_code, 500)
        r = WebhookReceipt.objects.get()
        self.assertEqual((r.status, r.error), ("failed", "RuntimeError"))
        job = OutboxJob.objects.get(kind="webhook_retry")
        self.assertGreater(job.next_attempt_at, timezone.now())
        with mock.patch("apps.donations.services.on_provider_event") as ok:
            self.assertTrue(outbox.run_job(job))
        ok.assert_called_once()
        r.refresh_from_db()
        self.assertEqual((r.status, r.attempts), ("processed", 2))
        # the provider's own retry after a failure is processed too (not treated as a duplicate)
        WebhookReceipt.objects.update(status="failed")
        with mock.patch("apps.donations.services.on_provider_event") as again:
            fake_webhook(client(), "charge.success", "NDC-BBB222")
        again.assert_called_once()

    def test_bad_signatures_rejected_counted_and_audited_past_20(self):
        cache.clear()
        for _ in range(21):
            r = client().post(reverse("payment-webhook", kwargs={"provider": "fake"}), data=b'{"event":"charge.success"}',
                              content_type="application/json", HTTP_X_FAKE_SIGNATURE="wrong")
            self.assertEqual(r.status_code, 400)
        self.assertEqual(AuditLog.objects.filter(action="security.webhook_signature_failures").count(), 1)
        self.assertFalse(WebhookReceipt.objects.exists())

    @override_settings(PAYMENT_PROVIDER="paystack", PAYSTACK_SECRET_KEY="sk_test_dummy_key_1234", PAYSTACK_WEBHOOK_IPS=["52.31.139.75"])
    def test_optional_paystack_ip_allow_list(self):
        r = client().post(reverse("payment-webhook", kwargs={"provider": "paystack"}), data=b"{}", content_type="application/json",
                          REMOTE_ADDR="10.1.1.1")
        self.assertEqual(r.status_code, 403)

    def test_prune_keeps_90_days(self):
        from django.core.management import call_command

        old = WebhookReceipt.objects.create(provider="fake", event_id="old")
        WebhookReceipt.objects.filter(pk=old.pk).update(received_at=timezone.now() - timedelta(days=91))
        WebhookReceipt.objects.create(provider="fake", event_id="new")
        call_command("prune_webhooks", stdout=__import__("io").StringIO())
        self.assertEqual(list(WebhookReceipt.objects.values_list("event_id", flat=True)), ["new"])


@override_settings(CLERK_SECRET_KEY="")
class ClerkWebhookTests(TestCase):
    def post(self, body, headers):
        return client().post(reverse("clerk-webhook"), data=body, content_type="application/json", **headers)

    def test_duplicates_no_op_and_replay_outside_window_rejected(self):
        from apps.accounts.tests.helpers import clerk_user

        body, headers = signed_webhook("user.created", clerk_user("user_wh1"), msg_id="msg_dup1")
        self.assertEqual(self.post(body, headers).status_code, 200)
        self.assertEqual(self.post(body, headers).status_code, 200)
        self.assertEqual(WebhookReceipt.objects.filter(provider="clerk", event_id="msg_dup1").count(), 1)
        self.assertEqual(User.objects.filter(clerk_user_id="user_wh1").count(), 1)
        body, headers = signed_webhook("user.created", clerk_user("user_wh2"), msg_id="msg_old", when=timezone.now() - timedelta(hours=1))
        self.assertEqual(self.post(body, headers).status_code, 400)
        self.assertFalse(User.objects.filter(clerk_user_id="user_wh2").exists())


class ClerkCallTests(TestCase):
    def test_ban_failure_is_queued_and_retried(self):
        from apps.accounts import clerk

        u = mk("user_banme")
        User.objects.filter(pk=u.pk).update(is_active=False)
        with override_settings(CLERK_SECRET_KEY="sk_test_x"), mock.patch.object(clerk, "set_banned", side_effect=TimeoutError), \
                self.assertLogs("apps.accounts.clerk", "ERROR"):
            self.assertFalse(clerk.ban_user("user_banme"))
        job = OutboxJob.objects.get(kind="clerk_ban")
        self.assertEqual(job.payload, {"clerk_user_id": "user_banme"})
        with mock.patch.object(clerk, "set_banned") as ok:
            self.assertTrue(outbox.run_job(job))
        ok.assert_called_once_with("user_banme", True)
        # reactivated meanwhile: a stale ban job does nothing
        User.objects.filter(pk=u.pk).update(is_active=True)
        stale = OutboxJob.objects.create(kind="clerk_ban", payload={"clerk_user_id": "user_banme"}, next_attempt_at=timezone.now())
        with mock.patch.object(clerk, "set_banned") as never:
            outbox.run_job(stale)
        never.assert_not_called()

    def test_clerk_calls_use_the_breaker_and_a_timeout(self):
        from apps.accounts import clerk

        H.reset("clerk")
        for _ in range(5):
            H.record_failure("clerk", "TimeoutError")
        try:
            with override_settings(CLERK_SECRET_KEY="sk_test_x"), self.assertLogs("apps.accounts.clerk", "WARNING"):
                self.assertIsNone(clerk.fetch_user("user_x"))  # circuit open: no network call
            with override_settings(CLERK_SECRET_KEY="sk_test_x"):
                self.assertEqual(clerk._client().sdk_configuration.timeout_ms, clerk.CLERK_TIMEOUT_MS)
        finally:
            H.reset("clerk")


class WebhookMigrationTests(TransactionTestCase):
    def test_processed_webhook_ids_move_to_receipts(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        ex = MigrationExecutor(connection)
        leaves = ex.loader.graph.leaf_nodes()
        before = [("integrations", "0001_initial"), ("accounts", "0003_adult_last_seen_mfa")]
        ex.migrate(before)
        old = MigrationExecutor(connection).loader.project_state(before).apps
        Old = old.get_model("accounts", "ProcessedWebhook")
        for i in range(3):
            Old.objects.create(svix_id=f"msg_{i}", event_type="user.updated")
        MigrationExecutor(connection).migrate(leaves)
        self.assertEqual(sorted(WebhookReceipt.objects.filter(provider="clerk").values_list("event_id", flat=True)),
                         ["msg_0", "msg_1", "msg_2"])


# ================================================================ /admin/integrations (§15.3)
class AdminIntegrationsTests(TestCase):
    def setUp(self):
        self.sa = mk("u_sa", Role.SYSTEM_ADMIN)

    def test_system_admins_only(self):
        self.assertEqual(client().get(reverse("admin-integrations")).status_code, 401)
        self.assertEqual(client(mk("u_pvc", Role.PVC_OFFICER)).get(reverse("admin-integrations")).status_code, 403)

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_ThisMustNotLeak", CLERK_SECRET_KEY="sk_test_NorThis", PAYMENT_PROVIDER="paystack")
    def test_overview_states_counts_and_no_values(self):
        H.reset("paystack")
        for _ in range(5):
            H.record_failure("paystack", "TimeoutError")
        WebhookReceipt.objects.create(provider="fake", event_id="a", status="failed")
        WebhookReceipt.objects.create(provider="fake", event_id="b", signature_valid=False, status="ignored")
        OutboxJob.objects.create(kind="media_destroy", status="failed", attempts=6, next_attempt_at=NOW, finished_at=NOW, last_error="x")
        try:
            r = client(self.sa).get(reverse("admin-integrations"))
        finally:
            H.reset("paystack")
        self.assertEqual(r.status_code, 200)
        text = json.dumps(r.json())
        self.assertNotIn("ThisMustNotLeak", text)
        self.assertNotIn("NorThis", text)
        svc = {s["key"]: s for s in r.json()["services"]}
        self.assertEqual(svc["paystack"]["state"], "unavailable")
        self.assertEqual(svc["cloudinary"]["state"], "not_in_use")
        self.assertIn("CLOUDINARY_API_SECRET", svc["cloudinary"]["settings"])  # names only
        self.assertEqual(r.json()["webhooks"], [{"provider": "fake", "processed": 0, "ignored": 1, "failed": 1, "bad_signatures": 1}])
        self.assertEqual((r.json()["outbox"]["failed"], r.json()["outbox"]["failed_by_kind"]), (1, {"media_destroy": 1}))
        self.assertEqual(r["Cache-Control"].count("no-store"), 1)

    def test_lists_and_retry_audited(self):
        WebhookReceipt.objects.create(provider="clerk", event_id="m1", status="failed")
        WebhookReceipt.objects.create(provider="fake", event_id="m2")
        r = client(self.sa).get(reverse("admin-integrations-webhooks") + "?provider=clerk&status=failed")
        self.assertEqual([x["provider"] for x in r.json()["results"]], ["clerk"])
        job = OutboxJob.objects.create(kind="media_destroy", status="failed", attempts=6, next_attempt_at=NOW, finished_at=NOW)
        r = client(self.sa).get(reverse("admin-integrations-outbox"))
        self.assertEqual([x["id"] for x in r.json()["results"]], [str(job.pk)])
        r = client(self.sa).post(reverse("admin-integrations-outbox-retry", args=[job.pk]))
        self.assertEqual((r.status_code, r.json()["status"], r.json()["attempts"]), (200, "pending", 0))
        self.assertTrue(AuditLog.objects.filter(action="integration.job_retried", metadata__kind="media_destroy").exists())
        OutboxJob.objects.filter(pk=job.pk).update(status="done")
        self.assertEqual(client(self.sa).post(reverse("admin-integrations-outbox-retry", args=[job.pk])).status_code, 409)

    @override_settings(CLERK_JWT_KEY=PUBLIC_PEM)
    def test_retry_needs_reverification(self):
        job = OutboxJob.objects.create(kind="x", status="failed", next_attempt_at=NOW, finished_at=NOW)
        stale = bearer(session_token(self.sa.clerk_user_id, mfa=True, fva=[30, 30]))
        r = APIClient().post(reverse("admin-integrations-outbox-retry", args=[job.pk]), **stale)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "reverification_required"))
        fresh = bearer(session_token(self.sa.clerk_user_id, mfa=True, fva=[0, 0]))
        self.assertEqual(APIClient().post(reverse("admin-integrations-outbox-retry", args=[job.pk]), **fresh).status_code, 200)

    def test_registry_lists_every_processor(self):
        from apps.privacy.register import processors

        names = {p.name for p in processors()}
        for i in registry.INTEGRATIONS:
            if i.in_use():
                self.assertIn(i.processor, names)
        with override_settings(MEDIA_BACKEND="cloudinary"):
            self.assertIn("Cloudinary", {p.name for p in processors()})

    def test_dashboard_card_and_readiness_group(self):
        from apps.dashboard.services import overview

        OutboxJob.objects.create(kind="notify_fanout", status="failed", next_attempt_at=NOW, finished_at=NOW)
        data = overview(self.sa)
        card = next(c for c in data["attention"] if c["key"] == "background_tasks_failing")
        self.assertEqual((card["count"], card["href"]), (1, "/admin/integrations"))
        items = {(i["group"], i["label"]): i for i in data["readiness"]["items"]}
        self.assertEqual(items[("integrations", "Background tasks")]["state"], "warning")
        self.assertEqual(items[("integrations", "Cloudinary")]["state"], "info")
