from datetime import datetime, timezone
from aiohttp import web
import cbrrr
import json

from mypds.app_util import get_db
from mypds.web import render, get_session, get_web_store, get_node_profile

APP_NAME = "repobrowser"
NSID     = None  # read-only browser, no records written

routes = web.RouteTableDef()

_BSKY_PREFIXES = ("app.bsky.", "chat.bsky.")

_TID_CHARSET = "234567abcdefghijklmnopqrstuvwxyz"


def _tid_to_unix(tid: str) -> int:
    """Decode ATProto TID (base32-sortable) to Unix timestamp (seconds)."""
    n = 0
    for c in tid.lower():
        idx = _TID_CHARSET.find(c)
        if idx >= 0:
            n = n * 32 + idx
    return (n >> 10) // 1_000_000


def _format_time(created_at: str, rkey: str) -> str:
    """Return a human-readable 'YYYY-MM-DD HH:MM' string for a record."""
    if created_at:
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    # Fall back to TID-decoded time (only valid for 13-char TID rkeys)
    if len(rkey) == 13 and all(c in _TID_CHARSET for c in rkey.lower()):
        ts = _tid_to_unix(rkey)
        if ts > 1_000_000_000:  # sanity check: after year 2001
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            return dt.strftime("%Y-%m-%d %H:%M")
    return ""


def _infer_domain(nsid: str, domains: list) -> str | None:
    nsid_parts = set(nsid.split("."))
    for domain in domains:
        clean = domain.replace("api.", "").replace("www.", "")
        significant = [p for p in clean.split(".") if len(p) > 3]
        if any(p in nsid_parts for p in significant):
            return domain
    return None


def _record_preview(decoded: dict) -> str:
    """Extract a short human-readable preview from decoded record."""
    for key in ("text", "title", "name", "description", "summary", "content"):
        val = decoded.get(key)
        if isinstance(val, str) and val.strip():
            return val[:120]
    return ""


def _get_collections(db, did: str, ws) -> list:
    user_row = db.con.execute("SELECT id FROM user WHERE did=?", (did,)).fetchone()
    if not user_row:
        return []
    user_id = user_row[0]
    rows = db.con.execute(
        "SELECT nsid, COUNT(*) as cnt, MAX(since) as last_updated "
        "FROM record WHERE repo=? GROUP BY nsid ORDER BY last_updated DESC",
        (user_id,),
    ).fetchall()

    logins = ws.get_app_logins()
    domains = [a["domain"] for a in logins]
    domain_info = {a["domain"]: a for a in logins}

    result = []
    for r in rows:
        nsid = r[0]
        if any(nsid.startswith(p) for p in _BSKY_PREFIXES):
            continue
        parts = nsid.rsplit(".", 1)
        domain = _infer_domain(nsid, domains)
        source = domain_info.get(domain) if domain else None
        result.append({
            "nsid": nsid,
            "ns": parts[0] + "." if len(parts) > 1 else "",
            "leaf": parts[1] if len(parts) > 1 else nsid,
            "count": r[1],
            "last_updated": r[2],
            "domain": domain,
            "client_url": source.get("client_url") if source else None,
        })
    return result


def _get_records(db, did: str, nsid: str) -> list:
    user_row = db.con.execute("SELECT id FROM user WHERE did=?", (did,)).fetchone()
    if not user_row:
        return []
    user_id = user_row[0]
    rows = db.con.execute(
        "SELECT rkey, cid, value FROM record WHERE repo=? AND nsid=? ORDER BY rkey DESC",
        (user_id, nsid),
    ).fetchall()
    records = []
    for rkey, cid, value in rows:
        try:
            decoded = cbrrr.decode_dag_cbor(value, atjson_mode=True)
            data_json = json.dumps(decoded, indent=2, default=str)
        except Exception as e:
            decoded = {}
            data_json = f"// decode error: {e}"
        cid_str = cbrrr.CID(cid).encode() if cid else None
        records.append({
            "rkey": rkey,
            "cid": cid_str,
            "data": decoded,
            "data_json": data_json,
        })
    return records


def _get_timeline(db, did: str, ws, domain_filter: str = "") -> list:
    user_row = db.con.execute("SELECT id FROM user WHERE did=?", (did,)).fetchone()
    if not user_row:
        return []
    user_id = user_row[0]
    rows = db.con.execute(
        "SELECT nsid, rkey, cid, value FROM record WHERE repo=? ORDER BY rkey DESC",
        (user_id,),
    ).fetchall()

    logins = ws.get_app_logins()
    domains = [a["domain"] for a in logins]
    domain_info = {a["domain"]: a for a in logins}

    entries = []
    for nsid, rkey, cid, value in rows:
        if any(nsid.startswith(p) for p in _BSKY_PREFIXES):
            continue
        domain = _infer_domain(nsid, domains)
        if domain_filter and domain != domain_filter:
            continue
        try:
            decoded = cbrrr.decode_dag_cbor(value, atjson_mode=True)
        except Exception:
            decoded = {}
        cid_str = cbrrr.CID(cid).encode() if cid else None
        created_at = decoded.get("createdAt", "")
        parts = nsid.rsplit(".", 1)
        source = domain_info.get(domain) if domain else None
        entries.append({
            "nsid": nsid,
            "ns": parts[0] + "." if len(parts) > 1 else "",
            "leaf": parts[1] if len(parts) > 1 else nsid,
            "rkey": rkey,
            "cid": cid_str,
            "time_str": _format_time(created_at, rkey),
            "domain": domain,
            "client_url": source.get("client_url") if source else None,
            "preview": _record_preview(decoded),
        })
    return entries


@routes.get("/repo-browser")
async def repobrowser_page(request: web.Request):
    session = get_session(request)
    if not session:
        raise web.HTTPFound("/login?next=/repo-browser")
    db = get_db(request)
    ws = get_web_store(request)
    profile = get_node_profile(db)
    collections = _get_collections(db, profile["did"], ws) if profile.get("did") else []
    return render(request, "plugin/repobrowser/main.html", {
        "collections": collections,
        "did": profile.get("did", ""),
    })


@routes.get("/repo-browser/timeline")
async def repobrowser_timeline(request: web.Request):
    session = get_session(request)
    if not session:
        raise web.HTTPFound("/login")
    domain_filter = request.rel_url.query.get("domain", "")
    db = get_db(request)
    ws = get_web_store(request)
    profile = get_node_profile(db)
    did = profile.get("did", "")
    entries = _get_timeline(db, did, ws, domain_filter) if did else []
    return render(request, "plugin/repobrowser/timeline.html", {
        "entries": entries,
        "did": did,
        "domain_filter": domain_filter,
    })


@routes.get("/repo-browser/{nsid:.*}")
async def repobrowser_collection(request: web.Request):
    session = get_session(request)
    if not session:
        raise web.HTTPFound("/login")
    nsid = request.match_info["nsid"]
    db = get_db(request)
    ws = get_web_store(request)
    profile = get_node_profile(db)
    did = profile.get("did", "")
    records = _get_records(db, did, nsid) if did else []
    logins = ws.get_app_logins()
    domains = [a["domain"] for a in logins]
    domain = _infer_domain(nsid, domains)
    domain_info = {a["domain"]: a for a in logins}
    source = domain_info.get(domain) if domain else None
    parts = nsid.rsplit(".", 1)
    return render(request, "plugin/repobrowser/collection.html", {
        "nsid": nsid,
        "ns": parts[0] + "." if len(parts) > 1 else "",
        "leaf": parts[1] if len(parts) > 1 else nsid,
        "records": records,
        "did": did,
        "domain": domain,
        "client_url": source.get("client_url") if source else None,
    })
