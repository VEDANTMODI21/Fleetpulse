import time
import os

import jwt
import pytest

from fleetpulse.api import security
from fleetpulse.api.resilience import CircuitBreaker, CircuitOpen, TokenBucket
from fleetpulse.api.security import AuthError, Principal, issue_token, mask_location, verify_token


def test_token_roundtrip_and_rbac():
    p = verify_token(issue_token("m@x", "fleet_manager", 7))
    assert p == Principal("m@x", "fleet_manager", 7)
    assert p.can("workorders:approve") and not p.can("audit:read")
    with pytest.raises(AuthError) as e:
        Principal("a", "analyst", 1).require("workorders:write")
    assert e.value.status == 403


def test_tenant_isolation():
    p = Principal("m", "fleet_manager", 7)
    assert p.scope_fleet(None) == 7 and p.scope_fleet(7) == 7
    with pytest.raises(AuthError):
        p.scope_fleet(8)
    assert Principal("a", "platform_admin", None).scope_fleet(8) == 8


@pytest.mark.parametrize("tok", [
    "garbage",
    jwt.encode({"sub": "x", "role": "fleet_manager", "aud": "fleetpulse-api", "iss": security.ISSUER,
                "iat": 1, "exp": int(time.time()) + 60}, "wrong-secret-0123456789012345678901234", algorithm="HS256"),
    jwt.encode({"sub": "x", "role": "fleet_manager", "aud": "other", "iss": security.ISSUER, "iat": 1,
                "exp": int(time.time()) + 60}, os.environ["JWT_SECRET"], algorithm="HS256"),
    jwt.encode({"sub": "x", "role": "fleet_manager"}, "", algorithm="none") if False else
    jwt.encode({"sub": "x", "role": "fleet_manager", "aud": "fleetpulse-api", "iss": security.ISSUER}, security._SECRET,
               algorithm="HS256"),   # missing exp/iat
])
def test_invalid_tokens(tok):
    with pytest.raises(AuthError) as e:
        verify_token(tok)
    assert e.value.status == 401


def test_expired_and_unknown_role():
    with pytest.raises(AuthError) as e:
        verify_token(issue_token("x", "fleet_manager", 1, ttl_s=-10))
    assert e.value.msg == "token expired"
    with pytest.raises(AuthError) as e:
        verify_token(issue_token("x", "superuser", 1))
    assert e.value.status == 403


def test_alg_none_rejected():
    tok = jwt.encode({"sub": "x", "role": "platform_admin", "aud": "fleetpulse-api", "iss": security.ISSUER,
                      "iat": 1, "exp": int(time.time()) + 60}, None, algorithm="none")
    with pytest.raises(AuthError):
        verify_token(tok)


def test_location_masking():
    assert mask_location(12.971612, 77.594612, True) == (12.971612, 77.594612)
    assert mask_location(12.971612, 77.594612, False) == (12.97, 77.59)


def test_token_bucket_local():
    now = [0.0]
    tb = TokenBucket(1, 3, clock=lambda: now[0])
    assert [tb.allow("u") for _ in range(4)] == [True, True, True, False]
    now[0] = 2.0
    assert tb.allow("u") and tb.allow("u") and not tb.allow("u")
    assert tb.allow("other")


def test_token_bucket_redis_falls_back_when_redis_errors():
    class R:
        def register_script(self, _):
            def boom(**_):
                raise ConnectionError()
            return boom
    tb = TokenBucket(1, 1, R())
    assert tb.allow("u") and not tb.allow("u")


def test_circuit_breaker_states():
    now = [0.0]
    cb = CircuitBreaker(threshold=2, reset_s=5, clock=lambda: now[0])

    def fail():
        raise OSError()
    for _ in range(2):
        with pytest.raises(IOError):
            cb.call(fail)
    assert cb.state == "OPEN"
    with pytest.raises(CircuitOpen):
        cb.call(lambda: 1)
    now[0] = 6
    with pytest.raises(IOError):
        cb.call(fail)                    # half-open probe fails -> open again
    assert cb.state == "OPEN"
    now[0] = 12
    assert cb.call(lambda: 42) == 42 and cb.state == "CLOSED"
