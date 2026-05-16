APP_NAME = "portal"
NSID = None

import asyncio
import hmac
import hashlib
import base64
import mimetypes
import os
import secrets
import logging
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

from aiohttp import web

from mypds import static_config
from mypds.app_util import get_db, get_client
from mypds.web import get_session, render, get_web_store
from mypds.web_store import MEDIA_DIR

_DB_PATH = static_config.DATA_DIR + "/plugins/portal.sqlite3"
_con: Optional[sqlite3.Connection] = None


def _get_con() -> sqlite3.Connection:
    global _con
    if _con is None:
        Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        _con = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _con.row_factory = sqlite3.Row
    return _con

logger = logging.getLogger(__name__)
routes = web.RouteTableDef()

_BSKY_API = "https://public.api.bsky.app/xrpc"
_SESSION_COOKIE = "portal_visitor"
_NONCE_TTL = 600  # seconds

SETTINGS = [
    {
        "key": "access_mode",
        "type": "select",
        "label": "Access mode",
        "description": "Who can enter the portal. Whitelist: only listed handles. Blacklist: everyone except listed handles.",
        "default": "blacklist",
        "options": [("open", "Open (anyone with a Bluesky account)"), ("whitelist", "Whitelist only"), ("blacklist", "Block listed handles")],
        "group": "access",
    },
    {
        "key": "allow_list",
        "type": "text",
        "label": "Whitelist",
        "description": "One handle or DID per line. Only these accounts can enter when mode is Whitelist.",
        "default": "",
        "group": "access",
    },
    {
        "key": "block_list",
        "type": "text",
        "label": "Blocklist",
        "description": "One handle or DID per line. These accounts are denied entry when mode is Blacklist.",
        "default": "",
        "group": "access",
    },
]

_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

# room_did -> [asyncio.Queue, ...]
_streams: dict = {}


def _ensure_tables():
    con = _get_con()
    con.execute("""CREATE TABLE IF NOT EXISTS portal_visitor (
        did TEXT PRIMARY KEY, handle TEXT, display_name TEXT, avatar TEXT,
        first_seen TEXT DEFAULT(datetime('now')),
        last_seen TEXT DEFAULT(datetime('now'))
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS portal_pending (
        did TEXT PRIMARY KEY, handle TEXT, display_name TEXT, avatar TEXT,
        nonce TEXT, expires_at TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS portal_drop (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_did TEXT NOT NULL, author TEXT NOT NULL,
        content TEXT NOT NULL DEFAULT '',
        image_path TEXT,
        created_at TEXT DEFAULT(datetime('now'))
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS portal_kicked (
        did TEXT PRIMARY KEY, handle TEXT, kicked_at TEXT DEFAULT(datetime('now'))
    )""")
    cols = {r[1] for r in con.execute("PRAGMA table_info(portal_drop)").fetchall()}
    if "image_path" not in cols:
        con.execute("ALTER TABLE portal_drop ADD COLUMN image_path TEXT")
    if "file_name" not in cols:
        con.execute("ALTER TABLE portal_drop ADD COLUMN file_name TEXT")
    kicked_cols = {r[1] for r in con.execute("PRAGMA table_info(portal_kicked)").fetchall()}
    if "handle" not in kicked_cols:
        con.execute("ALTER TABLE portal_kicked ADD COLUMN handle TEXT")
    con.commit()


def _make_token(secret: str, did: str) -> str:
    msg = did.encode()
    sig = hmac.new(secret.encode(), msg, digestmod=hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig + msg).decode().rstrip("=")


def _check_token(secret: str, token: str) -> Optional[str]:
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded)
        if len(raw) < 33:
            return None
        sig, did_bytes = raw[:32], raw[32:]
        did = did_bytes.decode()
        expected = hmac.new(secret.encode(), did_bytes, digestmod=hashlib.sha256).digest()
        if hmac.compare_digest(sig, expected):
            return did
    except Exception:
        pass
    return None


def _get_visitor_did(request) -> Optional[str]:
    token = request.cookies.get(_SESSION_COOKIE)
    if not token:
        return None
    secret = get_db(request).config.get("jwt_access_secret", "")
    return _check_token(secret, token)


def _parse_list(raw: str) -> set:
    return {line.strip().lstrip("@").lower() for line in raw.splitlines() if line.strip()}


async def _resolve_handle(client, handle: str) -> Optional[str]:
    handle = handle.lstrip("@").strip()
    if handle.startswith("did:"):
        return handle
    try:
        async with client.get(
            f"{_BSKY_API}/com.atproto.identity.resolveHandle",
            params={"handle": handle},
        ) as r:
            if r.status == 200:
                return (await r.json()).get("did")
    except Exception:
        pass
    return None


async def _fetch_profile(client, did: str) -> dict:
    try:
        async with client.get(
            f"{_BSKY_API}/app.bsky.actor.getProfile", params={"actor": did}
        ) as r:
            if r.status == 200:
                d = await r.json()
                return {
                    "handle": d.get("handle", did),
                    "display_name": d.get("displayName", ""),
                    "avatar": d.get("avatar", ""),
                }
    except Exception:
        pass
    return {"handle": did, "display_name": "", "avatar": ""}


async def _check_nonce(client, did: str, nonce: str) -> bool:
    try:
        async with client.get(
            f"{_BSKY_API}/app.bsky.feed.getAuthorFeed",
            params={"actor": did, "limit": 20, "filter": "posts_no_replies"},
        ) as r:
            if r.status != 200:
                return False
            for item in (await r.json()).get("feed", []):
                text = item.get("post", {}).get("record", {}).get("text", "")
                if nonce in text:
                    return True
    except Exception:
        pass
    return False


def _streams_add(room_did: str, q: asyncio.Queue) -> None:
    _streams.setdefault(room_did, []).append(q)


def _streams_remove(room_did: str, q: asyncio.Queue) -> None:
    try:
        _streams.get(room_did, []).remove(q)
    except ValueError:
        pass


async def _broadcast(room_did: str, payload: dict) -> None:
    import json
    msg = json.dumps(payload)
    for q in list(_streams.get(room_did, [])):
        await q.put(msg)


@routes.get("/portal")
async def portal_index(request: web.Request):
    _ensure_tables()
    session = get_session(request)
    visitor_did = _get_visitor_did(request)

    if session:
        visitors = _get_con().execute(
            "SELECT did, handle, display_name, avatar, last_seen FROM portal_visitor ORDER BY last_seen DESC"
        ).fetchall()
        return render(request, "plugin/portal/main.html", {"visitors": visitors})

    if visitor_did:
        row = _get_con().execute("SELECT did FROM portal_visitor WHERE did=?", (visitor_did,)).fetchone()
        if row:
            raise web.HTTPFound(f"/portal/room/{visitor_did}")

    raise web.HTTPFound("/portal/enter")


@routes.get("/portal/enter")
async def portal_enter(request: web.Request):
    return render(request, "plugin/portal/enter.html", {"error": None})


@routes.post("/portal/enter")
async def portal_enter_post(request: web.Request):
    _ensure_tables()
    data = await request.post()
    handle = data.get("handle", "").strip()
    if not handle:
        return render(request, "plugin/portal/enter.html", {"error": "enter a handle or DID"})

    client = get_client(request)
    did = await _resolve_handle(client, handle)
    if not did:
        return render(request, "plugin/portal/enter.html", {"error": "couldn't resolve that handle"})

    owner_row = get_db(request).con.execute("SELECT did FROM user LIMIT 1").fetchone()
    if owner_row and did == owner_row[0]:
        return render(request, "plugin/portal/enter.html", {"error": "that's the node owner — use the admin login"})

    kicked = _get_con().execute("SELECT did FROM portal_kicked WHERE did=?", (did,)).fetchone()
    if kicked:
        return render(request, "plugin/portal/enter.html", {"error": "access revoked"})

    ws = get_web_store(request)
    mode = ws.get_plugin_setting("portal", "access_mode") or "open"
    if mode == "whitelist":
        allow_raw = ws.get_plugin_setting("portal", "allow_list")
        allowed = _parse_list(allow_raw)
        handle_norm = handle.lstrip("@").strip().lower()
        if did.lower() not in allowed and handle_norm not in allowed:
            return render(request, "plugin/portal/enter.html", {"error": "this portal is invite-only"})
    elif mode == "blacklist":
        block_raw = ws.get_plugin_setting("portal", "block_list")
        blocked = _parse_list(block_raw)
        handle_norm = handle.lstrip("@").strip().lower()
        if did.lower() in blocked or handle_norm in blocked:
            return render(request, "plugin/portal/enter.html", {"error": "access denied"})

    profile = await _fetch_profile(client, did)
    nonce = secrets.token_hex(4).upper()
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=_NONCE_TTL)).isoformat()

    _get_con().execute(
        "INSERT OR REPLACE INTO portal_pending (did, handle, display_name, avatar, nonce, expires_at) VALUES (?,?,?,?,?,?)",
        (did, profile["handle"], profile["display_name"], profile["avatar"], nonce, expires_at),
    )
    _get_con().commit()
    logger.info(f"portal: pending created did={did} nonce={nonce}")
    raise web.HTTPFound(f"/portal/verify/{did}")


@routes.get("/portal/verify/{did:.*}")
async def portal_verify(request: web.Request):
    _ensure_tables()
    did = request.match_info["did"]
    row = _get_con().execute(
        "SELECT handle, display_name, avatar, nonce FROM portal_pending WHERE did=?", (did,)
    ).fetchone()
    if not row:
        raise web.HTTPFound("/portal/enter")
    handle, display_name, avatar, nonce = row
    host = get_db(request).config.get("pds_pfx", "").replace("https://", "").replace("http://", "").rstrip("/")
    return render(request, "plugin/portal/verify.html", {
        "did": did, "handle": handle, "display_name": display_name,
        "avatar": avatar, "nonce": nonce, "host": host, "error": None,
    })


@routes.post("/portal/verify/{did:.*}")
async def portal_verify_post(request: web.Request):
    _ensure_tables()
    did = request.match_info["did"]
    logger.info(f"portal: verify_post did={did!r}")
    row = _get_con().execute(
        "SELECT handle, display_name, avatar, nonce, expires_at FROM portal_pending WHERE did=?", (did,)
    ).fetchone()
    logger.info(f"portal: pending row={row}")
    if not row:
        raise web.HTTPFound("/portal/enter")
    handle, display_name, avatar, nonce, expires_at = row

    if datetime.now(timezone.utc) > datetime.fromisoformat(expires_at):
        _get_con().execute("DELETE FROM portal_pending WHERE did=?", (did,))
        _get_con().commit()
        raise web.HTTPFound("/portal/enter")

    client = get_client(request)
    verified = await _check_nonce(client, did, nonce)
    logger.info(f"portal: nonce check did={did} nonce={nonce} verified={verified}")

    if not verified:
        host = get_db(request).config.get("pds_pfx", "").replace("https://", "").replace("http://", "").rstrip("/")
        return render(request, "plugin/portal/verify.html", {
            "did": did, "handle": handle, "display_name": display_name,
            "avatar": avatar, "nonce": nonce, "host": host,
            "error": "post not found yet — make sure it's public and try again",
        })

    now = datetime.now(timezone.utc).isoformat()
    _get_con().execute(
        """INSERT INTO portal_visitor (did, handle, display_name, avatar, first_seen, last_seen) VALUES (?,?,?,?,?,?)
           ON CONFLICT(did) DO UPDATE SET handle=excluded.handle, display_name=excluded.display_name,
           avatar=excluded.avatar, last_seen=excluded.last_seen""",
        (did, handle, display_name, avatar, now, now),
    )
    _get_con().execute("DELETE FROM portal_pending WHERE did=?", (did,))
    _get_con().commit()

    secret = get_db(request).config.get("jwt_access_secret", "")
    token = _make_token(secret, did)
    resp = web.Response(status=302, headers={"Location": f"/portal/room/{did}"})
    resp.set_cookie(_SESSION_COOKIE, token, httponly=True, samesite="Lax", max_age=86400 * 30)
    return resp


# Download conversation — must be before generic GET /portal/room/{did:.*}
@routes.get("/portal/room/{did:.*}/download")
async def portal_download(request: web.Request):
    _ensure_tables()
    room_did = request.match_info["did"]
    session = get_session(request)
    visitor_did = _get_visitor_did(request)

    if not session and visitor_did != room_did:
        raise web.HTTPFound("/portal/enter")

    visitor = _get_con().execute(
        "SELECT did, handle, display_name, avatar FROM portal_visitor WHERE did=?", (room_did,)
    ).fetchone()
    if not visitor:
        raise web.HTTPNotFound()

    db = get_db(request)
    owner_row = db.con.execute("SELECT did, handle FROM user LIMIT 1").fetchone()
    owner_did, owner_handle = (owner_row[0], owner_row[1]) if owner_row else ("", "")

    _IMG_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
    raw = _get_con().execute(
        "SELECT id, author, content, created_at, image_path, file_name FROM portal_drop WHERE room_did=? ORDER BY created_at ASC",
        (room_did,),
    ).fetchall()

    import base64 as _b64
    import html as _html

    drops_html = []
    for row in raw:
        _, author, content, created_at, image_path, file_name = row
        is_owner_drop = (author == owner_did)
        is_mine = is_owner_drop if session else (author == visitor_did)

        parts = []
        if content:
            parts.append(f'<div class="text">{_html.escape(content)}</div>')
        if image_path:
            fpath = os.path.join(MEDIA_DIR, "portal", image_path)
            ext = os.path.splitext(image_path)[1].lower()
            is_img = ext in _IMG_EXTS
            if os.path.exists(fpath) and os.path.getsize(fpath) <= 8 * 1024 * 1024:
                with open(fpath, "rb") as fh:
                    enc = _b64.b64encode(fh.read()).decode()
                mime, _ = mimetypes.guess_type(image_path)
                mime = mime or "application/octet-stream"
                if is_img:
                    parts.append(f'<img src="data:{mime};base64,{enc}" style="max-width:100%;max-height:320px;border-radius:6px;margin-top:6px;display:block;">')
                else:
                    fn = _html.escape(file_name or image_path)
                    parts.append(f'<a href="data:{mime};base64,{enc}" download="{fn}" style="display:inline-block;margin-top:6px;padding:5px 10px;background:#1a2a3a;border-radius:6px;color:#67e8f9;text-decoration:none;font-size:.8rem;">&#8964; {fn}</a>')
        parts.append(f'<div class="time">{_html.escape(created_at[:16])}</div>')
        who = f"@{owner_handle}" if is_owner_drop else f"@{visitor[1]}"
        radius = "4px 12px 12px 12px" if not is_mine else "12px 4px 12px 12px"
        bg = "#1a2a2a" if is_owner_drop else "#1a1a2a"
        direction = "row-reverse" if is_mine else "row"
        drops_html.append(
            f'<div style="display:flex;flex-direction:{direction};margin-bottom:12px;">'
            f'<div style="max-width:72%;background:{bg};border-radius:{radius};padding:8px 12px;">'
            f'<div style="font-size:.65rem;color:#6b7280;margin-bottom:4px;">{_html.escape(who)}</div>'
            + "".join(parts) +
            f'</div></div>'
        )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    count = len(raw)
    html_out = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Portal · @{_html.escape(visitor[1])}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0a0a0a;color:#e0e0e0;font-family:system-ui,-apple-system,sans-serif;padding:24px 16px 48px}}
.header{{max-width:660px;margin:0 auto 28px;border-bottom:1px solid #222;padding-bottom:16px}}
.header h1{{font-size:1rem;font-weight:700;color:#67e8f9;margin-bottom:6px}}
.header p{{font-size:.75rem;color:#6b7280;margin-top:4px}}
.drops{{max-width:660px;margin:0 auto}}
.time{{font-size:.6rem;color:#6b7280;margin-top:4px}}
.text{{font-size:.82rem;line-height:1.5;word-break:break-word}}
</style>
</head>
<body>
<div class="header">
  <h1>Portal conversation</h1>
  <p>@{_html.escape(owner_handle)} &#x21c4; @{_html.escape(visitor[1])}</p>
  <p>{count} message{"s" if count != 1 else ""} &middot; downloaded {now}</p>
</div>
<div class="drops">
{"".join(drops_html) if drops_html else '<p style="text-align:center;color:#6b7280;padding:40px 0;">no messages</p>'}
</div>
</body>
</html>"""

    fname = f"portal-{visitor[1].replace('.', '-')}-{datetime.now(timezone.utc).strftime('%Y%m%d')}.html"
    return web.Response(
        body=html_out.encode(),
        content_type="text/html",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# SSE stream — must be registered before the generic GET /portal/room/{did:.*}
@routes.get("/portal/room/{did:.*}/stream")
async def portal_room_stream(request: web.Request):
    room_did = request.match_info["did"]
    session = get_session(request)
    visitor_did = _get_visitor_did(request)

    if not session and visitor_did != room_did:
        raise web.HTTPUnauthorized()

    q: asyncio.Queue = asyncio.Queue()
    _streams_add(room_did, q)

    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })
    await resp.prepare(request)

    try:
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=25)
                await resp.write(f"data: {msg}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")
    except Exception:
        pass
    finally:
        _streams_remove(room_did, q)

    return resp


@routes.get("/portal/room/{did:.*}")
async def portal_room(request: web.Request):
    _ensure_tables()
    room_did = request.match_info["did"]
    session = get_session(request)
    visitor_did = _get_visitor_did(request)

    logger.info(f"portal: room session={bool(session)} visitor_did={visitor_did!r} room_did={room_did!r}")
    if not session and visitor_did != room_did:
        logger.info("portal: room auth fail - cookie mismatch or missing")
        raise web.HTTPFound("/portal/enter")

    if visitor_did == room_did:
        kicked = _get_con().execute("SELECT did FROM portal_kicked WHERE did=?", (room_did,)).fetchone()
        if kicked:
            raise web.HTTPFound("/portal/enter")

    visitor = _get_con().execute(
        "SELECT did, handle, display_name, avatar, first_seen FROM portal_visitor WHERE did=?", (room_did,)
    ).fetchone()
    logger.info(f"portal: room visitor row={visitor}")
    if not visitor:
        raise web.HTTPFound("/portal/enter")

    db = get_db(request)
    owner_row = db.con.execute("SELECT did, handle FROM user LIMIT 1").fetchone()
    owner_did, owner_handle = (owner_row[0], owner_row[1]) if owner_row else ("", "")

    _IMG_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
    raw = _get_con().execute(
        "SELECT id, author, content, created_at, image_path, file_name FROM portal_drop WHERE room_did=? ORDER BY created_at ASC",
        (room_did,),
    ).fetchall()
    drops = []
    for row in raw:
        ip = row[4]
        is_img = bool(ip and os.path.splitext(ip)[1].lower() in _IMG_EXTS)
        drops.append((row[0], row[1], row[2], row[3], ip, row[5], is_img))

    if visitor_did == room_did:
        _get_con().execute("UPDATE portal_visitor SET last_seen=datetime('now') WHERE did=?", (room_did,))
        _get_con().commit()

    return render(request, "plugin/portal/room.html", {
        "visitor": visitor,
        "owner_did": owner_did,
        "owner_handle": owner_handle,
        "drops": drops,
        "is_owner": bool(session),
        "viewer_did": owner_did if session else visitor_did,
    })


@routes.post("/portal/room/{did:.*}/kick")
async def portal_kick(request: web.Request):
    _ensure_tables()
    session = get_session(request)
    if not session:
        raise web.HTTPUnauthorized()

    room_did = request.match_info["did"]
    visitor_row = _get_con().execute("SELECT handle FROM portal_visitor WHERE did=?", (room_did,)).fetchone()
    handle = visitor_row[0] if visitor_row else room_did
    _get_con().execute(
        "INSERT OR REPLACE INTO portal_kicked (did, handle, kicked_at) VALUES (?, ?, datetime('now'))",
        (room_did, handle),
    )
    _get_con().execute("DELETE FROM portal_visitor WHERE did=?", (room_did,))
    _get_con().commit()

    # Persist kick to block_list setting using handle (readable) not DID
    ws = get_web_store(request)
    block_raw = ws.get_plugin_setting("portal", "block_list") or ""
    blocked = {line.strip() for line in block_raw.splitlines() if line.strip()}
    blocked.add(handle)
    ws.set_plugin_setting("portal", "block_list", "\n".join(sorted(blocked)))

    await _broadcast(room_did, {"type": "kicked"})
    raise web.HTTPFound("/portal")


@routes.post("/portal/room/{did:.*}/purge")
async def portal_purge(request: web.Request):
    _ensure_tables()
    session = get_session(request)
    if not session:
        raise web.HTTPUnauthorized()

    room_did = request.match_info["did"]
    file_rows = _get_con().execute(
        "SELECT image_path FROM portal_drop WHERE room_did=? AND image_path IS NOT NULL", (room_did,)
    ).fetchall()
    for (fp,) in file_rows:
        try:
            os.unlink(os.path.join(MEDIA_DIR, "portal", fp))
        except OSError:
            pass

    _get_con().execute("DELETE FROM portal_drop WHERE room_did=?", (room_did,))
    _get_con().commit()

    await _broadcast(room_did, {"type": "purge"})
    raise web.HTTPFound(f"/portal/room/{room_did}")


@routes.post("/portal/room/{did:.*}/drop")
async def portal_drop(request: web.Request):
    _ensure_tables()
    room_did = request.match_info["did"]
    session = get_session(request)
    visitor_did = _get_visitor_did(request)

    if not session and visitor_did != room_did:
        raise web.HTTPUnauthorized()

    data = await request.post()
    content = data.get("content", "").strip()[:500]

    imgs = [f for f in data.getall("image") if f and hasattr(f, "filename") and (f.filename or "").strip()]

    if not content and not imgs:
        raise web.HTTPFound(f"/portal/room/{room_did}")

    if session:
        author = get_db(request).con.execute("SELECT did FROM user LIMIT 1").fetchone()[0]
    else:
        author = visitor_did

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    portal_dir = os.path.join(MEDIA_DIR, "portal")
    os.makedirs(portal_dir, exist_ok=True)

    if not imgs:
        cur = _get_con().execute(
            "INSERT INTO portal_drop (room_did, author, content, image_path, file_name) VALUES (?,?,?,?,?)",
            (room_did, author, content, None, None),
        )
        _get_con().commit()
        await _broadcast(room_did, {
            "type": "drop", "id": cur.lastrowid, "author": author,
            "content": content, "image": None, "is_image": False,
            "file_name": None, "created_at": now_str,
        })
    else:
        for i, img_field in enumerate(imgs):
            row_content = content if i == 0 else ""
            ct = (img_field.content_type or "").split(";")[0].strip()
            orig_name = img_field.filename
            orig_ext = os.path.splitext(orig_name)[1].lower()
            ext = orig_ext if (orig_ext and 1 < len(orig_ext) <= 10 and orig_ext[1:].isalnum()) else (mimetypes.guess_extension(ct) or ".bin")
            fname = secrets.token_hex(10) + ext
            img_bytes = img_field.file.read()
            image_path = None
            is_image = False
            if 0 < len(img_bytes) <= 10 * 1024 * 1024:
                with open(os.path.join(portal_dir, fname), "wb") as fh:
                    fh.write(img_bytes)
                image_path = fname
                is_image = ct in _IMAGE_TYPES
            cur = _get_con().execute(
                "INSERT INTO portal_drop (room_did, author, content, image_path, file_name) VALUES (?,?,?,?,?)",
                (room_did, author, row_content, image_path, orig_name if image_path else None),
            )
            _get_con().commit()
            await _broadcast(room_did, {
                "type": "drop", "id": cur.lastrowid, "author": author,
                "content": row_content,
                "image": f"/portal/img/{image_path}" if image_path else None,
                "is_image": is_image,
                "file_name": orig_name if image_path else None,
                "created_at": now_str,
            })

    raise web.HTTPFound(f"/portal/room/{room_did}")


@routes.get("/portal/img/{filename:[^/]+}")
async def portal_img(request: web.Request):
    filename = request.match_info["filename"]
    session = get_session(request)
    visitor_did = _get_visitor_did(request)
    if not session and not visitor_did:
        raise web.HTTPUnauthorized()
    path = os.path.join(MEDIA_DIR, "portal", filename)
    if not os.path.exists(path):
        raise web.HTTPNotFound()
    mime, _ = mimetypes.guess_type(filename)
    return web.FileResponse(path, headers={"Content-Type": mime or "application/octet-stream"})


def on_settings_save(plugin_name: str, post_data: dict) -> None:
    """Called by web.py after saving plugin settings. Syncs portal_kicked with block_list."""
    if plugin_name != APP_NAME:
        return
    block_raw = post_data.get("block_list", "")
    new_blocked = _parse_list(block_raw)
    con = _get_con()
    kicked_rows = con.execute("SELECT did, handle FROM portal_kicked").fetchall()
    to_unkick = []
    for did, handle in kicked_rows:
        did_norm = (did or "").lower()
        handle_norm = (handle or "").lstrip("@").lower()
        if did_norm not in new_blocked and handle_norm not in new_blocked:
            to_unkick.append(did)
    for did in to_unkick:
        con.execute("DELETE FROM portal_kicked WHERE did=?", (did,))
    if to_unkick:
        con.commit()
        logger.info(f"portal: unkicked {len(to_unkick)} account(s) removed from block_list")


if __name__ == "__main__":
    from mypds.plugin_runner import run_plugin
    run_plugin(routes, APP_NAME)
