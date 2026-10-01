"""AuthN/AuthZ: OIDC-compatible JWT verification + RBAC + tenant scoping.

Production: tokens are issued by the IdP (Keycloak / Auth0 / Entra ID) and
verified with RS256 against its JWKS (`OIDC_JWKS_URL`). Local/dev: a demo
issuer signs HS256 tokens with `JWT_SECRET` (from the secret store / env).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

import jwt

ISSUER = os.environ.get("OIDC_ISSUER", "https://idp.fleetpulse.local/realms/fleetpulse")
AUDIENCE = "fleetpulse-api"
ALGO = os.environ.get("JWT_ALG", "HS256")


def _signing_secret() -> str:
    secret = os.environ.get("JWT_SECRET")
    if not secret or len(secret) < 32:
        raise RuntimeError("JWT_SECRET must be configured with at least 32 characters")
    return secret

PERMISSIONS = {
    "platform_admin": {"vehicles:read", "alerts:read", "alerts:ack", "workorders:write", "workorders:approve",
                       "agent:ask", "privacy:erase", "audit:read", "location:precise", "ingest:write"},
    "fleet_manager": {"vehicles:read", "alerts:read", "alerts:ack", "workorders:write", "workorders:approve",
                      "agent:ask", "privacy:erase", "location:precise"},
    "technician": {"vehicles:read", "alerts:read", "alerts:ack", "workorders:write", "agent:ask"},
    "analyst": {"vehicles:read", "alerts:read"},           # coarse location only
    "oem_ingest": {"ingest:write"},
}


class AuthError(Exception):
    def __init__(self, status: int, msg: str):
        self.status, self.msg = status, msg


@dataclass(frozen=True)
class Principal:
    sub: str
    role: str
    fleet_id: int | None      # None only for platform_admin / oem_ingest

    def can(self, perm: str) -> bool:
        return perm in PERMISSIONS.get(self.role, set())

    def require(self, perm: str) -> None:
        if not self.can(perm):
            raise AuthError(403, f"role '{self.role}' lacks permission '{perm}'")

    def scope_fleet(self, requested: int | None) -> int | None:
        """Tenant isolation: non-admins are pinned to their own fleet."""
        if self.role == "platform_admin":
            return requested
        if requested is not None and requested != self.fleet_id:
            raise AuthError(403, "cross-tenant access denied")
        return self.fleet_id


def issue_token(sub: str, role: str, fleet_id: int | None, ttl_s: int = 3600) -> str:
    now = int(time.time())
    return jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": sub, "role": role, "fleet_id": fleet_id,
                       "iat": now, "exp": now + ttl_s}, _signing_secret(), algorithm="HS256")


def verify_token(token: str) -> Principal:
    try:
        claims = jwt.decode(token, _signing_secret(), algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER,
                            options={"require": ["exp", "iat", "sub", "aud", "iss"]})
    except jwt.ExpiredSignatureError as e:
        raise AuthError(401, "token expired") from e
    except jwt.PyJWTError as e:
        raise AuthError(401, "invalid token") from e
    role = claims.get("role")
    if role not in PERMISSIONS:
        raise AuthError(403, "unknown role")
    return Principal(claims["sub"], role, claims.get("fleet_id"))


def mask_location(lat: float, lon: float, precise: bool) -> tuple[float, float]:
    """Location minimisation: roles without `location:precise` get ~1.1 km grid (2 dp)."""
    return (lat, lon) if precise else (round(lat, 2), round(lon, 2))
