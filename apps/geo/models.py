"""
INEC's electoral geography (spec 16 §8): state → LGA → ward (INEC: registration area) → polling unit.
Public reference data, no personal data. Loaded by `manage.py load_geography <csv>`; codes are the keys, names update in
place, and units missing from a newer file become inactive (never deleted: records may point at them).
"""
from django.contrib.postgres.indexes import GinIndex
from django.db import models

from apps.core.fields import StateField


class State(models.Model):
    """The 36 states and the FCT. `slug` is the value stored in every state column ("lagos", "fct")."""

    code = models.CharField(max_length=2, unique=True)  # INEC's two-digit state code
    slug = StateField(unique=True, blank=False)
    name = models.CharField(max_length=60)

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
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["state", "code"], name="geo_lga_unique_code")]
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
    """Which file the rows came from, and what loading it changed."""

    source = models.CharField(max_length=300)
    published_on = models.DateField(null=True, blank=True)
    checksum = models.CharField(max_length=64)
    loaded_at = models.DateTimeField(auto_now_add=True)
    states = models.PositiveIntegerField(default=0)
    lgas = models.PositiveIntegerField(default=0)
    wards = models.PositiveIntegerField(default=0)
    polling_units = models.PositiveIntegerField(default=0)
    deactivated = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-loaded_at"]
