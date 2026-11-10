"""
INEC's electoral geography (spec 16 §8): state → LGA → ward (INEC: registration area) → polling unit.
Public reference data, no personal data. Loaded by `manage.py load_geography <csv>`; codes are the keys, names update in
place, and units missing from a newer file become inactive (never deleted: records may point at them).
"""
from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.db import models

from apps.core.fields import StateField


class State(models.Model):
    """The 36 states and the FCT. `slug` is the value stored in every state column ("lagos", "fct")."""

    code = models.CharField(max_length=2, unique=True)  # INEC's two-digit state code
    slug = StateField(unique=True, blank=False)
    name = models.CharField(max_length=60)
    capital = models.CharField(max_length=60, blank=True)  # spec 19 §11.1, from geo/reference.py CAPITALS

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Lga(models.Model):
    """A local government area (the FCT's six area councils count as LGAs)."""

    state = models.ForeignKey(State, on_delete=models.PROTECT, related_name="lgas")
    code = models.CharField(max_length=2)
    name = models.CharField(max_length=80)
    folded_name = models.CharField(max_length=80)
    slug = models.SlugField(max_length=90)  # spec 19 §6.2: /states/<state>/<lga>; unique within the state
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["state", "code"], name="geo_lga_unique_code"),
                       models.UniqueConstraint(fields=["state", "slug"], name="geo_lga_unique_slug")]
        indexes = [GinIndex(fields=["folded_name"], opclasses=["gin_trgm_ops"], name="geo_lga_name_trgm")]

    def __str__(self):
        return self.name


class Ward(models.Model):
    """An electoral ward; INEC calls it a registration area (RA)."""

    lga = models.ForeignKey(Lga, on_delete=models.PROTECT, related_name="wards")
    code = models.CharField(max_length=2)
    name = models.CharField(max_length=120)
    folded_name = models.CharField(max_length=120)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["lga", "code"], name="geo_ward_unique_code")]
        indexes = [GinIndex(fields=["folded_name"], opclasses=["gin_trgm_ops"], name="geo_ward_name_trgm")]

    def __str__(self):
        return self.name


class PollingUnit(models.Model):
    """A polling unit. `delimitation` is INEC's full code, state-LGA-ward-unit (e.g. 24-07-05-012)."""

    ward = models.ForeignKey(Ward, on_delete=models.PROTECT, related_name="polling_units")
    code = models.CharField(max_length=3)
    name = models.CharField(max_length=200)
    folded_name = models.CharField(max_length=200)
    delimitation = models.CharField(max_length=14, unique=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["delimitation"]
        constraints = [models.UniqueConstraint(fields=["ward", "code"], name="geo_pu_unique_code")]
        indexes = [GinIndex(fields=["folded_name"], opclasses=["gin_trgm_ops"], name="geo_pu_name_trgm")]

    def __str__(self):
        return f"{self.delimitation} {self.name}"


class Dataset(models.Model):
    """One geography file: previewed first, then applied (spec 19 §4.3). Counts are the active totals after applying."""

    class Kind(models.TextChoices):
        POLLING_UNITS = "polling_units", "Polling units"
        SENATORIAL_DISTRICTS = "senatorial_districts", "Senatorial districts"
        FEDERAL_CONSTITUENCIES = "federal_constituencies", "Federal constituencies"
        STATE_CONSTITUENCIES = "state_constituencies", "State constituencies"

    class Status(models.TextChoices):
        PREVIEWED = "previewed", "Previewed"
        APPLIED = "applied", "Applied"

    kind = models.CharField(max_length=24, choices=Kind.choices, default=Kind.POLLING_UNITS)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.APPLIED)
    source = models.CharField(max_length=300)
    published_on = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    checksum = models.CharField(max_length=64)
    file_name = models.CharField(max_length=200, blank=True)  # under PRIVATE_MEDIA_ROOT/geo/; never served
    preview = models.JSONField(default=dict, blank=True)
    loaded_at = models.DateTimeField(auto_now_add=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    applied_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    first_approval_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                          related_name="+")  # the 95% rule's first admin (spec 19 §4.3)
    states = models.PositiveIntegerField(default=0)
    lgas = models.PositiveIntegerField(default=0)
    wards = models.PositiveIntegerField(default=0)
    polling_units = models.PositiveIntegerField(default=0)
    constituencies = models.PositiveIntegerField(default=0)
    deactivated = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-loaded_at"]
        constraints = [models.CheckConstraint(condition=~models.Q(status="applied") | models.Q(applied_at__isnull=False),
                                              name="geo_dataset_applied_has_time")]


class Constituency(models.Model):
    """A senatorial district, federal constituency or state constituency (spec 19 §5), made of whole LGAs or of wards."""

    class Kind(models.TextChoices):
        SENATORIAL_DISTRICT = "senatorial_district", "Senatorial district"
        FEDERAL_CONSTITUENCY = "federal_constituency", "Federal constituency"
        STATE_CONSTITUENCY = "state_constituency", "State constituency"

    kind = models.CharField(max_length=24, choices=Kind.choices)
    state = models.ForeignKey(State, on_delete=models.PROTECT, related_name="constituencies")
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=160)
    folded_name = models.CharField(max_length=160)
    slug = models.SlugField(max_length=170)
    active = models.BooleanField(default=True)
    dataset = models.ForeignKey(Dataset, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["kind", "name"]
        constraints = [models.UniqueConstraint(fields=["kind", "code"], name="geo_constituency_unique_code")]
        indexes = [models.Index(fields=["kind", "state"], name="geo_constituency_kind_state")]

    def __str__(self):
        return self.name


class ConstituencyArea(models.Model):
    """One part of a constituency: a whole LGA, or one ward of a split LGA. Exactly one of the two."""

    constituency = models.ForeignKey(Constituency, on_delete=models.PROTECT, related_name="areas")
    lga = models.ForeignKey(Lga, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    ward = models.ForeignKey(Ward, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(lga__isnull=False, ward__isnull=True) | models.Q(lga__isnull=True, ward__isnull=False),
                                              name="geo_constituency_area_one_of")]


class Alias(models.Model):
    """Another name people use for an LGA, ward or polling unit (spec 19 §4.5). Suggestions and type-ahead only."""

    class Source(models.TextChoices):
        RENAME = "rename", "Old name after a rename"
        ADMIN = "admin", "Added by an admin"

    lga = models.ForeignKey(Lga, null=True, blank=True, on_delete=models.PROTECT, related_name="aliases")
    ward = models.ForeignKey(Ward, null=True, blank=True, on_delete=models.PROTECT, related_name="aliases")
    polling_unit = models.ForeignKey(PollingUnit, null=True, blank=True, on_delete=models.PROTECT, related_name="aliases")
    name = models.CharField(max_length=200)
    folded_name = models.CharField(max_length=200)
    source = models.CharField(max_length=10, choices=Source.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(condition=(models.Q(lga__isnull=False, ward__isnull=True, polling_unit__isnull=True)
                                              | models.Q(lga__isnull=True, ward__isnull=False, polling_unit__isnull=True)
                                              | models.Q(lga__isnull=True, ward__isnull=True, polling_unit__isnull=False)),
                                   name="geo_alias_one_target"),
        ]
        indexes = [GinIndex(fields=["folded_name"], opclasses=["gin_trgm_ops"], name="geo_alias_name_trgm")]


class Change(models.Model):
    """What applying a dataset changed (spec 19 §4.4). Append-only, enforced by the database."""

    class Level(models.TextChoices):
        STATE = "state", "State"
        LGA = "lga", "LGA"
        WARD = "ward", "Ward"
        POLLING_UNIT = "polling_unit", "Polling unit"
        CONSTITUENCY = "constituency", "Constituency"

    class Action(models.TextChoices):
        ADDED = "added", "Added"
        RENAMED = "renamed", "Renamed"
        RETIRED = "retired", "Retired"
        MOVED = "moved", "Moved"
        RESTORED = "restored", "Back in the list"
        AREAS = "areas", "Areas changed"

    dataset = models.ForeignKey(Dataset, on_delete=models.PROTECT, related_name="changes")
    level = models.CharField(max_length=14, choices=Level.choices)
    action = models.CharField(max_length=10, choices=Action.choices)
    code = models.CharField(max_length=40)  # the delimitation, or state-LGA(-ward), or the constituency code
    old_value = models.CharField(max_length=200, blank=True)
    new_value = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["level", "code"]
        indexes = [models.Index(fields=["dataset", "level"], name="geo_change_dataset_level")]


# Module-level names for the OpenAPI enum overrides (config/settings/base.py)
DatasetKind = Dataset.Kind
DatasetStatus = Dataset.Status
ConstituencyKind = Constituency.Kind
