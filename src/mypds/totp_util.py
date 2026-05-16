"""Shared TOTP pre-auth token helpers (used by web.py and the totp plugin)."""
import hmac
import hashlib
import base64
import json
import time


def make_preauth(did: str, handle: str, next_url: str, secret_key: str, ttl: int = 300) -> str:
    payload = json.dumps({"did": did, "handle": handle, "next": next_url, "ts": int(time.time()) + ttl})
    b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(secret_key.encode(), b64.encode(), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def check_preauth(token: str, secret_key: str) -> tuple[str, str, str]:
    """Returns (did, handle, next_url) or raises ValueError."""
    try:
        b64, sig = token.rsplit(".", 1)
    except ValueError:
        raise ValueError("malformed token")
    expected = hmac.new(secret_key.encode(), b64.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise ValueError("bad signature")
    padding = "=" * (-len(b64) % 4)
    data = json.loads(base64.urlsafe_b64decode(b64 + padding))
    if time.time() > data["ts"]:
        raise ValueError("token expired")
    return data["did"], data["handle"], data.get("next", "/dashboard")
