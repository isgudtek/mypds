"""
mycrab federation plugin — encrypted club posts over public ATProto firehose.

Each club is identified by (club_id, seed_url). Members share no central key server;
encryption uses hybrid NaCl: per-message symmetric key, sealed to each member's X25519 pubkey.
Pubkeys are published/exchanged at handshake time via the seed.
"""
import asyncio
import datetime
import json
import logging
import mimetypes
import os
import time
import uuid

import aiohttp
import jwt as _jwt
from aiohttp import web

from mypds.app_util import MILLIPDS_DB, MILLIPDS_AIOHTTP_CLIENT
from mypds.web import render, get_session, get_web_store, MILLIPDS_WEB_STORE

from . import crypto
from .peer import FederationPeer, CLUB_NSID

APP_NAME = "federation"
DEFAULT_ENABLED = True
NSID = CLUB_NSID
URL_PREFIXES = ["/club", "/xrpc/space.mycrab.federation.join"]
logger = logging.getLogger(__name__)
routes = web.RouteTableDef()

SETTINGS = [
    {
        "key": "club_id",
        "type": "text",
        "label": "Club ID",
        "description": "Unique identifier for your club (e.g. mycrab). All members must use the same value.",
        "default": "mycrab",
        "group": "club",
    },
    {
        "key": "seed_url",
        "type": "text",
        "label": "Seed server URL",
        "description": "Bootstrap peer URL (e.g. https://mypds.mycrab.space). This node connects first.",
        "default": "https://mypds.mycrab.space",
        "group": "club",
    },
    {
        "key": "membership",
        "type": "select",
        "label": "Membership",
        "description": "Who can join this club.",
        "default": "whitelist",
        "options": [("open", "Open (any node)"), ("whitelist", "Whitelist (domain pattern)")],
        "group": "access",
    },
    {
        "key": "whitelist_pattern",
        "type": "text",
        "label": "Whitelist pattern",
        "description": "Domain suffix to allow, e.g. *.mycrab.space. Only used when membership=whitelist.",
        "default": "*.mycrab.space",
        "group": "access",
    },
]

_peer_runner: FederationPeer | None = None
_peer_task: asyncio.Task | None = None
_club_streams: dict = {}  # club_id -> list[asyncio.Queue]


def _broadcast_club(club_id: str, payload: dict):
    for q in list(_club_streams.get(club_id, [])):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass


def _get_club_files_dir(db) -> str:
    row = db.con.execute("PRAGMA database_list").fetchone()
    db_path = row[2] if row else ""
    base = os.path.dirname(db_path) if db_path else "/tmp"
    d = os.path.join(base, "club_files")
    os.makedirs(d, exist_ok=True)
    return d


async def _fanout_join(client, peer_pds_url: str, payload: dict):
    """Fire-and-forget: push new member info to an existing peer."""
    try:
        async with client.post(
            f"{peer_pds_url}/xrpc/space.mycrab.federation.join",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=5),
        ) as _:
            pass
    except Exception:
        pass


def _ensure_tables(db):
    db.con.execute("""
        CREATE TABLE IF NOT EXISTS federation_member (
            did TEXT NOT NULL,
            club_id TEXT NOT NULL,
            pubkey TEXT NOT NULL,
            pds_url TEXT NOT NULL DEFAULT '',
            added_at TEXT NOT NULL,
            PRIMARY KEY (did, club_id)
        )
    """)
    db.con.execute("""
        CREATE TABLE IF NOT EXISTS federation_record (
            cid TEXT PRIMARY KEY,
            author_did TEXT NOT NULL,
            rkey TEXT NOT NULL,
            club_id TEXT NOT NULL,
            plaintext TEXT NOT NULL,
            created_at TEXT NOT NULL,
            indexed_at TEXT NOT NULL
        )
    """)
    db.con.execute("""
        CREATE TABLE IF NOT EXISTS federation_keypair (
            club_id TEXT PRIMARY KEY,
            privkey TEXT NOT NULL,
            pubkey TEXT NOT NULL,
            club_key TEXT NOT NULL DEFAULT ''
        )
    """)
    try:
        db.con.execute("ALTER TABLE federation_keypair ADD COLUMN club_key TEXT NOT NULL DEFAULT ''")
    except Exception:
        pass



def _get_or_create_keypair(db, club_id: str) -> tuple[str, str, str]:
    row = db.con.execute(
        "SELECT privkey, pubkey, club_key FROM federation_keypair WHERE club_id=?", (club_id,)
    ).fetchone()
    if row:
        priv, pub, club_key = row
        if not club_key:
            club_key = crypto.generate_club_key()
            db.con.execute("UPDATE federation_keypair SET club_key=? WHERE club_id=?", (club_key, club_id))
        return priv, pub, club_key
    priv, pub = crypto.generate_keypair()
    club_key = crypto.generate_club_key()
    db.con.execute(
        "INSERT INTO federation_keypair (club_id, privkey, pubkey, club_key) VALUES (?,?,?,?)",
        (club_id, priv, pub, club_key)
    )
    return priv, pub, club_key


def _own_did(db) -> str:
    row = db.con.execute("SELECT did FROM user LIMIT 1").fetchone()
    return row[0] if row else ""


def _start_peer(app, ws, club_id, seed_url, membership, whitelist_pattern, own_pds_url=""):
    global _peer_runner, _peer_task
    db = app[MILLIPDS_DB]
    _ensure_tables(db)

    own_did = _own_did(db)
    if not own_pds_url:
        own_pds_url = db.config.get("pds_pfx", "")
    privkey, pubkey, club_key = _get_or_create_keypair(db, club_id)

    if _peer_task and not _peer_task.done():
        _peer_task.cancel()

    _peer_runner = FederationPeer(
        db_conn=db.con,
        own_did=own_did,
        own_privkey=privkey,
        own_pubkey=pubkey,
        own_pds_url=own_pds_url,
        club_key=club_key,
        club_id=club_id,
        seed_url=seed_url,
        membership=membership,
        whitelist_pattern=whitelist_pattern,
        on_new_record=_broadcast_club,
    )
    _peer_task = asyncio.create_task(_peer_runner.run())
    logger.info(f"[federation] started club={club_id} seed={seed_url}")


# ── Routes ────────────────────────────────────────────────────────────────────

@routes.get("/club")
async def club_feed(request: web.Request):
    sess = get_session(request)
    if not sess:
        raise web.HTTPFound(f"/login?next=/club")
    db = request.app[MILLIPDS_DB]
    _ensure_tables(db)
    ws = get_web_store(request)
    club_id = ws.get_plugin_setting(APP_NAME, "club_id", "mycrab")

    rows = db.con.execute(
        "SELECT cid, author_did, rkey, plaintext, created_at, indexed_at"
        " FROM federation_record WHERE club_id=? ORDER BY created_at DESC LIMIT 40",
        (club_id,)
    ).fetchall()

    posts = []
    for cid, author_did, rkey, plaintext, created_at, indexed_at in rows:
        # resolve handle from member table
        member = db.con.execute(
            "SELECT pds_url FROM federation_member WHERE did=? AND club_id=?",
            (author_did, club_id)
        ).fetchone()
        pds_url = member[0] if member else ""
        handle = author_did  # fallback
        if pds_url:
            host = pds_url.replace("https://","").replace("http://","").split("/")[0]
            handle = host

        try:
            ts = datetime.datetime.fromisoformat(created_at.replace("Z",""))
            ts_human = ts.strftime("%-d %b %Y, %H:%M")
        except Exception:
            ts_human = created_at

        try:
            pl = json.loads(plaintext)
            post_text = pl.get("text", plaintext)
            file_ref = pl.get("file", "")
            file_name = pl.get("file_name", "")
            mime = pl.get("mime", "")
        except Exception:
            post_text = plaintext
            file_ref = file_name = mime = ""

        posts.append({
            "cid": cid,
            "author_did": author_did,
            "handle": handle,
            "rkey": rkey,
            "text": post_text,
            "file_ref": file_ref,
            "file_name": file_name,
            "mime": mime,
            "ts_human": ts_human,
            "created_at": created_at,
        })

    members = db.con.execute(
        "SELECT did, pds_url FROM federation_member WHERE club_id=?", (club_id,)
    ).fetchall()

    own_did = sess["did"] if sess else ""
    return render(request, "plugin/federation/club.html", {
        "posts": posts,
        "club_id": club_id,
        "member_count": len(members),
        "running": _peer_task and not _peer_task.done(),
        "own_did": own_did,
    })


@routes.post("/club/post")
async def club_post(request: web.Request):
    """Publish an encrypted post to the club."""
    sess = get_session(request)
    if not sess:
        raise web.HTTPFound("/login")
    db = request.app[MILLIPDS_DB]
    _ensure_tables(db)
    ws = get_web_store(request)
    club_id = ws.get_plugin_setting(APP_NAME, "club_id", "mycrab")

    data = await request.post()
    text = data.get("text", "").strip()
    own_did = sess["did"]
    privkey, pubkey, club_key = _get_or_create_keypair(db, club_id)

    # Handle optional file upload
    file_ref = file_name = mime = ""
    file_field = data.get("file")
    if file_field and hasattr(file_field, "filename") and file_field.filename:
        raw = file_field.file.read()
        if raw:
            ext = os.path.splitext(file_field.filename)[1].lower() or ".bin"
            fname = f"{uuid.uuid4().hex}{ext}"
            files_dir = _get_club_files_dir(db)
            with open(os.path.join(files_dir, fname), "wb") as fh:
                fh.write(raw)
            file_ref = fname
            file_name = file_field.filename
            mime = file_field.content_type or mimetypes.guess_type(file_field.filename)[0] or "application/octet-stream"

    if not text and not file_ref:
        raise web.HTTPFound("/club")

    # Build plaintext payload (JSON for rich posts, plain string for text-only backward compat)
    pl: dict = {"text": text}
    if file_ref:
        pl["file"] = file_ref
        pl["file_name"] = file_name
        pl["mime"] = mime
    plaintext_str = json.dumps(pl)

    encrypted = crypto.encrypt(plaintext_str, club_key)
    created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    rkey = created_at.replace(":", "").replace("-", "").replace("+", "")[:17]

    record = {
        "$type": CLUB_NSID,
        "clubId": club_id,
        "ciphertext": encrypted["ciphertext"],
        "createdAt": created_at,
    }

    # Publish to local ATProto repo so firehose carries it to peers
    cfg = db.config
    access_token = _jwt.encode({
        "scope": "com.atproto.access",
        "aud": cfg.get("pds_did", ""),
        "sub": own_did,
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
        "jti": str(uuid.uuid4()),
    }, cfg.get("jwt_access_secret", ""), "HS256")

    # Use local URL for internal calls — pds_pfx may not resolve from inside VPS
    pds_local = ws.get_node_setting("pds_local_url") or cfg.get("pds_pfx", "")
    client = request.app[MILLIPDS_AIOHTTP_CLIENT]
    try:
        async with client.post(
            f"{pds_local}/xrpc/com.atproto.repo.putRecord",
            json={"repo": own_did, "collection": CLUB_NSID, "rkey": rkey, "record": record},
            headers={"Authorization": f"Bearer {access_token}"},
        ) as resp:
            resp_data = await resp.json()
            actual_cid = resp_data.get("cid", "")
            if resp.status in (200, 201) and actual_cid:
                db.con.execute(
                    "INSERT OR IGNORE INTO federation_record"
                    " (cid, author_did, rkey, club_id, plaintext, created_at, indexed_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (actual_cid, own_did, rkey, club_id, plaintext_str, created_at, created_at)
                )
                try:
                    ts = datetime.datetime.fromisoformat(created_at.replace("Z", ""))
                    ts_human = ts.strftime("%-d %b %Y, %H:%M")
                except Exception:
                    ts_human = created_at
                pds_host = cfg.get("pds_pfx", "").replace("https://", "").replace("http://", "").split("/")[0]
                _broadcast_club(club_id, {
                    "cid": actual_cid, "author_did": own_did,
                    "handle": pds_host or own_did, "rkey": rkey,
                    "text": text, "file_ref": file_ref,
                    "file_name": file_name, "mime": mime,
                    "ts_human": ts_human, "created_at": created_at,
                })
            else:
                logger.warning(f"putRecord {resp.status}: {resp_data}")
    except Exception as e:
        logger.error(f"putRecord error: {e}")

    raise web.HTTPFound("/club")


# ── Seed endpoint: join handshake ─────────────────────────────────────────────

@routes.post("/xrpc/space.mycrab.federation.join")
async def federation_join(request: web.Request):
    """
    Peer handshake endpoint. Joining node sends its identity.
    We validate membership, add to member list, return full member list.
    """
    db = request.app[MILLIPDS_DB]
    _ensure_tables(db)
    ws = get_web_store(request)
    club_id = ws.get_plugin_setting(APP_NAME, "club_id", "mycrab")
    membership = ws.get_plugin_setting(APP_NAME, "membership", "whitelist")
    whitelist_pattern = ws.get_plugin_setting(APP_NAME, "whitelist_pattern", "*.mycrab.space")

    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest()

    joining_did = data.get("did", "")
    joining_pubkey = data.get("pubkey", "")
    joining_pds = data.get("pds_url", str(request.url.origin()))
    req_club_id = data.get("club_id", "")

    if req_club_id != club_id:
        raise web.HTTPForbidden(reason="unknown club")

    # Membership check
    handle = joining_did  # simplified — could resolve handle via DID
    if membership == "whitelist":
        pat = whitelist_pattern.strip()
        allowed = False
        if pat.startswith("*."):
            suffix = "." + pat[2:]
            # Check if pds_url host matches
            host = joining_pds.replace("https://","").replace("http://","").split("/")[0]
            allowed = host.endswith(suffix) or host == pat[2:]
        elif pat:
            host = joining_pds.replace("https://","").replace("http://","").split("/")[0]
            allowed = host == pat or joining_did == pat
        if not allowed:
            raise web.HTTPForbidden(reason="not in whitelist")

    # Register joining node
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    db.con.execute(
        "INSERT OR REPLACE INTO federation_member (did, club_id, pubkey, pds_url, added_at)"
        " VALUES (?,?,?,?,?)",
        (joining_did, club_id, joining_pubkey, joining_pds, now)
    )
    # Also register peers they told us about
    for peer in data.get("peers", []):
        pdid, ppub, ppds = peer.get("did"), peer.get("pubkey"), peer.get("pds_url","")
        if pdid and ppub:
            db.con.execute(
                "INSERT OR IGNORE INTO federation_member (did, club_id, pubkey, pds_url, added_at)"
                " VALUES (?,?,?,?,?)",
                (pdid, club_id, ppub, ppds, now)
            )


    # Wake peer runner immediately so it subscribes to the new node
    if _peer_runner:
        _peer_runner.notify_new_member()

    # Fan-out: push new member info to all existing peers so they discover immediately
    fanout_payload = {
        "did": joining_did, "pubkey": joining_pubkey,
        "pds_url": joining_pds, "club_id": club_id, "peers": [],
    }
    client = request.app[MILLIPDS_AIOHTTP_CLIENT]
    existing = db.con.execute(
        "SELECT pds_url FROM federation_member WHERE club_id=? AND did!=? AND pds_url!=''",
        (club_id, joining_did)
    ).fetchall()
    for (peer_url,) in existing:
        if peer_url and peer_url.rstrip("/") != joining_pds.rstrip("/"):
            asyncio.create_task(_fanout_join(client, peer_url, fanout_payload))

    # Return full member list + club key (so joining node can decrypt posts)
    _, _, club_key = _get_or_create_keypair(db, club_id)
    members = db.con.execute(
        "SELECT did, pubkey, pds_url FROM federation_member WHERE club_id=?", (club_id,)
    ).fetchall()

    return web.json_response({
        "club_id": club_id,
        "club_key": club_key,
        "members": [{"did": r[0], "pubkey": r[1], "pds_url": r[2]} for r in members]
    })


@routes.get("/club/posts.json")
async def club_posts_json(request: web.Request):
    """Polling endpoint — returns latest posts as JSON for 5s fallback poll."""
    sess = get_session(request)
    if not sess:
        raise web.HTTPUnauthorized()
    db = request.app[MILLIPDS_DB]
    ws = get_web_store(request)
    club_id = ws.get_plugin_setting(APP_NAME, "club_id", "mycrab")
    since = request.rel_url.query.get("since", "")
    query = (
        "SELECT cid, author_did, rkey, plaintext, created_at FROM federation_record"
        " WHERE club_id=?" + (" AND created_at > ?" if since else "") +
        " ORDER BY created_at DESC LIMIT 40"
    )
    params = (club_id, since) if since else (club_id,)
    rows = db.con.execute(query, params).fetchall()
    own_did = sess["did"]
    posts = []
    for cid, author_did, rkey, plaintext, created_at in rows:
        member = db.con.execute(
            "SELECT pds_url FROM federation_member WHERE did=? AND club_id=?",
            (author_did, club_id)
        ).fetchone()
        pds_url = member[0] if member else ""
        handle = pds_url.replace("https://","").replace("http://","").split("/")[0] if pds_url else author_did
        try:
            ts = datetime.datetime.fromisoformat(created_at.replace("Z",""))
            ts_human = ts.strftime("%-d %b %Y, %H:%M")
        except Exception:
            ts_human = created_at
        try:
            pl = json.loads(plaintext)
            post_text = pl.get("text", plaintext)
            file_ref = pl.get("file", "")
            file_name = pl.get("file_name", "")
            mime = pl.get("mime", "")
        except Exception:
            post_text = plaintext; file_ref = file_name = mime = ""
        posts.append({"cid": cid, "author_did": author_did, "handle": handle,
                      "text": post_text, "file_ref": file_ref, "file_name": file_name,
                      "mime": mime, "ts_human": ts_human, "created_at": created_at})
    return web.json_response({"posts": posts})


@routes.get("/club/stream")
async def club_stream(request: web.Request):
    """SSE endpoint — pushes new posts to connected browsers in real time."""
    sess = get_session(request)
    if not sess:
        raise web.HTTPUnauthorized()
    db = request.app[MILLIPDS_DB]
    ws = get_web_store(request)
    club_id = ws.get_plugin_setting(APP_NAME, "club_id", "mycrab")

    queue: asyncio.Queue = asyncio.Queue(maxsize=50)
    _club_streams.setdefault(club_id, []).append(queue)
    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)
    try:
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=25)
                await resp.write(f"data: {json.dumps(payload)}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        streams = _club_streams.get(club_id, [])
        if queue in streams:
            streams.remove(queue)
    return resp


@routes.get("/club/file/{filename}")
async def club_file(request: web.Request):
    """Serve a club file attachment — gated to club members only."""
    sess = get_session(request)
    if not sess:
        raise web.HTTPUnauthorized()
    db = request.app[MILLIPDS_DB]
    _ensure_tables(db)
    ws = get_web_store(request)
    club_id = ws.get_plugin_setting(APP_NAME, "club_id", "mycrab")

    own_did = sess["did"]
    member = db.con.execute(
        "SELECT did FROM federation_member WHERE club_id=? AND did=?", (club_id, own_did)
    ).fetchone()
    if not member:
        raise web.HTTPForbidden()

    filename = request.match_info["filename"]
    if "/" in filename or ".." in filename:
        raise web.HTTPBadRequest()

    path = os.path.join(_get_club_files_dir(db), filename)
    if not os.path.isfile(path):
        raise web.HTTPNotFound()

    content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return web.FileResponse(path, headers={"Content-Type": content_type})


async def _start_peer_on_startup(app):
    ws = app[MILLIPDS_WEB_STORE]
    db = app[MILLIPDS_DB]
    club_id = ws.get_plugin_setting(APP_NAME, "club_id", "mycrab")
    seed_url = ws.get_plugin_setting(APP_NAME, "seed_url", "https://mypds.mycrab.space")
    membership = ws.get_plugin_setting(APP_NAME, "membership", "whitelist")
    whitelist_pattern = ws.get_plugin_setting(APP_NAME, "whitelist_pattern", "*.mycrab.space")
    own_pds_url = db.config.get("pds_pfx", "")
    _start_peer(app, ws, club_id, seed_url, membership, whitelist_pattern, own_pds_url)


if __name__ == "__main__":
    from mypds.plugin_runner import run_plugin
    run_plugin(routes, APP_NAME)
