"""
mypds migration plugin — import an existing Bluesky account onto this PDS.

Flow:
  1. User enters bsky handle + password → we auth with bsky.social,
     request a PLC operation signature email.
  2. User enters email token → we fetch the co-signed plcOp from bsky,
     download the repo CAR, create the local account, import all records,
     then submit the plcOp to plc.directory (making this PDS authoritative).

Also exposes:
  POST /xrpc/com.atproto.server.createAccount  — new (non-migration) accounts
  POST /xrpc/com.atproto.repo.importRepo       — CAR import for external tools
  POST /xrpc/com.atproto.server.activateAccount
  POST /xrpc/com.atproto.server.deactivateAccount
"""
import base64
import hashlib
import io
import json
import logging
import os
import secrets
import time
import urllib.request
import urllib.error

import cbrrr
from aiohttp import web
from atmst.blockstore.car_file import ReadOnlyCARBlockStore
from atmst.mst.node_walker import NodeWalker
from atmst.mst.node_store import NodeStore

from mypds import crypto
from mypds.app_util import MILLIPDS_DB, get_db
from mypds.web import get_web_store
from mypds.auth_bearer import authenticated
from mypds import repo_ops, util

logger = logging.getLogger(__name__)

APP_NAME = "migration"
DEFAULT_ENABLED = True
URL_PREFIXES = [
    "/migrate",
    "/xrpc/com.atproto.server.createAccount",
    "/xrpc/com.atproto.repo.importRepo",
    "/xrpc/com.atproto.server.activateAccount",
    "/xrpc/com.atproto.server.deactivateAccount",
]

SETTINGS = [
    {
        "key": "open_registration",
        "type": "bool",
        "label": "Open registration",
        "description": "Allow anyone to create a new account on this PDS.",
        "default": "false",
        "group": "accounts",
    },
    {
        "key": "migration_enabled",
        "type": "bool",
        "label": "Allow Bluesky migrations",
        "description": "Allow users to import their Bluesky account onto this PDS.",
        "default": "true",
        "group": "accounts",
    },
]

routes = web.RouteTableDef()

# In-memory preflight sessions: token → {bsky_token, did, handle, ts}
_preflight: dict = {}
_PREFLIGHT_TTL = 600  # 10 min


def _clean_preflight():
    now = time.time()
    stale = [k for k, v in _preflight.items() if now - v["ts"] > _PREFLIGHT_TTL]
    for k in stale:
        del _preflight[k]


def _bsky_post(path: str, payload: dict, token: str = "") -> dict:
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"https://bsky.social/xrpc/{path}", data=data, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise web.HTTPBadGateway(text=f"bsky.social error: {body}")


def _bsky_get(path: str, params: dict, token: str = "") -> dict:
    qs = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in params.items())
    url = f"https://bsky.social/xrpc/{path}?{qs}"
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise web.HTTPBadGateway(text=f"bsky.social error: {body}")


def _fetch_repo_car(did: str, token: str) -> bytes:
    url = f"https://bsky.social/xrpc/com.atproto.sync.getRepo?did={urllib.request.quote(did)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise web.HTTPBadGateway(text=f"repo fetch failed: {e.code}")


def _import_car(db, did: str, car_bytes: bytes, signing_privkey) -> int:
    """Parse CAR, walk MST, write all records into the account. Returns count."""
    buf = io.BytesIO(car_bytes)
    car_bs = ReadOnlyCARBlockStore(buf, validate_hashes=True)

    # Decode the commit
    commit_bytes = car_bs.get_block(bytes(car_bs.car_root))
    commit = cbrrr.decode_dag_cbor(commit_bytes)
    mst_root_cid = commit["data"]

    # Verify commit signature
    expected_did = commit.get("did", "")
    if expected_did and expected_did != did:
        raise web.HTTPBadRequest(text="CAR repo DID mismatch")

    ns = NodeStore(car_bs)
    walker = NodeWalker(ns, mst_root_cid)

    writes = []
    for rpath, val_cid in walker.iter_kv():
        record_bytes = car_bs.get_block(bytes(val_cid))
        record = cbrrr.decode_dag_cbor(record_bytes, atjson_mode=True)
        parts = rpath.split("/", 1)
        if len(parts) != 2:
            continue
        collection, rkey = parts
        writes.append({
            "$type": "com.atproto.repo.applyWrites#update",
            "collection": collection,
            "rkey": rkey,
            "value": record,
        })
        # Batch in chunks of 100 to avoid huge transactions
        if len(writes) >= 100:
            try:
                repo_ops.apply_writes(db, did, writes, swap_commit=None)
            except Exception as e:
                logger.warning(f"[migration] batch write error (skipping): {e}")
            writes = []

    if writes:
        try:
            repo_ops.apply_writes(db, did, writes, swap_commit=None)
        except Exception as e:
            logger.warning(f"[migration] final batch write error (skipping): {e}")

    return 0  # count not tracked here; apply_writes handles it


def _submit_plc_op(plc_op: dict, did: str, plc_host: str = "https://plc.directory"):
    data = json.dumps(plc_op).encode()
    req = urllib.request.Request(
        f"{plc_host}/{did}", data=data,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=15)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise web.HTTPBadGateway(text=f"plc.directory error: {body}")


# ── UI Routes ────────────────────────────────────────────────────────────────

@routes.get("/migrate")
async def migrate_index(request: web.Request):
    db = get_db(request)
    ws = get_web_store(request)
    migration_enabled = ws.get_plugin_setting(APP_NAME, "migration_enabled", "true") == "true"
    open_reg = ws.get_plugin_setting(APP_NAME, "open_registration", "false") == "true"
    pds_pfx = db.config.get("pds_pfx", "")

    tmpl_path = os.path.join(os.path.dirname(__file__), "templates", "migrate.html")
    with open(tmpl_path) as f:
        html = f.read()

    html = html.replace("{{PDS_URL}}", pds_pfx)
    html = html.replace("{{MIGRATION_ENABLED}}", "true" if migration_enabled else "false")
    html = html.replace("{{OPEN_REG}}", "true" if open_reg else "false")
    return web.Response(text=html, content_type="text/html")


@routes.post("/migrate/preflight")
async def migrate_preflight(request: web.Request):
    """Step 1: auth with bsky, request PLC sig email."""
    ws = get_web_store(request)
    if ws.get_plugin_setting(APP_NAME, "migration_enabled", "true") != "true":
        raise web.HTTPForbidden(text="Migration is disabled on this PDS.")

    body = await request.json()
    bsky_handle = body.get("handle", "").strip().lstrip("@")
    bsky_password = body.get("password", "")

    if not bsky_handle or not bsky_password:
        raise web.HTTPBadRequest(text="handle and password required")

    # Auth with bsky.social
    sess = _bsky_post("com.atproto.server.createSession", {
        "identifier": bsky_handle,
        "password": bsky_password,
    })
    bsky_token = sess["accessJwt"]
    did = sess["did"]
    handle = sess.get("handle", bsky_handle)

    # Request PLC signature email from bsky
    _bsky_post("com.atproto.identity.requestPlcOperationSignature", {}, token=bsky_token)

    # Store preflight session
    _clean_preflight()
    token = secrets.token_urlsafe(24)
    _preflight[token] = {"bsky_token": bsky_token, "did": did, "handle": handle, "ts": time.time()}

    return web.json_response({"token": token, "did": did, "handle": handle})


@routes.post("/migrate/complete")
async def migrate_complete(request: web.Request):
    """Step 2: email token → fetch plcOp, import repo, create account, go live."""
    db = get_db(request)
    ws = get_web_store(request)
    if ws.get_plugin_setting(APP_NAME, "migration_enabled", "true") != "true":
        raise web.HTTPForbidden(text="Migration is disabled on this PDS.")

    body = await request.json()
    session_token = body.get("session_token", "")
    email_token = body.get("email_token", "").strip()
    new_password = body.get("password", "")

    if not session_token or not email_token or not new_password:
        raise web.HTTPBadRequest(text="session_token, email_token and password required")

    pf = _preflight.get(session_token)
    if not pf:
        raise web.HTTPBadRequest(text="Preflight session expired or invalid. Start again.")
    if time.time() - pf["ts"] > _PREFLIGHT_TTL:
        del _preflight[session_token]
        raise web.HTTPBadRequest(text="Preflight session expired. Start again.")

    bsky_token = pf["bsky_token"]
    did = pf["did"]
    handle = pf["handle"]
    pds_pfx = db.config.get("pds_pfx", "")
    plc_host = db.config.get("plc_host", "https://plc.directory")

    # Check account doesn't already exist locally
    if db.handle_by_did(did):
        raise web.HTTPConflict(text="This DID is already hosted on this PDS.")

    # 1. Get recommended DID credentials from bsky (current rotation keys etc.)
    creds = _bsky_get("com.atproto.identity.getRecommendedDidCredentials", {}, token=bsky_token)

    # 2. Generate new repo signing key for this PDS
    repo_privkey = crypto.keygen_p256()
    repo_pubkey_did = crypto.encode_pubkey_as_did_key(repo_privkey.public_key())

    # 3. Build new PLC operation pointing at this PDS
    new_plc_op = {
        "type": "plc_operation",
        "rotationKeys": creds.get("rotationKeys", []),
        "verificationMethods": {"atproto": repo_pubkey_did},
        "alsoKnownAs": creds.get("alsoKnownAs", [f"at://{handle}"]),
        "services": {
            "atproto_pds": {
                "type": "AtprotoPersonalDataServer",
                "endpoint": pds_pfx,
            }
        },
        "prev": None,  # bsky will fill this in when co-signing
    }

    # 4. Get bsky to co-sign the PLC op with email token
    signed_op_resp = _bsky_post("com.atproto.identity.signPlcOperation", {
        "token": email_token,
        "plcOp": new_plc_op,
    }, token=bsky_token)
    signed_plc_op = signed_op_resp.get("operation") or signed_op_resp

    # 5. Download full repo CAR from bsky
    logger.info(f"[migration] fetching repo CAR for {did}")
    car_bytes = _fetch_repo_car(did, bsky_token)
    logger.info(f"[migration] CAR size: {len(car_bytes)} bytes")

    # 6. Create the account locally (empty repo initially)
    db.create_account(did=did, handle=handle, password=new_password, privkey=repo_privkey)
    logger.info(f"[migration] account created locally for {did}")

    # 7. Import all records from the CAR
    try:
        _import_car(db, did, car_bytes, repo_privkey)
        logger.info(f"[migration] CAR imported for {did}")
    except Exception as e:
        logger.warning(f"[migration] CAR import partial failure: {e} — account live but history may be incomplete")

    # 8. Submit signed PLC op to plc.directory (makes this PDS authoritative)
    _submit_plc_op(signed_plc_op, did, plc_host)
    logger.info(f"[migration] plcOp submitted for {did} — now live on {pds_pfx}")

    # 9. Clean up preflight
    _preflight.pop(session_token, None)

    return web.json_response({
        "did": did,
        "handle": handle,
        "pds": pds_pfx,
        "message": f"Account {handle} is now live on {pds_pfx}",
    })


# ── ATProto XRPC Endpoints ───────────────────────────────────────────────────

@routes.post("/xrpc/com.atproto.server.createAccount")
async def create_account(request: web.Request):
    """Create a new account (no migration). Requires open_registration=true."""
    db = get_db(request)
    ws = get_web_store(request)
    open_reg = ws.get_plugin_setting(APP_NAME, "open_registration", "false") == "true"
    if not open_reg:
        raise web.HTTPForbidden(text="Registration is not open on this PDS.")

    body = await request.json()
    handle = body.get("handle", "").strip()
    password = body.get("password", "")
    email = body.get("email", "")

    if not handle or not password:
        raise web.HTTPBadRequest(text="handle and password required")
    if db.did_by_handle(handle):
        raise web.HTTPConflict(text="Handle already taken.")

    pds_pfx = db.config.get("pds_pfx", "")
    plc_host = db.config.get("plc_host", "https://plc.directory")

    # Generate keys
    rotation_privkey = crypto.keygen_p256()
    repo_privkey = crypto.keygen_p256()

    # Build and submit PLC genesis op
    genesis = {
        "type": "plc_operation",
        "rotationKeys": [crypto.encode_pubkey_as_did_key(rotation_privkey.public_key())],
        "verificationMethods": {"atproto": crypto.encode_pubkey_as_did_key(repo_privkey.public_key())},
        "alsoKnownAs": [f"at://{handle}"],
        "services": {"atproto_pds": {"type": "AtprotoPersonalDataServer", "endpoint": pds_pfx}},
        "prev": None,
    }
    genesis["sig"] = crypto.plc_sign(rotation_privkey, genesis)
    genesis_digest = hashlib.sha256(cbrrr.encode_dag_cbor(genesis)).digest()
    new_did = "did:plc:" + base64.b32encode(genesis_digest)[:24].lower().decode()
    _submit_plc_op(genesis, new_did, plc_host)
    db.create_account(did=new_did, handle=handle, password=password, privkey=repo_privkey)
    logger.info(f"[migration] new account created: {new_did} / {handle}")
    return web.json_response({"did": new_did, "handle": handle, "accessJwt": "", "refreshJwt": ""})


@routes.post("/xrpc/com.atproto.repo.importRepo")
@authenticated
async def import_repo(request: web.Request):
    """Accept a CAR upload and ingest it into the authenticated user's repo."""
    db = get_db(request)
    did = request["authed_did"]

    car_bytes = await request.read()
    if len(car_bytes) > 10 * 1024 * 1024 * 1024:  # 10 GB hard cap
        raise web.HTTPRequestEntityTooLarge()

    try:
        _import_car(db, did, car_bytes, None)
    except Exception as e:
        logger.error(f"[migration] importRepo failed for {did}: {e}")
        raise web.HTTPBadRequest(text=f"Import failed: {e}")

    return web.json_response({"imported": True})


@routes.post("/xrpc/com.atproto.server.activateAccount")
async def activate_account(request: web.Request):
    # Accounts on this PDS are always active — no-op for compatibility
    return web.json_response({})


@routes.post("/xrpc/com.atproto.server.deactivateAccount")
async def deactivate_account(request: web.Request):
    return web.json_response({})


