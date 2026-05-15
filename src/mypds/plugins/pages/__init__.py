import re
import os
import sqlite3
import secrets
import hashlib
import mimetypes
import time
from pathlib import Path
from typing import Optional

from aiohttp import web

from mypds.app_util import get_db
from mypds.web import render, get_session, get_node_profile
from mypds.web_store import MEDIA_DIR
from mypds import repo_ops, atproto_repo, util, static_config

APP_NAME = "pages"
NSID     = None

routes = web.RouteTableDef()

_DB_PATH = static_config.DATA_DIR + "/plugins/pages.sqlite3"
_con: sqlite3.Connection | None = None


def _get_con() -> sqlite3.Connection:
	global _con
	if _con is None:
		Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
		_con = sqlite3.connect(_DB_PATH, check_same_thread=False)
		_con.row_factory = sqlite3.Row
		_con.execute("PRAGMA journal_mode=WAL")
		_con.execute("""
			CREATE TABLE IF NOT EXISTS page (
				id           INTEGER PRIMARY KEY AUTOINCREMENT,
				slug         TEXT UNIQUE NOT NULL,
				title        TEXT NOT NULL,
				body         TEXT NOT NULL,
				is_published INTEGER NOT NULL DEFAULT 0,
				created_at   INTEGER NOT NULL,
				updated_at   INTEGER NOT NULL,
				published_at INTEGER,
				at_rkey      TEXT,
				at_uri       TEXT,
				password_hash TEXT
			)
		""")
		_con.commit()
	return _con


def _list_pages():
	rows = _get_con().execute(
		"SELECT id, slug, title, is_published, created_at, updated_at, password_hash FROM page ORDER BY updated_at DESC"
	).fetchall()
	result = []
	for r in rows:
		d = dict(r)
		d["is_protected"] = bool(d.get("password_hash"))
		result.append(d)
	return result


def _get_page(slug: str) -> Optional[dict]:
	row = _get_con().execute("SELECT * FROM page WHERE slug=?", (slug,)).fetchone()
	return dict(row) if row else None


def _get_page_by_id(page_id: int) -> Optional[dict]:
	row = _get_con().execute("SELECT * FROM page WHERE id=?", (page_id,)).fetchone()
	return dict(row) if row else None


def _create_page(slug, title, body, published=False) -> int:
	now = int(time.time())
	con = _get_con()
	cur = con.execute(
		"INSERT INTO page(slug, title, body, is_published, created_at, updated_at, published_at) VALUES (?,?,?,?,?,?,?)",
		(slug, title, body, int(published), now, now, now if published else None),
	)
	con.commit()
	return cur.lastrowid


def _update_page(page_id, slug, title, body, published):
	now = int(time.time())
	con = _get_con()
	row = con.execute("SELECT published_at FROM page WHERE id=?", (page_id,)).fetchone()
	pub_at = (row["published_at"] or now) if published else (row["published_at"] if row else None)
	con.execute(
		"UPDATE page SET slug=?, title=?, body=?, is_published=?, updated_at=?, published_at=? WHERE id=?",
		(slug, title, body, int(published), now, pub_at, page_id),
	)
	con.commit()


def _set_page_password(page_id, password_hash: Optional[str]):
	con = _get_con()
	con.execute("UPDATE page SET password_hash=? WHERE id=?", (password_hash, page_id))
	con.commit()


def _verify_page_password(page_id, raw_password) -> bool:
	row = _get_con().execute("SELECT password_hash FROM page WHERE id=?", (page_id,)).fetchone()
	if row is None or not row["password_hash"]:
		return True
	return row["password_hash"] == hashlib.sha256(raw_password.encode()).hexdigest()


def _set_page_atproto(page_id, at_rkey, at_uri):
	con = _get_con()
	con.execute("UPDATE page SET at_rkey=?, at_uri=? WHERE id=?", (at_rkey, at_uri, page_id))
	con.commit()


def _delete_page(page_id):
	con = _get_con()
	con.execute("DELETE FROM page WHERE id=?", (page_id,))
	con.commit()


def _md_to_plain(body: str) -> str:
	plain = re.sub(r'!\[.*?\]\(.*?\)', '', body)
	plain = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', plain)
	plain = re.sub(r'#{1,6}\s*', '', plain)
	plain = re.sub(r'[*_`~]+', '', plain)
	return re.sub(r'\n+', ' ', plain).strip()


async def _sync_atproto_entry(request, session, page_id, title, body, slug, existing_rkey=None):
	db = get_db(request)
	rkey = existing_rkey or util.tid_now()
	record = {
		"$type": "com.whtwnd.blog.entry",
		"title": title, "content": body,
		"createdAt": util.iso_string_now(),
		"visibility": "public",
	}
	op = "com.atproto.repo.applyWrites#update" if existing_rkey else "com.atproto.repo.applyWrites#create"
	write = {"$type": op, "collection": "com.whtwnd.blog.entry", "rkey": rkey, "value": record}
	res, seq, fbytes = repo_ops.apply_writes(db, session["did"], [write], None)
	await atproto_repo.firehose_broadcast(request, (seq, fbytes))
	at_uri = f"at://{session['did']}/com.whtwnd.blog.entry/{rkey}"
	_set_page_atproto(page_id, rkey, at_uri)
	return rkey


async def _announce_page(request, session, title, body, slug):
	db = get_db(request)
	profile = get_node_profile(db)
	handle = profile.get("handle", "")
	plain = _md_to_plain(body)
	page_url = f"https://{handle}/p/{slug}"
	budget = 280 - len(page_url.encode("utf-8")) - 2
	excerpt = plain[:budget].rsplit(' ', 1)[0] if len(plain) > budget else plain
	if len(plain) > budget:
		excerpt += "…"
	post_text = f"{excerpt}\n\n{page_url}"
	url_start = len(post_text.encode("utf-8")) - len(page_url.encode("utf-8"))
	url_end   = len(post_text.encode("utf-8"))
	writes = [{
		"$type": "com.atproto.repo.applyWrites#create",
		"collection": "app.bsky.feed.post",
		"value": {
			"$type": "app.bsky.feed.post", "text": post_text,
			"createdAt": util.iso_string_now(), "langs": ["en"],
			"facets": [{
				"$type": "app.bsky.richtext.facet",
				"index": {"$type": "app.bsky.richtext.facet#byteSlice", "byteStart": url_start, "byteEnd": url_end},
				"features": [{"$type": "app.bsky.richtext.facet#link", "uri": page_url}],
			}],
		},
	}]
	res, seq, fbytes = repo_ops.apply_writes(db, session["did"], writes, None)
	await atproto_repo.firehose_broadcast(request, (seq, fbytes))


# ── Public page view (no session required) ────────────────────────────────────

@routes.get("/p/{slug}")
async def page_view(request: web.Request):
	slug = request.match_info["slug"]
	page = _get_page(slug)
	if not page or not page["is_published"]:
		raise web.HTTPNotFound()
	if page.get("password_hash"):
		# check session-stored unlock
		token = request.cookies.get("page_unlock_" + slug)
		if not token or token != page["password_hash"]:
			raise web.HTTPFound(f"/p/{slug}/unlock")
	return render(request, "plugin/pages/view.html", {"page": page})


@routes.get("/p/{slug}/unlock")
async def page_unlock_get(request: web.Request):
	slug = request.match_info["slug"]
	page = _get_page(slug)
	if not page or not page["is_published"]:
		raise web.HTTPNotFound()
	return render(request, "plugin/pages/gate.html", {"slug": slug, "error": None})


@routes.post("/p/{slug}/unlock")
async def page_unlock_post(request: web.Request):
	slug = request.match_info["slug"]
	page = _get_page(slug)
	if not page:
		raise web.HTTPNotFound()
	data = await request.post()
	password = data.get("password", "")
	if not _verify_page_password(page["id"], password):
		return render(request, "plugin/pages/gate.html", {"slug": slug, "error": "Wrong password"})
	resp = web.Response(status=302, headers={"Location": f"/p/{slug}"})
	resp.set_cookie("page_unlock_" + slug, page["password_hash"], max_age=3600, httponly=True)
	return resp


# ── Owner CRUD ────────────────────────────────────────────────────────────────

@routes.get("/pages")
async def pages_list(request: web.Request):
	session = get_session(request)
	all_pages = _list_pages()
	pages = all_pages if session else [p for p in all_pages if p["is_published"]]
	return render(request, "plugin/pages/list.html", {"pages": pages})


@routes.get("/pages/new")
async def page_new(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	return render(request, "plugin/pages/edit.html", {"page": None, "error": None})


@routes.post("/pages/new")
async def page_new_post(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")

	data = await request.post()
	slug         = data.get("slug", "").strip().lower().replace(" ", "-")
	title        = data.get("title", "").strip()
	body         = data.get("body", "").strip()
	published    = data.get("published") == "1"
	announce     = data.get("announce") == "1"
	raw_password = data.get("page_password", "").strip()

	if not slug or not title or not body:
		return render(request, "plugin/pages/edit.html", {
			"page": {"slug": slug, "title": title, "body": body}, "error": "All fields required"
		})
	try:
		page_id = _create_page(slug, title, body, published)
	except Exception:
		return render(request, "plugin/pages/edit.html", {
			"page": {"slug": slug, "title": title, "body": body}, "error": f"Slug already taken: {slug}"
		})

	if raw_password:
		_set_page_password(page_id, hashlib.sha256(raw_password.encode()).hexdigest())

	if published and not raw_password:
		try:
			await _sync_atproto_entry(request, session, page_id, title, body, slug)
		except Exception:
			pass
		if announce:
			try:
				await _announce_page(request, session, title, body, slug)
			except Exception:
				pass
	raise web.HTTPFound("/pages")


@routes.get("/pages/{page_id}/edit")
async def page_edit(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	page = _get_page_by_id(int(request.match_info["page_id"]))
	if not page:
		raise web.HTTPNotFound()
	return render(request, "plugin/pages/edit.html", {"page": page, "error": None})


@routes.post("/pages/{page_id}/edit")
async def page_edit_post(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")

	page_id      = int(request.match_info["page_id"])
	data         = await request.post()
	slug         = data.get("slug", "").strip().lower().replace(" ", "-")
	title        = data.get("title", "").strip()
	body         = data.get("body", "").strip()
	published    = data.get("published") == "1"
	announce     = data.get("announce") == "1"
	raw_password = data.get("page_password", "").strip()
	clear_pw     = data.get("clear_password") == "1"

	existing = _get_page_by_id(page_id)
	_update_page(page_id, slug, title, body, published)

	if raw_password:
		_set_page_password(page_id, hashlib.sha256(raw_password.encode()).hexdigest())
	elif clear_pw:
		_set_page_password(page_id, None)

	updated = _get_page_by_id(page_id)
	is_protected = bool(updated and updated.get("password_hash"))

	if published and not is_protected:
		try:
			existing_rkey = existing.get("at_rkey") if existing else None
			await _sync_atproto_entry(request, session, page_id, title, body, slug, existing_rkey)
		except Exception:
			pass
		if announce:
			try:
				await _announce_page(request, session, title, body, slug)
			except Exception:
				pass
	raise web.HTTPFound("/pages")


@routes.post("/pages/{page_id}/delete")
async def page_delete(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	_delete_page(int(request.match_info["page_id"]))
	raise web.HTTPFound("/pages")


@routes.post("/pages/upload-image")
async def pages_upload_image(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPUnauthorized()

	reader = await request.multipart()
	field = await reader.next()
	if field is None or field.name != "file":
		raise web.HTTPBadRequest(text="No file field")

	orig_name = field.filename or "image"
	mime = field.headers.get("Content-Type", "") or mimetypes.guess_type(orig_name)[0] or "image/jpeg"
	ext = Path(orig_name).suffix or ".jpg"
	safe_name = secrets.token_hex(12) + ext
	dest = os.path.join(MEDIA_DIR, safe_name)

	with open(dest, "wb") as f:
		while True:
			chunk = await field.read_chunk(65536)
			if not chunk:
				break
			f.write(chunk)

	url = f"/media/{safe_name}"
	return web.json_response({"url": url, "markdown": f"![image]({url})"})


if __name__ == "__main__":
	from mypds.plugin_runner import run_plugin
	run_plugin(routes, APP_NAME)
