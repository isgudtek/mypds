from aiohttp import web
import cbrrr

from mypds.app_util import get_db
from mypds.web import render, get_session, get_node_profile, format_ts
from mypds import repo_ops, atproto_repo, util

APP_NAME = "places"
NSID     = "pub.places.pin"

routes = web.RouteTableDef()


def _get_places(db, did: str) -> list:
	user_id = db.con.execute("SELECT id FROM user WHERE did=?", (did,)).get
	if user_id is None:
		return []
	rows = db.con.execute(
		"SELECT rkey, value FROM record WHERE repo=? AND nsid=? ORDER BY rkey DESC",
		(user_id, NSID),
	).fetchall()
	places = []
	for rkey, value in rows:
		try:
			rec = cbrrr.decode_dag_cbor(value)
			places.append({
				"rkey":        rkey,
				"name":        rec.get("name", ""),
				"description": rec.get("description", ""),
				"lat":         float(rec.get("lat", 0)),
				"lng":         float(rec.get("lng", 0)),
				"url":         rec.get("url", ""),
				"tags":        rec.get("tags", []),
				"created_at":  rec.get("createdAt", ""),
				"ts_human":    format_ts(rec.get("createdAt", "")),
				"at_uri":      f"at://{did}/{NSID}/{rkey}",
			})
		except Exception:
			pass
	return places


async def _announce_place(request, session, name, description, lat, lng):
	db = get_db(request)
	profile = get_node_profile(db)
	handle = profile.get("handle", "")
	maps_url = f"https://www.openstreetmap.org/?mlat={lat}&mlon={lng}&zoom=15"
	body = f"📍 {name}"
	if description:
		body += f"\n{description[:160]}"
	body += f"\n\n{maps_url}"
	url_start = len(body.encode()) - len(maps_url.encode())
	url_end   = len(body.encode())
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
				"index": {"$type": "app.bsky.richtext.facet#byteSlice", "byteStart": url_start, "byteEnd": url_end},
				"features": [{"$type": "app.bsky.richtext.facet#link", "uri": maps_url}],
			}],
		},
	}]
	res, seq, fbytes = repo_ops.apply_writes(db, session["did"], writes, None)
	await atproto_repo.firehose_broadcast(request, (seq, fbytes))


@routes.get("/places")
async def places_page(request: web.Request):
	db = get_db(request)
	profile = get_node_profile(db)
	places = _get_places(db, profile["did"]) if profile["did"] else []
	return render(request, "plugin/places/main.html", {"profile": profile, "places": places})


@routes.get("/places/new")
async def places_new_page(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	return render(request, "plugin/places/new.html", {"error": None})


@routes.post("/places/new")
async def places_new_post(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")

	data        = await request.post()
	name        = data.get("name", "").strip()
	description = data.get("description", "").strip()
	lat_s       = data.get("lat", "").strip()
	lng_s       = data.get("lng", "").strip()
	url         = data.get("url", "").strip()
	tags_raw    = data.get("tags", "").strip()
	announce    = data.get("announce") == "1"

	if not name or not lat_s or not lng_s:
		return render(request, "plugin/places/new.html", {"error": "Name and location required"})
	try:
		lat = float(lat_s); lng = float(lng_s)
		if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
			raise ValueError()
	except ValueError:
		return render(request, "plugin/places/new.html", {"error": "Invalid coordinates"})

	tags = [t.strip() for t in tags_raw.replace(",", " ").split() if t.strip()]
	rkey = util.tid_now()
	record = {
		"$type": NSID, "name": name,
		"lat": f"{lat:.6f}", "lng": f"{lng:.6f}",
		"createdAt": util.iso_string_now(),
	}
	if description: record["description"] = description
	if url:         record["url"] = url
	if tags:        record["tags"] = tags

	db = get_db(request)
	writes = [{"$type": "com.atproto.repo.applyWrites#create", "collection": NSID, "rkey": rkey, "value": record}]
	res, seq, fbytes = repo_ops.apply_writes(db, session["did"], writes, None)
	await atproto_repo.firehose_broadcast(request, (seq, fbytes))

	if announce:
		try:
			await _announce_place(request, session, name, description, lat, lng)
		except Exception:
			pass
	raise web.HTTPFound("/places")


@routes.post("/places/{rkey}/delete")
async def places_delete(request: web.Request):
	session = get_session(request)
	if not session:
		raise web.HTTPFound("/login")
	rkey = request.match_info["rkey"]
	db = get_db(request)
	writes = [{"$type": "com.atproto.repo.applyWrites#delete", "collection": NSID, "rkey": rkey}]
	res, seq, fbytes = repo_ops.apply_writes(db, session["did"], writes, None)
	await atproto_repo.firehose_broadcast(request, (seq, fbytes))
	raise web.HTTPFound("/places")


if __name__ == "__main__":
	from mypds.plugin_runner import run_plugin
	run_plugin(routes, APP_NAME)
