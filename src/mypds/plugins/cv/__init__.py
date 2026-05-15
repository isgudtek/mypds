import json
from aiohttp import web
import cbrrr

from mypds.app_util import get_db
from mypds.web import render, get_session, get_web_store, get_node_profile

APP_NAME = "cv"
NSID     = None

routes = web.RouteTableDef()

_CV_KEY  = "cv_data"
_BSKY    = ("app.bsky.", "chat.bsky.")

_DEFAULT = {
    "public":     False,
    "headline":   "",
    "summary":    "",
    "location":   "",
    "links":      [],          # [{label, url}]
    "skills":     [],          # [str]
    "experience": [],          # [{company, role, from_, to, desc}]
    "projects":   [],          # [{name, desc, url}]
    "education":  [],          # [{school, degree, from_, to}]
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load(ws) -> dict:
    raw = ws.get_node_setting(_CV_KEY)
    try:
        return {**_DEFAULT, **json.loads(raw)} if raw else dict(_DEFAULT)
    except Exception:
        return dict(_DEFAULT)


def _save(ws, data: dict):
    ws.set_node_setting(_CV_KEY, json.dumps(data, ensure_ascii=False))


def _parse_form(post: dict) -> dict:
    def arr(key): return [v.strip() for v in post.getall(key, []) if v.strip()]
    def pairs(k1, k2):
        a, b = post.getall(k1, []), post.getall(k2, [])
        return [{"label": x.strip(), "url": y.strip()} for x, y in zip(a, b) if x.strip() or y.strip()]

    skills_raw = post.get("skills", "")
    skills = [s.strip() for s in skills_raw.replace(";", ",").split(",") if s.strip()]

    exp_companies = post.getall("exp_company", [])
    exp_roles     = post.getall("exp_role",    [])
    exp_froms     = post.getall("exp_from",    [])
    exp_tos       = post.getall("exp_to",      [])
    exp_descs     = post.getall("exp_desc",    [])
    experience = []
    for vals in zip(exp_companies, exp_roles, exp_froms, exp_tos, exp_descs):
        if any(v.strip() for v in vals):
            experience.append({"company": vals[0].strip(), "role": vals[1].strip(),
                                "from_": vals[2].strip(), "to": vals[3].strip(), "desc": vals[4].strip()})

    proj_names = post.getall("proj_name", [])
    proj_descs = post.getall("proj_desc", [])
    proj_urls  = post.getall("proj_url",  [])
    projects = []
    for vals in zip(proj_names, proj_descs, proj_urls):
        if vals[0].strip():
            projects.append({"name": vals[0].strip(), "desc": vals[1].strip(), "url": vals[2].strip()})

    edu_schools  = post.getall("edu_school",  [])
    edu_degrees  = post.getall("edu_degree",  [])
    edu_froms    = post.getall("edu_from",    [])
    edu_tos      = post.getall("edu_to",      [])
    education = []
    for vals in zip(edu_schools, edu_degrees, edu_froms, edu_tos):
        if vals[0].strip():
            education.append({"school": vals[0].strip(), "degree": vals[1].strip(),
                               "from_": vals[2].strip(), "to": vals[3].strip()})

    return {
        "public":     "public" in post,
        "headline":   post.get("headline", "").strip(),
        "summary":    post.get("summary",  "").strip(),
        "location":   post.get("location", "").strip(),
        "links":      pairs("link_label", "link_url"),
        "skills":     skills,
        "experience": experience,
        "projects":   projects,
        "education":  education,
    }


def _atproto_context(db, did: str) -> dict:
    """Pull live ATProto data to enrich the profile."""
    row = db.con.execute("SELECT id FROM user WHERE did=?", (did,)).fetchone()
    if not row:
        return {}
    uid = row[0]

    # record counts by namespace group
    rows = db.con.execute(
        "SELECT nsid, COUNT(*) FROM record WHERE repo=? GROUP BY nsid", (uid,)
    ).fetchall()
    counts = {r[0]: r[1] for r in rows}

    posts    = counts.get("app.bsky.feed.post", 0)
    likes    = counts.get("app.bsky.feed.like", 0)

    # Tangled repos
    tangled_rows = db.con.execute(
        "SELECT rkey, value FROM record WHERE repo=? AND nsid='sh.tangled.repo'", (uid,)
    ).fetchall()
    tangled = []
    for rkey, value in tangled_rows:
        try:
            d = cbrrr.decode_dag_cbor(value, atjson_mode=True)
            tangled.append({"rkey": rkey, "knot": d.get("knot", "")})
        except Exception:
            pass

    # WhiteWind entries
    ww_rows = db.con.execute(
        "SELECT rkey, value FROM record WHERE repo=? AND nsid='com.whtwnd.blog.entry' ORDER BY rkey DESC LIMIT 5",
        (uid,)
    ).fetchall()
    writing = []
    for rkey, value in ww_rows:
        try:
            d = cbrrr.decode_dag_cbor(value, atjson_mode=True)
            writing.append({"title": d.get("title", rkey), "rkey": rkey,
                             "visibility": d.get("visibility", "public")})
        except Exception:
            pass

    # plyr profile
    plyr = db.con.execute(
        "SELECT value FROM record WHERE repo=? AND nsid='fm.plyr.actor.profile'", (uid,)
    ).fetchone()
    plyr_url = ""
    if plyr:
        try:
            d = cbrrr.decode_dag_cbor(plyr[0], atjson_mode=True)
            plyr_url = d.get("url", "")
        except Exception:
            pass

    return {
        "posts": posts, "likes": likes,
        "tangled": tangled,
        "writing": writing,
        "plyr_url": plyr_url,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@routes.get("/cv")
async def cv_public(request: web.Request):
    db  = get_db(request)
    ws  = get_web_store(request)
    session = get_session(request)
    cv  = _load(ws)
    if not cv["public"] and not session:
        raise web.HTTPFound("/login?next=/cv")
    profile = get_node_profile(db)
    did = profile.get("did", "")
    atctx = _atproto_context(db, did) if did else {}
    ns = ws.get_node_setting
    return render(request, "plugin/cv/main.html", {
        "cv": cv,
        "atctx": atctx,
        "profile": profile,
        "pfp_url": ns("pfp_url") or "",
        "nickname": ns("nickname") or profile.get("handle", ""),
        "is_owner": bool(session),
    })


@routes.get("/cv/edit")
async def cv_edit_get(request: web.Request):
    session = get_session(request)
    if not session:
        raise web.HTTPFound("/login?next=/cv/edit")
    ws = get_web_store(request)
    cv = _load(ws)
    return render(request, "plugin/cv/edit.html", {"cv": cv})


@routes.post("/cv/edit")
async def cv_edit_post(request: web.Request):
    session = get_session(request)
    if not session:
        raise web.HTTPFound("/login")
    ws   = get_web_store(request)
    post = await request.post()
    _save(ws, _parse_form(post))
    raise web.HTTPFound("/cv")


if __name__ == "__main__":
    from mypds.plugin_runner import run_plugin
    run_plugin(routes, APP_NAME)
