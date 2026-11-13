"""
python manage.py i18n_compile [--check]

Spec 21 §6: compiles locale/<code>/LC_MESSAGES/django.po to .mo in pure Python (the Railway image has no GNU gettext).
`--check` (CI) fails when a committed .mo doesn't match its .po.
"""
from django.core.management.base import BaseCommand, CommandError

from apps.core import i18n


class Command(BaseCommand):
    help = "Compile the backend translation catalogues (.po → .mo), or check that the committed .mo files are current."

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true")

    def handle(self, *args, **opts):
        stale = []
        for lang in i18n.TRANSLATED:
            po = i18n.po_path(lang)
            if not po.exists():
                continue
            fresh = i18n.mo_bytes(i18n.read_po(po), lang)
            mo = po.with_suffix(".mo")
            if opts["check"]:
                if not mo.exists() or mo.read_bytes() != fresh:
                    stale.append(lang)
            else:
                mo.write_bytes(fresh)
                self.stdout.write(f"Compiled {mo}")
        if stale:
            raise CommandError(f"Out of date .mo files: {', '.join(stale)}. Run `python manage.py i18n_compile` and commit.")
        if opts["check"]:
            self.stdout.write("Compiled catalogues are current.")
