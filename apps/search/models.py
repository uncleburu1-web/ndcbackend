"""
Search index (spec 12 §5.1): one row per searchable PUBLIC item. A denormalised copy, never a
source of truth — rebuilt by `rebuild_search`, and re-checked against the source tables at query time.
"""
import uuid

from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models

from apps.core.fields import StateField, state_check


class Kind(models.TextChoices):
    CANDIDATE = "candidate", "Candidate"
    ARTICLE = "article", "News"
    POST = "post", "Candidate post"


class SearchEntry(models.Model):
    """Derived search index row for one public item (spec 12). Rebuilt by `rebuild_search`; never the source of truth."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=12, choices=Kind.choices, db_index=True)
    object_id = models.UUIDField()
    title = models.CharField(max_length=200)
    folded_title = models.CharField(max_length=200)
    title_key = models.CharField(max_length=200, blank=True, default="")  # sound keys of the name words (text.title_key)
    document = SearchVectorField(null=True)
    body_text = models.TextField(blank=True, default="")
    state = StateField(default="", db_index=True)  # blank = national
    position = models.CharField(max_length=20, blank=True, default="", db_index=True)
    election_year = models.PositiveSmallIntegerField(null=True, blank=True)
    source = models.CharField(max_length=10, blank=True, default="")
    published_at = models.DateTimeField(null=True, blank=True)
    rank_order = models.PositiveSmallIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["kind", "object_id"], name="search_entry_unique_item"),
                       state_check("state", "search_state_valid")]  # spec 16 F2
        indexes = [
            GinIndex(fields=["document"], name="search_document_gin"),
            GinIndex(fields=["folded_title"], name="search_title_trgm", opclasses=["gin_trgm_ops"]),
            models.Index(fields=["kind", "state", "published_at"], name="search_kind_state_date"),
        ]

    def __str__(self):
        return f"{self.kind}: {self.title}"
