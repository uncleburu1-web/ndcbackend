"""
The only module that talks to Clerk. Everything else goes through these functions,
so tests can replace Clerk at one boundary (spec §12).
"""
import logging
from dataclasses import dataclass
from typing import Optional

from clerk_backend_api import Clerk
from clerk_backend_api.security import AuthenticateRequestOptions, authenticate_request
from clerk_backend_api.security.types import AuthStatus, TokenVerificationErrorReason
from django.conf import settings

logger = logging.getLogger(__name__)

# Reasons that mean "we could not check the token", not "the token is bad".
# These must never fall back to treating the caller as signed in (spec §6.1, §11).
_UNAVAILABLE = {
    TokenVerificationErrorReason.JWK_FAILED_TO_LOAD,
    TokenVerificationErrorReason.JWK_REMOTE_INVALID,
    TokenVerificationErrorReason.JWK_FAILED_TO_RESOLVE,
    TokenVerificationErrorReason.SECRET_KEY_MISSING,
}


class ClerkUnavailable(Exception):
    """Clerk's keys could not be loaded, or Clerk isn't configured."""


@dataclass
class VerifiedSession:
    clerk_user_id: str
    claims: dict


class _HeadersOnly:
    """
    Expose ONLY the Authorization header to the SDK. The SDK would otherwise also
    accept Clerk's __session cookie, which a browser sends automatically — that would
    make state-changing endpoints CSRF-able. NDCConnect authenticates by Bearer token only.
    """

    def __init__(self, authorization: Optional[str]):
        self.headers = {"Authorization": authorization} if authorization else {}


def verify_session_token(authorization_header: Optional[str]) -> Optional[VerifiedSession]:
    """
    Returns the verified session, None if the token is missing/invalid/expired/wrong type,
    or raises ClerkUnavailable when verification itself can't be performed.
    """
    if not authorization_header or not authorization_header.startswith("Bearer "):
        return None

    secret = settings.CLERK_SECRET_KEY or None
    jwt_key = settings.CLERK_JWT_KEY or None
    if not secret and not jwt_key:
        raise ClerkUnavailable("Clerk is not configured (CLERK_SECRET_KEY / CLERK_JWT_KEY).")

    state = authenticate_request(
        _HeadersOnly(authorization_header),
        AuthenticateRequestOptions(
            secret_key=secret,
            jwt_key=jwt_key,
            authorized_parties=list(settings.CLERK_AUTHORIZED_PARTIES),  # tokens for other sites are rejected
            accepts_token=["session_token"],  # session tokens only
        ),
    )

    if state.status == AuthStatus.SIGNED_IN and state.payload and state.payload.get("sub"):
        return VerifiedSession(clerk_user_id=state.payload["sub"], claims=state.payload)

    if state.reason in _UNAVAILABLE:
        logger.error("Clerk token verification unavailable: %s", getattr(state.reason, "value", state.reason))
        raise ClerkUnavailable(str(state.reason))
    return None


# --------------------------------------------------------------------------
# Backend API (needs CLERK_SECRET_KEY)
# --------------------------------------------------------------------------
CLERK_TIMEOUT_MS = 8000  # spec 15 §12.2: 3 s connect + 5 s read, as one SDK budget


def _client() -> Clerk:
    if not settings.CLERK_SECRET_KEY:
        raise ClerkUnavailable("CLERK_SECRET_KEY is not set.")
    return Clerk(bearer_auth=settings.CLERK_SECRET_KEY, timeout_ms=CLERK_TIMEOUT_MS)


def _guarded(operation):
    from apps.integrations.http import guarded

    return guarded("clerk", operation)


def fetch_user(clerk_user_id: str) -> Optional[dict]:
    """Best-effort fetch of a Clerk user as the webhook-shaped dict, or None on any failure."""
    try:
        client = _client()  # not configured is not a Clerk failure: checked before the breaker
        with _guarded("users.get"), client as clerk:
            user = clerk.users.get(user_id=clerk_user_id)
    except ClerkUnavailable:
        logger.info("Skipping Clerk user fetch for %s: CLERK_SECRET_KEY not set (webhook will fill details).", clerk_user_id)
        return None
    except Exception:
        logger.warning("Could not fetch Clerk user %s", clerk_user_id, exc_info=True)
        return None
    return {
        "id": user.id,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "primary_email_address_id": user.primary_email_address_id,
        "email_addresses": [{"id": e.id, "email_address": e.email_address} for e in (user.email_addresses or [])],
    }


def set_banned(clerk_user_id: str, banned: bool) -> None:
    """Raises on failure. Used directly by the outbox handlers (spec 15 §9.3)."""
    client = _client()
    with _guarded("users.ban" if banned else "users.unban"), client as clerk:
        (clerk.users.ban if banned else clerk.users.unban)(user_id=clerk_user_id)


def _queue(kind, clerk_user_id):
    from datetime import timedelta

    from apps.core import outbox

    outbox.enqueue(kind, {"clerk_user_id": clerk_user_id}, dedupe_key=f"{kind}:{clerk_user_id}", delay=timedelta(minutes=1))


def ban_user(clerk_user_id: str) -> bool:
    """Ban in Clerk so the person can't sign in at all. On failure the ban is queued in the outbox and retried
    (spec 15 §12.3); the local deactivation already holds. Returns False when it had to be queued."""
    try:
        set_banned(clerk_user_id, True)
        return True
    except Exception:
        logger.error("Clerk ban failed for %s — local deactivation holds; queued for retry.", clerk_user_id, exc_info=True)
        _queue("clerk_ban", clerk_user_id)
        return False


def unban_user(clerk_user_id: str) -> bool:
    try:
        set_banned(clerk_user_id, False)
        return True
    except Exception:
        logger.error("Clerk unban failed for %s — queued for retry.", clerk_user_id, exc_info=True)
        _queue("clerk_unban", clerk_user_id)
        return False


def delete_user(clerk_user_id: str) -> bool:
    """Spec 14 §6.3: self-service deletion. Returns False on failure (logged); the caller erases nothing then."""
    try:
        client = _client()
        with _guarded("users.delete"), client as clerk:
            clerk.users.delete(user_id=clerk_user_id)
        return True
    except Exception:
        logger.error("Clerk delete failed for %s", clerk_user_id, exc_info=True)
        return False
