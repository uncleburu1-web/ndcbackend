"""Keep the index in step with every save and delete, inside the same transaction (spec 12 §3.1, §6.1)."""
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from . import index
from .models import Kind


def _connect():
    from apps.candidates.models import Candidate
    from apps.events.models import Event
    from apps.news.models import Article
    from apps.posts.models import CandidatePost

    @receiver(post_save, sender=Candidate, weak=False, dispatch_uid="search_candidate_save")
    def _c(sender, instance, raw=False, **kw):
        if not raw:
            index.index_candidate(instance)

    @receiver(post_save, sender=Article, weak=False, dispatch_uid="search_article_save")
    def _a(sender, instance, raw=False, **kw):
        if not raw:
            index.index_article(instance)

    @receiver(post_save, sender=CandidatePost, weak=False, dispatch_uid="search_post_save")
    def _p(sender, instance, raw=False, **kw):
        if not raw:
            index.index_post(instance)

    @receiver(post_save, sender=Event, weak=False, dispatch_uid="search_event_save")
    def _e(sender, instance, raw=False, **kw):
        if not raw:
            index.sync_event(instance)

    for model, kind in ((Candidate, Kind.CANDIDATE), (Article, Kind.ARTICLE), (CandidatePost, Kind.POST), (Event, Kind.EVENT)):
        post_delete.connect(lambda sender, instance, _k=kind, **kw: index.remove(_k, instance.pk), sender=model, weak=False,
                            dispatch_uid=f"search_{kind}_delete")


_connect()
