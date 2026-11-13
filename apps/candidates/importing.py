"""
Bulk import of researched candidate profiles from a JSON file (data/candidates/*.json).

Every record lands as a draft through the normal create/update services, so the same validation, slugs and audit
entries apply as for a profile typed in by an admin. Nothing is verified or published here: an admin still checks each
draft and runs the usual verify → publish workflow.

Idempotent. Each record carries a stable key; the importer writes it into the internal verification notes as
``import-key: <dataset>:<key>`` and finds its own drafts by it on the next run. A run never touches:
- a record that someone has verified, published, unpublished or archived (even one the importer first created);
- a record that wasn't created by this importer but has the same name, office, year and state (kept as it is).
"""
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from django.db import transaction
from rest_framework import serializers

from .models import (
    MAX_LINK_LABEL,
    MAX_LINKS,
    MAX_PRIORITIES,
    MAX_PRIORITY_LEN,
    Candidate,
)
from .models import (
    CandidateStatus as S,
)
from .reference import Position, State
from .services import create_candidate, update_candidate

STATUSES = {"inec_listed", "party_announced", "disputed"}
CONFIDENCE = {"high", "medium", "low"}
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,120}$")


class ImportFileError(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class Result:
    created: list = field(default_factory=list)
    updated: list = field(default_factory=list)
    unchanged: list = field(default_factory=list)
    kept_reviewed: list = field(default_factory=list)  # ours, but an admin has moved it on — never overwritten
    kept_existing: list = field(default_factory=list)  # same person already in the database, not created by us
    flagged: list = field(default_factory=list)  # disputed or low-confidence: needs extra care in review

    def summary(self, *, dry_run=False) -> str:
        head = "Dry run — nothing saved. " if dry_run else ""
        return (f"{head}{len(self.created)} created, {len(self.updated)} updated, {len(self.unchanged)} unchanged, "
                f"{len(self.kept_reviewed)} left alone (already reviewed), {len(self.kept_existing)} left alone "
                f"(already in the database). {len(self.flagged)} flagged for careful review.")


def fold(name: str) -> str:
    """Name for duplicate matching: accents, case, punctuation and spacing ignored."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s.lower()).split())


def marker(dataset: str, key: str) -> str:
    return f"import-key: {dataset}:{key}"


# ------------------------------------------------------------------------------------------------ file checks
def load(path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ImportFileError([f"Can't read {path}: {exc}"]) from exc
    check(data)
    return data


def check(data: dict):
    """Reject the whole file before any write if a record breaks the model's limits or the file's own rules."""
    errors = []
    if not isinstance(data, dict) or not isinstance(data.get("candidates"), list):
        raise ImportFileError(["The file needs a 'candidates' list."])
    dataset, year = data.get("dataset"), data.get("election_year")
    if not dataset or not KEY_RE.match(str(dataset)):
        errors.append("Set 'dataset' to a short lowercase id, like ndc-2027.")
    if not isinstance(year, int) or not 2023 <= year <= 2099:
        errors.append("Set 'election_year' to a four-digit year between 2023 and 2099.")
    seen_keys, seen_people = set(), set()
    for i, rec in enumerate(data["candidates"], 1):
        where = f"record {i} ({rec.get('key') or rec.get('full_name') or '?'})"
        key = rec.get("key", "")
        if not KEY_RE.match(key):
            errors.append(f"{where}: key must be lowercase letters, digits and hyphens.")
        if key in seen_keys:
            errors.append(f"{where}: duplicate key.")
        seen_keys.add(key)
        if rec.get("position") not in Position.values:
            errors.append(f"{where}: unknown position {rec.get('position')!r}.")
        if rec.get("state", "") and rec["state"] not in State.values:
            errors.append(f"{where}: unknown state {rec.get('state')!r}.")
        person = (fold(rec.get("full_name", "")), rec.get("position"), rec.get("state", ""), rec.get("area_name", ""))
        if person in seen_people:
            errors.append(f"{where}: the same person and office appear twice.")
        seen_people.add(person)
        if not fold(rec.get("full_name", "")):
            errors.append(f"{where}: full_name is required.")
        if len(rec.get("biography", "")) > 5000:
            errors.append(f"{where}: biography is over 5,000 characters.")
        if len(rec.get("campaign_summary", "")) > 1000:
            errors.append(f"{where}: campaign summary is over 1,000 characters.")
        pr = rec.get("priorities", [])
        if len(pr) > MAX_PRIORITIES or any(not isinstance(p, str) or not p.strip() or len(p) >= MAX_PRIORITY_LEN for p in pr):
            errors.append(f"{where}: at most {MAX_PRIORITIES} priorities, each non-empty and under {MAX_PRIORITY_LEN} characters.")
        links = rec.get("links", [])
        if len(links) > MAX_LINKS:
            errors.append(f"{where}: at most {MAX_LINKS} links.")
        for link in links:
            if not str(link.get("url", "")).startswith("https://") or not link.get("label") or len(link["label"]) > MAX_LINK_LABEL:
                errors.append(f"{where}: each link needs an https URL and a label up to {MAX_LINK_LABEL} characters.")
        r = rec.get("research") or {}
        if r.get("status") not in STATUSES:
            errors.append(f"{where}: research.status must be one of {sorted(STATUSES)}.")
        if r.get("confidence") not in CONFIDENCE:
            errors.append(f"{where}: research.confidence must be high, medium or low.")
        if not r.get("sources"):
            errors.append(f"{where}: list at least one source.")
    if errors:
        raise ImportFileError(errors)


# ---------------------------------------------------------------------------------------------- notes + fields
def notes_for(data: dict, rec: dict) -> str:
    """The internal research record kept in verification_notes. Never shown publicly."""
    r = rec["research"]
    lines = [
        marker(data["dataset"], rec["key"]),
        f"Research status: {r['status'].replace('_', ' ')} (confidence: {r['confidence']}). Checked {data.get('checked', '')}.",
        "NOT VERIFIED — imported research for admin review. Check each fact against the sources before verifying.",
        "Sources:",
        *(f"- {s}" for s in r["sources"]),
    ]
    if r.get("missing"):
        lines += ["Missing:", *(f"- {m}" for m in r["missing"])]
    if r.get("flags"):
        lines += ["Flags:", *(f"- {f}" for f in r["flags"])]
    return "\n".join(lines)


def fields_for(data: dict, rec: dict) -> dict:
    return {
        "full_name": rec["full_name"],
        "position": rec["position"],
        "election_year": data["election_year"],
        "state": rec.get("state", ""),
        "area_name": rec.get("area_name", ""),
        "party": data.get("party", "NDC"),
        "biography": rec.get("biography", ""),
        "campaign_summary": rec.get("campaign_summary", ""),
        "priorities": list(rec.get("priorities", [])),
        "links": [{"label": l["label"], "url": l["url"]} for l in rec.get("links", [])],
    }


def _same_person(rec_fields: dict):
    target = fold(rec_fields["full_name"])
    qs = Candidate.objects.filter(position=rec_fields["position"], election_year=rec_fields["election_year"],
                                  state=rec_fields["state"])
    return next((c for c in qs if fold(c.full_name) == target), None)


# ------------------------------------------------------------------------------------------------------ import
def run(data: dict, *, dry_run=False) -> Result:
    check(data)
    res = Result()
    with transaction.atomic():
        for rec in data["candidates"]:
            _one(data, rec, res)
        if dry_run:
            transaction.set_rollback(True)
    return res


def _one(data: dict, rec: dict, res: Result):
    label = f"{rec['full_name']} ({rec['position']}{', ' + rec['state'] if rec.get('state') else ''})"
    r = rec["research"]
    if r["status"] == "disputed" or r["confidence"] == "low":
        res.flagged.append(label)
    fields, notes = fields_for(data, rec), notes_for(data, rec)
    mine = Candidate.objects.filter(verification_notes__contains=marker(data["dataset"], rec["key"])).first()

    if mine is None:
        other = _same_person(fields)
        if other is not None:
            res.kept_existing.append(label)
            return
        try:
            create_candidate(data={**fields, "verification_notes": notes}, actor=None)
        except serializers.ValidationError as exc:
            raise ImportFileError([f"{label}: {exc.detail}"]) from exc
        res.created.append(label)
        return

    if mine.is_verified or mine.status != S.DRAFT:
        res.kept_reviewed.append(label)
        return
    changed = {f: v for f, v in fields.items() if getattr(mine, f) != v}
    if not changed and mine.verification_notes == notes:
        res.unchanged.append(label)
        return
    if changed:
        try:
            mine, _ = update_candidate(candidate=mine, data=changed, actor=None)
        except serializers.ValidationError as exc:
            raise ImportFileError([f"{label}: {exc.detail}"]) from exc
    if mine.verification_notes != notes:
        mine.verification_notes = notes
        mine.save(update_fields=["verification_notes", "updated_at"])
    res.updated.append(label)
