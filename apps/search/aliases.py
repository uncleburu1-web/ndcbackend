"""
Words people use for offices and places (spec 12 §6.5). Applied at index time: a governorship
candidate's document carries "governor guber gov", so "guber kano" just works. No name list:
spelling variants of names are left to trigram similarity, so nobody's name is "corrected"
into someone else's.
"""
from apps.candidates.reference import Position, State

OFFICE_WORDS = {
    Position.PRESIDENT: "president presidential",
    Position.GOVERNOR: "governor guber gov governorship",
    Position.SENATOR: "senator senate sen",
    Position.HOUSE_OF_REPS: "house of representatives reps house of reps hor representative",
    Position.STATE_ASSEMBLY: "state house of assembly sha assembly state house",
    Position.LGA_CHAIRMAN: "lga chairman chairman local government",
    Position.COUNCILLOR: "councillor ward",
}

PLACE_WORDS = {
    State.FCT: "abuja fct federal capital territory",
    "akwa_ibom": "akwa ibom akwaibom",
    "nasarawa": "nasarawa nassarawa",
    "cross_river": "cross river crossriver",
}


def office_words(position: str) -> str:
    try:
        return f"{Position(position).label} {OFFICE_WORDS.get(Position(position), '')}"
    except ValueError:
        return ""


def place_words(state: str) -> str:
    if not state:
        return "nigeria national"
    try:
        label = State(state).label
    except ValueError:
        return ""
    return f"{label} {PLACE_WORDS.get(state, '')}"
