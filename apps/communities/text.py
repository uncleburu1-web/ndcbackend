"""Plain-text checks for community posts and comments (spec 17 §7, §16)."""
import hashlib
import re

_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁩﻿]")
_RUN = re.compile(r"(.)\1{8,}", re.S)
_LINK = re.compile(r"\bhttps?://", re.I)
_BAD_SCHEME = re.compile(r"\b(?:javascript|data|vbscript|file):", re.I)


def clean(body: str) -> str:
    """Strip zero-width and bidirectional control characters; collapse runs of more than 8 identical characters."""
    text = _INVISIBLE.sub("", str(body or "")).replace("\r\n", "\n").replace("\r", "\n")
    text = _RUN.sub(lambda m: m.group(1) * 8, text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def link_count(text: str) -> int:
    return len(_LINK.findall(text))


def has_bad_link(text: str) -> bool:
    return bool(_BAD_SCHEME.search(text))


def body_hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()


def short_name(user) -> str:
    """First name and last initial: "Amaka E." (spec 17 §6). Deleted or missing accounts are "Former member"."""
    if user is None or not user.is_active or getattr(user, "clerk_deleted_at", None):
        return "Former member"
    parts = (user.full_name or "").split()
    if not parts:
        return "A member"
    return parts[0] if len(parts) == 1 else f"{parts[0]} {parts[-1][0].upper()}."
