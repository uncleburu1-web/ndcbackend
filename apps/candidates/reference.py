"""
Reference data — spec 03 §4 and §5.2.

Positions are in display order; each declares the area it needs.
The FCT has no Governor and no State House of Assembly, and its local
governments are Area Councils, so those rules are encoded here too.
"""
from django.db import models


class Position(models.TextChoices):
    PRESIDENT = "president", "President"
    GOVERNOR = "governor", "Governor"
    SENATOR = "senator", "Senator"
    HOUSE_OF_REPS = "house_of_reps", "Member, House of Representatives"
    STATE_ASSEMBLY = "state_assembly", "Member, State House of Assembly"
    LGA_CHAIRMAN = "lga_chairman", "LGA Chairman"
    COUNCILLOR = "councillor", "Councillor"


# Display / sort order (§4 column "#").
POSITION_ORDER = [p.value for p in Position]

# Which area fields each position requires (§4 column "Area required").
#   state: needs a state · area: needs area_name · ward: needs ward_name
AREA_RULES = {
    Position.PRESIDENT: {"state": False, "area": False, "ward": False},
    Position.GOVERNOR: {"state": True, "area": False, "ward": False},
    Position.SENATOR: {"state": True, "area": True, "ward": False},
    Position.HOUSE_OF_REPS: {"state": True, "area": True, "ward": False},
    Position.STATE_ASSEMBLY: {"state": True, "area": True, "ward": False},
    Position.LGA_CHAIRMAN: {"state": True, "area": True, "ward": False},
    Position.COUNCILLOR: {"state": True, "area": True, "ward": True},
}

AREA_LABELS = {
    Position.SENATOR: "Senatorial district",
    Position.HOUSE_OF_REPS: "Federal constituency",
    Position.STATE_ASSEMBLY: "State constituency",
    Position.LGA_CHAIRMAN: "Local government area",
    Position.COUNCILLOR: "Local government area",
}

# Offices that don't exist in the Federal Capital Territory.
NOT_IN_FCT = {Position.GOVERNOR, Position.STATE_ASSEMBLY}


class State(models.TextChoices):
    ABIA = "abia", "Abia"
    ADAMAWA = "adamawa", "Adamawa"
    AKWA_IBOM = "akwa_ibom", "Akwa Ibom"
    ANAMBRA = "anambra", "Anambra"
    BAUCHI = "bauchi", "Bauchi"
    BAYELSA = "bayelsa", "Bayelsa"
    BENUE = "benue", "Benue"
    BORNO = "borno", "Borno"
    CROSS_RIVER = "cross_river", "Cross River"
    DELTA = "delta", "Delta"
    EBONYI = "ebonyi", "Ebonyi"
    EDO = "edo", "Edo"
    EKITI = "ekiti", "Ekiti"
    ENUGU = "enugu", "Enugu"
    GOMBE = "gombe", "Gombe"
    IMO = "imo", "Imo"
    JIGAWA = "jigawa", "Jigawa"
    KADUNA = "kaduna", "Kaduna"
    KANO = "kano", "Kano"
    KATSINA = "katsina", "Katsina"
    KEBBI = "kebbi", "Kebbi"
    KOGI = "kogi", "Kogi"
    KWARA = "kwara", "Kwara"
    LAGOS = "lagos", "Lagos"
    NASARAWA = "nasarawa", "Nasarawa"
    NIGER = "niger", "Niger"
    OGUN = "ogun", "Ogun"
    ONDO = "ondo", "Ondo"
    OSUN = "osun", "Osun"
    OYO = "oyo", "Oyo"
    PLATEAU = "plateau", "Plateau"
    RIVERS = "rivers", "Rivers"
    SOKOTO = "sokoto", "Sokoto"
    TARABA = "taraba", "Taraba"
    YOBE = "yobe", "Yobe"
    ZAMFARA = "zamfara", "Zamfara"
    FCT = "fct", "Federal Capital Territory"


def state_display(state: str | None) -> str:
    if not state:
        return ""
    if state == State.FCT:
        return State.FCT.label
    return f"{State(state).label} State"


def _with_suffix(name: str, suffix: str) -> str:
    """'Nassarawa' → 'Nassarawa LGA'; leaves 'Nassarawa LGA' alone."""
    return name if name.lower().endswith(suffix.lower()) else f"{name} {suffix}"


def area_display(position: str, state: str | None, area_name: str = "", ward_name: str = "") -> str:
    """Human-readable area, built server-side (§5.2)."""
    if position == Position.PRESIDENT:
        return "Nigeria"
    where = state_display(state)
    if position == Position.GOVERNOR:
        return where
    if position in (Position.LGA_CHAIRMAN, Position.COUNCILLOR):
        local = _with_suffix(area_name, "Area Council" if state == State.FCT else "LGA")
        parts = [ward_name, local, where] if position == Position.COUNCILLOR else [local, where]
        return ", ".join(p for p in parts if p)
    return ", ".join(p for p in [area_name, where] if p)
