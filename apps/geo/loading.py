"""Load INEC's polling-unit list (spec 16 §8.1). Idempotent: codes are the keys; a second load of the same file changes
nothing; units missing from a newer file become inactive."""
import csv
import hashlib
import io
from dataclasses import dataclass, field

from django.db import transaction

from apps.search.text import fold

from .models import Dataset, Lga, PollingUnit, State, Ward

COLUMNS = ["state_code", "state", "lga_code", "lga", "ward_code", "ward", "pu_code", "polling_unit"]
STATE_ALIASES = {"federal capital territory": "fct", "fct": "fct", "abuja": "fct", "akwa-ibom": "akwa_ibom",
                 "cross-river": "cross_river", "nassarawa": "nasarawa"}


class GeographyFileError(Exception):
    pass


@dataclass
class Result:
    created: dict = field(default_factory=lambda: {"states": 0, "lgas": 0, "wards": 0, "polling_units": 0})
    updated: int = 0
    deactivated: int = 0
    rows: int = 0
    dataset: Dataset | None = None


def state_slug(name: str) -> str:
    from apps.candidates.reference import State as S

    f = fold(name).lower().replace(" state", "").strip()
    if f in STATE_ALIASES:
        return STATE_ALIASES[f]
    for value, label in S.choices:
        if fold(label).lower() == f or value == f.replace(" ", "_").replace("-", "_"):
            return value
    raise GeographyFileError(f"Unknown state name {name!r}")


def _code(v, width):
    v = (v or "").strip()
    if not v.isdigit() or len(v) > width:
        raise GeographyFileError(f"Bad code {v!r}")
    return v.zfill(width)


def read_rows(content: bytes):
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise GeographyFileError(f"Missing columns: {', '.join(missing)} (expected {', '.join(COLUMNS)})")
    for n, r in enumerate(reader, start=2):
        try:
            yield {"state_code": _code(r["state_code"], 2), "state": r["state"].strip(), "lga_code": _code(r["lga_code"], 2),
                   "lga": r["lga"].strip(), "ward_code": _code(r["ward_code"], 2), "ward": r["ward"].strip(),
                   "pu_code": _code(r["pu_code"], 3), "polling_unit": r["polling_unit"].strip()}
        except GeographyFileError as e:
            raise GeographyFileError(f"Line {n}: {e}") from None


def load(content: bytes, *, source: str, published_on=None, dry_run=False) -> Result:
    res = Result()
    checksum = hashlib.sha256(content).hexdigest()
    rows = list(read_rows(content))
    res.rows = len(rows)
    with transaction.atomic():
        states = {s.code: s for s in State.objects.all()}
        lgas = {(lga.state_id, lga.code): lga for lga in Lga.objects.all()}
        wards = {(w.lga_id, w.code): w for w in Ward.objects.all()}
        pus = {p.delimitation: p for p in PollingUnit.objects.all()}
        seen = set()
        for r in rows:
            st = states.get(r["state_code"])
            slug = state_slug(r["state"])
            if st is None:
                st = State.objects.create(code=r["state_code"], slug=slug, name=r["state"].title() if r["state"].isupper() else r["state"])
                states[st.code] = st
                res.created["states"] += 1
            elif st.slug != slug:
                raise GeographyFileError(f"State code {r['state_code']} is {st.slug} here but {slug} in the file")
            lga = lgas.get((st.pk, r["lga_code"]))
            if lga is None:
                lga = Lga.objects.create(state=st, code=r["lga_code"], name=r["lga"], folded_name=fold(r["lga"]).lower())
                lgas[(st.pk, lga.code)] = lga
                res.created["lgas"] += 1
            elif lga.name != r["lga"] or not lga.active:
                Lga.objects.filter(pk=lga.pk).update(name=r["lga"], folded_name=fold(r["lga"]).lower(), active=True)
                lga.name, lga.active = r["lga"], True
                res.updated += 1
            w = wards.get((lga.pk, r["ward_code"]))
            if w is None:
                w = Ward.objects.create(lga=lga, code=r["ward_code"], name=r["ward"], folded_name=fold(r["ward"]).lower())
                wards[(lga.pk, w.code)] = w
                res.created["wards"] += 1
            elif w.name != r["ward"] or not w.active:
                Ward.objects.filter(pk=w.pk).update(name=r["ward"], folded_name=fold(r["ward"]).lower(), active=True)
                w.name, w.active = r["ward"], True
                res.updated += 1
            delim = f"{r['state_code']}-{r['lga_code']}-{r['ward_code']}-{r['pu_code']}"
            seen.add(delim)
            p = pus.get(delim)
            if p is None:
                pus[delim] = PollingUnit.objects.create(ward=w, code=r["pu_code"], name=r["polling_unit"],
                                                        folded_name=fold(r["polling_unit"]).lower(), delimitation=delim)
                res.created["polling_units"] += 1
            elif p.name != r["polling_unit"] or not p.active or p.ward_id != w.pk:
                PollingUnit.objects.filter(pk=p.pk).update(name=r["polling_unit"], folded_name=fold(r["polling_unit"]).lower(),
                                                           active=True, ward=w)
                res.updated += 1
        gone = [d for d, p in pus.items() if d not in seen and p.active]
        res.deactivated = PollingUnit.objects.filter(delimitation__in=gone).update(active=False) if gone else 0
        changed = res.updated + res.deactivated + sum(res.created.values())
        if changed or not Dataset.objects.filter(checksum=checksum).exists():
            res.dataset = Dataset.objects.create(source=source[:300], published_on=published_on, checksum=checksum,
                                                 states=State.objects.count(), lgas=Lga.objects.count(), wards=Ward.objects.count(),
                                                 polling_units=PollingUnit.objects.filter(active=True).count(),
                                                 deactivated=res.deactivated)
        if dry_run:
            transaction.set_rollback(True)
    return res
