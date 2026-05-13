from typing import Optional, Set, Tuple
import importlib.metadata
import logging
import asyncio
import time
import os
import io
import json
import uuid
import hashlib
import re
from pathlib import Path
from datetime import datetime, timezone

import apsw
import aiohttp
from aiohttp_middlewares.cors import cors_middleware
from aiohttp import web
import jwt
from jinja2 import Environment, FileSystemLoader, ChoiceLoader, PrefixLoader

import cbrrr

from . import static_config
from . import database
from . import auth_oauth
from . import atproto_sync
from . import atproto_repo
from . import crypto
from . import util
from .appview_proxy import service_proxy
from .auth_bearer import authenticated, verify_symmetric_token
from .app_util import *
from .did import DIDResolver
import importlib
from .web import web_routes, MILLIPDS_WEB_STORE, MYPDS_PLUGINS
from .web_store import WebStore

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()


def get_version_string() -> str:
	return f"mypds v{importlib.metadata.version('mypds')}"


PROXY_OVERRIDE_PATHS = [
	# bsky-specific hack - appview does not implement these routes
	"/xrpc/app.bsky.actor.getPreferences",
	"/xrpc/app.bsky.actor.putPreferences",
]


@web.middleware
async def atproto_service_proxy_middleware(request: web.Request, handler):
	# if the PDS has split RS/AS config, enforce that separation
	is_as_route = getattr(handler, "_is_as_route", False)
	cfg = get_db(request).config
	if cfg["auth_pfx"] != cfg["pds_pfx"]:
		is_as_request = request.host == util.hostname_from_url(cfg["auth_pfx"])
		if is_as_request != is_as_route:
			raise web.HTTPNotFound()

	# https://atproto.com/specs/xrpc#service-proxying
	atproto_proxy = request.headers.get("atproto-proxy")
	if (
		atproto_proxy
		and request.path not in PROXY_OVERRIDE_PATHS
		and not request.path.startswith("/xrpc/com.atproto.")
		and not is_as_route
	):
		return await service_proxy(request, atproto_proxy)

	# else, normal response
	res: web.Response = await handler(request)

	# track external app logins via XRPC calls
	if (
		request.path.startswith("/xrpc/")
		and request.headers.get("Authorization")
		and res.status < 400
	):
		referer = request.headers.get("Referer", "")
		if referer:
			try:
				from urllib.parse import urlparse
				domain = urlparse(referer).netloc.lstrip("www.")
				nsid = request.path[len("/xrpc/"):]
				if domain and nsid and "." in nsid:
					ws = request.app[MILLIPDS_WEB_STORE]
					ws.track_app_call(domain, nsid)
			except Exception:
				pass

	# inject security headers (this should really be a separate middleware, but here works too)
	# skip for static files — they are not HTML documents and don't need document-level CSP
	is_static = request.path.startswith("/static/") or request.path == "/favicon.ico"
	res.headers.setdefault("X-Frame-Options", "DENY")  # prevent clickjacking
	res.headers.setdefault(
		"X-Content-Type-Options", "nosniff"
	)  # prevent XSS (almost vestigial at this point, I think)
	if not is_static:
		res.headers.setdefault(
			"Content-Security-Policy", "default-src 'none'; sandbox"
		)  # prevent everything for non-static responses
	# NB: HSTS and other TLS-related headers not set, set them in nginx or wherever you terminate TLS

	return res


@routes.get("/")
async def hello(request: web.Request):
	msg = f"""
                          ,dPYb, ,dPYb,                           8I
                          IP'`Yb IP'`Yb                           8I
                     gg   I8  8I I8  8I  gg                       8I
                     ""   I8  8' I8  8'  ""                       8I
  ,ggg,,ggg,,ggg,    gg   I8 dP  I8 dP   gg   gg,gggg,      ,gggg,8I     ,gg,
 ,8" "8P" "8P" "8,   88   I8dP   I8dP    88   I8P"  "Yb    dP"  "Y8I   ,8'8,
 I8   8I   8I   8I   88   I8P    I8P     88   I8'    ,8i  i8'    ,8I  ,8'  Yb
,dP   8I   8I   Yb,_,88,_,d8b,_ ,d8b,_ _,88,_,I8 _  ,d8' ,d8,   ,d8b,,8'_   8)
8P'   8I   8I   `Y88P""Y88P'"Y888P'"Y888P""Y8PI8 YY88888PP"Y8888P"`Y8P' "YY8P8P
                                              I8
                                              I8
                                              I8
                                              I8
                                              I8
                                              I8


Hello! This is an ATProto PDS instance, running {get_version_string()}

https://github.com/DavidBuchanan314/millipds
"""

	return web.Response(text=msg)


@routes.get("/.well-known/atproto-did")
async def well_known_atproto_did(request: web.Request):
	handle = request.host.split(":")[0]  # strip port if present
	db = get_db(request)
	did = db.did_by_handle(handle)
	if did is None:
		raise web.HTTPNotFound(text="handle not found")
	return web.Response(text=did, content_type="text/plain")


@routes.get(
	"/.well-known/did.json"
)  # serve this server's did:web document (nb: reference PDS impl doesn't do this, hard to know the right thing to do)
async def well_known_did_web(request: web.Request):
	cfg = get_db(request).config
	return web.json_response(
		{
			"@context": [
				"https://www.w3.org/ns/did/v1",
			],
			"id": cfg["pds_did"],
			"service": [
				{  # is this the right thing to do?
					"id": "#atproto_pds",
					"type": "AtprotoPersonalDataServer",
					"serviceEndpoint": cfg["pds_pfx"],
				}
			],
		}
	)


@routes.get("/robots.txt")
async def robots_txt(request: web.Request):
	return web.Response(
		text="""\
# this is an atproto pds. please crawl it.

User-Agent: *
Allow: /
"""
	)


# browsers love to request this unprompted, so here's an answer for them
@routes.get("/favicon.ico")
async def favicon(request: web.Request):
	return web.Response(
		text="""
			<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
				<text x="50%" y="0.95em" font-size="90" text-anchor="middle">🌐</text>
			</svg>
		""",
		content_type="image/svg+xml",
		headers={"Cache-Control": "max-age=864000"},
	)


# not a spec'd endpoint, but the reference impl has this too
@routes.get("/xrpc/_health")
async def health(request: web.Request):
	return web.json_response({"version": get_version_string()})


# we should not be implementing bsky-specific logic here!
# (ideally, a PDS should not be aware of app-specific logic)
@routes.post("/xrpc/app.bsky.actor.putPreferences")
@authenticated
async def actor_put_preferences(request: web.Request):
	# NOTE: we don't try to pull out the specific "preferences" field
	prefs = await request.json()
	pref_bytes = util.compact_json(prefs)
	db = get_db(request)
	db.con.execute(
		"UPDATE user SET prefs=? WHERE did=?",
		(pref_bytes, request["authed_did"]),
	)
	return web.Response()


@routes.get("/xrpc/app.bsky.actor.getPreferences")
@authenticated
async def actor_get_preferences(request: web.Request):
	db = get_db(request)
	row = db.con.execute(
		"SELECT prefs FROM user WHERE did=?", (request["authed_did"],)
	).fetchone()

	# should be impossible, otherwise we wouldn't be auth'd
	assert row is not None

	prefs = json.loads(row[0])

	# TODO: in the future™ this will be unnecessary because we initialize it
	# properly during account creation and/or I wrote a db migration script
	if not prefs:
		prefs = {"preferences": []}

	return web.json_response(prefs)


@routes.get("/xrpc/com.atproto.identity.resolveHandle")
async def identity_resolve_handle(request: web.Request):
	handle = request.query.get("handle")
	if handle is None:
		raise web.HTTPBadRequest(text="missing or invalid handle")

	did = get_db(request).did_by_handle(handle)
	if not did:
		# forward to appview (TODO: resolve it ourself?)
		return await service_proxy(request)

	# TODO: set cache control response headers?
	return web.json_response({"did": did})


@routes.get("/xrpc/com.atproto.server.describeServer")
async def server_describe_server(request: web.Request):
	return web.json_response(
		{
			"did": get_db(request).config["pds_did"],
			"availableUserDomains": [],
			"version": get_version_string(),  # off-spec
		}
	)


def session_info(request: web.Request) -> dict:
	return {
		"handle": get_db(request).handle_by_did(request["authed_did"]),
		"did": request["authed_did"],
		# we specify a fake email and claim it's verified, because otherwise
		# bsky.app would nag us to verify it
		"email": "tfw_no@email.invalid",
		"emailConfirmed": True,
		# "didDoc": {}, # iiuc this is only used for entryway usecase?
	}


def generate_session_tokens(request: web.Request) -> dict:
	db = get_db(request)
	unix_seconds_now = int(time.time())
	# use the same jti for both tokens, so revoking one revokes both
	jti = str(uuid.uuid4())
	access_jwt = jwt.encode(
		{
			"scope": "com.atproto.access",
			"aud": db.config["pds_did"],
			"sub": request["authed_did"],
			"iat": unix_seconds_now,
			"exp": unix_seconds_now + static_config.ACCESS_EXP,
			"jti": jti,
		},
		db.config["jwt_access_secret"],
		"HS256",
	)

	refresh_jwt = jwt.encode(
		{
			"scope": "com.atproto.refresh",
			"aud": db.config["pds_did"],
			"sub": request["authed_did"],
			"iat": unix_seconds_now,
			"exp": unix_seconds_now + static_config.REFRESH_EXP,
			"jti": jti,
		},
		db.config["jwt_access_secret"],
		"HS256",
	)

	return {
		"accessJwt": access_jwt,
		"refreshJwt": refresh_jwt,
	}


# TODO: ratelimit this!!!
@routes.post("/xrpc/com.atproto.server.createSession")
async def server_create_session(request: web.Request):
	# extract the args
	try:
		req_json: dict = await request.json()
	except json.JSONDecodeError:
		raise web.HTTPBadRequest(text="expected JSON")

	identifier = req_json.get("identifier")
	password = req_json.get("password")
	if not (isinstance(identifier, str) and isinstance(password, str)):
		raise web.HTTPBadRequest(text="invalid identifier or password")

	# do authentication
	db = get_db(request)
	try:
		did, handle = db.verify_account_login(
			did_or_handle=identifier, password=password
		)
	except KeyError:
		raise web.HTTPUnauthorized(text="user not found")
	except ValueError:
		raise web.HTTPUnauthorized(text="incorrect identifier or password")

	# both generate_session_tokens and session_info need this
	request["authed_did"] = did

	return web.json_response(
		session_info(request) | generate_session_tokens(request)
	)


@routes.post("/xrpc/com.atproto.server.refreshSession")
async def server_refresh_session(request: web.Request):
	auth = request.headers.get("Authorization", "")
	if not auth.startswith("Bearer "):
		raise web.HTTPUnauthorized(text="invalid auth type")
	token = auth.removeprefix("Bearer ")
	token_payload = verify_symmetric_token(
		request, token, "com.atproto.refresh"
	)
	request["authed_did"] = token_payload["sub"]

	get_db(request).con.execute(
		"INSERT INTO revoked_token (did, jti, expires_at) VALUES (?, ?, ?)",
		(token_payload["sub"], token_payload["jti"], token_payload["exp"]),
	)
	return web.json_response(
		session_info(request) | generate_session_tokens(request)
	)


# NOTE: deleteSession requires refresh token as auth, not access token
@routes.post("/xrpc/com.atproto.server.deleteSession")
async def server_delete_session(request: web.Request):
	auth = request.headers.get("Authorization", "")
	if not auth.startswith("Bearer "):
		raise web.HTTPUnauthorized(text="invalid auth type")
	token = auth.removeprefix("Bearer ")
	token_payload = verify_symmetric_token(
		request, token, "com.atproto.refresh"
	)

	# because (for now?) we set the same JTI in access tokens and refresh tokens,
	# revoking one revokes both
	get_db(request).con.execute(
		"INSERT INTO revoked_token (did, jti, expires_at) VALUES (?, ?, ?)",
		(token_payload["sub"], token_payload["jti"], token_payload["exp"]),
	)

	return web.Response()


@routes.get("/xrpc/com.atproto.server.getServiceAuth")
@authenticated
async def server_get_service_auth(request: web.Request):
	aud = request.query.get("aud")
	lxm = request.query.get("lxm")

	# default to 60s into the future
	now = int(time.time())
	exp = int(request.query.get("exp", now + 60))

	# lxm is not required by the lexicon but I'm requiring it anyway
	if not (aud and lxm):
		raise web.HTTPBadRequest(text="missing aud or lxm")
	if lxm == "com.atproto.server.getServiceAuth":
		raise web.HTTPBadRequest(text="can't generate auth tokens recursively!")

	max_exp = now + 60 * 30  # 30 mins
	if exp > max_exp:
		logger.info(
			f"requested exp too far into the future, truncating to {max_exp}"
		)
		exp = max_exp

	# TODO: strict validation of aud and lxm?

	db = get_db(request)
	signing_key = db.signing_key_pem_by_did(request["authed_did"])
	assert signing_key is not None
	return web.json_response(
		{
			"token": jwt.encode(
				{
					"iss": request["authed_did"],
					"aud": aud,
					"lxm": lxm,
					"exp": exp,
					"iat": now,
					"jti": str(uuid.uuid4()),
				},
				signing_key,
				algorithm=crypto.jwt_signature_alg_for_pem(signing_key),
			)
		}
	)


@routes.post("/xrpc/com.atproto.identity.updateHandle")
@authenticated
async def identity_update_handle(request: web.Request):
	req_json: dict = await request.json()
	handle = req_json.get("handle")
	if handle is None:
		raise web.HTTPBadRequest(text="missing or invalid handle")
	# TODO: actually validate it
	# (I'm writing this half-baked version just so I can send firehose #identity events)
	with get_db(request).new_con() as con:
		con.execute(
			"UPDATE user SET handle = ? WHERE did = ?",
			(handle, request["authed_did"]),
		)
		# TODO: refactor to avoid duplicated logic between here and apply_writes
		firehose_seq = con.execute(
			"SELECT IFNULL(MAX(seq), 0) + 1 FROM firehose"
		).get
		firehose_bytes = cbrrr.encode_dag_cbor(
			{"t": "#identity", "op": 1}
		) + cbrrr.encode_dag_cbor(
			{
				"seq": firehose_seq,
				"did": request["authed_did"],
				"time": util.iso_string_now(),
				"handle": handle,
			}
		)
		con.execute(
			"INSERT INTO firehose (seq, timestamp, msg) VALUES (?, ?, ?)",
			(
				firehose_seq,
				0,
				firehose_bytes,
			),  # TODO: put sensible timestamp here...
		)
	await atproto_repo.firehose_broadcast(
		request, (firehose_seq, firehose_bytes)
	)

	# temp hack: #account events shouldn't really be generated here
	with get_db(request).new_con() as con:
		# TODO: refactor to avoid duplicated logic between here and apply_writes
		firehose_seq = con.execute(
			"SELECT IFNULL(MAX(seq), 0) + 1 FROM firehose"
		).get
		firehose_bytes = cbrrr.encode_dag_cbor(
			{"t": "#account", "op": 1}
		) + cbrrr.encode_dag_cbor(
			{
				"seq": firehose_seq,
				"did": request["authed_did"],
				"time": util.iso_string_now(),
				"active": True,
			}
		)
		con.execute(
			"INSERT INTO firehose (seq, timestamp, msg) VALUES (?, ?, ?)",
			(
				firehose_seq,
				0,
				firehose_bytes,
			),  # TODO: put sensible timestamp here...
		)
	await atproto_repo.firehose_broadcast(
		request, (firehose_seq, firehose_bytes)
	)

	return web.Response()


@routes.get("/xrpc/com.atproto.server.getSession")
@authenticated
async def server_get_session(request: web.Request):
	return web.json_response(session_info(request))


def _discover_plugins() -> dict:
	"""Scan src/mypds/plugins/ and load any valid plugin packages."""
	plugins_dir = Path(__file__).parent / "plugins"
	plugins = {}
	if not plugins_dir.exists():
		return plugins
	for item in sorted(plugins_dir.iterdir()):
		if item.is_dir() and (item / "__init__.py").exists() and not item.name.startswith("_"):
			try:
				mod = importlib.import_module(f"mypds.plugins.{item.name}")
				if hasattr(mod, "APP_NAME") and hasattr(mod, "routes"):
					plugins[mod.APP_NAME] = mod
					logger.info(f"Loaded plugin: {mod.APP_NAME}")
			except Exception as e:
				logger.warning(f"Failed to load plugin '{item.name}': {e}")
	return plugins


def construct_app(
	routes, db: database.Database, client: aiohttp.ClientSession
) -> web.Application:
	cors = cors_middleware(  # TODO: review and reduce scope - and maybe just /xrpc/*?
		allow_all=True,
		expose_headers=["*"],
		allow_headers=["*"],
		allow_methods=["*"],
		allow_credentials=True,
		max_age=100_000_000,
	)

	client.headers.update({"User-Agent": get_version_string()})

	did_resolver = DIDResolver(client, static_config.PLC_DIRECTORY_HOST)

	# Discover plugins and register their app names
	plugins = _discover_plugins()
	for app_name in plugins:
		if app_name not in WebStore.KNOWN_APPS:
			WebStore.KNOWN_APPS.append(app_name)

	# Set up Jinja2 template environment with plugin template dirs
	template_dir = Path(__file__).parent / "templates"
	# PrefixLoader splits on first "/" so we need two levels:
	# "plugin/planner/main.html" → outer["plugin"] → inner["planner"] → "main.html"
	plugin_inner = {}
	for app_name, plugin_mod in plugins.items():
		plugin_tmpl_dir = Path(plugin_mod.__file__).parent / "templates"
		if plugin_tmpl_dir.exists():
			plugin_inner[app_name] = FileSystemLoader(str(plugin_tmpl_dir))

	jinja_loader = (
		ChoiceLoader([
			FileSystemLoader(str(template_dir)),
			PrefixLoader({"plugin": PrefixLoader(plugin_inner)}),
		])
		if plugin_inner else FileSystemLoader(str(template_dir))
	)
	jinja_env = Environment(
		loader=jinja_loader,
		autoescape=True,
		auto_reload=False,  # Templates loaded once at startup
	)

	# ── Jinja2 custom filters ──────────────────────────────────────────────
	def _markdown_filter(text: str) -> str:
		"""Minimal Markdown → HTML (no external deps required)."""
		# Escape HTML first (autoescape is on, so we mark result as safe in template)
		import html as _html
		t = _html.escape(str(text))
		# Code blocks
		t = re.sub(r'```([^`]*?)```', lambda m: f'<pre><code>{m.group(1).strip()}</code></pre>', t, flags=re.DOTALL)
		# Inline code
		t = re.sub(r'`([^`]+)`', r'<code>\1</code>', t)
		# Headers
		t = re.sub(r'^### (.+)$', r'<h3>\1</h3>', t, flags=re.MULTILINE)
		t = re.sub(r'^## (.+)$',  r'<h2>\1</h2>', t, flags=re.MULTILINE)
		t = re.sub(r'^# (.+)$',   r'<h1>\1</h1>', t, flags=re.MULTILINE)
		# HR
		t = re.sub(r'^---+$', '<hr>', t, flags=re.MULTILINE)
		# Bold / italic
		t = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', t)
		t = re.sub(r'\*(.+?)\*',     r'<em>\1</em>', t)
		# Blockquotes
		t = re.sub(r'^&gt; (.+)$', r'<blockquote>\1</blockquote>', t, flags=re.MULTILINE)
		# Unordered lists (simple)
		t = re.sub(r'(?m)^- (.+)$', r'<li>\1</li>', t)
		t = re.sub(r'(<li>.*</li>)', r'<ul>\1</ul>', t, flags=re.DOTALL)
		# Images (must come before links to avoid partial matches)
		t = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', r'<img src="\2" alt="\1" loading="lazy">', t)
		# Links
		t = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', t)
		# Paragraphs (double newline → p)
		blocks = re.split(r'\n\n+', t)
		result = []
		for block in blocks:
			block = block.strip()
			if not block:
				continue
			if block.startswith(('<h', '<ul', '<pre', '<blockquote', '<hr')):
				result.append(block)
			else:
				result.append(f'<p>{block.replace(chr(10), "<br>")}</p>')
		return '\n'.join(result)

	def _timestamp_filter(ts: int) -> str:
		try:
			dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
			return dt.strftime('%Y-%m-%d')
		except Exception:
			return str(ts)

	jinja_env.filters['markdown'] = _markdown_filter
	jinja_env.filters['timestamp'] = _timestamp_filter
	jinja_env.filters['tojson'] = lambda v: __import__('json').dumps(v, ensure_ascii=False)

	app = web.Application(middlewares=[cors, atproto_service_proxy_middleware])
	app[MILLIPDS_DB] = db
	app[MILLIPDS_AIOHTTP_CLIENT] = client
	app[MILLIPDS_FIREHOSE_QUEUES] = set()
	app[MILLIPDS_FIREHOSE_QUEUES_LOCK] = asyncio.Lock()
	app[MILLIPDS_DID_RESOLVER] = did_resolver
	app[MILLIPDS_JINJA_ENV] = jinja_env
	app[MILLIPDS_WEB_STORE] = WebStore()
	app[MYPDS_PLUGINS] = list(plugins.keys())

	# Static file serving
	static_dir = Path(__file__).parent / "static"
	app.router.add_static("/static", static_dir, name="static")

	# Web UI routes (registered before ATProto routes so / is ours)
	app.add_routes(web_routes)

	# Plugin routes
	for plugin_mod in plugins.values():
		app.add_routes(plugin_mod.routes)

	# ATProto protocol routes
	app.add_routes(routes)
	app.add_routes(auth_oauth.as_routes)
	app.add_routes(auth_oauth.routes)
	app.add_routes(atproto_sync.routes)
	app.add_routes(atproto_repo.routes)

	# fallback service proxying for bsky appview routes (hopefully not needed in the future, when atproto-proxy header is used)
	app.add_routes(
		[
			web.get("/xrpc/app.bsky.{_:.*}", service_proxy),
			web.post("/xrpc/app.bsky.{_:.*}", service_proxy),
		]
	)

	return app


async def run(
	db: database.Database,
	client: aiohttp.ClientSession,
	sock_path: Optional[str],
	host: str,
	port: int,
):
	"""
	This gets invoked via mypds.__main__.py
	"""

	app = construct_app(routes, db, client)
	runner = web.AppRunner(app, access_log_format=static_config.HTTP_LOG_FMT)
	await runner.setup()

	if sock_path is None:
		logger.info(f"listening on http://{host}:{port}")
		site = web.TCPSite(runner, host=host, port=port)
	else:
		logger.info(f"listening on {sock_path}")
		site = web.UnixSite(runner, path=sock_path)

	await site.start()

	# Kick the Bluesky relay after startup — retry until tunnel is up (max 5 attempts)
	async def _request_crawl_with_retry():
		try:
			pds_pfx = db.config["pds_pfx"]
			hostname = pds_pfx.removeprefix("https://").removeprefix("http://").rstrip("/")
			if not hostname:
				return
			for attempt in range(5):
				await asyncio.sleep(attempt * 8)  # 0, 8, 16, 24, 32s
				try:
					async with client.post(
						"https://bsky.network/xrpc/com.atproto.sync.requestCrawl",
						json={"hostname": hostname},
						timeout=aiohttp.ClientTimeout(total=10),
					) as resp:
						if resp.status == 200:
							logger.info(f"relay crawl ok for {hostname} (attempt {attempt+1})")
							return
						logger.info(f"relay crawl {resp.status} attempt {attempt+1}, retrying…")
				except Exception as e:
					logger.info(f"relay crawl attempt {attempt+1} failed: {e}, retrying…")
		except Exception as e:
			logger.warning(f"relay crawl setup failed: {e}")

	asyncio.ensure_future(_request_crawl_with_retry())

	if sock_path:
		# give group access to the socket (so that nginx can access it via a shared group)
		# see https://github.com/aio-libs/aiohttp/issues/4155#issuecomment-693979753
		import grp

		try:
			sock_gid = grp.getgrnam(static_config.GROUPNAME).gr_gid
			os.chown(sock_path, os.geteuid(), sock_gid)
		except KeyError:
			logger.warning(
				f"Failed to set socket group - group {static_config.GROUPNAME!r} not found."
			)
		except PermissionError:
			logger.warning(
				f"Failed to set socket group - are you a member of the {static_config.GROUPNAME!r} group?"
			)

		os.chmod(sock_path, 0o770)

	while True:
		await asyncio.sleep(3600)  # sleep forever
