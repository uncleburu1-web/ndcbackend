"""
Community rules (spec 13 §4.1, §5.2). Code, not database: every change is reviewed and versioned.
Every removal cites one of these (or LEGAL for a removal on a legal notice). Shipped marked
"Draft — under legal review" until the party signs the wording off (spec 13 §15.1).
"""
from dataclasses import asdict, dataclass, field
from datetime import date


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    summary: str
    examples: tuple = field(default_factory=tuple)
    law: str = ""


RULES_VERSION = 3
RULES_UPDATED = date(2026, 11, 5)
RULES_DRAFT = True  # spec 13 §15.1: until the wording is signed off
CHANGES = [(date(2026, 11, 2), "First published: rules R1–R10."),
           (date(2026, 11, 5), "R10 now covers communities: keep to politics and public life in that area."),
           (date(2026, 11, 5), "New R11 for events: no inducements, no campaigning at places of worship, police stations or public "
                               "offices, and no misstating who organises an event.")]

RULES = [
    Rule("R1", "No threats or incitement to violence",
         "Don't threaten anyone, call for violence, or praise violence against people or groups.",
         ("Calling on people to attack supporters of another party", "Threatening an official or a voter"),
         "Electoral Act 2026 s. 96; Cybercrimes (Amendment) Act 2024 s. 24"),
    Rule("R2", "No abusive or hateful language",
         "Don't use language that insults or stirs hatred because of religion, ethnicity, tribe or section.",
         ("Slurs about an ethnic group", "Mocking someone's religion to provoke anger"),
         "Electoral Act 2026 s. 96"),
    Rule("R3", "Election results only from INEC",
         "Only share results announced by INEC. Never present unofficial or made-up figures as results.",
         ("Posting a 'final tally' before INEC announces it", "Edited result sheets"),
         "Electoral Act 2026 s. 124(4)"),
    Rule("R4", "No knowingly false or misleading information",
         "Don't post claims you know are false, or present rumours as facts.",
         ("A fake quote from a candidate", "Old photos presented as today's events"),
         "Cybercrimes (Amendment) Act 2024 s. 24; NITDA Code of Practice Part V"),
    Rule("R5", "No one's private information without consent",
         "Don't share someone's phone number, address, ID numbers or other private details without their consent.",
         ("Posting a voter's phone number", "Sharing someone's home address"),
         "Nigeria Data Protection Act 2023"),
    Rule("R6", "No intimate images shared without consent",
         "Never share intimate or sexual images of someone without their consent, including fakes.",
         ("Leaked private photos", "Edited or AI-made intimate images of a real person"),
         "NITDA Code of Practice Part I"),
    Rule("R7", "Respect copyright",
         "Only post words, photos and videos you made or have the right to share.",
         ("A news photographer's picture used without permission",),
         "Copyright Act 2022 s. 54"),
    Rule("R8", "No spam, scams or advertising",
         "Don't post adverts, chain messages, fake giveaways or requests for money.",
         ("'Send ₦5,000 to get a job'", "Repeated promotional posts"),
         "Advance Fee Fraud and Other Fraud Related Offences Act 2006; Cybercrimes Act"),
    Rule("R9", "No impersonation",
         "Don't pretend to be another person, a candidate, the party or an official body.",
         ("Posting as if you were INEC", "A fake statement 'from' a candidate"),
         "Cybercrimes Act"),
    Rule("R10", "Stay on topic and follow the news and community rules",
         "Community news must be real, local, sourced and written by you (see the news rules). "
         "In communities, keep to politics and public life in that area.",
         ("Opinion pieces presented as news", "Copied articles"),
         ""),
    Rule("R11", "Lawful events and campaigning",
         "Events can't offer money, gifts or goods for attending or voting (a \"transport refund\" counts), can't be held at places "
         "of worship, police stations or public offices, and must say truthfully who organises them.",
         ("\"₦2,000 for everyone who comes\"", "A campaign rally at a church or mosque", "An event claiming to be the party's when it isn't"),
         "Electoral Act 2026 ss. 22, 96"),
]
RULE_IDS = [r.id for r in RULES] + ["LEGAL"]
LEGAL_TITLE = "Removed after a legal notice"


def rule(rule_id: str) -> Rule | None:
    return next((r for r in RULES if r.id == rule_id), None)


def rule_title(rule_id: str) -> str:
    if rule_id == "LEGAL":
        return LEGAL_TITLE
    r = rule(rule_id)
    return r.title if r else ""


# Report reasons (spec 13 §4.2): reason → (label, rule, priority). "copyright" routes to the legal form.
REASONS = {
    "violence": ("Threat or incitement to violence", "R1", "urgent"),
    "abuse": ("Abusive or hateful", "R2", "urgent"),
    "results": ("Election results not from INEC", "R3", "urgent"),
    "false": ("False or misleading", "R4", "high"),
    "privacy": ("Private information about someone", "R5", "urgent"),
    "ncii": ("Intimate image shared without consent", "R6", "urgent"),
    "copyright": ("Copyright (my content was used)", "R7", "route"),
    "spam": ("Spam or advertising", "R8", "low"),
    "impersonation": ("Impersonation", "R9", "high"),
    "other": ("Something else", "", "normal"),
}
# Spec 18 §8: events take 13's reasons plus two of their own (R11).
EVENT_REASONS = {**{k: v for k, v in REASONS.items()},
                 "inducement": ("Offers money or gifts to attend or vote", "R11", "high"),
                 "banned_venue": ("At a place of worship, police station or public office", "R11", "high")}
PROFILE_REASONS = {
    "profile_wrong": ("Wrong or out-of-date details", "", "normal"),
    "not_ndc": ("Not an NDC candidate", "", "normal"),
    "impersonation": ("Impersonation", "R9", "normal"),
    "other": ("Something else", "", "normal"),
}
ELECTION_URGENT = {"results", "violence", "inducement", "banned_venue"}  # spec 18 §8: R1 and R11 too  # always urgent in the election period (spec 13 §3.8)
LEGAL_DEADLINE_REASONS = {"ncii"}  # NITDA Code Part I: 48 hours

PRIORITY_RANK = {"legal": 0, "urgent": 1, "high": 2, "normal": 3, "low": 4}
PRIORITY_LABEL = {"legal": "Legal", "urgent": "Urgent", "high": "High", "normal": "Normal", "low": "Low"}
TARGET_HOURS = {"urgent": 24, "high": 48, "normal": 72, "low": 168, "legal": 48}


def reason_label(reason: str, kind: str = "") -> str:
    table = PROFILE_REASONS if kind == "candidate" else EVENT_REASONS if kind == "event" else REASONS
    return table.get(reason, EVENT_REASONS.get(reason, ("Something else",)))[0]


def public_payload() -> dict:
    return {
        "version": RULES_VERSION, "updated": RULES_UPDATED.isoformat(), "draft": RULES_DRAFT,
        "rules": [asdict(r) | {"examples": list(r.examples)} for r in RULES],
        "changes": [{"date": d.isoformat(), "text": t} for d, t in CHANGES],
        "reasons": [{"reason": k, "label": v[0], "rule": v[1]} for k, v in REASONS.items()],
        "event_reasons": [{"reason": k, "label": v[0], "rule": v[1]} for k, v in EVENT_REASONS.items()],
        "profile_reasons": [{"reason": k, "label": v[0]} for k, v in PROFILE_REASONS.items()],
    }
