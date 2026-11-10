"""
Loading INEC geography (spec 16 §8.1, spec 19 §4): preview first, then apply.

- `preview(content, kind=...)` reads a file, runs every check in §4.1 and works out what applying it would change
  (added, renamed, retired, moved, back in the list), plus how many people's records point at retired or moved units.
  It writes nothing except the `Dataset` row (status `previewed`) and the file in private storage.
- `apply(dataset, user)` writes those changes in one transaction, records `Change` rows, adds the old name of every
  rename as an alias, and queues the follow-up work (communities, search, notices) through the outbox.
- `load(content, ...)` is the old one-step call (tests, live checks): preview and apply together.

Places are never deleted: anything missing from a newer file becomes inactive. Applying the same file twice changes nothing.
"""
import csv
import hashlib
import io
from collections import defaultdict
from dataclasses import dataclass, field

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.db import transaction
from django.utils import timezone

from apps.core.exceptions import ServiceError
from apps.search.text import fold

from .models import Alias, Change, Constituency, ConstituencyArea, Dataset, Lga, PollingUnit, State, Ward
from .reference import CAPITALS, lga_slug

COLUMNS = ["state_code", "state", "lga_code", "lga", "ward_code", "ward", "pu_code", "polling_unit"]
CONSTITUENCY_COLUMNS = ["state_code", "code", "name", "lga_codes", "ward_codes"]
STATE_ALIASES = {"federal capital territory": "fct", "fct": "fct", "abuja": "fct", "akwa-ibom": "akwa_ibom",
                 "cross-river": "cross_river", "nassarawa": "nasarawa"}
CONSTITUENCY_KINDS = {
    Dataset.Kind.SENATORIAL_DISTRICTS: Constituency.Kind.SENATORIAL_DISTRICT,
    Dataset.Kind.FEDERAL_CONSTITUENCIES: Constituency.Kind.FEDERAL_CONSTITUENCY,
    Dataset.Kind.STATE_CONSTITUENCIES: Constituency.Kind.STATE_CONSTITUENCY,
}
MAX_ERRORS = 50
MAX_LISTED = 500  # per list in the stored preview; counts are always complete
MAX_BYTES = 50 * 1024 * 1024


class GeographyFileError(ServiceError):
    """A file that can't be read or applied (spec 19 §4.1). Carries the problems found."""

    def __init__(self, message, errors=None):
        super().__init__(message, code="geo_file_invalid", status_code=400)
        self.errors = errors or [message]
        self.extra = {"errors": self.errors[:MAX_ERRORS]}


def storage():
    return FileSystemStorage(location=settings.PRIVATE_MEDIA_ROOT, base_url=None)


def k(name: str) -> str:
    return fold(name or "").lower()


def state_slug(name: str) -> str:
    from apps.candidates.reference import State as S

    f = fold(name).lower().replace(" state", "").strip()
    if f in STATE_ALIASES:
        return STATE_ALIASES[f]
    for value, label in S.choices:
        if fold(label).lower() == f or value == f.replace(" ", "_").replace("-", "_"):
            return value
    raise ValueError(f"Unknown state name {name!r}")


def _code(v, width):
    v = (v or "").strip()
    if not v.isdigit() or len(v) > width:
        raise ValueError(f"Bad code {v!r}")
    return v.zfill(width)


def _text(content: bytes) -> str:
    if len(content) > MAX_BYTES:
        raise GeographyFileError("The file is larger than 50 MB.")
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise GeographyFileError("The file isn't UTF-8 text. Save it as CSV (UTF-8).") from None


def _reader(content: bytes, columns):
    reader = csv.DictReader(io.StringIO(_text(content)))
    missing = [c for c in columns if c not in (reader.fieldnames or [])]
    if missing:
        raise GeographyFileError(f"Missing columns: {', '.join(missing)} (expected {', '.join(columns)})")
    return reader


def read_rows(content: bytes):
    """Rows of a polling-unit file, as clean dicts with a line number. Problems come back as (line, message)."""
    rows, errors = [], []
    for n, r in enumerate(_reader(content, COLUMNS), start=2):
        try:
            row = {"line": n, "state_code": _code(r["state_code"], 2), "state": (r["state"] or "").strip(),
                   "lga_code": _code(r["lga_code"], 2), "lga": (r["lga"] or "").strip(),
                   "ward_code": _code(r["ward_code"], 2), "ward": (r["ward"] or "").strip(),
                   "pu_code": _code(r["pu_code"], 3), "polling_unit": (r["polling_unit"] or "").strip()}
            if not (row["lga"] and row["ward"] and row["polling_unit"]):
                raise ValueError("Empty name")
            row["slug"] = state_slug(row["state"])
            rows.append(row)
        except ValueError as e:
            errors.append(f"Line {n}: {e}")
    return rows, errors


# ------------------------------------------------------------------ preview
@dataclass
class Plan:
    kind: str
    rows: int = 0
    errors: list = field(default_factory=list)
    per_state: dict = field(default_factory=dict)
    added: dict = field(default_factory=lambda: defaultdict(list))
    renamed: dict = field(default_factory=lambda: defaultdict(list))
    restored: dict = field(default_factory=lambda: defaultdict(list))
    retired: dict = field(default_factory=lambda: defaultdict(list))
    moved: list = field(default_factory=list)
    affected: dict = field(default_factory=dict)
    active_units: int = 0
    current_units: int = 0
    data: dict = field(default_factory=dict)  # what apply needs (not stored)

    def counts(self):
        out = {}
        for name in ("added", "renamed", "restored", "retired"):
            out[name] = {lvl: len(v) for lvl, v in getattr(self, name).items() if v}
        out["moved"] = len(self.moved)
        return out

    def changes(self) -> int:
        return sum(len(v) for d in (self.added, self.renamed, self.restored, self.retired) for v in d.values()) + len(self.moved)

    def report(self) -> dict:
        listed = lambda d: {lvl: v[:MAX_LISTED] for lvl, v in d.items() if v}  # noqa: E731
        return {"rows": self.rows, "errors": self.errors[:MAX_ERRORS], "error_count": len(self.errors),
                "per_state": self.per_state, "counts": self.counts(), "changes": self.changes(),
                "added": listed(self.added), "renamed": listed(self.renamed), "restored": listed(self.restored),
                "retired": listed(self.retired), "moved": self.moved[:MAX_LISTED], "affected": self.affected,
                "active_units": self.active_units, "current_units": self.current_units,
                "needs_second_admin": self.needs_second_admin()}

    def needs_second_admin(self) -> bool:
        if self.kind != Dataset.Kind.POLLING_UNITS:
            return False
        floor = getattr(settings, "GEO_SECOND_ADMIN_MIN_UNITS", 1000)
        return self.current_units >= max(floor, 1) and self.active_units < 0.95 * self.current_units


def _plan_units(content: bytes) -> Plan:
    p = Plan(kind=Dataset.Kind.POLLING_UNITS)
    rows, errors = read_rows(content)
    p.rows, p.errors = len(rows) + len(errors), errors
    f_states, f_lgas, f_wards, f_pus = {}, {}, {}, {}
    per_state = defaultdict(int)
    for r in rows:
        sc, lc, wc = r["state_code"], r["lga_code"], r["ward_code"]
        delim = f"{sc}-{lc}-{wc}-{r['pu_code']}"
        prev = f_states.setdefault(sc, (r["slug"], r["state"]))
        if prev[0] != r["slug"]:
            p.errors.append(f"Line {r['line']}: state code {sc} is {prev[0]} earlier in the file but {r['slug']} here")
        if f_lgas.setdefault((sc, lc), r["lga"]) != r["lga"]:
            p.errors.append(f"Line {r['line']}: LGA {sc}-{lc} has two names ({f_lgas[(sc, lc)]!r} and {r['lga']!r})")
        if f_wards.setdefault((sc, lc, wc), r["ward"]) != r["ward"]:
            p.errors.append(f"Line {r['line']}: ward {sc}-{lc}-{wc} has two names ({f_wards[(sc, lc, wc)]!r} and {r['ward']!r})")
        if delim in f_pus:
            p.errors.append(f"Line {r['line']}: polling unit {delim} appears twice")
        f_pus[delim] = r["polling_unit"]
        per_state[r["slug"]] += 1
    p.per_state = dict(sorted(per_state.items()))
    if not rows and not p.errors:
        p.errors.append("The file has no rows.")
    # what's there now
    db_states = {s.code: s for s in State.objects.all()}
    for sc, (slug, _name) in f_states.items():
        st = db_states.get(sc)
        if st and st.slug != slug:
            p.errors.append(f"State code {sc} is {st.slug} in the database but {slug} in the file")
        taken = State.objects.filter(slug=slug).exclude(code=sc).first() if not st else None
        if taken:
            p.errors.append(f"{slug} already has state code {taken.code}, not {sc}")
    db_lgas = {(l_.state.code, l_.code): l_ for l_ in Lga.objects.select_related("state")}
    db_wards = {(w.lga.state.code, w.lga.code, w.code): w for w in Ward.objects.select_related("lga__state")}
    db_pus = {pu.delimitation: pu for pu in PollingUnit.objects.only("id", "delimitation", "name", "active", "ward_id", "folded_name")}
    p.current_units = sum(1 for pu in db_pus.values() if pu.active)
    p.active_units = len(f_pus)

    for sc, (slug, name) in f_states.items():
        if sc not in db_states:
            p.added["state"].append({"code": sc, "name": _state_name(name)})
    for lvl, f_map, db_map, label in (("lga", f_lgas, db_lgas, lambda key: "-".join(key)),
                                      ("ward", f_wards, db_wards, lambda key: "-".join(key))):
        for key, name in f_map.items():
            row = db_map.get(key)
            if row is None:
                p.added[lvl].append({"code": label(key), "name": name})
            else:
                if row.name != name:
                    p.renamed[lvl].append({"code": label(key), "old": row.name, "new": name})
                if not row.active:
                    p.restored[lvl].append({"code": label(key), "name": name})
        for key, row in db_map.items():
            if key not in f_map and row.active:
                p.retired[lvl].append({"code": label(key), "name": row.name})
    added_units, retired_units = [], []
    for delim, name in f_pus.items():
        pu = db_pus.get(delim)
        if pu is None:
            added_units.append({"code": delim, "name": name})
        else:
            if pu.name != name:
                p.renamed["polling_unit"].append({"code": delim, "old": pu.name, "new": name})
            if not pu.active:
                p.restored["polling_unit"].append({"code": delim, "name": name})
    for delim, pu in db_pus.items():
        if delim not in f_pus and pu.active:
            retired_units.append({"code": delim, "name": pu.name})
    # a retired unit and an added one with the same name in the same LGA is a move (§4.3)
    by_lga = defaultdict(list)
    for u in added_units:
        by_lga[(u["code"][:5], k(u["name"]))].append(u)
    gone_by_lga = defaultdict(int)
    for u in retired_units:
        gone_by_lga[(u["code"][:5], k(u["name"]))] += 1
    moved_old = set()
    for u in retired_units:
        key = (u["code"][:5], k(u["name"]))
        cands = by_lga.get(key, [])
        if len(cands) == 1 and gone_by_lga[key] == 1:
            p.moved.append({"from": u["code"], "to": cands[0]["code"], "name": cands[0]["name"]})
            moved_old.add(u["code"])
    moved_new = {m["to"] for m in p.moved}
    p.added["polling_unit"] = [u for u in added_units if u["code"] not in moved_new]
    p.retired["polling_unit"] = [u for u in retired_units if u["code"] not in moved_old]
    gone = [u["code"] for u in retired_units]
    p.affected = _affected(gone) if gone else {"memberships": 0, "pvc_records": 0}
    p.data = {"states": f_states, "lgas": f_lgas, "wards": f_wards, "pus": f_pus}
    return p


def _state_name(name):
    return name.title() if name.isupper() else name


def _affected(delimitations) -> dict:
    from apps.membership.models import Membership
    from apps.pvc.models import PvcVerification

    ids = list(PollingUnit.objects.filter(delimitation__in=delimitations).values_list("id", flat=True))
    return {"memberships": Membership.objects.filter(geo_polling_unit_id__in=ids).count(),
            "pvc_records": PvcVerification.objects.filter(geo_polling_unit_id__in=ids).count()}


def _plan_constituencies(content: bytes, kind: str) -> Plan:
    p = Plan(kind=kind)
    ckind = CONSTITUENCY_KINDS[kind]
    lgas = {(l_.state.code, l_.code): l_ for l_ in Lga.objects.select_related("state")}
    wards = {(w.lga.state.code, w.lga.code, w.code): w for w in Ward.objects.select_related("lga__state")}
    states = {s.code: s for s in State.objects.all()}
    found = {}
    for n, r in enumerate(_reader(content, CONSTITUENCY_COLUMNS), start=2):
        p.rows += 1
        try:
            sc = _code(r["state_code"], 2)
            code, name = (r["code"] or "").strip(), (r["name"] or "").strip()
            if not code or not name:
                raise ValueError("Empty code or name")
            if sc not in states:
                raise ValueError(f"Unknown state code {sc} (load the polling-unit list first)")
            if code in found:
                raise ValueError(f"Constituency {code} appears twice")
            area_lgas, area_wards = [], []
            for c in filter(None, (x.strip() for x in (r["lga_codes"] or "").split(";"))):
                key = (sc, _code(c, 2))
                if key not in lgas:
                    raise ValueError(f"Unknown LGA {sc}-{key[1]}")
                area_lgas.append(lgas[key].pk)
            for c in filter(None, (x.strip() for x in (r["ward_codes"] or "").split(";"))):
                parts = c.split("-")
                if len(parts) != 2:
                    raise ValueError(f"Ward code {c!r} should be LGA-ward, like 07-05")
                key = (sc, _code(parts[0], 2), _code(parts[1], 2))
                if key not in wards:
                    raise ValueError(f"Unknown ward {'-'.join(key)}")
                area_wards.append(wards[key].pk)
            if not area_lgas and not area_wards:
                raise ValueError("No LGAs or wards")
            found[code] = {"state": states[sc].pk, "name": name, "lgas": sorted(area_lgas), "wards": sorted(area_wards)}
        except ValueError as e:
            p.errors.append(f"Line {n}: {e}")
    existing = {c.code: c for c in Constituency.objects.filter(kind=ckind).prefetch_related("areas")}
    for code, c in found.items():
        row = existing.get(code)
        if row is None:
            p.added["constituency"].append({"code": code, "name": c["name"]})
            continue
        if row.name != c["name"]:
            p.renamed["constituency"].append({"code": code, "old": row.name, "new": c["name"]})
        if not row.active:
            p.restored["constituency"].append({"code": code, "name": c["name"]})
        cur_l = sorted(a.lga_id for a in row.areas.all() if a.lga_id)
        cur_w = sorted(a.ward_id for a in row.areas.all() if a.ward_id)
        if (cur_l, cur_w) != (c["lgas"], c["wards"]):
            p.renamed["areas"].append({"code": code, "old": f"{len(cur_l)} LGAs, {len(cur_w)} wards",
                                       "new": f"{len(c['lgas'])} LGAs, {len(c['wards'])} wards"})
    for code, row in existing.items():
        if code not in found and row.active:
            p.retired["constituency"].append({"code": code, "name": row.name})
    p.per_state = {s.slug: sum(1 for c in found.values() if c["state"] == s.pk) for s in states.values()
                   if any(c["state"] == s.pk for c in found.values())}
    p.data = {"found": found, "kind": ckind}
    return p


def plan(content: bytes, kind: str) -> Plan:
    if kind == Dataset.Kind.POLLING_UNITS:
        return _plan_units(content)
    if kind in CONSTITUENCY_KINDS:
        return _plan_constituencies(content, kind)
    raise GeographyFileError(f"Unknown kind {kind!r}")


def preview(content: bytes, *, kind=Dataset.Kind.POLLING_UNITS, source="", published_on=None, notes="", user=None,
            request=None) -> Dataset:
    """Check a file and record what applying it would change (spec 19 §4.3). Writes nothing else."""
    from apps.audit.models import AuditLog
    from apps.audit.services import record

    p = plan(content, kind)
    checksum = hashlib.sha256(content).hexdigest()
    name = f"geo/{checksum}.csv"
    st = storage()
    if not st.exists(name):
        st.save(name, ContentFile(content))
    ds = Dataset.objects.create(kind=kind, status=Dataset.Status.PREVIEWED, source=(source or "uploaded file")[:300],
                                published_on=published_on, notes=notes, checksum=checksum, file_name=name, preview=p.report())
    record(AuditLog.Action.GEO_DATASET_PREVIEWED, target=ds, actor=user, actor_label="" if user else "system:cli",
           request=request, kind=kind, changes=p.changes(), errors=len(p.errors))
    return ds


# ------------------------------------------------------------------ apply
def apply(ds: Dataset, user=None, *, request=None, second_admin=None) -> Dataset:
    """Write a previewed dataset (spec 19 §4.3). One transaction; refuses files with errors.

    The 95% rule: a file with fewer than 95% of the active polling units needs two different system admins. The first
    admin's apply is recorded as `first_approval_by` and refused with `geo_second_admin_required`; a second, different
    admin's apply then goes through. The command line passes the second admin as `second_admin` (`--confirm-by`).
    """
    from apps.audit.models import AuditLog
    from apps.audit.services import record
    from apps.core import outbox

    waiting = None
    with transaction.atomic():
        ds = Dataset.objects.select_for_update().get(pk=ds.pk)
        if ds.status == Dataset.Status.APPLIED:
            raise ServiceError("This file has already been applied.", code="invalid_transition", status_code=409)
        content = storage().open(ds.file_name, "rb").read()
        p = plan(content, ds.kind)  # recomputed: the database may have changed since the preview
        if p.errors:
            raise GeographyFileError("This file has problems and can't be applied.", p.errors)
        approvers = {x.pk for x in (user, second_admin) if x is not None}
        if user is None:
            approvers.add("system:cli")  # the command line counts as one admin (spec 19 §12.2)
        if p.needs_second_admin() and len(approvers) < 2:
            if user is not None and not ds.first_approval_by_id:
                Dataset.objects.filter(pk=ds.pk).update(first_approval_by=user)
            waiting = (p.active_units, p.current_units)
        else:
            if ds.kind == Dataset.Kind.POLLING_UNITS:
                _write_units(ds, p)
            else:
                _write_constituencies(ds, p)
            Dataset.objects.filter(pk=ds.pk).update(
                status=Dataset.Status.APPLIED, applied_at=timezone.now(), applied_by=user, preview=p.report(),
                states=State.objects.count(), lgas=Lga.objects.filter(active=True).count(),
                wards=Ward.objects.filter(active=True).count(), polling_units=PollingUnit.objects.filter(active=True).count(),
                constituencies=Constituency.objects.filter(active=True).count(),
                deactivated=sum(len(v) for v in p.retired.values()) + len(p.moved))
            ds.refresh_from_db()
            other = second_admin.pk if second_admin else (ds.first_approval_by_id if p.needs_second_admin() else None)
            record(AuditLog.Action.GEO_DATASET_APPLIED, target=ds, actor=user, actor_label="" if user else "system:cli",
                   request=request, kind=ds.kind, changes=p.changes(), second_admin=str(other or ""))
            # IDs only (spec 15 §8): the handler reads the retired and moved units from this dataset's Change rows
            outbox.enqueue("geo_dataset_applied", {"dataset": ds.pk}, dedupe_key=f"geo_dataset_applied:{ds.pk}")
    if waiting:
        raise ServiceError(
            f"This file has {waiting[0]} polling units, fewer than 95% of the {waiting[1]} active now. "
            "A second system admin must apply it too.", code="geo_second_admin_required", status_code=409)
    return ds


def _write_units(ds, p: Plan):
    d = p.data
    changes = []
    states = {s.code: s for s in State.objects.all()}
    for sc, (slug, name) in d["states"].items():
        if sc not in states:
            states[sc] = State.objects.create(code=sc, slug=slug, name=_state_name(name), capital=CAPITALS.get(slug, ""))
            changes.append(Change(dataset=ds, level="state", action="added", code=sc, new_value=states[sc].name))
    # LGAs
    lgas = {(l_.state.code, l_.code): l_ for l_ in Lga.objects.select_related("state")}
    taken = defaultdict(set)
    for l_ in lgas.values():
        taken[l_.state_id].add(l_.slug)
    aliases = []
    new = []
    for (sc, lc), name in d["lgas"].items():
        row = lgas.get((sc, lc))
        st = states[sc]
        if row is None:
            new.append(Lga(state=st, code=lc, name=name, folded_name=k(name), slug=lga_slug(name, lc, taken[st.pk])))
            changes.append(Change(dataset=ds, level="lga", action="added", code=f"{sc}-{lc}", new_value=name))
        elif row.name != name or not row.active:
            if row.name != name:
                changes.append(Change(dataset=ds, level="lga", action="renamed", code=f"{sc}-{lc}", old_value=row.name, new_value=name))
                aliases.append(Alias(lga=row, name=row.name, folded_name=k(row.name), source=Alias.Source.RENAME))
            if not row.active:
                changes.append(Change(dataset=ds, level="lga", action="restored", code=f"{sc}-{lc}", new_value=name))
            Lga.objects.filter(pk=row.pk).update(name=name, folded_name=k(name), active=True)
    Lga.objects.bulk_create(new, batch_size=2000)
    retire = [r for key, r in lgas.items() if key not in d["lgas"] and r.active]
    for r in retire:
        changes.append(Change(dataset=ds, level="lga", action="retired", code=f"{r.state.code}-{r.code}", old_value=r.name))
    Lga.objects.filter(pk__in=[r.pk for r in retire]).update(active=False)
    lgas = {(l_.state.code, l_.code): l_ for l_ in Lga.objects.select_related("state")}
    # wards
    wards = {(w.lga.state.code, w.lga.code, w.code): w for w in Ward.objects.select_related("lga__state")}
    new = []
    for (sc, lc, wc), name in d["wards"].items():
        row = wards.get((sc, lc, wc))
        code = f"{sc}-{lc}-{wc}"
        if row is None:
            new.append(Ward(lga=lgas[(sc, lc)], code=wc, name=name, folded_name=k(name)))
            changes.append(Change(dataset=ds, level="ward", action="added", code=code, new_value=name))
        elif row.name != name or not row.active:
            if row.name != name:
                changes.append(Change(dataset=ds, level="ward", action="renamed", code=code, old_value=row.name, new_value=name))
                aliases.append(Alias(ward=row, name=row.name, folded_name=k(row.name), source=Alias.Source.RENAME))
            if not row.active:
                changes.append(Change(dataset=ds, level="ward", action="restored", code=code, new_value=name))
            Ward.objects.filter(pk=row.pk).update(name=name, folded_name=k(name), active=True)
    Ward.objects.bulk_create(new, batch_size=2000)
    retire = [w for key, w in wards.items() if key not in d["wards"] and w.active]
    for w in retire:
        changes.append(Change(dataset=ds, level="ward", action="retired", code=f"{w.lga.state.code}-{w.lga.code}-{w.code}", old_value=w.name))
    Ward.objects.filter(pk__in=[w.pk for w in retire]).update(active=False)
    wards = {(w.lga.state.code, w.lga.code, w.code): w for w in Ward.objects.select_related("lga__state")}
    # polling units
    pus = {pu.delimitation: pu for pu in PollingUnit.objects.only("id", "delimitation", "name", "active", "ward_id")}
    moved = {m["to"]: m["from"] for m in p.moved}
    new = []
    for delim, name in d["pus"].items():
        row = pus.get(delim)
        sc, lc, wc, uc = delim.split("-")
        if row is None:
            new.append(PollingUnit(ward=wards[(sc, lc, wc)], code=uc, name=name, folded_name=k(name), delimitation=delim))
            if delim in moved:
                changes.append(Change(dataset=ds, level="polling_unit", action="moved", code=delim, old_value=moved[delim], new_value=name))
            else:
                changes.append(Change(dataset=ds, level="polling_unit", action="added", code=delim, new_value=name))
        elif row.name != name or not row.active:
            if row.name != name:
                changes.append(Change(dataset=ds, level="polling_unit", action="renamed", code=delim, old_value=row.name, new_value=name))
                aliases.append(Alias(polling_unit=row, name=row.name, folded_name=k(row.name), source=Alias.Source.RENAME))
            if not row.active:
                changes.append(Change(dataset=ds, level="polling_unit", action="restored", code=delim, new_value=name))
            PollingUnit.objects.filter(pk=row.pk).update(name=name, folded_name=k(name), active=True)
    PollingUnit.objects.bulk_create(new, batch_size=5000)
    retire = [delim for delim, pu in pus.items() if delim not in d["pus"] and pu.active]
    moved_from = set(moved.values())
    for delim in retire:
        if delim not in moved_from:
            changes.append(Change(dataset=ds, level="polling_unit", action="retired", code=delim, old_value=pus[delim].name))
    for i in range(0, len(retire), 5000):
        PollingUnit.objects.filter(delimitation__in=retire[i:i + 5000]).update(active=False)
    Change.objects.bulk_create(changes, batch_size=5000)
    Alias.objects.bulk_create(aliases, batch_size=2000)


def _write_constituencies(ds, p: Plan):
    found, ckind = p.data["found"], p.data["kind"]
    existing = {c.code: c for c in Constituency.objects.filter(kind=ckind).prefetch_related("areas")}
    changes = []
    for code, c in found.items():
        row = existing.get(code)
        if row is None:
            row = Constituency.objects.create(kind=ckind, state_id=c["state"], code=code, name=c["name"], folded_name=k(c["name"]),
                                              slug=lga_slug(c["name"], code.lower(), set()), dataset=ds)
            changes.append(Change(dataset=ds, level="constituency", action="added", code=code, new_value=c["name"]))
        else:
            cur = (sorted(a.lga_id for a in row.areas.all() if a.lga_id), sorted(a.ward_id for a in row.areas.all() if a.ward_id))
            if row.name != c["name"]:
                changes.append(Change(dataset=ds, level="constituency", action="renamed", code=code, old_value=row.name, new_value=c["name"]))
            if not row.active:
                changes.append(Change(dataset=ds, level="constituency", action="restored", code=code, new_value=c["name"]))
            Constituency.objects.filter(pk=row.pk).update(name=c["name"], folded_name=k(c["name"]), active=True, dataset=ds,
                                                          state_id=c["state"])
            if cur == (c["lgas"], c["wards"]):
                continue
            changes.append(Change(dataset=ds, level="constituency", action="areas", code=code))
            ConstituencyArea.objects.filter(constituency=row).delete()
        ConstituencyArea.objects.bulk_create([ConstituencyArea(constituency=row, lga_id=x) for x in c["lgas"]]
                                             + [ConstituencyArea(constituency=row, ward_id=x) for x in c["wards"]])
    for code, row in existing.items():
        if code not in found and row.active:
            changes.append(Change(dataset=ds, level="constituency", action="retired", code=code, old_value=row.name))
            Constituency.objects.filter(pk=row.pk).update(active=False)
    Change.objects.bulk_create(changes)


# ------------------------------------------------------------------ one step (tests, live checks, the old CLI)
@dataclass
class Result:
    created: dict = field(default_factory=lambda: {"states": 0, "lgas": 0, "wards": 0, "polling_units": 0})
    updated: int = 0
    deactivated: int = 0
    rows: int = 0
    dataset: Dataset | None = None


def load(content: bytes, *, source: str, published_on=None, dry_run=False, kind=Dataset.Kind.POLLING_UNITS) -> Result:
    """Preview and apply in one go, as the command line's system actor. `dry_run` previews without saving anything."""
    res = Result()
    p = plan(content, kind)
    if p.errors:
        raise GeographyFileError(p.errors[0], p.errors)
    res.rows = p.rows
    names = {"state": "states", "lga": "lgas", "ward": "wards", "polling_unit": "polling_units"}
    for lvl, items in p.added.items():
        if lvl in names:
            res.created[names[lvl]] = len(items)
    res.created["polling_units"] += len(p.moved)
    res.updated = sum(len(v) for v in p.renamed.values()) + sum(len(v) for v in p.restored.values())
    res.deactivated = sum(len(v) for v in p.retired.values()) + len(p.moved)
    if dry_run:
        return res
    checksum = hashlib.sha256(content).hexdigest()
    if not p.changes() and Dataset.objects.filter(checksum=checksum, status=Dataset.Status.APPLIED).exists():
        return res
    with transaction.atomic():
        ds = preview(content, kind=kind, source=source, published_on=published_on)
        res.dataset = apply(ds, None)
    return res
