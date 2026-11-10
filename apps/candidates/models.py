"""Candidate record — spec 03 §5.1."""
import re
from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils.text import slugify

from apps.core.fields import StateField, choice_check, state_check
from apps.core.models import TimeStampedModel

from .reference import (
    AREA_LABELS,
    AREA_RULES,
    NOT_IN_FCT,
    Position,
    State,
    area_display,
)

MAX_PRIORITIES = 8
MAX_PRIORITY_LEN = 120
MAX_LINKS = 5
MAX_LINK_LABEL = 40

# Changing any of these on a verified record clears verification; on a published
# record it also unpublishes it, so an unchecked identity is never live (§6.2).
IDENTITY_FIELDS = ("full_name", "position", "state", "area_name", "ward_name", "party", "election_year")


class CandidateStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    PUBLISHED = "published", "Published"
    UNPUBLISHED = "unpublished", "Unpublished"
    ARCHIVED = "archived", "Archived"


class Candidate(TimeStampedModel):
    """A candidate profile created by system admins; public only when published and verified (spec 03)."""

    slug = models.SlugField(max_length=170, unique=True)
    full_name = models.CharField(max_length=150)
    party = models.CharField(max_length=40, default="NDC")
    position = models.CharField(max_length=20, choices=Position.choices)
    election_year = models.PositiveSmallIntegerField()
    state = StateField(default="")
    area_name = models.CharField(max_length=120, blank=True, default="")
    ward_name = models.CharField(max_length=120, blank=True, default="")
    # spec 16 §8.2: optional links to INEC's geography beside what the person typed (never overwritten)
    geo_lga = models.ForeignKey("geo.Lga", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    geo_ward = models.ForeignKey("geo.Ward", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    # spec 19 §5.2: senators, House of Representatives and State Assembly candidates stand in a constituency
    geo_constituency = models.ForeignKey("geo.Constituency", null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    biography = models.TextField(max_length=5000, blank=True, default="")
    campaign_summary = models.TextField(max_length=1000, blank=True, default="")
    priorities = models.JSONField(default=list, blank=True)
    links = models.JSONField(default=list, blank=True)

    photo_800 = models.CharField(max_length=255, blank=True, default="")  # storage names (§5.3)
    photo_200 = models.CharField(max_length=255, blank=True, default="")
    photo_asset = models.ForeignKey("media.MediaAsset", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")  # spec 15 §7.4

    status = models.CharField(max_length=12, choices=CandidateStatus.choices, default=CandidateStatus.DRAFT, db_index=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    verification_notes = models.TextField(blank=True, default="")  # internal — never public
    published_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["full_name"]
        constraints = [
            models.CheckConstraint(
                condition=~Q(status=CandidateStatus.PUBLISHED) | Q(verified_at__isnull=False),
                name="candidates_published_requires_verified",
            ),
            # spec 16 §6.3
            state_check("state", "candidates_state_valid"),
            choice_check("status", CandidateStatus.values, "candidates_status_valid"),
            models.CheckConstraint(condition=Q(election_year__gte=2023, election_year__lte=2099), name="candidates_election_year_plausible"),
        ]
        indexes = [models.Index(fields=["status", "election_year", "position", "state"])]

    def __str__(self):
        return f"{self.full_name} — {self.get_position_display()}"

    # ------------------------------------------------------------------ derived
    @property
    def is_verified(self) -> bool:
        return self.verified_at is not None

    @property
    def area_display(self) -> str:
        return area_display(self.position, self.state, self.area_name, self.ward_name)

    # --------------------------------------------------------------- validation
    def clean(self):
        errors: dict[str, list[str]] = {}
        self.full_name = " ".join((self.full_name or "").split())
        self.area_name = " ".join((self.area_name or "").split())
        self.ward_name = " ".join((self.ward_name or "").split())

        rules = AREA_RULES.get(self.position)
        if rules:
            label = Position(self.position).label
            if rules["state"] and not self.state:
                errors.setdefault("state", []).append(f"Choose the state for a {label} candidate.")
            if not rules["state"] and self.state:
                errors.setdefault("state", []).append(f"A {label} candidate isn't tied to a state. Leave it empty.")
            if rules["area"] and not self.area_name:
                errors.setdefault("area_name", []).append(f"Enter the {AREA_LABELS[self.position].lower()}.")
            if not rules["area"] and self.area_name:
                errors.setdefault("area_name", []).append(f"A {label} candidate doesn't need this. Leave it empty.")
            if rules["ward"] and not self.ward_name:
                errors.setdefault("ward_name", []).append("Enter the ward.")
            if not rules["ward"] and self.ward_name:
                errors.setdefault("ward_name", []).append("Only councillors have a ward. Leave it empty.")
            if self.state == State.FCT and self.position in NOT_IN_FCT:
                errors.setdefault("state", []).append(f"The FCT has no {label}. Choose another state or position.")

        if self.election_year and not (2000 <= self.election_year <= 2100):
            errors.setdefault("election_year", []).append("Enter a four-digit election year, like 2027.")

        errors.update(_validate_priorities(self.priorities))
        errors.update(_validate_links(self.links))
        if errors:
            raise ValidationError(errors)

    # -------------------------------------------------------------------- slugs
    def assign_slug(self):
        """Slug from the name, unique. Fixed once the record has ever been published (§5.1)."""
        if self.slug and self.published_at:
            return
        base = slugify(self.full_name)[:150] or "candidate"
        if self.slug and re.fullmatch(rf"{re.escape(base)}(-\d+)?", self.slug):
            return  # name unchanged
        slug, n = base, 2
        while Candidate.objects.filter(slug=slug).exclude(pk=self.pk).exists():
            slug = f"{base}-{n}"
            n += 1
        self.slug = slug


def _validate_priorities(value) -> dict:
    if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
        return {"priorities": ["Priorities must be a list of short sentences."]}
    msgs = []
    if len(value) > MAX_PRIORITIES:
        msgs.append(f"Add at most {MAX_PRIORITIES} priorities.")
    if any(not p.strip() for p in value):
        msgs.append("Remove empty priorities.")
    if any(len(p) > MAX_PRIORITY_LEN for p in value):
        msgs.append(f"Keep each priority under {MAX_PRIORITY_LEN} characters.")
    return {"priorities": msgs} if msgs else {}


def _validate_links(value) -> dict:
    if not isinstance(value, list) or not all(isinstance(l, dict) for l in value):
        return {"links": ["Links must be a list of {label, url}."]}
    msgs = []
    if len(value) > MAX_LINKS:
        msgs.append(f"Add at most {MAX_LINKS} links.")
    for link in value:
        label = (link.get("label") or "").strip()
        url = (link.get("url") or "").strip()
        if not label or len(label) > MAX_LINK_LABEL:
            msgs.append(f"Each link needs a label under {MAX_LINK_LABEL} characters.")
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc or any(c.isspace() for c in url):
            msgs.append(f"“{url or '(empty)'}” isn't a valid https:// link.")
    return {"links": msgs} if msgs else {}
