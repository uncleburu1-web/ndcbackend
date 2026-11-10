"""Response shapes for the geography API (spec 19 §13). Reference data only, except the `/me` and review shapes."""
from rest_framework import serializers as S

from .models import Constituency, Dataset


class StateSchema(S.Serializer):
    slug = S.CharField()
    code = S.CharField()
    name = S.CharField()
    capital = S.CharField()
    lgas = S.IntegerField(help_text="Active LGAs (area councils in the FCT) in the loaded list")


class LgaSchema(S.Serializer):
    id = S.IntegerField()
    code = S.CharField()
    name = S.CharField()
    slug = S.CharField()
    active = S.BooleanField()


class WardSchema(S.Serializer):
    id = S.IntegerField()
    code = S.CharField()
    name = S.CharField()
    polling_units = S.IntegerField()


class PollingUnitSchema(S.Serializer):
    id = S.IntegerField()
    code = S.CharField()
    delimitation = S.CharField(help_text="INEC's code: state-LGA-ward-unit, e.g. 24-07-05-004")
    name = S.CharField()


class _Ref(S.Serializer):
    id = S.IntegerField()
    code = S.CharField()
    name = S.CharField()


class _LgaRef(_Ref):
    slug = S.CharField()
    active = S.BooleanField()


class _StateRef(S.Serializer):
    slug = S.CharField()
    code = S.CharField()
    name = S.CharField()


class PollingUnitDetailSchema(PollingUnitSchema):
    active = S.BooleanField(help_text="False: no longer in INEC's current list")
    ward = _Ref()
    lga = _LgaRef()
    state = _StateRef()
    inec_url = S.CharField(help_text="INEC's polling-unit locator: the official answer")


class ConstituencySchema(S.Serializer):
    id = S.IntegerField()
    kind = S.ChoiceField(choices=Constituency.Kind.choices)
    code = S.CharField()
    name = S.CharField()
    slug = S.CharField()
    state = S.CharField()
    lga_ids = S.ListField(child=S.IntegerField())
    ward_ids = S.ListField(child=S.IntegerField())


class DatasetPublicSchema(S.Serializer):
    kind = S.ChoiceField(choices=Dataset.Kind.choices)
    source = S.CharField()
    published_on = S.DateField(allow_null=True)
    applied_at = S.DateTimeField()
    states = S.IntegerField()
    lgas = S.IntegerField()
    wards = S.IntegerField()
    polling_units = S.IntegerField()
    constituencies = S.IntegerField()


class AboutSchema(S.Serializer):
    datasets = DatasetPublicSchema(many=True)
    inec_url = S.CharField()


def results(child, name):
    return type(name, (S.Serializer,), {"results": child(many=True)})


StateList = results(StateSchema, "GeoStateList")
LgaList = results(LgaSchema, "GeoLgaList")
WardList = results(WardSchema, "GeoWardList")
PollingUnitList = results(PollingUnitSchema, "GeoPollingUnitList")
PollingUnitFound = results(PollingUnitDetailSchema, "GeoPollingUnitFound")
ConstituencyList = results(ConstituencySchema, "GeoConstituencyList")


# ------------------------------------------------------------------ LGA page (§6.2)
class CandidateGroupSchema(S.Serializer):
    key = S.CharField(help_text="An office (lga_chairman, councillor) or a constituency's slug")
    label = S.CharField()
    position = S.CharField()
    constituency = S.CharField(allow_null=True, help_text="Constituency code when the group is a constituency")
    total = S.IntegerField()
    results = S.ListField(child=S.DictField())
    see_all = S.CharField(help_text="Path to /candidates filtered to this group")


class LgaCommunitySchema(S.Serializer):
    id = S.UUIDField()
    path = S.CharField()
    name = S.CharField()
    members = S.CharField(help_text="Banded (spec 17 §4.4)")


class LgaPageSchema(S.Serializer):
    state = S.CharField()
    state_label = S.CharField()
    lga = LgaSchema()
    area_council = S.BooleanField(help_text="FCT: say \"Area council\"")
    election_year = S.IntegerField(allow_null=True)
    groups = CandidateGroupSchema(many=True)
    events = S.ListField(child=S.DictField())
    community = LgaCommunitySchema(allow_null=True)
    wards = WardSchema(many=True)
    about = DatasetPublicSchema(allow_null=True)


# ------------------------------------------------------------------ the person's own records (§8.1)
class SuggestItem(S.Serializer):
    id = S.IntegerField()
    name = S.CharField()
    code = S.CharField()
    score = S.FloatField()


class SuggestionSchema(S.Serializer):
    lga = SuggestItem(allow_null=True)
    ward = SuggestItem(allow_null=True)
    polling_unit = SuggestItem(allow_null=True)


class _PlaceItem(S.Serializer):
    id = S.IntegerField()
    name = S.CharField()
    code = S.CharField()
    active = S.BooleanField()


class PlaceSchema(S.Serializer):
    lga = _PlaceItem(allow_null=True)
    ward = _PlaceItem(allow_null=True)
    polling_unit = _PlaceItem(allow_null=True)


class TypedSchema(S.Serializer):
    lga = S.CharField()
    ward = S.CharField()
    polling_unit = S.CharField()


class ConfirmItemSchema(S.Serializer):
    kind = S.ChoiceField(choices=["membership", "pvc"])
    state = S.CharField()
    typed = TypedSchema()
    current = PlaceSchema()
    retired = S.BooleanField()
    suggestion = SuggestionSchema()


ConfirmList = results(ConfirmItemSchema, "GeoConfirmList")


class ConfirmRequest(S.Serializer):
    lga_id = S.IntegerField(required=False, allow_null=True)
    ward_id = S.IntegerField(required=False, allow_null=True)
    polling_unit_id = S.IntegerField(required=False, allow_null=True)
    geo_not_listed = S.BooleanField(required=False)


class ConfirmResult(S.Serializer):
    kind = S.CharField()
    geo_source = S.CharField()
    waits_for_officer = S.BooleanField(help_text="A verified PVC check: an officer accepts the change before communities use it")
    place = PlaceSchema()


# ------------------------------------------------------------------ admin (§13.3)
class DatasetAdminSchema(S.Serializer):
    id = S.IntegerField()
    kind = S.ChoiceField(choices=Dataset.Kind.choices)
    status = S.ChoiceField(choices=Dataset.Status.choices)
    source = S.CharField()
    published_on = S.DateField(allow_null=True)
    notes = S.CharField()
    checksum = S.CharField()
    loaded_at = S.DateTimeField()
    applied_at = S.DateTimeField(allow_null=True)
    applied_by = S.CharField(allow_null=True)
    first_approval_by = S.CharField(allow_null=True)
    states = S.IntegerField()
    lgas = S.IntegerField()
    wards = S.IntegerField()
    polling_units = S.IntegerField()
    constituencies = S.IntegerField()
    deactivated = S.IntegerField()
    changes = S.IntegerField()
    error_count = S.IntegerField()
    needs_second_admin = S.BooleanField()


class DatasetDetailSchema(DatasetAdminSchema):
    preview = S.JSONField(help_text="The §4.3 report: counts, lists (first 500 of each), errors with line numbers, affected counts")


DatasetList = results(DatasetAdminSchema, "GeoDatasetList")


class DatasetUpload(S.Serializer):
    file = S.FileField()
    kind = S.ChoiceField(choices=Dataset.Kind.choices, default=Dataset.Kind.POLLING_UNITS)
    source = S.CharField(max_length=300, required=False, allow_blank=True)
    published_on = S.DateField(required=False, allow_null=True)
    notes = S.CharField(max_length=2000, required=False, allow_blank=True)


class ApplyRequest(S.Serializer):
    confirm_second = S.BooleanField(required=False, help_text="The second system admin confirms (the 95% rule)")


class ChangeSchema(S.Serializer):
    level = S.CharField()
    action = S.CharField()
    code = S.CharField()
    old_value = S.CharField()
    new_value = S.CharField()


class ChangePage(S.Serializer):
    count = S.IntegerField()
    next = S.IntegerField(allow_null=True)
    results = ChangeSchema(many=True)


class ReviewItemSchema(S.Serializer):
    kind = S.ChoiceField(choices=["membership", "pvc"])
    id = S.UUIDField()
    state = S.CharField()
    typed = TypedSchema()
    why = S.ChoiceField(choices=["unlinked", "not_listed", "retired", "person_choice"])
    current = PlaceSchema()
    pending = PlaceSchema(allow_null=True)
    suggestion = SuggestionSchema()
    record_url = S.CharField(help_text="The record's own admin page (names are shown only there)")


class ReviewGroupSchema(S.Serializer):
    state = S.CharField()
    lga = S.CharField()
    count = S.IntegerField()


class ReviewPage(S.Serializer):
    kinds = S.ListField(child=S.CharField())
    count = S.IntegerField()
    next = S.IntegerField(allow_null=True)
    groups = ReviewGroupSchema(many=True)
    results = ReviewItemSchema(many=True)


class ReviewAction(S.Serializer):
    action = S.ChoiceField(choices=["link", "accept", "ask"])
    lga_id = S.IntegerField(required=False, allow_null=True)
    ward_id = S.IntegerField(required=False, allow_null=True)
    polling_unit_id = S.IntegerField(required=False, allow_null=True)


class AliasSchema(S.Serializer):
    id = S.IntegerField()
    level = S.ChoiceField(choices=["lga", "ward", "polling_unit"])
    target_id = S.IntegerField()
    target_name = S.CharField()
    target_code = S.CharField()
    name = S.CharField()
    source = S.CharField()
    created_at = S.DateTimeField()


AliasList = results(AliasSchema, "GeoAliasList")


class AliasCreate(S.Serializer):
    level = S.ChoiceField(choices=["lga", "ward", "polling_unit"])
    target_id = S.IntegerField()
    name = S.CharField(max_length=200)
