"""
python manage.py i18n_import --lang ha --file ha-backend.csv [--status machine-draft|translated|reviewed] [--dry-run]

Spec 21 §15.3. Reads a translator's CSV (from i18n_export), checks it (known keys only, placeholders like {name} kept,
no empty English), writes locale/<lang>/LC_MESSAGES/django.po and compiles the .mo. Rows marked "machine-draft"
never count as reviewed; only a recorded review (Admin → Languages) can switch a language on.
"""
import csv
import re

from django.core.management.base import BaseCommand, CommandError

from apps.core import i18n

PLACEHOLDER = re.compile(r"\{[a-z_0-9]*\}|%\([a-z_]+\)s|%s|%d")


class Command(BaseCommand):
    help = "Import a translator's CSV of backend messages into the .po file and compile it."

    def add_arguments(self, parser):
        parser.add_argument("--lang", choices=i18n.TRANSLATED, required=True)
        parser.add_argument("--file", required=True)
        parser.add_argument("--status", choices=["machine-draft", "translated", "reviewed"],
                            help="Set this status on every imported row (default: the CSV's status column)")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        lang = opts["lang"]
        wanted = i18n.all_msgids()
        have = i18n.read_po(i18n.po_path(lang))
        errors, n = [], 0
        try:
            rows = list(csv.DictReader(open(opts["file"], encoding="utf-8")))
        except OSError as exc:
            raise CommandError(f"Can't read {opts['file']}: {exc}") from exc
        for i, row in enumerate(rows, 2):
            key, tr = row.get("key", ""), (row.get("translation") or "").strip()
            if not tr:
                continue
            if key not in wanted:
                errors.append(f"row {i}: unknown key (the English changed?): {key[:60]}")
                continue
            if sorted(PLACEHOLDER.findall(key)) != sorted(PLACEHOLDER.findall(tr)):
                errors.append(f"row {i}: placeholders differ from the English: {key[:60]}")
                continue
            status = opts["status"] or (row.get("status") if row.get("status") in ("machine-draft", "translated", "reviewed") else "translated")
            have[key] = i18n.Entry(key, tr, [wanted[key]], status)
            n += 1
        if errors:
            raise CommandError("Nothing imported:\n- " + "\n- ".join(errors[:50]))
        if opts["dry_run"]:
            self.stdout.write(f"Dry run: {n} translations would be imported.")
            return
        entries = [have.get(m) or i18n.Entry(m, "", [w]) for m, w in wanted.items()]
        i18n.write_po(i18n.po_path(lang), lang, entries)
        i18n.compile_language(lang)
        s = i18n.stats(lang)
        self.stdout.write(f"{lang}: imported {n}; now {s['translated']}/{s['total']} ({s['percent']}%), {s['reviewed']} reviewed")
