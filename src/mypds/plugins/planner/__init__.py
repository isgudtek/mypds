from aiohttp import web
import cbrrr

from mypds.app_util import get_db
from mypds.web import render, get_session, get_web_store, get_node_profile
from mypds import repo_ops, atproto_repo, util

APP_NAME = "planner"
NSID     = "pub.planner.event"

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_LABELS = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday",
              "thu": "Thursday", "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}

routes = web.RouteTableDef()


# ── DB helper ─────────────────────────────────────────────────────────────────

def _get_events(db, did: str) -> dict:
    user_row = db.con.execute("SELECT id FROM user WHERE did=?", (did,)).fetchone()
    if user_row is None:
        return {d: [] for d in DAYS}
    user_id = user_row[0]
    rows = db.con.execute(
        "SELECT rkey, value FROM record WHERE repo=? AND nsid=? ORDER BY rkey ASC",
        (user_id, NSID),
    ).fetchall()
    by_day = {d: [] for d in DAYS}
    for rkey, value in rows:
        try:
            rec = cbrrr.decode_dag_cbor(value)
            day = rec.get("day", "mon")
            if day not in by_day:
                continue
            by_day[day].append({
                "rkey":  rkey,
                "title": rec.get("title", ""),
                "time":  rec.get("time", ""),
                "notes": rec.get("notes", ""),
                "color": rec.get("color", "cyan"),
            })
        except Exception:
            continue
    # sort each day by time
    for day in DAYS:
        by_day[day].sort(key=lambda e: e["time"] or "99:99")
    return by_day


# ── Routes ────────────────────────────────────────────────────────────────────

@routes.get("/planner")
async def planner_page(request: web.Request):
    ws = get_web_store(request)
    if not ws.get_app_enabled(APP_NAME):
        raise web.HTTPNotFound()
    db = get_db(request)
    profile = get_node_profile(db)
    events = _get_events(db, profile["did"]) if profile["did"] else {d: [] for d in DAYS}
    return render(request, "plugin/planner/main.html", {
        "profile": profile,
        "events":  events,
        "days":    DAYS,
        "day_labels": DAY_LABELS,
    })


@routes.get("/planner/new")
async def planner_new_page(request: web.Request):
    session = get_session(request)
    if not session:
        raise web.HTTPFound("/login")
    day = request.rel_url.query.get("day", "mon")
    return render(request, "plugin/planner/new.html", {"error": None, "day": day})


@routes.post("/planner/new")
async def planner_new_post(request: web.Request):
    session = get_session(request)
    if not session:
        raise web.HTTPFound("/login")

    data  = await request.post()
    title = data.get("title", "").strip()
    day   = data.get("day", "mon").strip()
    time  = data.get("time", "").strip()
    notes = data.get("notes", "").strip()
    color = data.get("color", "cyan").strip()

    if not title:
        return render(request, "plugin/planner/new.html",
                      {"error": "Title is required", "day": day})
    if day not in DAYS:
        day = "mon"

    db   = get_db(request)
    rkey = util.tid_now()

    write = {
        "$type":      "com.atproto.repo.applyWrites#create",
        "collection": NSID,
        "rkey":       rkey,
        "value": {
            "$type":     NSID,
            "title":     title,
            "day":       day,
            "time":      time,
            "notes":     notes,
            "color":     color,
            "createdAt": util.iso_string_now(),
        },
    }
    res, seq, fbytes = repo_ops.apply_writes(db, session["did"], [write], None)
    await atproto_repo.firehose_broadcast(request, (seq, fbytes))
    raise web.HTTPFound("/planner")


@routes.post("/planner/{rkey}/delete")
async def planner_delete(request: web.Request):
    session = get_session(request)
    if not session:
        raise web.HTTPFound("/login")

    rkey  = request.match_info["rkey"]
    db    = get_db(request)
    write = {
        "$type":      "com.atproto.repo.applyWrites#delete",
        "collection": NSID,
        "rkey":       rkey,
    }
    res, seq, fbytes = repo_ops.apply_writes(db, session["did"], [write], None)
    await atproto_repo.firehose_broadcast(request, (seq, fbytes))
    raise web.HTTPFound("/planner")
