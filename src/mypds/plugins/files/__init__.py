import os
import sqlite3
import secrets
import mimetypes
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from mypds.app_util import get_db
from mypds.web import render, get_session, get_node_profile
from mypds.web_store import MEDIA_DIR
from mypds import static_config

APP_NAME = "files"
NSID     = None

routes = web.RouteTableDef()

_DB_PATH = static_config.DATA_DIR + "/plugins/files.sqlite3"
_con: sqlite3.Connection | None = None


def _get_con() -> sqlite3.Connection:
	global _con
	if _con is None:
		Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
		_con = sqlite3.connect(_DB_PATH, check_same_thread=False)
		_con.row_factory = sqlite3.Row
		_con.execute("PRAGMA journal_mode=WAL")
		_con.execute("""
			CREATE TABLE IF NOT EXISTS media_file (
				id         INTEGER PRIMARY KEY AUTOINCREMENT,
				filename   TEXT NOT NULL UNIQUE,
				orig_name  TEXT NOT NULL,
				mime_type  TEXT NOT NULL,
				size       INTEGER NOT NULL,
				is_public  INTEGER NOT NULL DEFAULT 1,
				created_at TEXT NOT NULL
			)
		""")
		_con.commit()
	return _con


def _list_files():
	return [dict(r) for r in _get_con().execute(
		"SELECT * FROM media_file ORDER BY created_at DESC"
	).fetchall()]


def _save_file(filename, orig_name, mime, size, is_public=True):
	con = _get_con()
	con.execute(
		"INSERT INTO media_file(filename, orig_name, mime_type, size, is_public, created_at) VALUES (?,?,?,?,?,?)",
		(filename, orig_name, mime, size, int(is_public),
		 datetime.now(timezone.utc).isoformat()),
	)
	con.commit()


def _delete_file(file_id):
	con = _get_con()
	row = con.execute("SELECT filename FROM media_file WHERE id=?", (file_id,)).fetchone()
	if row:
		path = os.path.join(MEDIA_DIR, row["filename"])
		if os.path.exists(path):
			os.remove(path)
	con.execute("DELETE FROM media_file WHERE id=?", (file_id,))
	con.commit()


def _toggle_visibility(file_id):
	con = _get_con()
	con.execute("UPDATE media_file SET is_public = NOT is_public WHERE id=?", (file_id,))
	con.commit()


@routes.get("/files")
async def files_page(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	return render(request, "plugin/files/main.html", {"files": _list_files(), "error": None})


@routes.post("/files/upload")
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

	_save_file(safe_name, orig_name, mime, size, is_public=True)
	raise web.HTTPFound("/files")


@routes.post("/files/{file_id}/delete")
async def files_delete(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	_delete_file(int(request.match_info["file_id"]))
	raise web.HTTPFound("/files")


@routes.post("/files/{file_id}/toggle")
async def files_toggle(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	_toggle_visibility(int(request.match_info["file_id"]))
	raise web.HTTPFound("/files")


if __name__ == "__main__":
	from mypds.plugin_runner import run_plugin
	run_plugin(routes, APP_NAME)
