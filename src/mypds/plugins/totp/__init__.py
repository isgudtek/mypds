"""TOTP 2FA plugin — setup, verify, and remove TOTP authentication."""
import io
import time
import logging

import pyotp
from aiohttp import web

from mypds.web import render, get_session, get_web_store
from mypds.app_util import MILLIPDS_DB
from mypds.totp_util import make_preauth, check_preauth

APP_NAME = "totp"
URL_PREFIX = "/totp"
NSID = None

logger = logging.getLogger(__name__)
routes = web.RouteTableDef()

COOKIE_NAME = "mpds_sid"
COOKIE_OPTS = dict(httponly=True, samesite="Strict", max_age=60 * 60 * 24 * 7, path="/")


def _db_config(request: web.Request) -> dict:
    return request.app[MILLIPDS_DB].config


def _require_session(request: web.Request) -> dict:
    sess = get_session(request)
    if not sess:
        raise web.HTTPFound("/login")
    return sess


def _qr_png_dataurl(uri: str) -> str:
    try:
        import base64
        import segno
        buf = io.BytesIO()
        segno.make(uri, error="M").save(buf, kind="png", scale=8, border=4)
        b64 = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/png;base64,{b64}"
    except Exception:
        return ""


# ── Setup ────────────────────────────────────────────────────────────────────

@routes.get("/totp/setup")
async def setup_get(request: web.Request):
    import asyncio
    _require_session(request)
    ws = get_web_store(request)
    has_secret = bool(ws.get_plugin_setting(APP_NAME, "secret"))

    secret = pyotp.random_base32()
    ws.set_plugin_setting(APP_NAME, "pending_secret", secret)
    ws.set_plugin_setting(APP_NAME, "pending_ts", str(int(time.time())))

    cfg = _db_config(request)
    issuer = cfg.get("pds_pfx", "mypds").replace("https://", "").rstrip("/")
    handle = get_session(request)["handle"]
    uri = pyotp.TOTP(secret).provisioning_uri(name=handle, issuer_name=issuer)

    loop = asyncio.get_event_loop()
    qr_url = await loop.run_in_executor(None, _qr_png_dataurl, uri)

    return render(request, "plugin/totp/setup.html", {
        "secret": secret,
        "uri": uri,
        "qr_url": qr_url,
        "has_secret": has_secret,
        "error": None,
    })


@routes.post("/totp/setup")
async def setup_post(request: web.Request):
    _require_session(request)
    ws = get_web_store(request)
    data = await request.post()
    code = data.get("code", "").strip().replace(" ", "")

    pending_secret = ws.get_plugin_setting(APP_NAME, "pending_secret")
    pending_ts = int(ws.get_plugin_setting(APP_NAME, "pending_ts", "0"))

    if not pending_secret or (time.time() - pending_ts) > 600:
        raise web.HTTPFound("/totp/setup")

    totp = pyotp.TOTP(pending_secret)
    if not totp.verify(code, valid_window=1):
        import asyncio
        cfg = _db_config(request)
        issuer = cfg.get("pds_pfx", "mypds").replace("https://", "").rstrip("/")
        handle = get_session(request)["handle"]
        uri = totp.provisioning_uri(name=handle, issuer_name=issuer)
        loop = asyncio.get_event_loop()
        qr_url = await loop.run_in_executor(None, _qr_png_dataurl, uri)
        return render(request, "plugin/totp/setup.html", {
            "secret": pending_secret,
            "uri": uri,
            "qr_url": qr_url,
            "has_secret": bool(ws.get_plugin_setting(APP_NAME, "secret")),
            "error": "Invalid code — try again.",
        }, status=422)

    ws.set_plugin_setting(APP_NAME, "secret", pending_secret)
    ws.set_plugin_setting(APP_NAME, "pending_secret", "")
    ws.set_plugin_setting(APP_NAME, "pending_ts", "")
    raise web.HTTPFound("/dashboard")


# ── Verify (login step) ───────────────────────────────────────────────────────

@routes.get("/totp/verify")
async def verify_get(request: web.Request):
    token = request.query.get("t", "")
    cfg = _db_config(request)
    try:
        check_preauth(token, cfg.get("jwt_access_secret", ""))
    except ValueError:
        raise web.HTTPFound("/login")
    return render(request, "plugin/totp/verify.html", {"token": token, "error": None})


@routes.post("/totp/verify")
async def verify_post(request: web.Request):
    data = await request.post()
    token = data.get("t", "")
    code = data.get("code", "").strip().replace(" ", "")

    cfg = _db_config(request)
    secret_key = cfg.get("jwt_access_secret", "")
    try:
        did, handle, next_url = check_preauth(token, secret_key)
    except ValueError:
        raise web.HTTPFound("/login")

    ws = get_web_store(request)
    totp_secret = ws.get_plugin_setting(APP_NAME, "secret")
    if not totp_secret:
        session_token = ws.create_session(did, handle)
        resp = web.HTTPFound(next_url)
        resp.set_cookie(COOKIE_NAME, session_token, **COOKIE_OPTS)
        return resp

    if not pyotp.TOTP(totp_secret).verify(code, valid_window=1):
        return render(request, "plugin/totp/verify.html", {"token": token, "error": "Invalid code."}, status=422)

    session_token = ws.create_session(did, handle)
    resp = web.HTTPFound(next_url)
    resp.set_cookie(COOKIE_NAME, session_token, **COOKIE_OPTS)
    return resp


# ── Remove ────────────────────────────────────────────────────────────────────

@routes.post("/totp/remove")
async def remove_post(request: web.Request):
    _require_session(request)
    ws = get_web_store(request)
    ws.set_plugin_setting(APP_NAME, "secret", "")
    ws.set_plugin_setting(APP_NAME, "pending_secret", "")
    raise web.HTTPFound("/totp/setup")


if __name__ == "__main__":
    from mypds.plugin_runner import run_plugin
    run_plugin(routes, APP_NAME)
