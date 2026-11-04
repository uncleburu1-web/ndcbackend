"""GET /api/v1/ready/ (spec 15 §12.5). Names and ok/failed only, never values. Provider outages are not part of it."""
import time

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.utils import timezone

_MIGRATIONS = {"at": 0.0, "ok": False}


def shared_cache_expected() -> bool:
    return not (settings.DEBUG or getattr(settings, "TESTING", False))


def cache_is_shared() -> bool:
    return settings.CACHES["default"]["BACKEND"].endswith(("DatabaseCache", "RedisCache", "PyMemcacheCache"))


def _database():
    connection.ensure_connection()
    with connection.cursor() as cur:
        cur.execute("SELECT 1")
    return True


def _migrations():
    now = time.monotonic()
    if now - _MIGRATIONS["at"] < 60 and _MIGRATIONS["ok"]:
        return True
    from django.db.migrations.executor import MigrationExecutor

    ok = not MigrationExecutor(connection).migration_plan(MigrationExecutor(connection).loader.graph.leaf_nodes())
    _MIGRATIONS.update(at=now, ok=ok)
    return ok


def _cache():
    if shared_cache_expected() and not cache_is_shared():
        return False
    key = "ready:probe"
    cache.set(key, "1", 30)
    return cache.get(key) == "1"


def _media():
    if getattr(settings, "MEDIA_BACKEND", "local") == "cloudinary":
        from apps.media.cloudinary_api import configured

        return configured()
    return True


def _outbox():
    from .models import OutboxJob
    from .outbox import STUCK_AFTER

    oldest = (OutboxJob.objects.filter(status=OutboxJob.Status.PENDING, next_attempt_at__lte=timezone.now())
              .order_by("next_attempt_at").values_list("next_attempt_at", flat=True).first())
    return oldest is None or timezone.now() - oldest < STUCK_AFTER


CHECKS = [("database", _database), ("migrations", _migrations), ("cache", _cache), ("media", _media), ("outbox", _outbox)]


def run() -> tuple[bool, dict]:
    out, db_ok = {}, True
    for name, fn in CHECKS:
        if not db_ok and name in ("migrations", "outbox"):
            out[name] = "failed"
            continue
        try:
            ok = bool(fn())
        except Exception:
            ok = False
        out[name] = "ok" if ok else "failed"
        if name == "database":
            db_ok = ok
    return all(v == "ok" for v in out.values()), out
