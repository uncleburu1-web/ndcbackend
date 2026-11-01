"""
How a PVC is checked — spec 06 §6.2.

INEC's public voter checker (cvr.inecnigeria.org/vvs) has no API and a security check,
so the default is a person: a membership officer looks the voter up and records the result.
NDCConnect never scrapes INEC. A licensed KYC provider could be added as another checker
later without changing statuses or screens.
"""
from django.conf import settings


class PvcChecker:
    name = ""
    automatic = False

    def officer_lookup_url(self) -> str:  # pragma: no cover - interface
        return ""


class ManualChecker(PvcChecker):
    name = "manual"
    automatic = False

    def officer_lookup_url(self) -> str:
        return settings.INEC_VOTER_CHECK_URL


def get_checker() -> PvcChecker:
    return ManualChecker()
