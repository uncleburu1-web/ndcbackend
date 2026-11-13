"""
Languages and translation (spec 21).

- The five interface languages, and the content-language values (`und` = "not sure").
- Django's own gettext does the translating (§6). Catalogues are `locale/<code>/LC_MESSAGES/django.po`; this module reads
  and writes them and compiles `.mo` files in pure Python, because the Railway image has no GNU gettext. The msgid is
  the English text itself, so an English change simply has no translation until a translator adds one (English shows).
- `translate_payload` translates the prose in a payload (rules, privacy notice) and says whether it was complete.
- Text people write is normalised to NFC on save (§11), so the same letter typed two ways is one string.
"""
import ast
import functools
import re
import struct
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import translation

LANGUAGES = ("en", "ha", "ig", "yo", "pcm")
NATIVE = {"en": "English", "ha": "Hausa", "ig": "Igbo", "yo": "Yorùbá", "pcm": "Naijá"}
ENGLISH_NAME = {"en": "English", "ha": "Hausa", "ig": "Igbo", "yo": "Yoruba", "pcm": "Nigerian Pidgin"}
UNSURE = "und"
CONTENT_LANGUAGES = LANGUAGES + (UNSURE,)
CONTENT_CHOICES = [(c, ENGLISH_NAME.get(c, "Not sure")) for c in CONTENT_LANGUAGES]
TRANSLATED = tuple(c for c in LANGUAGES if c != "en")


def language_field(default=UNSURE, **kw):
    return models.CharField(max_length=3, choices=CONTENT_CHOICES, default=default, db_default=default, **kw)


def language_check(name: str, field_name: str = "language") -> models.CheckConstraint:
    return models.CheckConstraint(name=name, condition=Q(**{f"{field_name}__in": CONTENT_LANGUAGES}))


def clean_content_language(value, fallback: str) -> str:
    """A writer's chosen language (§9): one of the five or `und`; anything else is a field error."""
    from rest_framework import serializers

    if value in (None, ""):
        return fallback if fallback in CONTENT_LANGUAGES else UNSURE
    if value not in CONTENT_LANGUAGES:
        raise serializers.ValidationError({"language": ["Choose English, Hausa, Igbo, Yoruba, Nigerian Pidgin or “Not sure”."]})
    return value


def user_language(user) -> str:
    """The person's interface language (20): the default for what they write (§9)."""
    from apps.accounts.models import UserPreferences

    if not getattr(user, "pk", None):
        return "en"
    return UserPreferences.objects.filter(user=user).values_list("language", flat=True).first() or "en"


def writer_language(request) -> str:
    return getattr(request, "ndc_language", "en") or "en"


def param_language(request) -> str:
    """`?lang=` for cached public endpoints (rule 7): an enabled code, else English. Never Accept-Language."""
    from apps.accounts.language import enabled

    code = (request.query_params.get("lang") if hasattr(request, "query_params") else request.GET.get("lang")) or "en"
    return code if code in enabled() else "en"


# ---------------------------------------------------------------- payload translation
_PROSE = re.compile(r"[A-Za-z].*\s")
_NOT_PROSE = re.compile(r"^(https?://|mailto:|/|\d{4}-\d\d-\d\d)|@\S+\.\S+$")


def is_prose(s) -> bool:
    return isinstance(s, str) and len(s) > 2 and bool(_PROSE.search(s)) and not _NOT_PROSE.search(s.strip())


def walk_strings(data):
    if isinstance(data, str):
        if is_prose(data):
            yield data
    elif isinstance(data, dict):
        for v in data.values():
            yield from walk_strings(v)
    elif isinstance(data, (list, tuple)):
        for v in data:
            yield from walk_strings(v)


def translate_payload(data, lang: str):
    """(translated copy, complete?) — every prose string through gettext in `lang`. English: unchanged, complete."""
    if lang == "en":
        return data, True
    state = {"missing": 0}

    def tr(v):
        if isinstance(v, str):
            if not is_prose(v):
                return v
            out = translation.gettext(v)
            if out == v:
                state["missing"] += 1
            return out
        if isinstance(v, dict):
            return {k: tr(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [tr(x) for x in v]
        return v

    with translation.override(lang):
        out = tr(data)
    return out, state["missing"] == 0


def legal_version() -> str:
    from apps.moderation.rules import RULES_VERSION
    from apps.privacy.register import PRIVACY_VERSION

    return f"rules-{RULES_VERSION}.privacy-{PRIVACY_VERSION}"


def legal_reviewed(lang: str) -> bool:
    """§8: translated legal texts need a recorded legal review of the current versions; until then English shows."""
    if lang == "en":
        return True
    from apps.accounts.models import LanguageReview

    return LanguageReview.objects.filter(language=lang, scope=LanguageReview.Scope.LEGAL, catalogue_version=legal_version()).exists()


def legal_payload(data, lang: str):
    """A legal text in `lang` when it is translated and legally reviewed, else English, with a note for the page."""
    if lang != "en" and legal_reviewed(lang):
        out, complete = translate_payload(data, lang)
        if complete:
            return out, {"language": lang, "complete": True, "authoritative": "en"}
    return data, {"language": "en", "complete": lang == "en", "requested": lang, "authoritative": "en"}


# ---------------------------------------------------------------- .po files
@dataclass
class Entry:
    msgid: str
    msgstr: str = ""
    comments: list = field(default_factory=list)  # "#." lines (context for translators)
    status: str = ""  # translated | reviewed | machine-draft


def locale_dir() -> Path:
    return Path(settings.LOCALE_PATHS[0])


def po_path(lang: str) -> Path:
    return locale_dir() / lang / "LC_MESSAGES" / "django.po"


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")


def _unesc(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(n, n))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def read_po(path: Path) -> dict[str, Entry]:
    entries: dict[str, Entry] = {}
    if not path.exists():
        return entries
    cur, key, comments, status = None, None, [], ""
    for raw in path.read_text(encoding="utf-8").splitlines() + [""]:
        line = raw.strip()
        if not line:
            if cur is not None and cur.msgid:
                entries[cur.msgid] = cur
            cur, key, comments, status = None, None, [], ""
            continue
        if line.startswith("#."):
            comments.append(line[2:].strip())
        elif line.startswith("#,"):
            status = next((f.strip() for f in line[2:].split(",") if f.strip() in ("translated", "reviewed", "machine-draft")), status)
        elif line.startswith("#"):
            continue
        elif line.startswith("msgid "):
            cur = Entry("", comments=comments, status=status)
            key = "msgid"
            cur.msgid = _unesc(line[6:].strip()[1:-1])
        elif line.startswith("msgstr "):
            key = "msgstr"
            cur.msgstr = _unesc(line[7:].strip()[1:-1])
        elif line.startswith('"') and cur is not None:
            setattr(cur, key, getattr(cur, key) + _unesc(line[1:-1]))
    return entries


def write_po(path: Path, lang: str, entries: list[Entry]):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# NDCConnect backend messages (spec 21). msgid is the English text; English is authoritative.",
             'msgid ""', 'msgstr ""', f'"Language: {lang}\\n"', '"MIME-Version: 1.0\\n"',
             '"Content-Type: text/plain; charset=UTF-8\\n"', '"Content-Transfer-Encoding: 8bit\\n"', ""]
    for e in sorted(entries, key=lambda e: e.msgid):
        lines += [f"#. {c}" for c in e.comments]
        if e.status:
            lines.append(f"#, {e.status}")
        lines += [f'msgid "{_esc(e.msgid)}"', f'msgstr "{_esc(e.msgstr)}"', ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def mo_bytes(entries: dict[str, Entry], lang: str) -> bytes:
    """GNU .mo (little-endian), as Tools/i18n/msgfmt.py writes it. Only translated entries; the header first."""
    header = f"Language: {lang}\nMIME-Version: 1.0\nContent-Type: text/plain; charset=UTF-8\nContent-Transfer-Encoding: 8bit\n"
    pairs = {"": header} | {e.msgid: e.msgstr for e in entries.values() if e.msgstr}
    keys = sorted(pairs)
    ids = b""
    strs = b""
    offsets = []
    for k in keys:
        kb, vb = k.encode("utf-8"), pairs[k].encode("utf-8")
        offsets.append((len(ids), len(kb), len(strs), len(vb)))
        ids += kb + b"\0"
        strs += vb + b"\0"
    n = len(keys)
    keystart = 7 * 4 + 16 * n
    valuestart = keystart + len(ids)
    koffsets, voffsets = [], []
    for o1, l1, o2, l2 in offsets:
        koffsets += [l1, o1 + keystart]
        voffsets += [l2, o2 + valuestart]
    out = struct.pack("Iiiiiii", 0x950412DE, 0, n, 7 * 4, 7 * 4 + n * 8, 0, 0)
    out += struct.pack(f"{len(koffsets)}i", *koffsets) + struct.pack(f"{len(voffsets)}i", *voffsets)
    return out + ids + strs


def compile_language(lang: str) -> Path:
    po = po_path(lang)
    mo = po.with_suffix(".mo")
    mo.write_bytes(mo_bytes(read_po(po), lang))
    return mo


# ---------------------------------------------------------------- what the backend says (msgids)
ERROR_CALLS = {"ServiceError", "_err", "_bad", "_conflict", "_fail", "deny", "ValidationError", "PermissionDenied", "NotFound",
               "NotAuthenticated", "_denied", "_invalid", "Throttled"}
FIXED = [  # the envelope's own words (core/exceptions.py) and DRF's most common field errors
    "Something went wrong on our side.", "Please correct the highlighted fields.", "Sign in to continue.",
    "The request could not be completed.", "This field is required.", "This field may not be blank.", "This field may not be null.",
    "A valid integer is required.", "Enter a valid email address.", "Not a valid string.", "Must be a valid boolean.",
    "Date has wrong format. Use one of these formats instead: YYYY-MM-DD.", "Invalid pk \"{pk_value}\" - object does not exist.",
    "Not found.", "You do not have permission to perform this action.", "Authentication credentials were not provided.",
]


def _strings_in(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
    elif isinstance(node, (ast.List, ast.Tuple)):
        for n in node.elts:
            yield from _strings_in(n)
    elif isinstance(node, ast.Dict):
        for n in node.values:
            yield from _strings_in(n)


@functools.lru_cache(maxsize=1)
def code_msgids() -> dict[str, str]:
    """{English message: where} from error raises and `errors[...] = [...]` assignments in the apps."""
    base = Path(settings.BASE_DIR) / "apps"
    found: dict[str, str] = {}
    for py in sorted(base.rglob("*.py")):
        rel = py.relative_to(base.parent).as_posix()
        if "/tests" in rel or "/migrations/" in rel or "/management/" in rel:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            texts = []
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                if name in ERROR_CALLS:
                    for a in node.args[:1]:
                        texts += list(_strings_in(a))
                    for kw in node.keywords:
                        if kw.arg in ("message", "detail"):
                            texts += list(_strings_in(kw.value))
            elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Subscript) and getattr(t.value, "id", "") in ("errors", "errs")
                                                        for t in node.targets):
                texts += list(_strings_in(node.value))
            for t in texts:
                if is_prose(t) or (t and t[0].isupper() and len(t) > 3):
                    found.setdefault(t, f"{rel}:{node.lineno}")
    return found


def payload_msgids() -> dict[str, str]:
    from apps.moderation import rules
    from apps.privacy import register

    out = {}
    for where, data in (("rules", rules.public_payload()), ("privacy notice", register.public_payload())):
        for s in walk_strings(data):
            out.setdefault(s, where)
    return out


def all_msgids() -> dict[str, str]:
    return {s: "core/exceptions.py" for s in FIXED} | code_msgids() | payload_msgids()


def stats(lang: str) -> dict:
    wanted = all_msgids()
    have = read_po(po_path(lang)) if lang != "en" else {}
    done = sum(1 for m in wanted if have.get(m) and have[m].msgstr)
    reviewed = sum(1 for m in wanted if have.get(m) and have[m].msgstr and have[m].status == "reviewed")
    total = len(wanted)
    return {"total": total, "translated": total if lang == "en" else done, "reviewed": total if lang == "en" else reviewed,
            "percent": 100 if lang == "en" or total == 0 else int(done * 100 / total)}


# ---------------------------------------------------------------- NFC on save (§11)
NFC_FIELDS = {
    "news.Article": ("title", "body"), "news.ArticleTranslation": ("title", "body"), "posts.CandidatePost": ("body",),
    "communities.CommunityPost": ("body",), "communities.CommunityComment": ("body",), "events.Event": ("title", "description"),
    "candidates.Candidate": ("biography",), "accounts.UserPreferences": ("bio",),
}


def nfc(value):
    return unicodedata.normalize("NFC", value) if isinstance(value, str) else value


def normalise_instance(sender, instance, **kwargs):
    for f in NFC_FIELDS.get(sender._meta.label, ()):
        setattr(instance, f, nfc(getattr(instance, f)))


def connect_signals():
    from django.apps import apps
    from django.db.models.signals import pre_save

    for label in NFC_FIELDS:
        try:
            model = apps.get_model(label)
        except LookupError:
            continue
        pre_save.connect(normalise_instance, sender=model, dispatch_uid=f"nfc:{label}")
