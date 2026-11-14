"""
Bulk import of community news written for named members (data/news/2026-11-member-news.json).

Each item goes through the same path as the app: `services.submit` as its author (profile complete, not paused,
declarations, body length, https source), then `services.approve` by a news reviewer who is not the author
(spec 07 §6.1: no self-review). The file names a `reviewer` and a `second_reviewer`; the second one approves the
reviewer's own items. With `pending=True` items are only submitted and wait in the review queue — the app's limit of
NEWS_MAX_PENDING per author still applies, so the rest of that author's items are reported as not submitted.

Approvals here don't notify the author (one bulk import would otherwise send each member a dozen notifications).
An approved item keeps the date its source published it, so the feed stays in real order.

Authors (and the reviewers) are written in the file as short handles such as "m1", mapped to account emails at run
time (`authors={"m1": "ada@example.com", ...}`), so no member's email address is stored in the repository. A value
that already contains "@" is used as the email as it is.

Idempotent: an item is matched by author + exact title against that author's community articles in any status.
A match is left exactly as it is, so a later edit, withdrawal or removal is never undone.
"""
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from apps.access.models import Role
from apps.access.services import has_role
from apps.candidates.reference import State
from apps.core.exceptions import ServiceError
from apps.core.i18n import CONTENT_LANGUAGES

from . import services
from .importing import ImportFileError
from .models import Article, ArticleStatus, Source

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,80}$")
LAGOS = ZoneInfo("Africa/Lagos")
MAX_TITLE = Article._meta.get_field("title").max_length
MAX_URL = Article._meta.get_field("source_url").max_length


@dataclass
class Result:
    created: list = field(default_factory=list)
    existing: list = field(default_factory=list)
    not_submitted: list = field(default_factory=list)
    per_author: dict = field(default_factory=dict)

    def summary(self, *, dry_run=False, pending=False) -> str:
        head = "Dry run — nothing saved. " if dry_run else ""
        verb = ("would be submitted for review" if pending else "would be published") if dry_run else \
            ("submitted for review" if pending else "published")
        tail = f", {len(self.not_submitted)} not submitted (see --list)" if self.not_submitted else ""
        return f"{head}{len(self.created)} {verb}, {len(self.existing)} already there (left alone){tail}."


def load(path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ImportFileError([f"Can't read {path}: {exc}"]) from exc
    check(data)
    return data


def check(data: dict):
    """Validate the whole file (shape only; accounts are checked in `run`) before anything is saved."""
    errors = []
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise ImportFileError(["The file needs a non-empty \"items\" list."])
    seen_keys, seen = set(), set()
    for n, it in enumerate(items, 1):
        where = f"item {n} ({it.get('key', '?') if isinstance(it, dict) else '?'})"
        if not isinstance(it, dict):
            errors.append(f"{where}: not an object")
            continue
        key, title, body = str(it.get("key", "")), str(it.get("title", "")).strip(), services.clean_text(it.get("body", ""))
        author = str(it.get("author", "")).strip().lower()
        if not KEY_RE.fullmatch(key):
            errors.append(f"{where}: key must be lower-case letters, digits and dashes")
        elif key in seen_keys:
            errors.append(f"{where}: duplicate key")
        seen_keys.add(key)
        if not re.fullmatch(r"[a-z0-9][a-z0-9._+-]*(@[a-z0-9.-]+)?", author):
            errors.append(f"{where}: author must be a handle (like m1) or an email address")
        if not title or len(title) > MAX_TITLE:
            errors.append(f"{where}: title must be 1–{MAX_TITLE} characters")
        elif (author, title) in seen:
            errors.append(f"{where}: duplicate title for {author}")
        seen.add((author, title))
        if not settings.NEWS_COMMUNITY_MIN_BODY <= len(body) <= settings.NEWS_COMMUNITY_MAX_BODY:
            errors.append(f"{where}: body must be {settings.NEWS_COMMUNITY_MIN_BODY}–{settings.NEWS_COMMUNITY_MAX_BODY:,} characters")
        url = str(it.get("source_url", ""))
        if not url.startswith("https://") or len(url) > MAX_URL:
            errors.append(f"{where}: source_url must be an https link")
        if it.get("state", "") and it["state"] not in State.values:
            errors.append(f"{where}: unknown state {it['state']!r}")
        if it.get("language", "en") not in CONTENT_LANGUAGES:
            errors.append(f"{where}: unknown language {it.get('language')!r}")
        try:
            if date.fromisoformat(str(it.get("published_at", ""))) > timezone.localdate():
                errors.append(f"{where}: published_at is in the future")
        except ValueError:
            errors.append(f"{where}: published_at must be YYYY-MM-DD")
    if errors:
        raise ImportFileError(errors)


def _user(email):
    return get_user_model().objects.filter(email__iexact=(email or "").strip()).first()


def _email(handle, authors):
    handle = (handle or "").strip().lower()
    return handle if "@" in handle else authors.get(handle, "")


def parse_authors(text: str) -> dict:
    """"m1=a@x.com,m2=b@y.com" -> {"m1": "a@x.com", ...}"""
    out = {}
    for part in filter(None, (p.strip() for p in (text or "").split(","))):
        handle, sep, email = part.partition("=")
        if not sep or "@" not in email:
            raise ImportFileError([f"--authors entry {part!r} must look like m1=name@example.com"])
        out[handle.strip().lower()] = email.strip().lower()
    return out


def _accounts(data, authors, reviewer, second):
    """Resolve every account up front, so a missing or unready account stops the run before anything is saved."""
    errors = []
    unmapped = sorted({h for h in [*(it["author"].strip().lower() for it in data["items"]), reviewer, second]
                       if h and "@" not in h and h not in authors})
    if unmapped:
        raise ImportFileError([f"No email given for {', '.join(unmapped)}: pass --authors " +
                               ",".join(f"{h}=<email>" for h in unmapped)])
    reviewer_email, second_email = _email(reviewer, authors), _email(second, authors)
    reviewer, second = _user(reviewer_email), _user(second_email)
    for label, email, u in (("reviewer", reviewer_email, reviewer), ("second reviewer", second_email, second)):
        if not email:
            errors.append(f"No {label} given (set it in the file or on the command line).")
        elif u is None:
            errors.append(f"The {label} {email} has no account.")
        elif not has_role(u, Role.NEWS_REVIEWER):
            errors.append(f"The {label} {email} is not an active news reviewer or system admin.")
    if reviewer and second and reviewer.pk == second.pk:
        errors.append("The reviewer and second reviewer must be different people (no one reviews their own article).")
    accounts = {}
    for handle in sorted({it["author"].strip().lower() for it in data["items"]}):
        email = _email(handle, authors)
        u = _user(email)
        if u is None:
            errors.append(f"Author {email} has no account.")
        elif not u.is_active:
            errors.append(f"Author {email} is deactivated.")
        elif not u.profile_complete:
            errors.append(f"Author {email} hasn't finished setting up their profile, so they can't post news yet.")
        accounts[handle] = u
    if errors:
        raise ImportFileError(errors)
    return reviewer, second, accounts


def _published_at(day: str, n: int):
    # Spread same-day items a minute apart so the feed order is stable.
    return datetime.combine(date.fromisoformat(day), time(9, 0), tzinfo=LAGOS) + timedelta(minutes=n)


def run(data: dict, *, authors=None, reviewer="", second="", pending=False, show_names=False, dry_run=False) -> Result:
    reviewer, second, accounts = _accounts(data, authors or {}, (reviewer or data.get("reviewer", "")).strip().lower(),
                                           (second or data.get("second_reviewer", "")).strip().lower())
    res = Result()
    queued = {}  # author pk -> pending count, for the dry run and for --pending
    for n, it in enumerate(data["items"]):
        author, title = accounts[it["author"].strip().lower()], it["title"].strip()
        email = author.email
        label = f"{email}: {title}"
        if Article.objects.filter(author=author, source=Source.COMMUNITY, title=title).exists():
            res.existing.append(label)
            continue
        if pending:
            if author.pk not in queued:
                queued[author.pk] = Article.objects.filter(author=author, source=Source.COMMUNITY,
                                                           status=ArticleStatus.PENDING).count()
            if queued[author.pk] >= settings.NEWS_MAX_PENDING:
                res.not_submitted.append(f"{label} — {email} already has {settings.NEWS_MAX_PENDING} waiting for review")
                continue
            queued[author.pk] += 1
        res.created.append(label)
        res.per_author[email] = res.per_author.get(email, 0) + 1
        if dry_run:
            continue
        payload = {"title": title, "body": it["body"], "state": it.get("state", ""), "source_url": it["source_url"],
                   "language": it.get("language") or "en", "show_author_name": show_names,
                   "declarations": {k: True for k in services.DECLARATIONS}}
        try:
            with transaction.atomic():
                a = services.submit(author, payload)
                if not pending:
                    services.approve(a, second if reviewer.pk == author.pk else reviewer, notify=False)
                    Article.objects.filter(pk=a.pk).update(published_at=_published_at(it["published_at"], n))
        except ServiceError as exc:  # e.g. the author was paused, or a campaign blackout began mid-run
            res.created.pop()
            res.per_author[email] -= 1
            res.not_submitted.append(f"{label} — {exc}")
    return res
