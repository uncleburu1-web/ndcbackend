"""
Test doubles for Clerk at the SDK boundary (spec §12).

Tokens are REAL RS256 JWTs signed with a key generated per test run; the backend
verifies them with the real Clerk SDK (networkless `jwt_key` mode). Webhooks are
signed with the real Svix library. Nothing in the verification path is faked.
"""
import json
import time
import uuid
from datetime import datetime, timezone

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings
from svix.webhooks import Webhook


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    public_pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private_pem, public_pem


PRIVATE_PEM, PUBLIC_PEM = _keypair()
OTHER_PRIVATE_PEM, _ = _keypair()  # a key Clerk never published
FRONTEND = "http://localhost:5173"


def session_token(sub="user_test123", *, azp=FRONTEND, expires_in=300, key=PRIVATE_PEM, **extra):
    now = int(time.time())
    claims = {"sub": sub, "azp": azp, "iat": now - 5, "nbf": now - 5, "exp": now + expires_in,
              "sid": "sess_" + uuid.uuid4().hex[:12], "iss": "https://clerk.ndcconnect.test", **extra}
    if azp is None:
        claims.pop("azp")
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "ins_test"})


def bearer(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def clerk_user(clerk_id="user_test123", email="ada@example.com", first="Ada", last="Obi"):
    return {
        "id": clerk_id,
        "first_name": first,
        "last_name": last,
        "primary_email_address_id": "idn_1",
        "email_addresses": [{"id": "idn_0", "email_address": "old@example.com"}, {"id": "idn_1", "email_address": email}],
        "public_metadata": {},
    }


def signed_webhook(event_type, data, *, msg_id=None, secret=None, when=None):
    """Return (body_bytes, headers) exactly as Svix would deliver them."""
    msg_id = msg_id or "msg_" + uuid.uuid4().hex
    when = when or datetime.now(tz=timezone.utc)
    body = json.dumps({"type": event_type, "object": "event", "data": data})
    signature = Webhook(secret or settings.CLERK_WEBHOOK_SIGNING_SECRET).sign(msg_id, when, body)
    headers = {
        "HTTP_SVIX_ID": msg_id,
        "HTTP_SVIX_TIMESTAMP": str(int(when.timestamp())),
        "HTTP_SVIX_SIGNATURE": signature,
    }
    return body.encode(), headers
