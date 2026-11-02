"""
Folding (spec 12 §6.2). The same function runs on stored text and on every query, so "Ọ̀ṣun",
"OSUN" and "osun" all match, with no database extension needed for accents.
"""
import re
import unicodedata

_SPACE = re.compile(r"\s+")


def _fold_char(ch: str) -> str:
    if ch in "'’‘`-–—_/":
        return " "
    if ch == "ß":
        return "ss"
    out = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
    return out.lower()


def fold(text: str) -> str:
    return _SPACE.sub(" ", "".join(_fold_char(c) for c in (text or ""))).strip()


def fold_with_map(text: str) -> tuple[str, list[int]]:
    """Folded text (no whitespace collapsing) plus, for each folded character, its index in `text`."""
    out, idx = [], []
    for i, ch in enumerate(text or ""):
        f = _fold_char(ch)
        out.append(f)
        idx.extend([i] * len(f))
    return "".join(out), idx


def tokens(q: str) -> tuple[list[str], list[str]]:
    """(wanted, excluded) alphanumeric tokens from a query; "-word" excludes. Exclusions are read before
    folding, because folding turns hyphens into spaces."""
    want, drop = [], []
    for raw in (q or "").replace('"', " ").split():
        negative = raw.startswith("-") and len(raw) > 1
        for part in fold(raw[1:] if negative else raw).split():
            t = re.sub(r"[^a-z0-9]", "", part)
            if t:
                (drop if negative else want).append(t)
    return want[:12], drop[:5]


_VOWELS = set("aeiouy")


def sound_key(word: str) -> str:
    """Consonant skeleton of one folded word: keep the first letter, drop later vowels, collapse repeats.
    "mohammed" and "muhammad" → "mhmd"; "ahmed" and "ahmad" → "ahmd". An algorithm, not a list of names
    (spec 12 §6.5), so spelling variants match without "correcting" anyone's name."""
    w = re.sub(r"[^a-z]", "", word)
    if not w:
        return ""
    out = [w[0]]
    for ch in w[1:]:
        if ch in _VOWELS:
            continue
        if ch != out[-1]:
            out.append(ch)
    return "".join(out)


def title_key(folded_title: str) -> str:
    return " ".join(k for k in (sound_key(w) for w in folded_title.split()) if k)
