"""
web.py — Personal Home Node web UI routes.

All routes here are isolated from the ATProto protocol layer.
They share the DB and app state, but use a separate session system.
"""

import os
import time
import secrets
import mimetypes
import hashlib
import json
from pathlib import Path
from typing import Optional

import cbrrr
from aiohttp import web

from . import repo_ops
from . import atproto_repo
from . import util
from .app_util import get_db, get_jinja_env, get_firehose_queues, get_firehose_queues_lock
from .web_store import WebStore, MEDIA_DIR

web_routes = web.RouteTableDef()

# ── App key for WebStore ───────────────────────────────────────────────────────

MILLIPDS_WEB_STORE = web.AppKey("MILLIPDS_WEB_STORE", WebStore)

COOKIE_NAME = "mpds_sid"
NODE_CSP = (
	"default-src 'self'; "
	"style-src 'self' 'unsafe-inline' https://unpkg.com; "
	"script-src 'self' 'unsafe-inline' https://unpkg.com; "
	"img-src 'self' data: blob: https:; "
	"connect-src 'self' https://nominatim.openstreetmap.org; "
	"font-src 'self' data:; "
	"frame-ancestors 'none'"
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_web_store(request: web.Request) -> WebStore:
	return request.app[MILLIPDS_WEB_STORE]


def get_session(request: web.Request) -> Optional[dict]:
	token = request.cookies.get(COOKIE_NAME)
	if not token:
		return None
	return get_web_store(request).get_session(token)


def set_session_cookie(response: web.Response, token: str):
	response.set_cookie(
		COOKIE_NAME,
		token,
		httponly=True,
		samesite="Strict",
		max_age=60 * 60 * 24 * 7,
		path="/",
	)


def clear_session_cookie(response: web.Response):
	response.del_cookie(COOKIE_NAME, path="/")


def render(request: web.Request, template: str, ctx: dict = {}, status: int = 200) -> web.Response:
	jinja = get_jinja_env(request)
	session = get_session(request)
	ws = get_web_store(request)
	apps = ws.get_all_app_settings()
	_ns = ws.get_all_node_settings()
	node_settings = {
		"nickname": _ns.get("nickname", ""),
		"pfp_url": _ns.get("pfp_url", ""),
		"accent_color": _ns.get("accent_color", ""),
	}
	tmpl = jinja.get_template(template)
	html = tmpl.render(session=session, apps=apps, node_settings=node_settings, **ctx)
	resp = web.Response(text=html, content_type="text/html", charset="utf-8", status=status)
	resp.headers["Content-Security-Policy"] = NODE_CSP
	resp.headers["X-Frame-Options"] = "DENY"
	return resp


def redirect(location: str) -> web.Response:
	resp = web.HTTPFound(location)
	return resp


def get_recent_posts(db, did: str, limit: int = 20) -> list:
	"""Fetch recent app.bsky.feed.post records for a DID, decoded from CBOR."""
	user_id = db.con.execute("SELECT id FROM user WHERE did=?", (did,)).get
	if user_id is None:
		return []
	rows = db.con.execute(
		"SELECT rkey, value FROM record WHERE repo=? AND nsid='app.bsky.feed.post' ORDER BY rkey DESC LIMIT ?",
		(user_id, limit),
	).fetchall()
	posts = []
	for rkey, value in rows:
		try:
			rec = cbrrr.decode_dag_cbor(value)
			posts.append({
				"rkey": rkey,
				"text": rec.get("text", ""),
				"created_at": rec.get("createdAt", ""),
				"embed": rec.get("embed"),
			})
		except Exception:
			pass
	return posts


def get_node_profile(db) -> dict:
	"""Return the primary account's handle and DID."""
	row = db.con.execute("SELECT did, handle FROM user LIMIT 1").get
	if row is None:
		return {"did": "", "handle": ""}
	return {"did": row[0], "handle": row[1]}


def format_ts(ts_str: str) -> str:
	"""Format ISO timestamp to a readable short form."""
	try:
		from datetime import datetime, timezone
		dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
		now = datetime.now(timezone.utc)
		diff = now - dt
		if diff.days > 365:
			return f"{diff.days // 365}y ago"
		if diff.days > 0:
			return f"{diff.days}d ago"
		if diff.seconds > 3600:
			return f"{diff.seconds // 3600}h ago"
		if diff.seconds > 60:
			return f"{diff.seconds // 60}m ago"
		return "just now"
	except Exception:
		return ts_str[:10] if ts_str else ""


# ── Public routes ─────────────────────────────────────────────────────────────

@web_routes.get("/")
async def homepage(request: web.Request):
	db = get_db(request)
	profile = get_node_profile(db)
	posts = []
	if profile["did"]:
		posts = get_recent_posts(db, profile["did"])
		for p in posts:
			p["ts_human"] = format_ts(p["created_at"])

	ws = get_web_store(request)
	apps = ws.get_all_app_settings()
	pages = [p for p in ws.list_pages() if p["is_published"]] if apps.get("pages", True) else []
	files = [f for f in ws.list_files() if f["is_public"]] if apps.get("files", True) else []
	gallery = _get_gallery(db, profile["did"], limit=6) if (profile["did"] and apps.get("gallery", True)) else []
	links  = _get_linktree(db, profile["did"]) if (profile["did"] and apps.get("links", True)) else []
	places = _get_places(db, profile["did"])[:3] if (profile["did"] and apps.get("places", True)) else []
	# Note: apps also injected by render() — not passed explicitly to avoid duplicate kwarg
	version = _get_version()

	return render(request, "node_home.html", {
		"profile": profile,
		"posts": posts,
		"pages": pages,
		"files": files,
		"gallery": gallery,
		"links": links,
		"places": places,
		"version": version,
	})


@web_routes.get("/node-info")
async def node_info(request: web.Request):
	db = get_db(request)
	profile = get_node_profile(db)
	version = _get_version()

	# count records
	user_id = db.con.execute("SELECT id FROM user WHERE did=?", (profile["did"],)).get if profile["did"] else None
	post_count = 0
	blob_count = 0
	if user_id is not None:
		post_count = db.con.execute(
			"SELECT COUNT(*) FROM record WHERE repo=? AND nsid='app.bsky.feed.post'", (user_id,)
		).get or 0
		blob_count = db.con.execute(
			"SELECT COUNT(*) FROM blob WHERE repo=? AND refcount > 0", (user_id,)
		).get or 0

	ws = get_web_store(request)
	page_count = len([p for p in ws.list_pages() if p["is_published"]])
	file_count = len([f for f in ws.list_files() if f["is_public"]])

	return render(request, "node_info.html", {
		"profile": profile,
		"version": version,
		"post_count": post_count,
		"blob_count": blob_count,
		"page_count": page_count,
		"file_count": file_count,
	})


@web_routes.get("/p/{slug}")
async def page_view(request: web.Request):
	slug = request.match_info["slug"]
	ws = get_web_store(request)
	page = ws.get_page(slug)
	if page is None or not page["is_published"]:
		raise web.HTTPNotFound(text="Page not found")
	# Password gate: check if page is protected and user is not logged in
	if page.get("password_hash"):
		session = get_session(request)
		if not session:
			# Check for valid page unlock cookie
			cookie_val = request.cookies.get(f"mpds_page_{slug}")
			page_hash = page["password_hash"]
			if cookie_val != page_hash:
				return render(request, "node_page_gate.html", {"page": page, "error": None})
	return render(request, "node_page_view.html", {"page": page})


@web_routes.post("/p/{slug}/unlock")
async def page_unlock(request: web.Request):
	slug = request.match_info["slug"]
	ws = get_web_store(request)
	page = ws.get_page(slug)
	if page is None or not page["is_published"]:
		raise web.HTTPNotFound(text="Page not found")

	data = await request.post()
	password = data.get("password", "")
	given_hash = hashlib.sha256(password.encode()).hexdigest()

	if given_hash != page.get("password_hash", ""):
		return render(request, "node_page_gate.html", {"page": page, "error": "Incorrect password"}, status=401)

	resp = redirect(f"/p/{slug}")
	resp.set_cookie(
		f"mpds_page_{slug}",
		page["password_hash"],
		httponly=True,
		samesite="Strict",
		max_age=60 * 60 * 24,  # 24 hours
		path=f"/p/{slug}",
	)
	return resp


@web_routes.get("/media/{filename}")
async def media_serve(request: web.Request):
	filename = request.match_info["filename"]
	ws = get_web_store(request)
	meta = ws.get_file_by_name(filename)
	if meta is None:
		raise web.HTTPNotFound()
	if not meta["is_public"]:
		session = get_session(request)
		if session is None:
			raise web.HTTPForbidden()
	path = os.path.join(MEDIA_DIR, filename)
	if not os.path.exists(path):
		raise web.HTTPNotFound()
	return web.FileResponse(path, headers={
		"Content-Type": meta["mime_type"],
		"Cache-Control": "public, max-age=86400",
	})


# ── Auth ──────────────────────────────────────────────────────────────────────

@web_routes.get("/login")
async def login_page(request: web.Request):
	if get_session(request):
		raise web.HTTPFound("/dashboard")
	return render(request, "node_login.html", {"error": None})


@web_routes.post("/login")
async def login_post(request: web.Request):
	data = await request.post()
	identifier = data.get("identifier", "").strip()
	password = data.get("password", "")

	db = get_db(request)
	try:
		did, handle = db.verify_account_login(identifier, password)
	except (KeyError, ValueError):
		return render(request, "node_login.html", {"error": "Invalid credentials"}, status=401)

	ws = get_web_store(request)
	token = ws.create_session(did, handle)
	resp = redirect("/dashboard")
	set_session_cookie(resp, token)
	return resp


@web_routes.get("/logout")
async def logout(request: web.Request):
	token = request.cookies.get(COOKIE_NAME)
	if token:
		get_web_store(request).delete_session(token)
	resp = redirect("/")
	clear_session_cookie(resp)
	return resp


# ── Dashboard ─────────────────────────────────────────────────────────────────

@web_routes.get("/dashboard")
async def dashboard(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")

	db = get_db(request)
	profile = get_node_profile(db)
	posts = get_recent_posts(db, session["did"], limit=5)
	for p in posts:
		p["ts_human"] = format_ts(p["created_at"])

	ws = get_web_store(request)
	pages = ws.list_pages()
	files = ws.list_files()
	version = _get_version()

	# count blobs
	user_id = db.con.execute("SELECT id FROM user WHERE did=?", (session["did"],)).get
	blob_count = db.con.execute(
		"SELECT COUNT(*) FROM blob WHERE repo=? AND refcount > 0", (user_id,)
	).get or 0

	return render(request, "node_dashboard.html", {
		"profile": profile,
		"posts": posts,
		"pages": pages,
		"files": files,
		"blob_count": blob_count,
		"version": version,
	})


# ── Compose ───────────────────────────────────────────────────────────────────

@web_routes.get("/compose")
async def compose_page(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	return render(request, "node_compose.html", {"error": None, "success": None})


@web_routes.post("/compose")
async def compose_post(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")

	# Handle multipart (image attachment) or regular form
	embed = None
	if request.content_type and "multipart" in request.content_type:
		reader = await request.multipart()
		text = ""
		image_data = None
		image_mime = "image/jpeg"
		while True:
			field = await reader.next()
			if field is None:
				break
			if field.name == "text":
				text = (await field.read()).decode("utf-8", errors="replace").strip()
			elif field.name == "image" and field.filename:
				image_mime = field.headers.get("Content-Type", "") or "image/jpeg"
				image_data = await field.read()
		if not image_data:
			embed = None
		else:
			# Store blob in ATProto blob store (using apsw — no .commit() needed)
			db = get_db(request)
			db.con.execute(
				"INSERT INTO blob (repo, refcount) VALUES ((SELECT id FROM user WHERE did=?), 0)",
				(session["did"],),
			)
			blob_id = db.con.last_insert_rowid()
			hasher = hashlib.sha256()
			chunk_size = 0x10000  # 64KB
			for i in range(0, len(image_data), chunk_size):
				chunk = image_data[i:i + chunk_size]
				hasher.update(chunk)
				db.con.execute(
					"INSERT INTO blob_part (blob, idx, data) VALUES (?, ?, ?)",
					(blob_id, i // chunk_size, chunk),
				)
			digest = hasher.digest()
			cid = cbrrr.CID(cbrrr.CID.CIDV1_RAW_SHA256_32_PFX + digest)
			try:
				db.con.execute("UPDATE blob SET cid=? WHERE id=?", (bytes(cid), blob_id))
			except Exception:
				db.con.execute("DELETE FROM blob_part WHERE blob=?", (blob_id,))
				db.con.execute("DELETE FROM blob WHERE id=?", (blob_id,))
				cid = None

			if cid is not None:
				embed = {
					"$type": "app.bsky.embed.images",
					"images": [{
						"$type": "app.bsky.embed.images#image",
						"image": {
							"$type": "blob",
							"ref": {"$link": cid.encode()},
							"mimeType": image_mime,
							"size": len(image_data),
						},
						"alt": "",
					}],
				}
	else:
		data = await request.post()
		text = data.get("text", "").strip()

	if not text:
		return render(request, "node_compose.html", {"error": "Post text is required", "success": None})
	if len(text) > 300:
		return render(request, "node_compose.html", {"error": "Post too long (max 300 chars)", "success": None})

	db = get_db(request)
	post_value = {
		"$type": "app.bsky.feed.post",
		"text": text,
		"createdAt": util.iso_string_now(),
		"langs": ["en"],
	}
	if embed:
		post_value["embed"] = embed

	writes = [{
		"$type": "com.atproto.repo.applyWrites#create",
		"collection": "app.bsky.feed.post",
		"value": post_value,
	}]

	try:
		res, firehose_seq, firehose_bytes = repo_ops.apply_writes(
			db, session["did"], writes, None
		)
		await atproto_repo.firehose_broadcast(request, (firehose_seq, firehose_bytes))

		rkey = list(res.get("results", [{}])[0].get("uri", "/").split("/"))[-1]
		return render(request, "node_compose.html", {
			"error": None,
			"success": f"Posted! AT-URI: at://{session['did']}/app.bsky.feed.post/{rkey}",
		})
	except Exception as e:
		return render(request, "node_compose.html", {"error": str(e), "success": None})


# ── Files ─────────────────────────────────────────────────────────────────────

@web_routes.get("/files")
async def files_page(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	ws = get_web_store(request)
	return render(request, "node_files.html", {"files": ws.list_files(), "error": None})


@web_routes.post("/files/upload")
async def files_upload(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")

	reader = await request.multipart()
	field = await reader.next()
	if field is None or field.name != "file":
		raise web.HTTPBadRequest(text="No file field")

	orig_name = field.filename or "upload"
	mime = field.content_type or mimetypes.guess_type(orig_name)[0] or "application/octet-stream"

	ext = Path(orig_name).suffix
	safe_name = secrets.token_hex(12) + ext
	dest = os.path.join(MEDIA_DIR, safe_name)

	size = 0
	with open(dest, "wb") as f:
		while True:
			chunk = await field.read_chunk(65536)
			if not chunk:
				break
			f.write(chunk)
			size += len(chunk)

	is_public = (await reader.next()) is None  # single-field form = public by default
	ws = get_web_store(request)
	ws.save_file(safe_name, orig_name, mime, size, is_public=True)

	raise web.HTTPFound("/files")


@web_routes.post("/files/{file_id}/delete")
async def files_delete(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	file_id = int(request.match_info["file_id"])
	get_web_store(request).delete_file(file_id)
	raise web.HTTPFound("/files")


@web_routes.post("/files/{file_id}/toggle")
async def files_toggle(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	file_id = int(request.match_info["file_id"])
	get_web_store(request).toggle_file_visibility(file_id)
	raise web.HTTPFound("/files")


# ── Page image upload ─────────────────────────────────────────────────────

@web_routes.post("/pages/upload-image")
async def pages_upload_image(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPUnauthorized()

	reader = await request.multipart()
	field = await reader.next()
	if field is None or field.name != "file":
		raise web.HTTPBadRequest(text="No file field")

	orig_name = field.filename or "image"
	content_disposition = field.headers.get("Content-Type", "")
	mime = content_disposition or mimetypes.guess_type(orig_name)[0] or "image/jpeg"

	ext = Path(orig_name).suffix or ".jpg"
	safe_name = secrets.token_hex(12) + ext
	dest = os.path.join(MEDIA_DIR, safe_name)

	size = 0
	with open(dest, "wb") as f:
		while True:
			chunk = await field.read_chunk(65536)
			if not chunk:
				break
			f.write(chunk)
			size += len(chunk)

	ws = get_web_store(request)
	ws.save_file(safe_name, orig_name, mime, size, is_public=True)

	url = f"/media/{safe_name}"
	return web.json_response({"url": url, "markdown": f"![image]({url})"})


# ── Pages ─────────────────────────────────────────────────────────────────────

@web_routes.get("/pages")
async def pages_list(request: web.Request):
	session = get_session(request)
	ws = get_web_store(request)
	all_pages = ws.list_pages()
	# Public visitors see published pages only; owner sees all
	pages = all_pages if session else [p for p in all_pages if p["is_published"]]
	return render(request, "node_pages.html", {"pages": pages})


@web_routes.get("/pages/new")
async def page_new(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	return render(request, "node_page_edit.html", {"page": None, "error": None})


@web_routes.post("/pages/new")
async def page_new_post(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")

	data = await request.post()
	slug = data.get("slug", "").strip().lower().replace(" ", "-")
	title = data.get("title", "").strip()
	body = data.get("body", "").strip()
	published = data.get("published") == "1"
	announce = data.get("announce") == "1"
	raw_password = data.get("page_password", "").strip()

	if not slug or not title or not body:
		return render(request, "node_page_edit.html", {
			"page": {"slug": slug, "title": title, "body": body},
			"error": "All fields required",
		})

	ws = get_web_store(request)
	try:
		page_id = ws.create_page(slug, title, body, published)
	except Exception as e:
		return render(request, "node_page_edit.html", {
			"page": {"slug": slug, "title": title, "body": body},
			"error": f"Slug already taken: {slug}",
		})

	# Store password if set
	if raw_password:
		ph = hashlib.sha256(raw_password.encode()).hexdigest()
		ws.set_page_password(page_id, ph)

	# Only sync to ATProto if published AND not password-protected
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


@web_routes.get("/pages/{page_id}/edit")
async def page_edit(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	page_id = int(request.match_info["page_id"])
	ws = get_web_store(request)
	page = ws.get_page_by_id(page_id)
	if not page:
		raise web.HTTPNotFound()
	return render(request, "node_page_edit.html", {"page": page, "error": None})


@web_routes.post("/pages/{page_id}/edit")
async def page_edit_post(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")

	page_id = int(request.match_info["page_id"])
	data = await request.post()
	slug = data.get("slug", "").strip().lower().replace(" ", "-")
	title = data.get("title", "").strip()
	body = data.get("body", "").strip()
	published = data.get("published") == "1"
	announce = data.get("announce") == "1"
	raw_password = data.get("page_password", "").strip()
	clear_password = data.get("clear_password") == "1"

	ws = get_web_store(request)
	existing = ws.get_page_by_id(page_id)
	ws.update_page(page_id, slug, title, body, published)

	# Handle password changes
	if raw_password:
		ph = hashlib.sha256(raw_password.encode()).hexdigest()
		ws.set_page_password(page_id, ph)
	elif clear_password:
		ws.set_page_password(page_id, None)

	# Determine effective password state
	updated_page = ws.get_page_by_id(page_id)
	is_protected = bool(updated_page and updated_page.get("password_hash"))

	# Only sync to ATProto if published AND not password-protected
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


@web_routes.post("/pages/{page_id}/delete")
async def page_delete(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	page_id = int(request.match_info["page_id"])
	get_web_store(request).delete_page(page_id)
	raise web.HTTPFound("/pages")


# ── Helpers ───────────────────────────────────────────────────────────────────

import re as _re

def _md_to_plain(body: str) -> str:
	"""Strip markdown to plain text for excerpts."""
	plain = _re.sub(r'!\[.*?\]\(.*?\)', '', body)
	plain = _re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', plain)
	plain = _re.sub(r'#{1,6}\s*', '', plain)
	plain = _re.sub(r'[*_`~]+', '', plain)
	plain = _re.sub(r'\n+', ' ', plain).strip()
	return plain


async def _sync_atproto_entry(
	request: web.Request,
	session: dict,
	page_id: int,
	title: str,
	body: str,
	slug: str,
	existing_rkey: Optional[str] = None,
) -> str:
	"""
	Write or update a com.whtwnd.blog.entry record in the ATProto repo.
	Returns the rkey used.
	"""
	db = get_db(request)
	rkey = existing_rkey or util.tid_now()

	record = {
		"$type": "com.whtwnd.blog.entry",
		"title": title,
		"content": body,
		"createdAt": util.iso_string_now(),
		"visibility": "public",
	}

	op_type = "com.atproto.repo.applyWrites#update" if existing_rkey else "com.atproto.repo.applyWrites#create"
	write = {"$type": op_type, "collection": "com.whtwnd.blog.entry", "rkey": rkey, "value": record}

	res, firehose_seq, firehose_bytes = repo_ops.apply_writes(db, session["did"], [write], None)
	await atproto_repo.firehose_broadcast(request, (firehose_seq, firehose_bytes))

	profile = get_node_profile(db)
	at_uri = f"at://{session['did']}/com.whtwnd.blog.entry/{rkey}"
	ws = get_web_store(request)
	ws.set_page_atproto(page_id, rkey, at_uri)

	return rkey


async def _announce_page(request: web.Request, session: dict, title: str, body: str, slug: str):
	"""Post a short bsky announcement linking to the published page."""
	db = get_db(request)
	profile = get_node_profile(db)
	handle = profile.get("handle", "")

	plain = _md_to_plain(body)
	page_url = f"https://{handle}/p/{slug}"

	budget = 280 - len(page_url.encode("utf-8")) - 2  # 2 for \n\n
	excerpt = plain[:budget].rsplit(' ', 1)[0] if len(plain) > budget else plain
	if len(plain) > budget:
		excerpt += "…"

	post_text = f"{excerpt}\n\n{page_url}"
	url_start = len(post_text.encode("utf-8")) - len(page_url.encode("utf-8"))
	url_end = len(post_text.encode("utf-8"))

	writes = [{
		"$type": "com.atproto.repo.applyWrites#create",
		"collection": "app.bsky.feed.post",
		"value": {
			"$type": "app.bsky.feed.post",
			"text": post_text,
			"createdAt": util.iso_string_now(),
			"langs": ["en"],
			"facets": [{
				"$type": "app.bsky.richtext.facet",
				"index": {
					"$type": "app.bsky.richtext.facet#byteSlice",
					"byteStart": url_start,
					"byteEnd": url_end,
				},
				"features": [{
					"$type": "app.bsky.richtext.facet#link",
					"uri": page_url,
				}],
			}],
		},
	}]

	res, firehose_seq, firehose_bytes = repo_ops.apply_writes(db, session["did"], writes, None)
	await atproto_repo.firehose_broadcast(request, (firehose_seq, firehose_bytes))


# ── Gallery ───────────────────────────────────────────────────────────────────
#
# Lexicon: pub.gallery.image
# Record stored in user's ATProto repo — CID-addressed, signed, portable.
# Any ATProto client can read it at:
#   at://<did>/pub.gallery.image/<rkey>

GALLERY_NSID  = "pub.gallery.image"
LINKTREE_NSID = "pub.social.linktree"
PLACES_NSID   = "pub.places.pin"


def _store_blob(db, did: str, data: bytes, mime: str):
	"""Store raw bytes as an ATProto blob, return CID or None."""
	user_id = db.con.execute("SELECT id FROM user WHERE did=?", (did,)).get
	if user_id is None:
		return None
	db.con.execute(
		"INSERT INTO blob (repo, refcount) VALUES (?, 0)", (user_id,)
	)
	blob_id = db.con.last_insert_rowid()
	hasher = hashlib.sha256()
	chunk_size = 0x10000
	for i in range(0, len(data), chunk_size):
		chunk = data[i:i + chunk_size]
		hasher.update(chunk)
		db.con.execute(
			"INSERT INTO blob_part (blob, idx, data) VALUES (?, ?, ?)",
			(blob_id, i // chunk_size, chunk),
		)
	digest = hasher.digest()
	cid = cbrrr.CID(cbrrr.CID.CIDV1_RAW_SHA256_32_PFX + digest)
	try:
		db.con.execute("UPDATE blob SET cid=? WHERE id=?", (bytes(cid), blob_id))
		return cid
	except Exception:
		db.con.execute("DELETE FROM blob_part WHERE blob=?", (blob_id,))
		db.con.execute("DELETE FROM blob WHERE id=?", (blob_id,))
		return None


def _get_gallery(db, did: str, limit: int = 60) -> list:
	"""Fetch pub.gallery.image records from the ATProto repo."""
	user_id = db.con.execute("SELECT id FROM user WHERE did=?", (did,)).get
	if user_id is None:
		return []
	rows = db.con.execute(
		"SELECT rkey, value FROM record WHERE repo=? AND nsid=? ORDER BY rkey DESC LIMIT ?",
		(user_id, GALLERY_NSID, limit),
	).fetchall()
	images = []
	for rkey, value in rows:
		try:
			rec = cbrrr.decode_dag_cbor(value)
			img_blob = rec.get("image", {})
			blob_ref = img_blob.get("ref", {})
			cid_link = blob_ref.get("$link") if isinstance(blob_ref, dict) else None
			images.append({
				"rkey": rkey,
				"title": rec.get("title", ""),
				"description": rec.get("description", ""),
				"tags": rec.get("tags", []),
				"created_at": rec.get("createdAt", ""),
				"mime": img_blob.get("mimeType", "image/jpeg"),
				"cid": cid_link,
				"webUrl": rec.get("webUrl", ""),
				"at_uri": f"at://{did}/{GALLERY_NSID}/{rkey}",
			})
		except Exception:
			pass
	return images


@web_routes.get("/gallery")
async def gallery_page(request: web.Request):
	db = get_db(request)
	profile = get_node_profile(db)
	images = _get_gallery(db, profile["did"]) if profile["did"] else []
	return render(request, "node_gallery.html", {
		"profile": profile,
		"images": images,
	})


@web_routes.get("/gallery/upload")
async def gallery_upload_page(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	return render(request, "node_gallery_upload.html", {"error": None})


@web_routes.post("/gallery/upload")
async def gallery_upload_post(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")

	reader = await request.multipart()
	title = ""
	description = ""
	tags_raw = ""
	image_data = None
	image_mime = "image/jpeg"
	image_name = "image.jpg"

	while True:
		field = await reader.next()
		if field is None:
			break
		if field.name == "title":
			title = (await field.read()).decode("utf-8", errors="replace").strip()
		elif field.name == "description":
			description = (await field.read()).decode("utf-8", errors="replace").strip()
		elif field.name == "tags":
			tags_raw = (await field.read()).decode("utf-8", errors="replace").strip()
		elif field.name == "image" and field.filename:
			image_name = field.filename
			image_mime = field.headers.get("Content-Type", "") or \
				mimetypes.guess_type(image_name)[0] or "image/jpeg"
			image_data = await field.read()

	if not image_data:
		return render(request, "node_gallery_upload.html", {"error": "No image selected"})
	if len(image_data) > 10 * 1024 * 1024:
		return render(request, "node_gallery_upload.html", {"error": "Image too large (max 10 MB)"})

	db = get_db(request)
	cid = _store_blob(db, session["did"], image_data, image_mime)
	if cid is None:
		return render(request, "node_gallery_upload.html", {"error": "Failed to store image blob"})

	# Also save to media dir for web serving via /xrpc/com.atproto.sync.getBlob
	ext = Path(image_name).suffix or ".jpg"
	safe_name = secrets.token_hex(12) + ext
	dest = os.path.join(MEDIA_DIR, safe_name)
	with open(dest, "wb") as f:
		f.write(image_data)
	ws = get_web_store(request)
	ws.save_file(safe_name, image_name, image_mime, len(image_data), is_public=True)

	tags = [t.strip() for t in tags_raw.replace(",", " ").split() if t.strip()]
	rkey = util.tid_now()

	record = {
		"$type": GALLERY_NSID,
		"title": title or Path(image_name).stem,
		"description": description,
		"tags": tags,
		"createdAt": util.iso_string_now(),
		"image": {
			"$type": "blob",
			"ref": {"$link": cid.encode()},
			"mimeType": image_mime,
			"size": len(image_data),
		},
		# Web-accessible fallback URL (for clients that can't fetch blobs directly)
		"webUrl": f"/media/{safe_name}",
	}

	writes = [{
		"$type": "com.atproto.repo.applyWrites#create",
		"collection": GALLERY_NSID,
		"rkey": rkey,
		"value": record,
	}]
	res, firehose_seq, firehose_bytes = repo_ops.apply_writes(
		db, session["did"], writes, None
	)
	await atproto_repo.firehose_broadcast(request, (firehose_seq, firehose_bytes))

	raise web.HTTPFound("/gallery")


@web_routes.post("/gallery/{rkey}/delete")
async def gallery_delete(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")

	rkey = request.match_info["rkey"]
	db = get_db(request)

	writes = [{
		"$type": "com.atproto.repo.applyWrites#delete",
		"collection": GALLERY_NSID,
		"rkey": rkey,
	}]
	res, firehose_seq, firehose_bytes = repo_ops.apply_writes(
		db, session["did"], writes, None
	)
	await atproto_repo.firehose_broadcast(request, (firehose_seq, firehose_bytes))
	raise web.HTTPFound("/gallery")


def _get_version() -> str:
	try:
		import importlib.metadata
		return f"millipds v{importlib.metadata.version('millipds')}"
	except Exception:
		return "millipds"


# ── Linktree ──────────────────────────────────────────────────────────────────

def _get_linktree(db, did: str) -> list:
	"""Read pub.social.linktree/self from the ATProto repo."""
	user_id = db.con.execute("SELECT id FROM user WHERE did=?", (did,)).get
	if user_id is None:
		return []
	row = db.con.execute(
		"SELECT value FROM record WHERE repo=? AND nsid=? AND rkey='self'",
		(user_id, LINKTREE_NSID),
	).fetchone()
	if not row:
		return []
	try:
		rec = cbrrr.decode_dag_cbor(row[0])
		return rec.get("links", [])
	except Exception:
		return []


async def _save_linktree(request: web.Request, session: dict, links: list):
	"""Write pub.social.linktree/self to the ATProto repo (upsert via create or update)."""
	db = get_db(request)
	record = {
		"$type": LINKTREE_NSID,
		"links": links,
		"updatedAt": util.iso_string_now(),
	}
	# Determine if record already exists — use update, else create
	user_id = db.con.execute("SELECT id FROM user WHERE did=?", (session["did"],)).get
	existing = db.con.execute(
		"SELECT rkey FROM record WHERE repo=? AND nsid=? AND rkey='self'",
		(user_id, LINKTREE_NSID),
	).fetchone() if user_id else None
	optype = "com.atproto.repo.applyWrites#update" if existing else "com.atproto.repo.applyWrites#create"
	write = {"$type": optype, "collection": LINKTREE_NSID, "rkey": "self", "value": record}
	res, firehose_seq, firehose_bytes = repo_ops.apply_writes(
		db, session["did"], [write], None
	)
	await atproto_repo.firehose_broadcast(request, (firehose_seq, firehose_bytes))


@web_routes.get("/links")
async def links_page(request: web.Request):
	ws = get_web_store(request)
	if not ws.get_app_enabled("links"):
		raise web.HTTPNotFound()
	db = get_db(request)
	profile = get_node_profile(db)
	links = _get_linktree(db, profile["did"]) if profile["did"] else []
	return render(request, "node_links.html", {"profile": profile, "links": links})


@web_routes.get("/links/edit")
async def links_edit_page(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	db = get_db(request)
	profile = get_node_profile(db)
	links = _get_linktree(db, session["did"])
	return render(request, "node_links_edit.html", {"profile": profile, "links": links, "error": None})


@web_routes.post("/links/edit")
async def links_edit_post(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	data = await request.post()
	titles = data.getall("title", [])
	urls = data.getall("url", [])
	platforms = data.getall("platform", [])
	links = []
	for i, (title, url) in enumerate(zip(titles, urls)):
		title = title.strip()
		url = url.strip()
		if title and url:
			platform = platforms[i].strip() if i < len(platforms) else "other"
			links.append({"title": title, "url": url, "platform": platform})
	await _save_linktree(request, session, links)
	raise web.HTTPFound("/links/edit")


# ── Places ────────────────────────────────────────────────────────────────────
#
# Lexicon: pub.places.pin
# Each record = one map pin with name, description, lat, lng, tags, optional URL.
# Pins are stored in the user's ATProto repo — portable + firehose-broadcastable.

def _get_places(db, did: str) -> list:
	"""Fetch pub.places.pin records from the ATProto repo."""
	user_id = db.con.execute("SELECT id FROM user WHERE did=?", (did,)).get
	if user_id is None:
		return []
	rows = db.con.execute(
		"SELECT rkey, value FROM record WHERE repo=? AND nsid=? ORDER BY rkey DESC",
		(user_id, PLACES_NSID),
	).fetchall()
	places = []
	for rkey, value in rows:
		try:
			rec = cbrrr.decode_dag_cbor(value)
			places.append({
				"rkey": rkey,
				"name": rec.get("name", ""),
				"description": rec.get("description", ""),
				"lat": float(rec.get("lat", 0)),
				"lng": float(rec.get("lng", 0)),
				"url": rec.get("url", ""),
				"tags": rec.get("tags", []),
				"created_at": rec.get("createdAt", ""),
				"ts_human": format_ts(rec.get("createdAt", "")),
				"at_uri": f"at://{did}/{PLACES_NSID}/{rkey}",
			})
		except Exception:
			pass
	return places


async def _announce_place(request: web.Request, session: dict, name: str, description: str, lat: float, lng: float):
	"""Post a bsky ping about a new place pin."""
	db = get_db(request)
	profile = get_node_profile(db)
	handle = profile.get("handle", "")

	maps_url = f"https://www.openstreetmap.org/?mlat={lat}&mlon={lng}&zoom=15"
	places_url = f"https://{handle}/places"

	body = f"📍 {name}"
	if description:
		body += f"\n{description[:160]}"
	body += f"\n\n{maps_url}"

	url_start = len(body.encode()) - len(maps_url.encode())
	url_end = len(body.encode())

	writes = [{
		"$type": "com.atproto.repo.applyWrites#create",
		"collection": "app.bsky.feed.post",
		"value": {
			"$type": "app.bsky.feed.post",
			"text": body,
			"createdAt": util.iso_string_now(),
			"langs": ["en"],
			"facets": [{
				"$type": "app.bsky.richtext.facet",
				"index": {
					"$type": "app.bsky.richtext.facet#byteSlice",
					"byteStart": url_start,
					"byteEnd": url_end,
				},
				"features": [{"$type": "app.bsky.richtext.facet#link", "uri": maps_url}],
			}],
		},
	}]
	res, seq, fbytes = repo_ops.apply_writes(db, session["did"], writes, None)
	await atproto_repo.firehose_broadcast(request, (seq, fbytes))


@web_routes.get("/places")
async def places_page(request: web.Request):
	ws = get_web_store(request)
	if not ws.get_app_enabled("places"):
		raise web.HTTPNotFound()
	db = get_db(request)
	profile = get_node_profile(db)
	places = _get_places(db, profile["did"]) if profile["did"] else []
	return render(request, "node_places.html", {"profile": profile, "places": places})


@web_routes.get("/places/new")
async def places_new_page(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	return render(request, "node_places_new.html", {"error": None})


@web_routes.post("/places/new")
async def places_new_post(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")

	data = await request.post()
	name        = data.get("name", "").strip()
	description = data.get("description", "").strip()
	lat_s       = data.get("lat", "").strip()
	lng_s       = data.get("lng", "").strip()
	url         = data.get("url", "").strip()
	tags_raw    = data.get("tags", "").strip()
	announce    = data.get("announce") == "1"

	if not name or not lat_s or not lng_s:
		return render(request, "node_places_new.html", {"error": "Name and location required"})

	try:
		lat = float(lat_s)
		lng = float(lng_s)
		if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
			raise ValueError()
	except ValueError:
		return render(request, "node_places_new.html", {"error": "Invalid coordinates"})

	tags = [t.strip() for t in tags_raw.replace(",", " ").split() if t.strip()]
	rkey = util.tid_now()
	record = {
		"$type": PLACES_NSID,
		"name": name,
		"lat": lat,
		"lng": lng,
		"createdAt": util.iso_string_now(),
	}
	if description: record["description"] = description
	if url:         record["url"] = url
	if tags:        record["tags"] = tags

	db = get_db(request)
	writes = [{"$type": "com.atproto.repo.applyWrites#create", "collection": PLACES_NSID, "rkey": rkey, "value": record}]
	res, seq, fbytes = repo_ops.apply_writes(db, session["did"], writes, None)
	await atproto_repo.firehose_broadcast(request, (seq, fbytes))

	if announce:
		try:
			await _announce_place(request, session, name, description, lat, lng)
		except Exception:
			pass

	raise web.HTTPFound("/places")


@web_routes.post("/places/{rkey}/delete")
async def places_delete(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	rkey = request.match_info["rkey"]
	db = get_db(request)
	writes = [{"$type": "com.atproto.repo.applyWrites#delete", "collection": PLACES_NSID, "rkey": rkey}]
	res, seq, fbytes = repo_ops.apply_writes(db, session["did"], writes, None)
	await atproto_repo.firehose_broadcast(request, (seq, fbytes))
	raise web.HTTPFound("/places")


# ── Dropbox ───────────────────────────────────────────────────────────────────
#
# Public file inbox: anyone can send a file + optional message.
# Owner reviews in /dropbox/inbox — accept (saves to files) or delete.

DROPBOX_MAX_MB = 20

@web_routes.get("/dropbox")
async def dropbox_public(request: web.Request):
	ws = get_web_store(request)
	if not ws.get_app_enabled("dropbox"):
		raise web.HTTPNotFound()
	db = get_db(request)
	profile = get_node_profile(db)
	return render(request, "node_dropbox.html", {"profile": profile, "sent": False, "error": None})


@web_routes.post("/dropbox")
async def dropbox_post(request: web.Request):
	ws = get_web_store(request)
	if not ws.get_app_enabled("dropbox"):
		raise web.HTTPNotFound()
	db = get_db(request)
	profile = get_node_profile(db)

	reader = await request.multipart()
	sender_name = ""
	message = ""
	file_data = None
	orig_name = "file"
	mime = "application/octet-stream"

	while True:
		field = await reader.next()
		if field is None:
			break
		if field.name == "sender_name":
			sender_name = (await field.read()).decode("utf-8", errors="replace").strip()
		elif field.name == "message":
			message = (await field.read()).decode("utf-8", errors="replace").strip()
		elif field.name == "file" and field.filename:
			orig_name = field.filename
			mime = field.headers.get("Content-Type", "") or mimetypes.guess_type(orig_name)[0] or "application/octet-stream"
			file_data = await field.read()

	if not file_data:
		return render(request, "node_dropbox.html", {"profile": profile, "sent": False, "error": "No file selected"})
	if len(file_data) > DROPBOX_MAX_MB * 1024 * 1024:
		return render(request, "node_dropbox.html", {"profile": profile, "sent": False, "error": f"File too large (max {DROPBOX_MAX_MB} MB)"})

	# Save to dropbox quarantine dir
	import pathlib
	dropbox_dir = pathlib.Path(MEDIA_DIR) / "dropbox"
	dropbox_dir.mkdir(exist_ok=True)
	ext = pathlib.Path(orig_name).suffix or ""
	safe_name = secrets.token_hex(14) + ext
	(dropbox_dir / safe_name).write_bytes(file_data)

	ws.save_dropbox_item(safe_name, orig_name, mime, len(file_data), sender_name, message)
	return render(request, "node_dropbox.html", {"profile": profile, "sent": True, "error": None})


@web_routes.get("/dropbox/inbox")
async def dropbox_inbox(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	ws = get_web_store(request)
	items = ws.list_dropbox_items()
	pending = [i for i in items if i["status"] == "pending"]
	accepted = [i for i in items if i["status"] == "accepted"]
	return render(request, "node_dropbox_inbox.html", {"pending": pending, "accepted": accepted})


@web_routes.post("/dropbox/{item_id}/accept")
async def dropbox_accept(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	item_id = int(request.match_info["item_id"])
	ws = get_web_store(request)
	item = ws.get_dropbox_item(item_id)
	if item and item["status"] == "pending":
		# Copy from dropbox quarantine into public media
		import pathlib, shutil
		src = pathlib.Path(MEDIA_DIR) / "dropbox" / item["filename"]
		dst = pathlib.Path(MEDIA_DIR) / item["filename"]
		if src.exists():
			shutil.copy2(src, dst)
		ws.save_file(item["filename"], item["orig_name"], item["mime_type"], item["size"], is_public=True)
		ws.accept_dropbox_item(item_id)
	raise web.HTTPFound("/dropbox/inbox")


@web_routes.post("/dropbox/{item_id}/delete")
async def dropbox_delete(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	item_id = int(request.match_info["item_id"])
	get_web_store(request).delete_dropbox_item(item_id)
	raise web.HTTPFound("/dropbox/inbox")


@web_routes.get("/dropbox/file/{filename}")
async def dropbox_file_serve(request: web.Request):
	"""Serve quarantine files — owner only."""
	session = get_session(request)
	if not session:
		raise web.HTTPForbidden()
	filename = request.match_info["filename"]
	import pathlib
	path = pathlib.Path(MEDIA_DIR) / "dropbox" / filename
	if not path.exists():
		raise web.HTTPNotFound()
	return web.FileResponse(path)


# ── App toggle ────────────────────────────────────────────────────────────────

@web_routes.post("/apps/{app}/toggle")
async def app_toggle(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	app_name = request.match_info["app"]
	ws = get_web_store(request)
	if app_name in ws.KNOWN_APPS and app_name != "compose":
		current = ws.get_app_enabled(app_name)
		ws.set_app_enabled(app_name, not current)
	raise web.HTTPFound("/dashboard")


# ── Node Settings ─────────────────────────────────────────────────────────────

@web_routes.get("/settings")
async def settings_page(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	return render(request, "node_settings.html", {"saved": False})


@web_routes.post("/settings")
async def settings_post(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	data = await request.post()
	ws = get_web_store(request)
	nickname = data.get("nickname", "").strip()[:64]
	pfp_url = data.get("pfp_url", "").strip()[:512]
	accent_color = data.get("accent_color", "").strip()
	ws.set_node_setting("nickname", nickname)
	ws.set_node_setting("pfp_url", pfp_url)
	# Only store valid hex colors
	import re as _re_local
	if accent_color and _re_local.match(r'^#[0-9a-fA-F]{6}$', accent_color):
		ws.set_node_setting("accent_color", accent_color)
	elif not accent_color:
		ws.set_node_setting("accent_color", "")
	return render(request, "node_settings.html", {"saved": True})
