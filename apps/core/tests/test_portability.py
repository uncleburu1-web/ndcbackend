"""The backend runs on Windows too (the owner develops there): no glibc-only date formats, no platform text encodings,
no hard-coded Unix paths, and tests never read the developer's .env."""
import os
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[3]
SOURCES = [p for d in ("apps", "config") for p in (ROOT / d).rglob("*.py") if "migrations" not in p.parts]


def _text(p):
    return p.read_text(encoding="utf-8")


class PortabilityTests(SimpleTestCase):
    def test_no_glibc_only_date_formats(self):
        # %-d, %-H… crash with "Invalid format string" on Windows; use apps.core.dates.day_month or d.day.
        bad = [f"{p.relative_to(ROOT)}:{i}" for p in SOURCES for i, ln in enumerate(_text(p).splitlines(), 1)
               if re.search(r"%-[a-zA-Z]", ln) and p.name != "test_portability.py"]
        self.assertEqual(bad, [])

    def test_text_files_name_their_encoding(self):
        # Windows defaults to cp1252: every read_text/write_text and text-mode open() says utf-8.
        call = re.compile(r"\.(read_text|write_text)\(([^()]*(\([^()]*\))?[^()]*)\)")
        bad = []
        for p in SOURCES:
            if p.name == "test_portability.py":
                continue
            for i, ln in enumerate(_text(p).splitlines(), 1):
                for m in call.finditer(ln):
                    if "encoding" not in m.group(2):
                        bad.append(f"{p.relative_to(ROOT)}:{i}")
        self.assertEqual(bad, [])

    def test_no_hard_coded_unix_temp_dir(self):
        bad = [f"{p.relative_to(ROOT)}:{i}" for p in SOURCES for i, ln in enumerate(_text(p).splitlines(), 1)
               if re.search(r"""["']/tmp\b""", ln) and p.name != "test_portability.py"]
        self.assertEqual(bad, [])

    def test_tests_ignore_dotenv(self):
        # manage.py test sets NDC_TESTS=1 and the test settings; local keys, gates and consent switches don't leak in.
        self.assertEqual(os.environ.get("NDC_TESTS"), "1")
        self.assertTrue(settings.TESTING)
        self.assertEqual(settings.PAYMENT_PROVIDER, "fake")
        self.assertEqual(settings.CLERK_SECRET_KEY, "")
