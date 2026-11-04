"""Fan-out jobs (spec 15 §9.3). Each runs one idempotent INSERT … SELECT … ON CONFLICT DO NOTHING, so a retry after a
partial failure completes the delivery without duplicates."""
from apps.core.outbox import handler


@handler("notify_fanout")
def notify_fanout(payload):
    from . import services

    if payload["kind"] == "announcement":
        from apps.news.models import Article

        a = Article.objects.filter(pk=payload["article"]).first()
        if a is not None and a.status == "successful":
            services.deliver_announcement(a)
    elif payload["kind"] == "candidate_post":
        from apps.posts.models import CandidatePost

        p = CandidatePost.objects.filter(pk=payload["post"]).select_related("candidate").first()
        if p is not None:
            services.deliver_post(p)
    else:
        raise ValueError(f"Unknown fan-out kind {payload['kind']!r}")
