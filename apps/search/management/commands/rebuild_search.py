"""Rebuild the search index from the source tables (spec 12 §6.1). Safe to repeat. Schedule weekly and run after deploys."""
from apps.core.jobs import TrackedCommand
from apps.search.index import rebuild


class Command(TrackedCommand):
    help = "Rebuild the public search index and remove orphans."
    job = "rebuild_search"

    def add_arguments(self, parser):
        parser.add_argument("--kind", choices=["candidate", "article", "post"], default=None)

    def run(self, *args, **opts):
        out = rebuild(opts["kind"])
        text = ", ".join(f"{k}: {v['indexed']} indexed, {v['orphans_removed']} orphans removed" for k, v in out.items())
        self.stdout.write(text)
        return text[:255]
