from collections import defaultdict
from datetime import datetime, timezone, timedelta
from aiohttp import web
import cbrrr

from mypds.app_util import get_db
from mypds.web import render, get_node_profile, get_session, get_web_store

APP_NAME = "activity"
NSID     = None

SETTINGS = [
    {
        "key": "public_page",
        "type": "bool",
        "label": "Public page",
        "description": "Allow visitors to see /activity without logging in. Also adds a link in the site navigation.",
        "default": "0",
        "group": "visibility",
    },
    {
        "key": "feed_limit",
        "type": "select",
        "label": "Feed size",
        "description": "Number of records shown in the recent records feed.",
        "default": "60",
        "options": [("20", "20 records"), ("40", "40 records"), ("60", "60 records"), ("100", "100 records")],
        "group": "feed",
    },
    {
        "key": "heatmap_weeks",
        "type": "select",
        "label": "Heatmap range",
        "description": "How far back the activity heatmap shows.",
        "default": "52",
        "options": [("26", "6 months"), ("52", "1 year"), ("104", "2 years")],
        "group": "feed",
    },
]

_TID_CHARSET = "234567abcdefghijklmnopqrstuvwxyz"


def _tid_to_dt(rkey: str):
    if len(rkey) != 13:
        return None
    rk = rkey.lower()
    if not all(c in _TID_CHARSET for c in rk):
        return None
    n = 0
    for c in rk:
        n = n * 32 + _TID_CHARSET.index(c)
    us = n >> 10
    ts = us / 1_000_000
    if ts < 1_000_000_000:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _tid_to_date(rkey: str):
    dt = _tid_to_dt(rkey)
    return dt.date() if dt else None


def _nsid_domain(nsid: str) -> str:
    """Reverse first two NSID segments to get the origin domain."""
    parts = nsid.split(".")
    if len(parts) < 2:
        return nsid
    tld, name = parts[0], parts[1]
    if tld == "pub":          # our own pub.* NSIDs — local
        return "local"
    return f"{name}.{tld}"


def _external_url(nsid: str, rkey: str, did: str, handle: str, rec: dict) -> str | None:
    """Return a hotlink to the published content, or None."""
    if nsid == "app.bsky.feed.post":
        return f"https://bsky.app/profile/{did}/post/{rkey}"
    if nsid == "com.whtwnd.blog.entry":
        return f"https://whtwnd.com/@{handle}/{rkey}"
    if nsid == "sh.tangled.repo":
        name = rec.get("name", "")
        if name:
            return f"https://tangled.sh/@{handle}/{name}"
    if nsid == "wiki.lichen.wiki":
        slug = rec.get("name", "") or rkey
        return f"https://lichen.wiki/@{handle}/{slug}"
    if nsid == "blog.pckt.publication":
        return f"https://pckt.blog/@{handle}"
    if nsid == "site.standard.publication":
        return f"https://frontpage.fyi/post/{rkey}"
    return None


def _extract(nsid: str, rec: dict) -> dict:
    kind = nsid.split(".")[-1]
    text = None
    title = None
    ref = None

    subject = rec.get("subject")
    if isinstance(subject, dict):
        ref = subject.get("uri", "")
    elif isinstance(subject, str):
        ref = subject

    for key in ("text", "content", "body"):
        val = rec.get(key, "")
        if val:
            text = str(val)[:200]
            break

    for key in ("title", "name"):
        val = rec.get(key, "")
        if val:
            title = str(val)[:120]
            break

    return {"nsid": nsid, "kind": kind, "text": text, "title": title, "ref": ref}


def _get_recent_records(db, did: str, handle: str, limit: int = 60) -> list:
    row = db.con.execute("SELECT id FROM user WHERE did=?", (did,)).fetchone()
    if not row:
        return []
    rows = db.con.execute(
        "SELECT nsid, rkey, value FROM record WHERE repo=? ORDER BY rkey DESC LIMIT ?",
        (row[0], limit),
    ).fetchall()
    out = []
    for nsid, rkey, value in rows:
        try:
            rec = cbrrr.decode_dag_cbor(value)
            if not isinstance(rec, dict):
                rec = {}
        except Exception:
            rec = {}
        dt = _tid_to_dt(rkey)
        entry = _extract(nsid, rec)
        entry["rkey"]   = rkey
        entry["ts"]     = dt.strftime("%Y-%m-%d %H:%M") if dt else ""
        entry["domain"] = _nsid_domain(nsid)
        entry["url"]    = _external_url(nsid, rkey, did, handle, rec)
        out.append(entry)
    return out


def _get_activity(db, did: str) -> dict:
    row = db.con.execute("SELECT id FROM user WHERE did=?", (did,)).fetchone()
    if not row:
        return {}
    rows = db.con.execute("SELECT rkey FROM record WHERE repo=?", (row[0],)).fetchall()
    counts = defaultdict(int)
    for (rkey,) in rows:
        d = _tid_to_date(rkey)
        if d:
            counts[d.isoformat()] += 1
    return dict(counts)


def _day_level(count: int) -> int:
    if count == 0:  return 0
    if count <= 2:  return 1
    if count <= 5:  return 2
    if count <= 9:  return 3
    return 4


def _build_grid(activity: dict, weeks: int = 52) -> list[list[dict]]:
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(weeks=weeks)
    start = start - timedelta(days=start.weekday())
    grid = []
    cur = start
    while cur <= today + timedelta(days=6 - today.weekday()):
        week = []
        for _ in range(7):
            ds = cur.isoformat()
            count = activity.get(ds, 0)
            week.append({
                "date":   ds,
                "count":  count,
                "level":  _day_level(count),
                "future": cur > today,
                "month":  cur.strftime("%b") if cur.day == 1 else "",
            })
            cur += timedelta(days=1)
        grid.append(week)
    return grid


def _streaks(activity: dict) -> tuple[int, int]:
    today = datetime.now(timezone.utc).date()
    cur_streak = 0
    d = today
    while activity.get(d.isoformat(), 0) > 0:
        cur_streak += 1
        d -= timedelta(days=1)
    longest = cur = 0
    prev = None
    for ds in sorted(activity):
        if activity[ds] == 0:
            cur = 0
        else:
            day = datetime.fromisoformat(ds).date()
            if prev and (day - prev).days == 1:
                cur += 1
            else:
                cur = 1
            longest = max(longest, cur)
            prev = day
    return cur_streak, longest


def _stats(activity: dict, grid: list) -> dict:
    total = sum(activity.values())
    active_days = sum(1 for v in activity.values() if v > 0)
    cur_streak, longest = _streaks(activity)
    busiest_date = max(activity, key=activity.get) if activity else ""
    busiest_count = activity.get(busiest_date, 0)
    return {
        "total": total,
        "active_days": active_days,
        "cur_streak": cur_streak,
        "longest": longest,
        "busiest_date": busiest_date,
        "busiest_count": busiest_count,
    }


routes = web.RouteTableDef()


@routes.get("/activity/data")
async def activity_data(request: web.Request):
    db = get_db(request)
    profile = get_node_profile(db)
    did = profile.get("did", "")
    activity = _get_activity(db, did) if did else {}
    return web.json_response(activity)


@routes.get("/activity")
async def activity_page(request: web.Request):
    ws = get_web_store(request)
    if ws.get_plugin_setting("activity", "public_page") != "1" and not get_session(request):
        raise web.HTTPFound("/login?next=/activity")
    db = get_db(request)
    profile = get_node_profile(db)
    did    = profile.get("did", "")
    handle = profile.get("handle", "")
    activity = _get_activity(db, did) if did else {}
    weeks    = int(ws.get_plugin_setting("activity", "heatmap_weeks") or "52")
    limit    = int(ws.get_plugin_setting("activity", "feed_limit") or "60")
    grid     = _build_grid(activity, weeks=weeks)
    stats    = _stats(activity, grid)
    records  = _get_recent_records(db, did, handle, limit=limit) if did else []
    domains  = sorted({r["domain"] for r in records})
    return render(request, "plugin/activity/main.html", {
        "grid":    grid,
        "stats":   stats,
        "handle":  handle,
        "records": records,
        "domains": domains,
    })


if __name__ == "__main__":
    from mypds.plugin_runner import run_plugin
    run_plugin(routes, APP_NAME)
