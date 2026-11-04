"""Spec 15 §16 — API standards: request IDs, the error catalogue, rate limits, filters, readiness, the schema and the outbox."""
import io
import json
import re
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.cache.backends.db import DatabaseCache
from django.core.cache.backends.locmem import LocMemCache
from django.core.management import call_command
from django.db import transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.access.models import Role
from apps.access.services import grant_role
from apps.accounts.models import User
from apps.core import outbox
from apps.core.errors import CATALOGUE
from apps.core.models import OutboxJob
from apps.core.requestid import set_current
from apps.core.urls import ReadyThrottle

ROOT = Path(settings.BASE_DIR)
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


# ================================================================ request IDs (§4.10)
class RequestIdTests(TestCase):
    def test_generated_kept_or_replaced(self):
        r = client().get(reverse("health"))
        self.assertRegex(r["X-Request-ID"], r"^[0-9a-f]{32}$")
        r = client().get(reverse("health"), HTTP_X_REQUEST_ID="proxy-abc_12345")
        self.assertEqual(r["X-Request-ID"], "proxy-abc_12345")
        for bad in ("short", "has spaces in it!", "x" * 65, "<script>alert(1)</script>"):
            r = client().get(reverse("health"), HTTP_X_REQUEST_ID=bad)
            self.assertNotEqual(r["X-Request-ID"], bad)
            self.assertRegex(r["X-Request-ID"], r"^[0-9a-f]{32}$")

    def test_in_every_error_envelope_and_log_line(self):
        with self.assertLogs("apps.core.requests", "INFO") as logs:
            r = client().get(reverse("me"), HTTP_X_REQUEST_ID="trace-0001-abcd")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["error"]["request_id"], "trace-0001-abcd")
        line = logs.records[-1]
        self.assertEqual(line.request_id, "trace-0001-abcd")
        self.assertIn(" me ", line.getMessage())  # the route name, never the raw path
        # hand-built envelopes (webhooks) are stamped too
        r = client().post(reverse("payment-webhook", kwargs={"provider": "fake"}), data=b"{}", content_type="application/json",
                          HTTP_X_REQUEST_ID="trace-0002-abcd")
        self.assertEqual(r.json()["error"]["request_id"], "trace-0002-abcd")

    def test_unhandled_error_is_generic_with_reference(self):
        u = mk("u_err")
        c = client(u)
        c.raise_request_exception = False
        with mock.patch("apps.notifications.services.unread_count", side_effect=RuntimeError("SELECT secret FROM users")), \
                self.assertLogs("apps.core.exceptions", "ERROR"), self.assertLogs("django.request", "ERROR"):
            r = c.get(reverse("my-notifications-unread"), HTTP_X_REQUEST_ID="9f2c1a7e-0000-0000")
        self.assertEqual(r.status_code, 500)
        err = r.json()["error"]
        self.assertEqual((err["code"], err["request_id"]), ("server_error", "9f2c1a7e-0000-0000"))
        self.assertIn("Reference: 9f2c1a7e", err["message"])
        self.assertNotIn("SELECT", json.dumps(err))

    def test_outbox_rows_carry_the_request_id(self):
        token = set_current("req-for-outbox-1")
        try:
            job = outbox.enqueue("notify_fanout", {"kind": "announcement", "article": "x"}, dedupe_key="t:1", kick=False)
        finally:
            from apps.core.requestid import _current

            _current.reset(token)
        self.assertEqual(job.request_id, "req-for-outbox-1")


# ================================================================ error catalogue (§4.5)
CODE_PATTERNS = [
    re.compile(r"""\bcode\s*=\s*["']([a-z][a-z0-9_]+)["']"""),
    re.compile(r"""ServiceError\(\s*(?:f?["'][^"']*["']|[A-Za-z_][\w.]*)\s*,\s*["']([a-z][a-z0-9_]+)["']"""),
    re.compile(r"""_err\(\s*(?:f?["'][^"']*["']|[A-Za-z_][\w.]*)\s*,\s*["']([a-z][a-z0-9_]+)["']"""),
    re.compile(r"""["']code["']\s*:\s*["']([a-z][a-z0-9_]+)["']"""),
    re.compile(r"""default_code\s*=\s*["']([a-z][a-z0-9_]+)["']"""),
]
NOT_ERROR_CODES = {"utf", "json", "ok"}  # `code=` used for other things (encodings, test labels)


class CatalogueTests(TestCase):
    def scan(self):
        found = {}
        for path in (ROOT / "apps").rglob("*.py"):
            if "/tests/" in str(path) or "/migrations/" in str(path):
                continue
            text = path.read_text()
            for pat in CODE_PATTERNS:
                for m in pat.finditer(text):
                    found.setdefault(m.group(1), str(path.relative_to(ROOT)))
        return {k: v for k, v in found.items() if k not in NOT_ERROR_CODES}

    def test_every_code_raised_is_catalogued(self):
        found = self.scan()
        self.assertGreater(len(found), 50, "the scan found the codes")
        missing = {c: where for c, where in found.items() if c not in CATALOGUE}
        self.assertEqual(missing, {}, "add these to apps/core/errors.py")

    def test_catalogue_shape_and_standard_codes(self):
        for code, (st, meaning, web) in CATALOGUE.items():
            self.assertTrue(100 <= st < 600 and meaning and isinstance(web, bool), code)
        for code in ("validation_error", "not_authenticated", "permission_denied", "account_deactivated", "mfa_required",
                     "reverification_required", "consent_required", "not_found", "method_not_allowed", "invalid_transition", "removed",
                     "throttled", "server_error", "auth_unavailable", "storage_unavailable", "media_unavailable", "invalid_filter"):
            self.assertIn(code, CATALOGUE)

    def test_uncatalogued_code_is_flagged(self):
        from apps.core.exceptions import ServiceError, api_exception_handler

        with self.assertLogs("apps.core.exceptions", "WARNING") as logs:
            api_exception_handler(ServiceError("x", code="not_a_real_code_xyz", status_code=400), {})
        self.assertIn("not_a_real_code_xyz", logs.output[0])


# ================================================================ rate limits (§11.4)
class RateLimitTests(TestCase):
    def setUp(self):
        ReadyThrottle.cache.clear() if hasattr(ReadyThrottle.cache, "clear") else None

    def test_429_has_retry_after_header_and_field(self):
        loc = LocMemCache("rl-test", {})
        with mock.patch.object(ReadyThrottle, "rate", "2/min", create=True), mock.patch.object(ReadyThrottle, "cache", loc):
            codes = [client().get(reverse("ready")).status_code for _ in range(2)]
            r = client().get(reverse("ready"))
        self.assertEqual(codes, [200, 200])
        self.assertEqual(r.status_code, 429)
        err = r.json()["error"]
        self.assertEqual(err["code"], "throttled")
        self.assertEqual(int(r["Retry-After"]), err["retry_after"])
        self.assertIn(f"Try again in {err['retry_after']} seconds", err["message"])

    def test_database_cache_shares_counts_between_workers(self):
        # two cache clients = two Gunicorn workers; the memory cache would let each count separately
        a, b = DatabaseCache("ndc_cache", {}), DatabaseCache("ndc_cache", {})
        a.clear()
        with mock.patch.object(ReadyThrottle, "rate", "2/min", create=True):
            seen = []
            for i in range(3):
                with mock.patch.object(ReadyThrottle, "cache", a if i % 2 == 0 else b):
                    seen.append(client().get(reverse("ready"), REMOTE_ADDR="10.0.0.9").status_code)
        self.assertEqual(seen, [200, 200, 429])
        x, y = LocMemCache("w1", {}), LocMemCache("w2", {})
        with mock.patch.object(ReadyThrottle, "rate", "2/min", create=True):
            seen = []
            for i in range(3):
                with mock.patch.object(ReadyThrottle, "cache", x if i % 2 == 0 else y):
                    seen.append(client().get(reverse("ready"), REMOTE_ADDR="10.0.0.9").status_code)
        self.assertEqual(seen, [200, 200, 200], "per-process caches don't share counts (why production needs the database cache)")

    def test_cache_outage_fails_open_and_is_logged(self):
        broken = mock.Mock()
        broken.get.side_effect = ConnectionError("cache down")
        with mock.patch.object(ReadyThrottle, "cache", broken), self.assertLogs("apps.core.throttling", "WARNING"):
            self.assertEqual(client().get(reverse("ready")).status_code, 200)

    def test_every_scope_is_catalogued_with_a_rate(self):
        from apps.core.throttling import CATALOGUE as RL
        from config.settings import base

        rates = base.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
        self.assertEqual(sorted(set(rates) - set(RL)), [])
        self.assertEqual(sorted(set(RL) - set(rates)), [])
        used = set()
        for path in (ROOT / "apps").rglob("*.py"):
            if "/tests/" not in str(path):
                used |= set(re.findall(r'^\s+scope = "([a-z_]+)"\s*$', path.read_text(), re.M))
        self.assertEqual(sorted(used - set(RL)), [])

    def test_check_security_needs_a_shared_cache(self):
        from apps.privacy import checks

        results = dict(checks.run())
        self.assertEqual(results["Shared cache for rate limits"], checks.FAIL)
        with override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "ndc_cache"}}):
            self.assertEqual(dict(checks.run())["Shared cache for rate limits"], checks.PASS)


# ================================================================ filters, ordering, pagination (§4.6–4.8)
class FilterTests(TestCase):
    def test_unknown_choice_values_are_400_invalid_filter(self):
        sa = mk("u_sa", Role.SYSTEM_ADMIN)
        for url, user in [(reverse("candidates") + "?state=atlantis", None), (reverse("candidates") + "?position=king", None),
                          (reverse("news-list") + "?source=rumours", None), (reverse("admin-pvc") + "?status=maybe", sa),
                          (reverse("admin-integrations-webhooks") + "?status=lost", sa),
                          (reverse("admin-integrations-outbox") + "?status=everything", sa)]:
            r = client(user).get(url)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "invalid_filter"), url)
        # unknown parameter NAMES are ignored
        self.assertEqual(client().get(reverse("candidates") + "?colour=blue").status_code, 200)

    def test_ordering_helper(self):
        from apps.core import filters as F
        from apps.core.exceptions import ServiceError

        req = mock.Mock(query_params={"ordering": "-created_at"})
        self.assertEqual(F.ordering(req, {"created_at"}, ("-created_at", "-id")), ("-created_at", "-id"))
        with self.assertRaises(ServiceError):
            F.ordering(mock.Mock(query_params={"ordering": "follower_count"}), {"created_at"}, ("-created_at",))

    def test_page_envelope_and_past_the_end(self):
        sa = mk("u_sa2", Role.SYSTEM_ADMIN)
        r = client(sa).get(reverse("admin-integrations-webhooks"))
        self.assertEqual(set(r.json()), {"count", "next", "previous", "results"})
        self.assertEqual(client(sa).get(reverse("admin-integrations-webhooks") + "?page=9").status_code, 404)


# ================================================================ readiness (§12.5)
class ReadyTests(TestCase):
    def test_ok_names_only(self):
        r = client().get(reverse("ready"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ready", "checks": {"database": "ok", "migrations": "ok", "cache": "ok", "media": "ok",
                                                                  "outbox": "ok"}})
        self.assertEqual(r["Cache-Control"], "no-store")

    def test_failures_name_the_check_never_values(self):
        OutboxJob.objects.create(kind="notify_fanout", payload={}, next_attempt_at=timezone.now() - timedelta(minutes=20))
        with override_settings(MEDIA_BACKEND="cloudinary", CLOUDINARY_CLOUD_NAME="", CLOUDINARY_API_KEY="k", CLOUDINARY_API_SECRET="s3cr3t"):
            r = client().get(reverse("ready"))
        self.assertEqual(r.status_code, 503)
        body = r.json()
        self.assertEqual((body["status"], body["checks"]["media"], body["checks"]["outbox"]), ("not_ready", "failed", "failed"))
        self.assertNotIn("s3cr3t", json.dumps(body))

    def test_memory_cache_fails_outside_dev_and_tests(self):
        from apps.core import ready

        with mock.patch.object(ready, "shared_cache_expected", return_value=True):
            self.assertEqual(ready.run()[1]["cache"], "failed")

    def test_database_down_is_503(self):
        from apps.core import ready

        def down():
            raise Exception("down")

        with mock.patch.object(ready, "CHECKS", [("database", down), *ready.CHECKS[1:]]):
            ok, checks = ready.run()
        self.assertFalse(ok)
        self.assertEqual((checks["database"], checks["migrations"], checks["outbox"]), ("failed", "failed", "failed"))

    def test_provider_outage_does_not_fail_ready(self):
        from apps.integrations import http

        for _ in range(6):
            http.record_failure("paystack", "timeout")
        try:
            self.assertEqual(client().get(reverse("ready")).status_code, 200)
        finally:
            http.reset("paystack")


# ================================================================ the outbox (§9.3)
@outbox.handler("test_job")
def _test_job(payload):
    if payload.get("fail"):
        raise RuntimeError("provider said no; key=sk_test_abcdefgh123")
    _test_job.runs = getattr(_test_job, "runs", 0) + 1


class OutboxTests(TestCase):
    def test_rolled_back_with_the_caller(self):
        try:
            with transaction.atomic():
                outbox.enqueue("test_job", {}, dedupe_key="rb", kick=False)
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertFalse(OutboxJob.objects.exists())

    def test_dedupe_while_pending(self):
        self.assertIsNotNone(outbox.enqueue("test_job", {}, dedupe_key="d1", kick=False))
        self.assertIsNone(outbox.enqueue("test_job", {}, dedupe_key="d1", kick=False))
        OutboxJob.objects.update(status="done")
        self.assertIsNotNone(outbox.enqueue("test_job", {}, dedupe_key="d1", kick=False))

    def test_backoff_then_failed_and_scrubbed(self):
        job = outbox.enqueue("test_job", {"fail": True}, kick=False)
        t = timezone.now()
        delays = []
        for i in range(outbox.MAX_ATTEMPTS):
            OutboxJob.objects.filter(pk=job.pk).update(next_attempt_at=t)
            with self.assertLogs("apps.core.outbox", "WARNING"):
                outbox.process(now=t)
            job.refresh_from_db()
            if job.status == "pending":
                delays.append(round((job.next_attempt_at - t).total_seconds() / 60))
        self.assertEqual(delays, [1, 5, 30, 120, 720])
        self.assertEqual((job.status, job.attempts), ("failed", 6))
        self.assertNotIn("sk_test_abcdefgh123", job.last_error)

    def test_kicked_after_commit_and_claimed_once(self):
        _test_job.runs = 0
        with self.captureOnCommitCallbacks(execute=True):
            job = outbox.enqueue("test_job", {}, dedupe_key="k1")
        job.refresh_from_db()
        self.assertEqual((job.status, _test_job.runs), ("done", 1))
        outbox.process()
        self.assertEqual(_test_job.runs, 1)

    def test_retry_resets_and_stats(self):
        job = OutboxJob.objects.create(kind="test_job", payload={}, status="failed", attempts=6, next_attempt_at=timezone.now(),
                                       finished_at=timezone.now())
        self.assertEqual(outbox.stats()["failed"], 1)
        outbox.retry(job.pk)
        job.refresh_from_db()
        self.assertEqual((job.status, job.attempts), ("pending", 0))

    def test_prune(self):
        old = timezone.now() - timedelta(days=95)
        OutboxJob.objects.create(kind="x", status="done", next_attempt_at=old, finished_at=timezone.now() - timedelta(days=31))
        OutboxJob.objects.create(kind="x", status="failed", next_attempt_at=old, finished_at=old)
        OutboxJob.objects.create(kind="x", status="failed", next_attempt_at=old, finished_at=timezone.now() - timedelta(days=10))
        self.assertEqual(outbox.prune(), 2)


class OutboxConcurrencyTests(TransactionTestCase):
    def test_skip_locked_claims_each_job_once(self):
        import threading

        for i in range(20):
            OutboxJob.objects.create(kind="test_job", payload={}, next_attempt_at=timezone.now())
        claimed = []

        def worker():
            from django.db import connection

            claimed.extend(j.pk for j in outbox._claim(20, timezone.now()))
            connection.close()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(claimed), 20)
        self.assertEqual(len(set(claimed)), 20)


# ================================================================ the schema (§14)
class SchemaTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        out = subprocess.run([sys.executable, "manage.py", "spectacular", "--format", "openapi-json", "--validate", "--fail-on-warn"],
                             cwd=ROOT, capture_output=True, text=True, env={**__import__("os").environ,
                                                                           "DJANGO_SETTINGS_MODULE": "config.settings.schema"})
        cls.generated, cls.stderr, cls.code = out.stdout, out.stderr, out.returncode

    def test_generates_without_warnings_and_matches_the_snapshot(self):
        self.assertEqual(self.code, 0, self.stderr[-2000:])
        committed = (ROOT / "docs" / "openapi.json").read_text()
        self.assertEqual(json.loads(self.generated), json.loads(committed),
                         "docs/openapi.json is stale: DJANGO_SETTINGS_MODULE=config.settings.schema python manage.py spectacular "
                         "--format openapi-json --file docs/openapi.json")

    def test_every_operation_documented(self):
        d = json.loads(self.generated)
        ops = [(m, p, o) for p, v in d["paths"].items() for m, o in v.items()]
        self.assertGreater(len(ops), 150)
        for m, p, o in ops:
            where = f"{m.upper()} {p}"
            self.assertTrue(o.get("summary"), where)
            self.assertTrue(o.get("tags"), where)
            self.assertIn("security", o, where)
            self.assertTrue(o.get("x-permissions"), where)
            self.assertTrue(set(o.get("x-error-codes", [])) <= set(CATALOGUE), where)
            self.assertIn("server_error", o["x-error-codes"], where)
        ids = [o["operationId"] for *_, o in ops]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("x-error-codes", d)
        self.assertIn("x-rate-limits", d)
        self.assertEqual(d["x-rate-limits"]["ready"]["rate"], "120/min", "production rates, not test rates")

    def test_untyped_operations_only_shrink(self):
        d = json.loads(self.generated)
        untyped = {f"{m.upper()} {p}" for p, v in d["paths"].items() for m, o in v.items() if o.get("x-untyped")}
        baseline = {ln.strip() for ln in (ROOT / "docs" / "openapi-untyped.txt").read_text().splitlines()
                    if ln.strip() and not ln.startswith("#")}
        self.assertEqual(sorted(untyped - baseline), [], "new endpoints must declare their response schema with @extend_schema")
        for typed in ("GET /api/v1/ready/", "POST /api/v1/media/upload-signature/", "GET /api/v1/me/notifications/",
                      "GET /api/v1/admin/integrations/"):
            self.assertNotIn(typed, untyped)

    def test_reverified_and_public_operations_are_marked(self):
        d = json.loads(self.generated)
        retry = d["paths"]["/api/v1/admin/integrations/outbox/{id}/retry/"]["post"]
        self.assertIn("reverification_required", retry["x-error-codes"])
        self.assertTrue(any("reverification" in p for p in retry["x-permissions"]))
        self.assertEqual(d["paths"]["/api/v1/ready/"]["get"]["security"], [{}])
        self.assertEqual(d["paths"]["/api/v1/me/"]["get"]["security"], [{"bearerAuth": []}])

    def test_schema_endpoint_is_admin_only_in_production(self):
        self.assertIn(client().get("/api/v1/schema/").status_code, (401, 403))
        self.assertEqual(client(mk("u_plain")).get("/api/v1/schema/").status_code, 403)
        r = client(mk("u_sys", Role.SYSTEM_ADMIN)).get("/api/v1/schema/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("openapi", json.loads(r.content))


class CompatTests(TestCase):
    def setUp(self):
        self.old = {"paths": {"/api/v1/a/": {"get": {"responses": {"200": {"content": {"application/json": {"schema": {
            "type": "object", "properties": {"x": {"type": "string"}, "y": {"type": "integer"}}}}}}}}}}}

    def run_compat(self, new, log=""):
        import tempfile

        d = Path(tempfile.mkdtemp())
        (d / "old.json").write_text(json.dumps(self.old))
        (d / "new.json").write_text(json.dumps(new))
        (d / "changes.md").write_text(log)
        out = io.StringIO()
        call_command("api_compat", base=str(d / "old.json"), new=str(d / "new.json"), changes=str(d / "changes.md"), stdout=out)
        return out.getvalue()

    def test_additions_pass_removals_fail_unless_explained(self):
        from django.core.management.base import CommandError

        added = json.loads(json.dumps(self.old))
        added["paths"]["/api/v1/a/"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["z"] = {"type": "string"}
        added["paths"]["/api/v1/b/"] = {"get": {"responses": {}}}
        self.assertIn("API compatible", self.run_compat(added))
        removed = json.loads(json.dumps(self.old))
        del removed["paths"]["/api/v1/a/"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["x"]
        with self.assertRaises(CommandError):
            self.run_compat(removed)
        self.assertIn("explained", self.run_compat(removed, "- 2026-11-04 GET /api/v1/a/: x removed because …"))
        retyped = json.loads(json.dumps(self.old))
        retyped["paths"]["/api/v1/a/"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["y"] = {"type": "string"}
        with self.assertRaises(CommandError):
            self.run_compat(retyped)
        with self.assertRaises(CommandError):
            self.run_compat({"paths": {}})


# ================================================================ jobs, query counts (§12.6, §15.2)
class JobsAndQueriesTests(TestCase):
    def test_new_commands_are_known_jobs(self):
        from apps.core.jobs import KNOWN_JOBS

        for job in ("process_outbox", "prune_outbox", "prune_webhooks", "media_audit", "migrate_media_to_cloudinary"):
            self.assertIn(job, KNOWN_JOBS)
        call_command("process_outbox", stdout=io.StringIO())
        call_command("prune_outbox", stdout=io.StringIO())
        call_command("prune_webhooks", stdout=io.StringIO())
        call_command("media_audit", stdout=io.StringIO())

    def test_list_query_counts_do_not_grow_with_rows(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from apps.candidates.models import Candidate

        def count():
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(client().get(reverse("candidates")).status_code, 200)
            return len(ctx.captured_queries)

        def add(n, start):
            for i in range(start, start + n):
                Candidate.objects.create(slug=f"c{i}", full_name=f"Cand {i}", position="governor", election_year=2027, state="lagos",
                                         status="published", verified_at=NOW, published_at=NOW, photo_800=f"candidates/s{i}_800.webp",
                                         photo_200=f"candidates/s{i}_200.webp")

        add(2, 0)
        few = count()
        add(15, 2)
        self.assertEqual(count(), few)

    def test_explain_uses_indexes_for_the_due_outbox(self):
        from django.db import connection

        for i in range(50):
            OutboxJob.objects.create(kind="x", status="done", next_attempt_at=timezone.now(), finished_at=timezone.now())
        with connection.cursor() as cur:
            cur.execute("ANALYZE core_outboxjob")
            cur.execute("SET enable_seqscan = off")
            qs = OutboxJob.objects.filter(status="pending", next_attempt_at__lte=timezone.now()).order_by("next_attempt_at")
            plan = qs.explain()
            cur.execute("SET enable_seqscan = on")
        self.assertIn("core_outbox_due_idx", plan)
