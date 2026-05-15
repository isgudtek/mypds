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

# ── App keys ──────────────────────────────────────────────────────────────────

MILLIPDS_WEB_STORE = web.AppKey("MILLIPDS_WEB_STORE", WebStore)
MYPDS_PLUGINS = web.AppKey("MYPDS_PLUGINS", list)  # list of loaded plugin app names
MYPDS_PLUGIN_MANAGER = web.AppKey("MYPDS_PLUGIN_MANAGER")  # PluginManager instance

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


_INIT_EXEMPT_TEMPLATES = {"node_init.html", "node_login.html"}

def render(request: web.Request, template: str, ctx: dict = {}, status: int = 200) -> web.Response:
	jinja = get_jinja_env(request)
	session = get_session(request)
	ws = get_web_store(request)

	# First-run lock: until owner logs in for the first time, nothing is public
	if not ws.is_initialized() and not session and template not in _INIT_EXEMPT_TEMPLATES:
		raise web.HTTPFound("/")

	apps = ws.get_all_app_settings()
	_ns = ws.get_all_node_settings()
	node_settings = {
		**_ns,
		"nickname":     _ns.get("nickname", ""),
		"pfp_url":      _ns.get("pfp_url", ""),
		"accent_color": _ns.get("accent_color", ""),
		"defense_mode": _ns.get("defense_mode", "0"),
	}
	try:
		plugin_names = request.app[MYPDS_PLUGINS]
	except KeyError:
		plugin_names = []
	tmpl = jinja.get_template(template)
	html = tmpl.render(session=session, apps=apps, node_settings=node_settings, plugin_names=plugin_names, **ctx)
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


def _get_version() -> str:
	try:
		import importlib.metadata
		return f"mypds v{importlib.metadata.version('mypds')}"
	except Exception:
		return "mypds"


# ── Public routes ─────────────────────────────────────────────────────────────

@web_routes.get("/")
async def homepage(request: web.Request):
	ws = get_web_store(request)

	# First run: show init/jingle page before owner has ever logged in
	if not ws.is_initialized():
		session = get_session(request)
		if not session:
			db = get_db(request)
			profile = get_node_profile(db)
			return render(request, "node_init.html", {"profile": profile})

	db = get_db(request)
	profile = get_node_profile(db)
	posts = []
	if profile["did"]:
		posts = get_recent_posts(db, profile["did"])
		for p in posts:
			p["ts_human"] = format_ts(p["created_at"])

	version = _get_version()

	# cv widget data
	cv_widget = None
	try:
		import json as _json
		_raw = ws.get_node_setting("cv_data")
		_cv = _json.loads(_raw) if _raw else {}
		if _cv.get("public") or get_session(request):
			cv_widget = _cv
	except Exception:
		pass

	return render(request, "node_home.html", {
		"profile": profile,
		"posts": posts,
		"version": version,
		"cv_widget": cv_widget,
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

	return render(request, "node_info.html", {
		"profile": profile,
		"version": version,
		"post_count": post_count,
		"blob_count": blob_count,
	})


@web_routes.get("/media/{filename}")
async def media_serve(request: web.Request):
	filename = request.match_info["filename"]
	# Prevent directory traversal
	if "/" in filename or filename.startswith("."):
		raise web.HTTPForbidden()
	path = os.path.join(MEDIA_DIR, filename)
	if not os.path.exists(path):
		raise web.HTTPNotFound()
	return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


# ── Auth ──────────────────────────────────────────────────────────────────────

@web_routes.get("/login")
async def login_page(request: web.Request):
	ws = get_web_store(request)
	if not ws.is_initialized():
		raise web.HTTPFound("/")
	if get_session(request):
		raise web.HTTPFound(request.query.get("next", "/dashboard"))
	return render(request, "node_login.html", {"error": None, "next": request.query.get("next", "")})


@web_routes.post("/login")
async def login_post(request: web.Request):
	data = await request.post()
	identifier = data.get("identifier", "").strip()
	password = data.get("password", "")
	next_url = request.query.get("next", "/dashboard")
	# Only allow relative paths to prevent open redirect
	if not next_url.startswith("/"):
		next_url = "/dashboard"

	db = get_db(request)
	ws = get_web_store(request)
	first_run = not ws.is_initialized()

	try:
		did, handle = db.verify_account_login(identifier, password)
	except (KeyError, ValueError):
		if first_run:
			return render(request, "node_init.html", {"error": "Invalid credentials"})
		return render(request, "node_login.html", {"error": "Invalid credentials", "next": next_url}, status=401)

	if first_run:
		ws.mark_initialized()

	token = ws.create_session(did, handle)
	resp = redirect(next_url)
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
	version = _get_version()

	# count blobs
	user_id = db.con.execute("SELECT id FROM user WHERE did=?", (session["did"],)).get
	blob_count = db.con.execute(
		"SELECT COUNT(*) FROM blob WHERE repo=? AND refcount > 0", (user_id,)
	).get or 0

	# total data directory size
	import pathlib
	data_dir = pathlib.Path(MEDIA_DIR).parent
	total_bytes = sum(f.stat().st_size for f in data_dir.rglob("*") if f.is_file())
	if total_bytes < 1024 * 1024:
		data_size = f"{total_bytes / 1024:.0f} KB"
	elif total_bytes < 1024 ** 3:
		data_size = f"{total_bytes / 1024 / 1024:.1f} MB"
	else:
		data_size = f"{total_bytes / 1024 ** 3:.2f} GB"

	return render(request, "node_dashboard.html", {
		"profile": profile,
		"posts": posts,
		"blob_count": blob_count,
		"data_size": data_size,
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


# ── App toggle ────────────────────────────────────────────────────────────────

@web_routes.post("/apps/{app}/toggle")
async def app_toggle(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	app_name = request.match_info["app"]
	ws = get_web_store(request)
	manager = request.app.get(MYPDS_PLUGIN_MANAGER)
	if app_name != "compose" and manager and manager.plugin_exists(app_name):
		current = ws.get_app_enabled(app_name)
		enabling = not current
		ws.set_app_enabled(app_name, enabling)
		if enabling:
			manager.start(app_name)
		else:
			manager.stop(app_name)
	raise web.HTTPFound("/dashboard")


# ── Plugin Settings ───────────────────────────────────────────────────────────

@web_routes.get("/plugins/{name}/settings")
async def plugin_settings_get(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound(f"/login?next=/plugins/{request.match_info['name']}/settings")
	name = request.match_info["name"]
	plugin_names = request.app.get(MYPDS_PLUGINS, [])
	if name not in plugin_names:
		raise web.HTTPNotFound()
	import importlib
	mod = importlib.import_module(f"mypds.plugins.{name}")
	schema = getattr(mod, "SETTINGS", [])
	ws = get_web_store(request)
	values = {s["key"]: ws.get_plugin_setting(name, s["key"], s.get("default", "")) for s in schema}
	return render(request, "node_plugin_settings.html", {
		"plugin_name": name,
		"schema": schema,
		"values": values,
		"saved": False,
	})


@web_routes.post("/plugins/{name}/settings")
async def plugin_settings_post(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	name = request.match_info["name"]
	plugin_names = request.app.get(MYPDS_PLUGINS, [])
	if name not in plugin_names:
		raise web.HTTPNotFound()
	import importlib
	mod = importlib.import_module(f"mypds.plugins.{name}")
	schema = getattr(mod, "SETTINGS", [])
	ws = get_web_store(request)
	data = await request.post()
	for s in schema:
		key = s["key"]
		if s["type"] == "bool":
			ws.set_plugin_setting(name, key, "1" if data.get(key) else "0")
		else:
			ws.set_plugin_setting(name, key, data.get(key, s.get("default", "")))
	values = {s["key"]: ws.get_plugin_setting(name, s["key"], s.get("default", "")) for s in schema}
	return render(request, "node_plugin_settings.html", {
		"plugin_name": name,
		"schema": schema,
		"values": values,
		"saved": True,
	})


# ── Node Settings ─────────────────────────────────────────────────────────────

@web_routes.get("/settings")
async def settings_page(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	dob = get_db(request).get_birthdate(session["did"])
	return render(request, "node_settings.html", {"saved": False, "pw_error": None, "pw_saved": False, "dob": dob})


@web_routes.post("/settings")
async def settings_post(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	data = await request.post()
	ws = get_web_store(request)
	import re as _re_local

	# ── Identity / appearance ──────────────────────────────────────────────
	nickname = data.get("nickname", "").strip()[:64]
	pfp_url = data.get("pfp_url", "").strip()[:512]
	accent_color = data.get("accent_color", "").strip()
	ws.set_node_setting("nickname", nickname)
	ws.set_node_setting("pfp_url", pfp_url)
	if accent_color and _re_local.match(r'^#[0-9a-fA-F]{6}$', accent_color):
		ws.set_node_setting("accent_color", accent_color)
	elif not accent_color:
		ws.set_node_setting("accent_color", "")

	# ── Defense mode ──────────────────────────────────────────────────────
	defense_mode = "1" if data.get("defense_mode") == "1" else "0"
	ws.set_node_setting("defense_mode", defense_mode)

	# ── Date of birth (stored in ATProto user.birthdate) ──────────────────
	dob = data.get("dob", "").strip()
	if dob and _re_local.match(r'^\d{4}-\d{2}-\d{2}$', dob):
		get_db(request).set_birthdate(session["did"], dob)

	# ── Password change ────────────────────────────────────────────────────
	old_pw = data.get("old_password", "").strip()
	new_pw = data.get("new_password", "").strip()
	confirm_pw = data.get("confirm_password", "").strip()
	pw_error = None
	pw_saved = False
	if old_pw or new_pw or confirm_pw:
		if not old_pw:
			pw_error = "Enter your current password."
		elif not new_pw:
			pw_error = "Enter a new password."
		elif len(new_pw) < 8:
			pw_error = "New password must be at least 8 characters."
		elif new_pw != confirm_pw:
			pw_error = "New passwords don't match."
		else:
			try:
				get_db(request).change_account_password(session["did"], old_pw, new_pw)
				pw_saved = True
			except ValueError:
				pw_error = "Current password is incorrect."

	current_dob = get_db(request).get_birthdate(session["did"])
	return render(request, "node_settings.html", {"saved": True, "pw_error": pw_error, "pw_saved": pw_saved, "dob": current_dob})


