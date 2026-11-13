"""
python manage.py i18n_export --lang ha [--out ha-backend.csv]
python manage.py i18n_export --update     # refresh every locale/<code>/django.po with today's English (keeps translations)

Spec 21 §15.3. The CSV has one row per English message: key (the English text), english, context (where it's used),
translation, status (new | translated | reviewed | machine-draft). Translators fill `translation` and send it back
to `i18n_import`.
"""
import csv
import sys

from django.core.management.base import BaseCommand, CommandError

from apps.core import i18n


def merged(lang: str) -> list[i18n.Entry]:
    have = i18n.read_po(i18n.po_path(lang))
    out = []
    for msgid, where in i18n.all_msgids().items():
        old = have.get(msgid)
        out.append(i18n.Entry(msgid, old.msgstr if old else "", [where], old.status if old and old.msgstr else ""))
    return out


class Command(BaseCommand):
    help = "Export backend messages for translators (CSV), or refresh the .po files with the current English."

    def add_arguments(self, parser):
        parser.add_argument("--lang", choices=i18n.TRANSLATED)
        parser.add_argument("--out", help="CSV path (default: stdout)")
        parser.add_argument("--update", action="store_true", help="Rewrite every .po with the current English messages and compile.")

    def handle(self, *args, **opts):
        if opts["update"]:
            for lang in i18n.TRANSLATED:
                i18n.write_po(i18n.po_path(lang), lang, merged(lang))
                i18n.compile_language(lang)
                s = i18n.stats(lang)
                self.stdout.write(f"{lang}: {s['translated']}/{s['total']} translated ({s['percent']}%), {s['reviewed']} reviewed")
            return
        if not opts["lang"]:
            raise CommandError("Give --lang (ha, ig, yo or pcm) or --update.")
        fh = open(opts["out"], "w", newline="", encoding="utf-8") if opts["out"] else sys.stdout
        w = csv.writer(fh)
        w.writerow(["key", "english", "context", "translation", "status"])
        for e in sorted(merged(opts["lang"]), key=lambda e: e.comments[0] if e.comments else ""):
            w.writerow([e.msgid, e.msgid, "; ".join(e.comments), e.msgstr, e.status or ("translated" if e.msgstr else "new")])
        if opts["out"]:
            fh.close()
            self.stdout.write(f"Wrote {opts['out']}")
