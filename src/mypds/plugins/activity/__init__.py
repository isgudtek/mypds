from collections import defaultdict
from datetime import datetime, timezone, timedelta
from aiohttp import web

from mypds.app_util import get_db
from mypds.web import render, get_node_profile

APP_NAME = "activity"
NSID     = None

routes = web.RouteTableDef()

_TID_CHARSET = "234567abcdefghijklmnopqrstuvwxyz"


def _tid_to_date(rkey: str):
    if len(rkey) != 13:
        return None
    rkey = rkey.lower()
    if not all(c in _TID_CHARSET for c in rkey):
        return None
    n = 0
    for c in rkey:
        n = n * 32 + _TID_CHARSET.index(c)
    ts = (n >> 10) // 1_000_000
    if ts < 1_000_000_000:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).date()


def _get_activity(db, did: str) -> dict:
    row = db.con.execute("SELECT id FROM user WHERE did=?", (did,)).fetchone()
    if not row:
        return {}
    rows = db.con.execute(
        "SELECT rkey FROM record WHERE repo=?", (row[0],)
    ).fetchall()
    counts = defaultdict(int)
    for (rkey,) in rows:
        d = _tid_to_date(rkey)
        if d:
            counts[d.isoformat()] += 1
    return dict(counts)


def _day_level(count: int) -> int:
    if count == 0:   return 0
    if count <= 2:   return 1
    if count <= 5:   return 2
    if count <= 9:   return 3
    return 4


def _build_grid(activity: dict, weeks: int = 52) -> list[list[dict]]:
    today = datetime.now(timezone.utc).date()
    # Align start to nearest past Monday, weeks back
    start = today - timedelta(weeks=weeks)
    start = start - timedelta(days=start.weekday())  # Mon = 0
    grid = []   # list of weeks, each week is 7 days Mon→Sun
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
    # current streak
    cur_streak = 0
    d = today
    while activity.get(d.isoformat(), 0) > 0:
        cur_streak += 1
        d -= timedelta(days=1)
    # longest streak
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
    # busiest day
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


@routes.get("/activity/data")
async def activity_data(request: web.Request):
    db = get_db(request)
    profile = get_node_profile(db)
    did = profile.get("did", "")
    activity = _get_activity(db, did) if did else {}
    return web.json_response(activity)


@routes.get("/activity")
async def activity_page(request: web.Request):
    db = get_db(request)
    profile = get_node_profile(db)
    did = profile.get("did", "")
    activity = _get_activity(db, did) if did else {}
    grid = _build_grid(activity)
    stats = _stats(activity, grid)
    return render(request, "plugin/activity/main.html", {
        "grid": grid,
        "stats": stats,
        "handle": profile.get("handle", ""),
    })


if __name__ == "__main__":
    from mypds.plugin_runner import run_plugin
    run_plugin(routes, APP_NAME)
