from datetime import datetime, timezone
from aiohttp import web
import cbrrr
import json

from mypds.app_util import get_db
from mypds.web import render, get_session, get_web_store, get_node_profile

APP_NAME   = "connectedapps"
NSID       = None  # no ATProto records — internal tracking only
URL_PREFIX = "/connected-apps"

routes = web.RouteTableDef()

_BSKY_PREFIXES = ("app.bsky.", "chat.bsky.")
_TID_CHARSET   = "234567abcdefghijklmnopqrstuvwxyz"


def _infer_domain(nsid: str, domains: list) -> str | None:
    """Heuristic: find which domain likely wrote this NSID by matching name parts."""
    nsid_parts = set(nsid.split("."))
    for domain in domains:
        clean = domain.replace("api.", "").replace("www.", "")
        significant = [p for p in clean.split(".") if len(p) > 3]
        if any(p in nsid_parts for p in significant):
            return domain
    return None


def _tid_to_unix(tid: str) -> int:
    n = 0
    for c in tid.lower():
        idx = _TID_CHARSET.find(c)
        if idx >= 0:
            n = n * 32 + idx
    return (n >> 10) // 1_000_000


def _format_time(created_at: str, rkey: str) -> str:
    if created_at:
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    if len(rkey) == 13 and all(c in _TID_CHARSET for c in rkey.lower()):
        ts = _tid_to_unix(rkey)
        if ts > 1_000_000_000:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            return dt.strftime("%Y-%m-%d %H:%M")
    return ""


def _record_preview(decoded: dict) -> str:
    for key in ("text", "title", "name", "description", "summary", "content"):
        val = decoded.get(key)
        if isinstance(val, str) and val.strip():
            return val[:120]
    return ""


def _get_record_nsids(db, did: str) -> set:
    row = db.con.execute("SELECT id FROM user WHERE did=?", (did,)).fetchone()
    if not row:
        return set()
    rows = db.con.execute(
        "SELECT DISTINCT nsid FROM record WHERE repo=?", (row[0],)
    ).fetchall()
    return {r[0] for r in rows if not any(r[0].startswith(p) for p in _BSKY_PREFIXES)}


def _get_app_timeline(db, did: str, record_nsids: list) -> list:
    """Get records for a specific app's attributed NSIDs, newest first."""
    if not record_nsids:
        return []
    row = db.con.execute("SELECT id FROM user WHERE did=?", (did,)).fetchone()
    if not row:
        return []
    user_id = row[0]
    placeholders = ",".join("?" * len(record_nsids))
    rows = db.con.execute(
        f"SELECT nsid, rkey, cid, value FROM record WHERE repo=? AND nsid IN ({placeholders}) ORDER BY rkey DESC",
        (user_id, *record_nsids),
    ).fetchall()
    entries = []
    for nsid, rkey, cid, value in rows:
        try:
            decoded = cbrrr.decode_dag_cbor(value, atjson_mode=True)
        except Exception:
            decoded = {}
        cid_str = cbrrr.CID(cid).encode() if cid else None
        created_at = decoded.get("createdAt", "")
        parts = nsid.rsplit(".", 1)
        entries.append({
            "nsid": nsid,
            "ns": parts[0] + "." if len(parts) > 1 else "",
            "leaf": parts[1] if len(parts) > 1 else nsid,
            "rkey": rkey,
            "cid": cid_str,
            "time_str": _format_time(created_at, rkey),
            "preview": _record_preview(decoded),
        })
    return entries


@routes.get("/connected-apps")
async def connected_apps_page(request: web.Request):
    session = get_session(request)
    if not session:
        raise web.HTTPFound("/login?next=/connected-apps")
    db = get_db(request)
    ws = get_web_store(request)
    profile = get_node_profile(db)

    local_nsids = _get_record_nsids(db, profile["did"]) if profile.get("did") else set()
    apps = ws.get_app_logins()
    domains = [app["domain"] for app in apps]

    domain_records: dict = {}
    for nsid in local_nsids:
        domain = _infer_domain(nsid, domains)
        if domain:
            domain_records.setdefault(domain, []).append(nsid)

    for app in apps:
        app["record_nsids"] = sorted(domain_records.get(app["domain"], []))

    return render(request, "plugin/connectedapps/main.html", {
        "connected_apps": apps,
        "has_records": bool(local_nsids),
    })


@routes.get("/connected-apps/{domain:.*}")
async def connected_app_detail(request: web.Request):
    session = get_session(request)
    if not session:
        raise web.HTTPFound("/login")
    domain = request.match_info["domain"]
    db = get_db(request)
    ws = get_web_store(request)
    profile = get_node_profile(db)
    did = profile.get("did", "")

    apps = ws.get_app_logins()
    app = next((a for a in apps if a["domain"] == domain), None)
    if not app:
        raise web.HTTPNotFound()

    local_nsids = _get_record_nsids(db, did) if did else set()
    domains = [a["domain"] for a in apps]
    domain_records: dict = {}
    for nsid in local_nsids:
        d = _infer_domain(nsid, domains)
        if d:
            domain_records.setdefault(d, []).append(nsid)

    record_nsids = sorted(domain_records.get(domain, []))
    timeline = _get_app_timeline(db, did, record_nsids)

    display = domain.split("api.", 1)[1] if domain.startswith("api.") else domain
    return render(request, "plugin/connectedapps/detail.html", {
        "app": app,
        "display": display,
        "did": did,
        "record_nsids": record_nsids,
        "timeline": timeline,
    })


if __name__ == "__main__":
    from mypds.plugin_runner import run_plugin
    run_plugin(routes, APP_NAME)
