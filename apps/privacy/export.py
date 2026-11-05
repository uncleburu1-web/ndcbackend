"""Build the 'Download your data' ZIP (spec 14 §6.2). Built in memory and streamed; never stored on disk."""
import importlib
import io
import json
import zipfile

from django.utils import timezone

from .register import CATEGORIES, PRIVACY_VERSION

SCHEMA_VERSION = 1
EXPORT_APPS = ["accounts", "access", "follows", "donations", "membership", "pvc", "news", "posts", "communities", "events", "volunteers", "notifications", "moderation", "privacy"]

README = """NDCConnect: your data
=====================

This archive holds the personal data NDCConnect keeps about you, as at {when}.
Each JSON file covers one part of the service. Dates are in UTC (ISO 8601). Amounts are in kobo (100 kobo = 1 naira).

What is left out, and why:
- Your NIN and VIN are shown masked. Ask the Data Protection Officer if you need the full values.
- Who reported you, who reviewed your content and which staff member made a decision are not included:
  that is other people's personal data.
- Security logs about the service (not about you) are not included.

Files:
{files}

Questions or corrections: {contact}
Privacy notice version {version}. Export format version {schema}.
"""


def exporter(app):
    return importlib.import_module(f"apps.{app}.privacy")


def build_export(user) -> bytes:
    from django.conf import settings

    buf = io.BytesIO()
    lines = []
    now = timezone.now()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for app in EXPORT_APPS:
            mod = exporter(app)
            data = mod.export_for(user)
            name = f"{app}.json"
            z.writestr(name, json.dumps({"schema_version": SCHEMA_VERSION, "about": mod.DESCRIPTION, **data}, indent=2, ensure_ascii=False,
                                        default=str))
            lines.append(f"- {name}: {mod.DESCRIPTION}")
        z.writestr("README.txt", README.format(when=now.isoformat(timespec="seconds"), files="\n".join(lines),
                                               contact=settings.PRIVACY_CONTACT_EMAIL, version=PRIVACY_VERSION, schema=SCHEMA_VERSION))
        z.writestr("manifest.json", json.dumps({"schema_version": SCHEMA_VERSION, "generated_at": now.isoformat(), "privacy_version": PRIVACY_VERSION,
                                                "categories": [c.key for c in CATEGORIES if c.public]}, indent=2))
    return buf.getvalue()


def filename(user) -> str:
    return f"ndcconnect-data-{timezone.now():%Y%m%d}.zip"
