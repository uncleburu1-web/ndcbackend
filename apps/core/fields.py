"""Shared field shapes (spec 16 §3.2, F2)."""
from django.db import models
from django.db.models import Q


def _states():
    from apps.candidates.reference import State

    return State


class StateField(models.CharField):
    """A Nigerian state (36 states + FCT) as its slug, e.g. "lagos", "fct". Blank means national or not given.
    One shape everywhere: varchar(20), the 37 choices. Pair it with `state_check(name)` in Meta.constraints."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_length", 20)
        kwargs.setdefault("choices", _states().choices)
        kwargs.setdefault("blank", True)
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        kwargs.pop("choices", None)  # always the same list; keeps migrations stable
        if kwargs.get("max_length") == 20:
            kwargs.pop("max_length")
        if kwargs.get("blank") is True:
            kwargs.pop("blank")
        return name, "apps.core.fields.StateField", args, kwargs


def state_check(field: str, name: str, *, allow_blank=True) -> models.CheckConstraint:
    values = list(_states().values)
    cond = Q(**{f"{field}__in": values})
    if allow_blank:
        cond |= Q(**{field: ""})
    return models.CheckConstraint(name=name, condition=cond)


def choice_check(field: str, choices, name: str) -> models.CheckConstraint:
    return models.CheckConstraint(name=name, condition=Q(**{f"{field}__in": list(choices)}))
