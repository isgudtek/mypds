import sqlite3
import secrets
import mimetypes
import shutil
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from mypds.app_util import get_db
from mypds.web import render, get_session, get_node_profile
from mypds.web_store import MEDIA_DIR
from mypds import static_config

APP_NAME    = "dropbox"
NSID        = None
MAX_MB      = 20

routes = web.RouteTableDef()

_DB_PATH = static_config.DATA_DIR + "/plugins/dropbox.sqlite3"
_con: sqlite3.Connection | None = None


def _get_con() -> sqlite3.Connection:
	global _con
	if _con is None:
		Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
		_con = sqlite3.connect(_DB_PATH, check_same_thread=False)
		_con.row_factory = sqlite3.Row
		_con.execute("PRAGMA journal_mode=WAL")
		_con.execute("""
			CREATE TABLE IF NOT EXISTS dropbox_item (
				id          INTEGER PRIMARY KEY AUTOINCREMENT,
				filename    TEXT NOT NULL,
				orig_name   TEXT NOT NULL,
				mime_type   TEXT NOT NULL,
				size        INTEGER NOT NULL,
				sender_name TEXT NOT NULL DEFAULT '',
				message     TEXT NOT NULL DEFAULT '',
				status      TEXT NOT NULL DEFAULT 'pending',
				created_at  TEXT NOT NULL
			)
		""")
		_con.commit()
	return _con


def _save_item(filename, orig_name, mime, size, sender_name, message):
	con = _get_con()
	con.execute(
		"INSERT INTO dropbox_item(filename, orig_name, mime_type, size, sender_name, message, status, created_at) "
		"VALUES (?,?,?,?,?,?,'pending',?)",
		(filename, orig_name, mime, size, sender_name, message,
		 datetime.now(timezone.utc).isoformat()),
	)
	con.commit()


def _list_items():
	return [dict(r) for r in _get_con().execute(
		"SELECT * FROM dropbox_item ORDER BY created_at DESC"
	).fetchall()]


def _get_item(item_id):
	row = _get_con().execute("SELECT * FROM dropbox_item WHERE id=?", (item_id,)).fetchone()
	return dict(row) if row else None


def _accept_item(item_id):
	con = _get_con()
	con.execute("UPDATE dropbox_item SET status='accepted' WHERE id=?", (item_id,))
	con.commit()


def _delete_item(item_id):
	con = _get_con()
	row = con.execute("SELECT filename FROM dropbox_item WHERE id=?", (item_id,)).fetchone()
	if row:
		path = Path(MEDIA_DIR) / "dropbox" / row["filename"]
		if path.exists():
			path.unlink()
	con.execute("DELETE FROM dropbox_item WHERE id=?", (item_id,))
	con.commit()


@routes.get("/dropbox")
async def dropbox_public(request: web.Request):
	db = get_db(request)
	profile = get_node_profile(db)
	return render(request, "plugin/dropbox/main.html", {"profile": profile, "sent": False, "error": None})


@routes.post("/dropbox")
async def dropbox_post(request: web.Request):
	db = get_db(request)
	profile = get_node_profile(db)

	reader = await request.multipart()
	sender_name = message = ""
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
		return render(request, "plugin/dropbox/main.html", {"profile": profile, "sent": False, "error": "No file selected"})
	if len(file_data) > MAX_MB * 1024 * 1024:
		return render(request, "plugin/dropbox/main.html", {"profile": profile, "sent": False, "error": f"File too large (max {MAX_MB} MB)"})

	dropbox_dir = Path(MEDIA_DIR) / "dropbox"
	dropbox_dir.mkdir(exist_ok=True)
	ext = Path(orig_name).suffix or ""
	safe_name = secrets.token_hex(14) + ext
	(dropbox_dir / safe_name).write_bytes(file_data)
	_save_item(safe_name, orig_name, mime, len(file_data), sender_name, message)
	return render(request, "plugin/dropbox/main.html", {"profile": profile, "sent": True, "error": None})


@routes.get("/dropbox/inbox")
async def dropbox_inbox(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	items = _list_items()
	pending  = [i for i in items if i["status"] == "pending"]
	accepted = [i for i in items if i["status"] == "accepted"]
	return render(request, "plugin/dropbox/inbox.html", {"pending": pending, "accepted": accepted})


@routes.post("/dropbox/{item_id}/accept")
async def dropbox_accept(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	item_id = int(request.match_info["item_id"])
	item = _get_item(item_id)
	if item and item["status"] == "pending":
		src = Path(MEDIA_DIR) / "dropbox" / item["filename"]
		dst = Path(MEDIA_DIR) / item["filename"]
		if src.exists():
			shutil.copy2(src, dst)
		_accept_item(item_id)
	raise web.HTTPFound("/dropbox/inbox")


@routes.post("/dropbox/{item_id}/delete")
async def dropbox_delete(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	_delete_item(int(request.match_info["item_id"]))
	raise web.HTTPFound("/dropbox/inbox")


@routes.get("/dropbox/file/{filename}")
async def dropbox_file_serve(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPForbidden()
	filename = request.match_info["filename"]
	path = Path(MEDIA_DIR) / "dropbox" / filename
	if not path.exists():
		raise web.HTTPNotFound()
	return web.FileResponse(path)


if __name__ == "__main__":
	from mypds.plugin_runner import run_plugin
	run_plugin(routes, APP_NAME)
