from aiohttp import web
import cbrrr

from mypds.app_util import get_db
from mypds.web import render, get_session, get_node_profile
from mypds import repo_ops, atproto_repo, util

APP_NAME = "links"
NSID     = "pub.social.linktree"

routes = web.RouteTableDef()


def _get_linktree(db, did: str) -> list:
	user_id = db.con.execute("SELECT id FROM user WHERE did=?", (did,)).get
	if user_id is None:
		return []
	row = db.con.execute(
		"SELECT value FROM record WHERE repo=? AND nsid=? AND rkey='self'",
		(user_id, NSID),
	).fetchone()
	if not row:
		return []
	try:
		return cbrrr.decode_dag_cbor(row[0]).get("links", [])
	except Exception:
		return []


async def _save_linktree(request: web.Request, session: dict, links: list):
	db = get_db(request)
	record = {"$type": NSID, "links": links, "updatedAt": util.iso_string_now()}
	user_id = db.con.execute("SELECT id FROM user WHERE did=?", (session["did"],)).get
	existing = db.con.execute(
		"SELECT rkey FROM record WHERE repo=? AND nsid=? AND rkey='self'",
		(user_id, NSID),
	).fetchone() if user_id else None
	op = "com.atproto.repo.applyWrites#update" if existing else "com.atproto.repo.applyWrites#create"
	write = {"$type": op, "collection": NSID, "rkey": "self", "value": record}
	res, seq, fbytes = repo_ops.apply_writes(db, session["did"], [write], None)
	await atproto_repo.firehose_broadcast(request, (seq, fbytes))


@routes.get("/links")
async def links_page(request: web.Request):
	db = get_db(request)
	profile = get_node_profile(db)
	links = _get_linktree(db, profile["did"]) if profile["did"] else []
	return render(request, "plugin/links/main.html", {"profile": profile, "links": links})


@routes.get("/links/edit")
async def links_edit_page(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	db = get_db(request)
	profile = get_node_profile(db)
	links = _get_linktree(db, session["did"])
	return render(request, "plugin/links/edit.html", {"profile": profile, "links": links, "error": None})


@routes.post("/links/edit")
async def links_edit_post(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	data = await request.post()
	titles    = data.getall("title", [])
	urls      = data.getall("url", [])
	platforms = data.getall("platform", [])
	links = []
	for i, (title, url) in enumerate(zip(titles, urls)):
		title = title.strip(); url = url.strip()
		if title and url:
			platform = platforms[i].strip() if i < len(platforms) else "other"
			links.append({"title": title, "url": url, "platform": platform})
	await _save_linktree(request, session, links)
	raise web.HTTPFound("/links/edit")


if __name__ == "__main__":
	from mypds.plugin_runner import run_plugin
	run_plugin(routes, APP_NAME)
