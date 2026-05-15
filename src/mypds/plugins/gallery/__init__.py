import os
import secrets
import mimetypes
import hashlib
from pathlib import Path

from aiohttp import web
import cbrrr

from mypds.app_util import get_db
from mypds.web import render, get_session, get_node_profile
from mypds.web_store import MEDIA_DIR
from mypds import repo_ops, atproto_repo, util

APP_NAME = "gallery"
NSID     = "pub.gallery.image"

routes = web.RouteTableDef()


def _store_blob(db, did: str, data: bytes, mime: str):
	user_id = db.con.execute("SELECT id FROM user WHERE did=?", (did,)).get
	if user_id is None:
		return None
	db.con.execute("INSERT INTO blob (repo, refcount) VALUES (?, 0)", (user_id,))
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
	user_id = db.con.execute("SELECT id FROM user WHERE did=?", (did,)).get
	if user_id is None:
		return []
	rows = db.con.execute(
		"SELECT rkey, value FROM record WHERE repo=? AND nsid=? ORDER BY rkey DESC LIMIT ?",
		(user_id, NSID, limit),
	).fetchall()
	images = []
	for rkey, value in rows:
		try:
			rec = cbrrr.decode_dag_cbor(value)
			img_blob = rec.get("image", {})
			blob_ref = img_blob.get("ref", {})
			cid_link = blob_ref.get("$link") if isinstance(blob_ref, dict) else None
			images.append({
				"rkey":        rkey,
				"title":       rec.get("title", ""),
				"description": rec.get("description", ""),
				"tags":        rec.get("tags", []),
				"created_at":  rec.get("createdAt", ""),
				"mime":        img_blob.get("mimeType", "image/jpeg"),
				"cid":         cid_link,
				"webUrl":      rec.get("webUrl", ""),
				"at_uri":      f"at://{did}/{NSID}/{rkey}",
			})
		except Exception:
			pass
	return images


@routes.get("/gallery")
async def gallery_page(request: web.Request):
	db = get_db(request)
	profile = get_node_profile(db)
	images = _get_gallery(db, profile["did"]) if profile["did"] else []
	return render(request, "plugin/gallery/main.html", {"profile": profile, "images": images})


@routes.get("/gallery/upload")
async def gallery_upload_page(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	return render(request, "plugin/gallery/upload.html", {"error": None})


@routes.post("/gallery/upload")
async def gallery_upload_post(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")

	reader = await request.multipart()
	title = description = tags_raw = ""
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
		return render(request, "plugin/gallery/upload.html", {"error": "No image selected"})
	if len(image_data) > 10 * 1024 * 1024:
		return render(request, "plugin/gallery/upload.html", {"error": "Image too large (max 10 MB)"})

	db = get_db(request)
	cid = _store_blob(db, session["did"], image_data, image_mime)
	if cid is None:
		return render(request, "plugin/gallery/upload.html", {"error": "Failed to store image blob"})

	ext = Path(image_name).suffix or ".jpg"
	safe_name = secrets.token_hex(12) + ext
	dest = os.path.join(MEDIA_DIR, safe_name)
	with open(dest, "wb") as f:
		f.write(image_data)

	tags = [t.strip() for t in tags_raw.replace(",", " ").split() if t.strip()]
	rkey = util.tid_now()
	record = {
		"$type":       NSID,
		"title":       title or Path(image_name).stem,
		"description": description,
		"tags":        tags,
		"createdAt":   util.iso_string_now(),
		"image": {
			"$type":    "blob",
			"ref":      {"$link": cid.encode()},
			"mimeType": image_mime,
			"size":     len(image_data),
		},
		"webUrl": f"/media/{safe_name}",
	}
	writes = [{"$type": "com.atproto.repo.applyWrites#create", "collection": NSID, "rkey": rkey, "value": record}]
	res, seq, fbytes = repo_ops.apply_writes(db, session["did"], writes, None)
	await atproto_repo.firehose_broadcast(request, (seq, fbytes))
	raise web.HTTPFound("/gallery")


@routes.post("/gallery/{rkey}/delete")
async def gallery_delete(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	rkey = request.match_info["rkey"]
	db = get_db(request)
	writes = [{"$type": "com.atproto.repo.applyWrites#delete", "collection": NSID, "rkey": rkey}]
	res, seq, fbytes = repo_ops.apply_writes(db, session["did"], writes, None)
	await atproto_repo.firehose_broadcast(request, (seq, fbytes))
	raise web.HTTPFound("/gallery")


if __name__ == "__main__":
	from mypds.plugin_runner import run_plugin
	run_plugin(routes, APP_NAME)
