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

from aiohttp import web

from mypds.app_util import MILLIPDS_DB
from mypds.web import render, get_session, get_web_store, MILLIPDS_WEB_STORE

from . import crypto
from .peer import FederationPeer, CLUB_NSID

APP_NAME = "federation"
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
            pubkey TEXT NOT NULL
        )
    """)



def _get_or_create_keypair(db, club_id: str) -> tuple[str, str]:
    row = db.con.execute(
        "SELECT privkey, pubkey FROM federation_keypair WHERE club_id=?", (club_id,)
    ).fetchone()
    if row:
        return row[0], row[1]
    priv, pub = crypto.generate_keypair()
    db.con.execute(
        "INSERT INTO federation_keypair (club_id, privkey, pubkey) VALUES (?,?,?)",
        (club_id, priv, pub)
    )

    return priv, pub


def _start_peer(app, ws, club_id, seed_url, membership, whitelist_pattern, own_pds_url=""):
    global _peer_runner, _peer_task
    db = app[MILLIPDS_DB]
    _ensure_tables(db)

    own_did = db.config.get("did", "")
    if not own_pds_url:
        own_pds_url = db.config.get("pds_pfx", "")
    privkey, pubkey = _get_or_create_keypair(db, club_id)

    if _peer_task and not _peer_task.done():
        _peer_task.cancel()

    _peer_runner = FederationPeer(
        db_conn=db.con,
        own_did=own_did,
        own_privkey=privkey,
        own_pubkey=pubkey,
        own_pds_url=own_pds_url,
        club_id=club_id,
        seed_url=seed_url,
        membership=membership,
        whitelist_pattern=whitelist_pattern,
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

        posts.append({
            "cid": cid,
            "author_did": author_did,
            "handle": handle,
            "rkey": rkey,
            "text": plaintext,
            "ts_human": ts_human,
            "created_at": created_at,
        })

    members = db.con.execute(
        "SELECT did, pds_url FROM federation_member WHERE club_id=?", (club_id,)
    ).fetchall()

    return render(request, "plugin/federation/club.html", {
        "posts": posts,
        "club_id": club_id,
        "member_count": len(members),
        "running": _peer_task and not _peer_task.done(),
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
    if not text:
        raise web.HTTPFound("/club")

    own_did = db.config.get("did", "")
    privkey, pubkey = _get_or_create_keypair(db, club_id)

    members = db.con.execute(
        "SELECT did, pubkey FROM federation_member WHERE club_id=?", (club_id,)
    ).fetchall()
    member_map = {r[0]: r[1] for r in members}
    if own_did not in member_map:
        member_map[own_did] = pubkey

    encrypted = crypto.encrypt(text, member_map)
    created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    record = {
        "$type": CLUB_NSID,
        "clubId": club_id,
        "ciphertext": encrypted["ciphertext"],
        "keys": encrypted["keys"],
        "createdAt": created_at,
    }

    # Store locally — round-trip through decrypt to verify keys are correct
    plaintext_check = crypto.decrypt(record, own_did, privkey)
    if plaintext_check:
        import hashlib
        pseudo_cid = "local-" + hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()[:16]
        db.con.execute(
            "INSERT OR IGNORE INTO federation_record"
            " (cid, author_did, rkey, club_id, plaintext, created_at, indexed_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (pseudo_cid, own_did, created_at.replace(":","").replace("-","")[:13],
             club_id, text, created_at, created_at)
        )

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


    # Return full member list
    members = db.con.execute(
        "SELECT did, pubkey, pds_url FROM federation_member WHERE club_id=?", (club_id,)
    ).fetchall()

    return web.json_response({
        "club_id": club_id,
        "members": [{"did": r[0], "pubkey": r[1], "pds_url": r[2]} for r in members]
    })


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
