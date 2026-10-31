"""
DRF authentication with Clerk session tokens — spec §6.1.

- Bearer token only (never Clerk's cookie — see clerk._HeadersOnly).
- Invalid / expired / missing / wrong-type token → anonymous; protected endpoints then 401.
- Verification impossible (keys can't load, Clerk not configured) → 503 auth_unavailable.
- Deactivated local user → 403 account_deactivated, even with a valid Clerk token.
"""
from rest_framework import exceptions, status
from rest_framework.authentication import BaseAuthentication

from . import clerk
from .services import get_or_create_for_session


class AuthUnavailable(exceptions.APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_code = "auth_unavailable"
    default_detail = "Sign-in is temporarily unavailable. Try again shortly."


class AccountDeactivated(exceptions.APIException):
    status_code = status.HTTP_403_FORBIDDEN
    default_code = "account_deactivated"
    default_detail = "This account has been deactivated. Contact NDCConnect support if you think this is a mistake."


class ClerkSessionAuthentication(BaseAuthentication):
    def authenticate(self, request):
        header = request.META.get("HTTP_AUTHORIZATION")
        if not header:
            return None
        try:
            session = clerk.verify_session_token(header)
        except clerk.ClerkUnavailable as exc:
            raise AuthUnavailable() from exc
        if session is None:
            return None  # anonymous; IsSignedIn turns this into 401 where required

        user = get_or_create_for_session(session.clerk_user_id)
        if not user.is_active:
            raise AccountDeactivated()
        return (user, session)

    def authenticate_header(self, request):
        # Makes DRF answer 401 (not 403) when credentials are missing or invalid.
        return 'Bearer realm="api"'
