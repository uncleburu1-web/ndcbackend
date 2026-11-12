"""
DRF authentication with Clerk session tokens — spec §6.1.

- Bearer token only (never Clerk's cookie — see clerk._HeadersOnly).
- Invalid / expired / missing / wrong-type token → anonymous; protected endpoints then 401.
- Verification impossible (keys can't load, Clerk not configured) → 503 auth_unavailable.
- Deactivated local user → 403 account_deactivated, even with a valid Clerk token.
- Spec 20 §12: a person who paused their own account → 403 account_paused, except on the few endpoints that let them
  come back, download their data or delete the account.
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


class AccountPaused(exceptions.APIException):
    status_code = status.HTTP_403_FORBIDDEN
    default_code = "account_paused"
    default_detail = "You paused your account. Reactivate it to use NDCConnect again."


# url names a paused person can still use (spec 20 §12)
PAUSED_ALLOWED = {"me", "my-reactivate", "my-data-export", "my-delete"}


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
            if user.deactivation_kind != "self":
                raise AccountDeactivated()
            match = getattr(request._request, "resolver_match", None)
            if match is None or match.url_name not in PAUSED_ALLOWED:
                raise AccountPaused()
        from .language import attach_account_language

        attach_account_language(request, user)
        from apps.access.security import enforce

        enforce(request, user, session.claims)  # spec 14: admin two-step verification and reverification
        return (user, session)

    def authenticate_header(self, request):
        # Makes DRF answer 401 (not 403) when credentials are missing or invalid.
        return 'Bearer realm="api"'
