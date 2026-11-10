"""Spec 19 §12.3. Payloads hold the dataset ID only."""
from apps.core.outbox import handler


@handler("geo_dataset_applied")
def geo_dataset_applied(payload):
    """After a dataset is applied: communities, the search index's places, then notices to people whose unit changed."""
    from django.core.management import call_command

    from apps.search.index import rebuild
    from apps.search.models import Kind

    from . import records
    from .models import Dataset

    ds = Dataset.objects.filter(pk=payload["dataset"], status=Dataset.Status.APPLIED).first()
    if ds is None:
        return
    call_command("sync_communities", placements=True, verbosity=0)
    rebuild(Kind.STATE)
    rebuild(Kind.LGA)
    if ds.kind == Dataset.Kind.POLLING_UNITS:
        records.notify_unit_changed(ds)
