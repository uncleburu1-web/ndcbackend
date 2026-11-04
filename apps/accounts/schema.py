"""OpenAPI description of Clerk session auth (spec 15 §14.1): `Authorization: Bearer <Clerk session token>`."""
from drf_spectacular.extensions import OpenApiAuthenticationExtension


class ClerkSessionScheme(OpenApiAuthenticationExtension):
    target_class = "apps.accounts.authentication.ClerkSessionAuthentication"
    name = "bearerAuth"

    def get_security_definition(self, auto_schema):
        return {"type": "http", "scheme": "bearer", "bearerFormat": "Clerk session JWT",
                "description": "A Clerk session token. Django verifies signature, expiry and authorised party itself."}
