import logging
import hashlib
import base64
import secrets
import time
import uuid
from typing import Dict, Any

import jwt
import cbrrr
import json

from aiohttp import web
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization

from . import database
from .app_util import *

logger = logging.getLogger(__name__)


def jwk_thumbprint(jwk_dict: Dict[str, Any]) -> str:
	"""Compute JWK thumbprint per RFC 7638"""
	required = {k: jwk_dict[k] for k in sorted(["crv", "kty", "x", "y"]) if k in jwk_dict}
	canon = json.dumps(required, separators=(',', ':'), sort_keys=True)
	digest = hashlib.sha256(canon.encode()).digest()
	return base64.urlsafe_b64encode(digest).rstrip(b'=').decode()


def generate_authorization_code() -> str:
	return secrets.token_urlsafe(32)


def generate_request_uri() -> str:
	return f"urn:ietf:params:oauth:request_uri:{uuid.uuid4()}"


def pkce_verify(code_verifier: str, stored_challenge: str) -> bool:
	"""Verify PKCE code_verifier against stored challenge (S256)"""
	computed = base64.urlsafe_b64encode(
		hashlib.sha256(code_verifier.encode()).digest()
	).rstrip(b'=').decode()
	return computed == stored_challenge


def sign_oauth_token(
	privkey_pem: str,
	iss: str,
	aud: str,
	sub: str,
	scope: str,
	cnf_jkt: str,
) -> str:
	"""Sign an OAuth access token JWT with ES256"""
	privkey = serialization.load_pem_private_key(privkey_pem.encode(), password=None)
	payload = {
		"iss": iss,
		"aud": aud,
		"sub": sub,
		"scope": scope,
		"cnf": {"jkt": cnf_jkt},
		"iat": int(time.time()),
		"exp": int(time.time()) + 7200,  # 2 hours
		"jti": str(uuid.uuid4()),
	}
	return jwt.encode(payload, privkey, algorithm="ES256")


# this is a bit of a hack to annotate routes as AS-only, to be checked via middleware
class AnnotatedRouteTableDef(web.RouteTableDef):
	def route(self, method: str, path: str, **kwargs):
		decorator = super().route(method, path, **kwargs)

		def wrapper(handler):
			# Apply the original decorator
			result = decorator(handler)
			# Set the attribute on the handler (which is returned by decorator)
			setattr(result, "_is_as_route", True)
			return result

		return wrapper


as_routes = AnnotatedRouteTableDef()
routes = web.RouteTableDef()

# we need to use a weaker-than-usual CSP to let the CSS and form submission work
WEBUI_HEADERS = {
	"Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"
}


# example: https://shiitake.us-east.host.bsky.network/.well-known/oauth-protected-resource
@routes.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource(request: web.Request):
	cfg = get_db(request).config
	return web.json_response(
		{
			"resource": cfg["pds_pfx"],
			"authorization_servers": [cfg["auth_pfx"]],
			"scopes_supported": [],
			"bearer_methods_supported": ["header"],
			"resource_documentation": "https://atproto.com",
		}
	)


# example: https://bsky.social/.well-known/oauth-authorization-server
@as_routes.get("/.well-known/oauth-authorization-server")
async def oauth_authorization_server(request: web.Request):
	# XXX: most of these values are currently bogus!!! I copy pasted bsky's one
	# TODO: fill in alg_supported lists based on what pyjwt actually supports
	# perhaps via jwt.api_jws.get_default_algorithms().keys(), but we'd want to exclude the symmetric ones
	cfg = get_db(request).config
	pfx = cfg["auth_pfx"]
	return web.json_response(
		{
			"issuer": pfx,
			"scopes_supported": [
				"atproto",
				"transition:generic",
				"transition:chat.bsky",
			],
			"subject_types_supported": ["public"],
			"response_types_supported": ["code"],
			"response_modes_supported": ["query", "fragment", "form_post"],
			"grant_types_supported": ["authorization_code", "refresh_token"],
			"code_challenge_methods_supported": ["S256"],
			"ui_locales_supported": ["en-US"],
			"display_values_supported": ["page", "popup", "touch"],
			"authorization_response_iss_parameter_supported": True,
			"request_object_signing_alg_values_supported": [
				"RS256",
				"RS384",
				"RS512",
				"PS256",
				"PS384",
				"PS512",
				"ES256",
				"ES256K",
				"ES384",
				"ES512",
				"none",
			],
			"request_object_encryption_alg_values_supported": [],
			"request_object_encryption_enc_values_supported": [],
			"request_parameter_supported": True,
			"request_uri_parameter_supported": True,
			"require_request_uri_registration": True,
			"jwks_uri": pfx + "/oauth/jwks",
			"authorization_endpoint": pfx + "/oauth/authorize",
			"token_endpoint": pfx + "/oauth/token",
			"token_endpoint_auth_methods_supported": [
				"none",
				"private_key_jwt",
			],
			"token_endpoint_auth_signing_alg_values_supported": [
				"RS256",
				"RS384",
				"RS512",
				"PS256",
				"PS384",
				"PS512",
				"ES256",
				"ES256K",
				"ES384",
				"ES512",
			],
			"revocation_endpoint": pfx + "/oauth/revoke",
			"introspection_endpoint": pfx + "/oauth/introspect",
			"pushed_authorization_request_endpoint": pfx + "/oauth/par",
			"require_pushed_authorization_requests": True,
			"dpop_signing_alg_values_supported": [
				"RS256",
				"RS384",
				"RS512",
				"PS256",
				"PS384",
				"PS512",
				"ES256",
				"ES256K",
				"ES384",
				"ES512",
			],
			"client_id_metadata_document_supported": True,
		}
	)


@as_routes.get("/oauth/jwks")
async def oauth_jwks(request: web.Request):
	"""Return the OAuth server's public key as JWKS"""
	cfg = get_db(request).config
	privkey_pem = cfg["server_as_privkey"]
	privkey = serialization.load_pem_private_key(privkey_pem.encode(), password=None)
	pubkey = privkey.public_key()

	# Extract coordinates from the public key
	numbers = pubkey.public_numbers()

	# Pad x and y to 32 bytes (256 bits for P-256)
	x_bytes = numbers.x.to_bytes(32, byteorder='big')
	y_bytes = numbers.y.to_bytes(32, byteorder='big')
	x_b64 = base64.urlsafe_b64encode(x_bytes).rstrip(b'=').decode()
	y_b64 = base64.urlsafe_b64encode(y_bytes).rstrip(b'=').decode()

	jwk = {
		"kty": "EC",
		"crv": "P-256",
		"x": x_b64,
		"y": y_b64,
		"alg": "ES256",
		"use": "sig",
		"kid": jwk_thumbprint({"kty": "EC", "crv": "P-256", "x": x_b64, "y": y_b64}),
	}

	return web.json_response({"keys": [jwk]})


# this is where a client will redirect to during the auth flow.
# they'll see a webpage asking them to login
@as_routes.get("/oauth/authorize")
async def oauth_authorize(request: web.Request):
	request_uri = request.query.get("request_uri")
	if not request_uri:
		raise web.HTTPBadRequest(text="missing request_uri")

	db = get_db(request)

	# Look up PAR request
	par_row = db.con.execute(
		"SELECT client_id, scope, expires_at FROM oauth_par_request WHERE request_uri=?",
		(request_uri,),
	).fetchone()

	if not par_row:
		raise web.HTTPBadRequest(text="invalid request_uri")

	client_id, scope, expires_at = par_row

	if expires_at < int(time.time()):
		raise web.HTTPBadRequest(text="request_uri expired")

	# Render login form with embedded PAR info
	html = get_jinja_env(request).get_template("authn.html").render(
		request_uri=request_uri,
		client_id=client_id,
		scope=scope,
	)
	return web.Response(
		text=html,
		content_type="text/html",
		headers=WEBUI_HEADERS,
	)


# after login, issue authorization code and redirect back to client
@as_routes.post("/oauth/authorize")
async def oauth_authorize_handle_login(request: web.Request):
	data = await request.post()
	db = get_db(request)
	cfg = db.config

	request_uri = data.get("request_uri") or request.query.get("request_uri")
	handle_or_did = data.get("handle")
	password = data.get("password")

	if not all([request_uri, handle_or_did, password]):
		raise web.HTTPBadRequest(text="missing required fields")

	# Look up PAR request
	par_row = db.con.execute(
		"SELECT client_id, redirect_uri, scope, code_challenge, code_challenge_method, dpop_jwk, expires_at FROM oauth_par_request WHERE request_uri=?",
		(request_uri,),
	).fetchone()

	if not par_row:
		raise web.HTTPBadRequest(text="invalid request_uri")

	client_id, redirect_uri, scope, code_challenge, code_challenge_method, dpop_jwk_blob, expires_at = par_row

	if expires_at < int(time.time()):
		raise web.HTTPBadRequest(text="request_uri expired")

	# Verify credentials
	try:
		did, _ = db.verify_account_login(handle_or_did, password)
	except (KeyError, ValueError):
		raise web.HTTPBadRequest(text="invalid credentials")

	# Generate authorization code
	auth_code = generate_authorization_code()
	code_expires_at = int(time.time()) + 600  # 10 minutes

	# Compute DPoP JKT from stored JWK
	dpop_jwk = cbrrr.decode_dag_cbor(dpop_jwk_blob)
	dpop_jkt = jwk_thumbprint(dpop_jwk)

	# Store authorization code
	db.con.execute(
		"""
		INSERT INTO oauth_auth_code(
			code, did, scope, dpop_jkt, redirect_uri, client_id, pkce_challenge, expires_at
		) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
		""",
		(
			auth_code,
			did,
			scope,
			dpop_jkt,
			redirect_uri,
			client_id,
			code_challenge,
			code_expires_at,
		),
	)

	# Redirect back to client with code
	redirect_url = f"{redirect_uri}?code={auth_code}&iss={cfg['auth_pfx']}"

	# Retrieve state from PAR request if stored
	stored_state = None
	if hasattr(request.app, "par_state") and request_uri in request.app.get("par_state", {}):
		stored_state = request.app["par_state"].pop(request_uri)

	if stored_state:
		redirect_url += f"&state={stored_state}"

	return web.Response(
		status=302,
		headers={"Location": redirect_url},
	)


DPOP_NONCE = "placeholder_nonce_value"  # this needs to get rotated! (does it matter that it's global?)


def dpop_protected(handler):
	async def dpop_handler(request: web.Request):
		logger.info(f"DPoP-protected {request.method} {request.path} - headers: {dict(request.headers)}")
		dpop = request.headers.get("dpop")
		if dpop is None:
			logger.error(f"Missing DPoP header on {request.method} {request.path}")
			raise web.HTTPBadRequest(text="missing dpop")

		# we're not verifying yet, we just want to pull out the jwk from the header
		unverified = jwt.api_jwt.decode_complete(
			dpop, options={"verify_signature": False}
		)
		jwk_data = unverified["header"]["jwk"]
		jwk = jwt.PyJWK.from_dict(jwk_data)
		decoded: dict = jwt.decode(
			dpop, key=jwk
		)  # actual signature verification happens here

		logger.info(decoded)
		logger.info(request.url)

		# TODO: verify iat?, iss?

		if request.method != decoded["htm"]:
			raise web.HTTPBadRequest(text="dpop: bad htm")

		# Reconstruct the public-facing URL using the configured PDS prefix,
		# since request.url uses the internal http:// scheme behind Cloudflare.
		cfg = get_db(request).config
		pds_pfx = cfg["pds_pfx"].rstrip("/")
		public_url = pds_pfx + request.path
		if request.query_string:
			public_url += "?" + request.query_string
		if public_url != decoded["htu"]:
			logger.info(f"{public_url!r} != {decoded['htu']!r}")
			raise web.HTTPBadRequest(
				text="dpop: bad htu (if your application is reverse-proxied, make sure the Host header is getting set properly)"
			)

		if decoded.get("nonce") != DPOP_NONCE:
			raise web.HTTPBadRequest(
				body=json.dumps(
					{
						"error": "use_dpop_nonce",
						"error_description": "Authorization server requires nonce in DPoP proof",
					}
				),
				headers={
					"DPoP-Nonce": DPOP_NONCE,
					"Content-Type": "application/json",
				},  # if we don't put it here, the client will never see it
			)

		request["dpop_jwk"] = cbrrr.encode_dag_cbor(
			jwk_data
		)  # for easy comparison in db etc.
		request["dpop_jti"] = decoded[
			"jti"
		]  # XXX: should replay prevention happen here?
		request["dpop_iss"] = decoded.get("iss")  # iss is optional in DPoP spec

		res: web.Response = await handler(request)
		res.headers["DPoP-Nonce"] = (
			DPOP_NONCE  # TODO: make sure this always gets set even under error conditions?
		)
		return res

	return dpop_handler


@as_routes.post("/oauth/par")
@dpop_protected
async def oauth_pushed_authorization_request(request: web.Request):
	db = get_db(request)

	# Handle both JSON and form-encoded POST data
	content_type = request.headers.get("content-type", "").lower()
	if "application/json" in content_type:
		data = await request.json()
	else:
		data = await request.post()

	logger.info(f"PAR request data: {dict(data)}")

	# Verify client_id matches DPoP issuer if iss is present
	if request.get("dpop_iss") and data.get("client_id") != request.get("dpop_iss"):
		raise web.HTTPBadRequest(text="client_id does not match dpop iss")

	# Verify required fields
	required_fields = ["client_id", "redirect_uri", "scope", "code_challenge", "code_challenge_method"]
	missing = [f for f in required_fields if f not in data]
	if missing:
		logger.error(f"PAR missing fields: {missing}, got: {list(data.keys())}")
		raise web.HTTPBadRequest(text=f"missing required fields: {', '.join(missing)}")

	if data["code_challenge_method"] != "S256":
		raise web.HTTPBadRequest(text="only S256 code_challenge_method is supported")

	# Generate a unique request_uri
	request_uri = generate_request_uri()
	expires_at = int(time.time()) + 600  # 10 minutes

	# Store state in request context if provided (will be retrieved later)
	# Note: state is typically in PAR data but handled separately
	state = data.get("state", "")

	# Store PAR request
	db.con.execute(
		"""
		INSERT INTO oauth_par_request(
			request_uri, client_id, redirect_uri, scope,
			code_challenge, code_challenge_method, dpop_jwk, expires_at
		) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
		""",
		(
			request_uri,
			data["client_id"],
			data["redirect_uri"],
			data["scope"],
			data["code_challenge"],
			data["code_challenge_method"],
			request["dpop_jwk"],
			expires_at,
		),
	)

	# Store state temporarily (using a simple in-memory approach for now)
	if state:
		request.app["par_state"] = request.app.get("par_state", {})
		request.app["par_state"][request_uri] = state

	return web.json_response(
		{
			"request_uri": request_uri,
			"expires_in": 600,
		}
	)


@as_routes.post("/oauth/token")
@dpop_protected
async def oauth_token(request: web.Request):
	"""Exchange authorization code for access token"""
	db = get_db(request)
	cfg = db.config

	# Handle both JSON and form-encoded POST data
	content_type = request.headers.get("content-type", "").lower()
	if "application/json" in content_type:
		data = await request.json()
	else:
		data = await request.post()

	logger.info(f"Token request data: {dict(data)}")

	# Verify grant_type
	if data.get("grant_type") != "authorization_code":
		raise web.HTTPBadRequest(text=f"unsupported grant_type: {data.get('grant_type')}")

	# Look up and validate authorization code
	code = data.get("code")
	if not code:
		logger.error("Token request missing code")
		raise web.HTTPBadRequest(text="missing code")

	auth_code_row = db.con.execute(
		"SELECT did, scope, dpop_jkt, redirect_uri, client_id, pkce_challenge, used FROM oauth_auth_code WHERE code=?",
		(code,),
	).fetchone()

	if not auth_code_row:
		raise web.HTTPBadRequest(text="invalid code")

	did, scope, dpop_jkt, redirect_uri, client_id, pkce_challenge, used = auth_code_row

	# Check if code was already used
	if used:
		raise web.HTTPBadRequest(text="code already used")

	# Verify expiration
	expires_row = db.con.execute(
		"SELECT expires_at FROM oauth_auth_code WHERE code=?", (code,)
	).fetchone()
	if expires_row and expires_row[0] < int(time.time()):
		raise web.HTTPBadRequest(text="code expired")

	# Verify redirect_uri matches
	if data.get("redirect_uri") != redirect_uri:
		raise web.HTTPBadRequest(text="redirect_uri mismatch")

	# Verify client_id matches
	if data.get("client_id") != client_id:
		raise web.HTTPBadRequest(text="client_id mismatch")

	# Verify PKCE
	code_verifier = data.get("code_verifier")
	if not code_verifier or not pkce_verify(code_verifier, pkce_challenge):
		raise web.HTTPBadRequest(text="invalid code_verifier")

	# Verify DPoP JWK thumbprint matches
	current_dpop_jkt = jwk_thumbprint(request["dpop_jwk"])
	if current_dpop_jkt != dpop_jkt:
		raise web.HTTPBadRequest(text="dpop jkt mismatch")

	# Mark code as used
	db.con.execute("UPDATE oauth_auth_code SET used=1 WHERE code=?", (code,))

	# Sign access token
	access_token = sign_oauth_token(
		privkey_pem=cfg["server_as_privkey"],
		iss=cfg["auth_pfx"],
		aud=cfg["pds_pfx"],
		sub=did,
		scope=scope,
		cnf_jkt=current_dpop_jkt,
	)

	return web.json_response(
		{
			"access_token": access_token,
			"token_type": "DPoP",
			"expires_in": 7200,
			"scope": scope,
			"sub": did,
		}
	)
