"""Fixed reference facts shown on state pages (spec 19 §11.1, §23.11: capitals only)."""
from django.utils.text import slugify

CAPITALS = {
    "abia": "Umuahia", "adamawa": "Yola", "akwa_ibom": "Uyo", "anambra": "Awka", "bauchi": "Bauchi", "bayelsa": "Yenagoa",
    "benue": "Makurdi", "borno": "Maiduguri", "cross_river": "Calabar", "delta": "Asaba", "ebonyi": "Abakaliki",
    "edo": "Benin City", "ekiti": "Ado-Ekiti", "enugu": "Enugu", "gombe": "Gombe", "imo": "Owerri", "jigawa": "Dutse",
    "kaduna": "Kaduna", "kano": "Kano", "katsina": "Katsina", "kebbi": "Birnin Kebbi", "kogi": "Lokoja", "kwara": "Ilorin",
    "lagos": "Ikeja", "nasarawa": "Lafia", "niger": "Minna", "ogun": "Abeokuta", "ondo": "Akure", "osun": "Osogbo",
    "oyo": "Ibadan", "plateau": "Jos", "rivers": "Port Harcourt", "sokoto": "Sokoto", "taraba": "Jalingo", "yobe": "Damaturu",
    "zamfara": "Gusau", "fct": "Abuja",
}


def lga_slug(name: str, code: str, taken: set) -> str:
    """URL slug for an LGA, unique within its state; the INEC code breaks a tie between two similar names."""
    base = slugify(name)[:80] or f"lga-{code}"
    s = base if base not in taken else f"{base}-{code}"
    taken.add(s)
    return s
