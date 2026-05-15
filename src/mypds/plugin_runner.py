"""
plugin_runner.py — Framework for isolated plugin subprocesses.

Each plugin runs as `python -m mypds.plugins.{name}` on a Unix socket at
data/plugins/{name}.sock. The main PDS proxies matching requests there.
The subprocess gets its own DB connections and HTTP client, sharing no
in-process state with the PDS.
"""

import re
import sys
import time
import logging
import asyncio
import subprocess
from pathlib import Path
from typing import Optional, Dict, Tuple

import apsw
import aiohttp
from aiohttp import web
from jinja2 import Environment, FileSystemLoader, ChoiceLoader, PrefixLoader

from . import static_config
from .app_util import (
    MILLIPDS_DB, MILLIPDS_AIOHTTP_CLIENT, MILLIPDS_JINJA_ENV,
    MILLIPDS_FIREHOSE_QUEUES, MILLIPDS_FIREHOSE_QUEUES_LOCK,
)
from .web_store import WebStore

logger = logging.getLogger(__name__)

# ── Plugin database shim ──────────────────────────────────────────────────────

_CONFIG_FIELDS = (
    "jwt_access_secret", "server_as_privkey", "pds_pfx", "pds_did",
    "auth_pfx", "bsky_appview_pfx", "bsky_appview_did",
)


class PluginDatabase:
    """Drop-in for database.Database usable in plugin subprocesses.
    Opens mypds.sqlite3 directly via apsw (WAL mode, full R/W).
    """
    def __init__(self):
        self.con = apsw.Connection(static_config.MAIN_DB_PATH)
        self.con.execute("PRAGMA journal_mode=WAL")

    @property
    def config(self) -> dict:
        row = self.con.execute(
            f"SELECT {','.join(_CONFIG_FIELDS)} FROM config"
        ).fetchone()
        if row is None:
            return {}
        return dict(zip(_CONFIG_FIELDS, row))


# ── Jinja helpers ─────────────────────────────────────────────────────────────

def _timestamp_filter(ts: int) -> str:
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
    except Exception:
        return str(ts)


def _markdown_filter(text: str) -> str:
    import html as _html
    t = _html.escape(str(text))
    t = re.sub(r'```([^`]*?)```',
               lambda m: f'<pre><code>{m.group(1).strip()}</code></pre>', t, flags=re.DOTALL)
    t = re.sub(r'`([^`]+)`', r'<code>\1</code>', t)
    t = re.sub(r'^### (.+)$', r'<h3>\1</h3>', t, flags=re.MULTILINE)
    t = re.sub(r'^## (.+)$',  r'<h2>\1</h2>', t, flags=re.MULTILINE)
    t = re.sub(r'^# (.+)$',   r'<h1>\1</h1>', t, flags=re.MULTILINE)
    t = re.sub(r'^---+$', '<hr>', t, flags=re.MULTILINE)
    t = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', t)
    t = re.sub(r'\*(.+?)\*',     r'<em>\1</em>', t)
    t = re.sub(r'^&gt; (.+)$', r'<blockquote>\1</blockquote>', t, flags=re.MULTILINE)
    t = re.sub(r'(?m)^- (.+)$', r'<li>\1</li>', t)
    t = re.sub(r'(<li>.*</li>)', r'<ul>\1</ul>', t, flags=re.DOTALL)
    t = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', r'<img src="\2" alt="\1" loading="lazy">', t)
    t = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', t)
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


def build_jinja_env(app_name: str) -> Environment:
    main_tmpl = Path(__file__).parent / "templates"
    plugin_tmpl = Path(__file__).parent / "plugins" / app_name / "templates"
    loaders: list = [FileSystemLoader(str(main_tmpl))]
    if plugin_tmpl.exists():
        loaders.append(PrefixLoader({
            "plugin": PrefixLoader({app_name: FileSystemLoader(str(plugin_tmpl))})
        }))
    env = Environment(loader=ChoiceLoader(loaders), autoescape=True, auto_reload=False)
    env.filters["tojson"]    = lambda v: __import__("json").dumps(v, ensure_ascii=False)
    env.filters["timestamp"] = _timestamp_filter
    env.filters["markdown"]  = _markdown_filter
    return env


# ── Subprocess app builder ────────────────────────────────────────────────────

def build_subprocess_app(routes: web.RouteTableDef, app_name: str) -> web.Application:
    """Create an aiohttp app that mimics the main app's AppKey structure."""
    # Lazy imports to avoid circular imports at module level
    from .web import MILLIPDS_WEB_STORE, MYPDS_PLUGINS

    plugin_db  = PluginDatabase()
    web_store  = WebStore()
    jinja_env  = build_jinja_env(app_name)
    plugin_names = list(web_store.get_all_app_settings().keys())

    app = web.Application()
    app[MILLIPDS_DB]                   = plugin_db
    app[MILLIPDS_WEB_STORE]            = web_store
    app[MILLIPDS_JINJA_ENV]            = jinja_env
    app[MYPDS_PLUGINS]                 = plugin_names
    app[MILLIPDS_FIREHOSE_QUEUES]      = set()
    app[MILLIPDS_FIREHOSE_QUEUES_LOCK] = asyncio.Lock()

    async def on_startup(a):
        a[MILLIPDS_AIOHTTP_CLIENT] = aiohttp.ClientSession()

    async def on_cleanup(a):
        await a[MILLIPDS_AIOHTTP_CLIENT].close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.add_routes(routes)
    return app


def run_plugin(routes: web.RouteTableDef, app_name: str):
    """Entry point for a plugin subprocess. Call from __main__."""
    sock_path = static_config.DATA_DIR + f"/plugins/{app_name}.sock"
    Path(sock_path).parent.mkdir(parents=True, exist_ok=True)
    Path(sock_path).unlink(missing_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format=f"[plugin:{app_name}] %(levelname)s %(name)s %(message)s",
    )
    logger.info(f"starting on {sock_path}")

    app = build_subprocess_app(routes, app_name)
    web.run_app(app, path=sock_path, print=lambda *_: None)


# ── Process manager ───────────────────────────────────────────────────────────

class PluginManager:
    """Spawns and kills plugin subprocesses. Stored as an AppKey on the main app."""

    def __init__(self):
        self._procs: Dict[str, subprocess.Popen] = {}

    def sock_path(self, app_name: str) -> str:
        return static_config.DATA_DIR + f"/plugins/{app_name}.sock"

    def plugin_exists(self, app_name: str) -> bool:
        """True if a plugin module exists for this app name."""
        import importlib.util
        return importlib.util.find_spec(f"mypds.plugins.{app_name}") is not None

    def is_running(self, app_name: str) -> bool:
        return Path(self.sock_path(app_name)).exists()

    def start(self, app_name: str, timeout: float = 5.0) -> bool:
        """Spawn plugin subprocess, wait for socket (up to timeout seconds)."""
        # Trust our own process table, not stale socket files from prior runs
        proc = self._procs.get(app_name)
        if proc and proc.poll() is None and self.is_running(app_name):
            return True
        sock = self.sock_path(app_name)
        Path(sock).parent.mkdir(parents=True, exist_ok=True)
        Path(sock).unlink(missing_ok=True)  # remove stale socket from previous run

        proc = subprocess.Popen(
            [sys.executable, "-m", f"mypds.plugins.{app_name}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self._procs[app_name] = proc

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if Path(sock).exists():
                logger.info(f"plugin {app_name} ready")
                return True
            time.sleep(0.1)

        logger.warning(f"plugin {app_name} did not start in {timeout}s")
        return False

    def stop(self, app_name: str):
        """Terminate plugin subprocess and remove socket."""
        proc = self._procs.pop(app_name, None)
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        Path(self.sock_path(app_name)).unlink(missing_ok=True)
        logger.info(f"plugin {app_name} stopped")

    def stop_all(self):
        for name in list(self._procs):
            self.stop(name)


# ── HTTP proxy handler ────────────────────────────────────────────────────────

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
})

_FORWARD_RESP_HEADERS = frozenset({
    "content-type", "location", "set-cookie", "x-frame-options",
    "content-security-policy", "cache-control", "x-accel-buffering",
    "content-disposition",
})


async def proxy_to_plugin(request: web.Request, app_name: str) -> web.Response:
    """Forward an incoming request to the plugin's Unix socket."""
    sock = static_config.DATA_DIR + f"/plugins/{app_name}.sock"
    if not Path(sock).exists():
        raise web.HTTPNotFound()

    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in _HOP_BY_HOP}
    body = await request.read()

    connector = aiohttp.UnixConnector(path=sock)
    jar = aiohttp.DummyCookieJar()  # preserve Set-Cookie pass-through
    try:
        async with aiohttp.ClientSession(connector=connector, cookie_jar=jar) as sess:
            async with sess.request(
                method=request.method,
                url=f"http://plugin{request.path_qs}",
                headers=headers,
                data=body,
                allow_redirects=False,
            ) as upstream:
                ct = upstream.headers.get("Content-Type", "")

                if "text/event-stream" in ct:
                    # streaming SSE — don't buffer
                    stream_resp = web.StreamResponse(
                        status=upstream.status,
                        headers={"Content-Type": ct, "Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"},
                    )
                    await stream_resp.prepare(request)
                    async for chunk in upstream.content.iter_any():
                        await stream_resp.write(chunk)
                    return stream_resp

                resp_body = await upstream.read()
                resp = web.Response(status=upstream.status, body=resp_body)
                for k, v in upstream.headers.items():
                    if k.lower() in _FORWARD_RESP_HEADERS:
                        resp.headers.add(k, v)
                return resp
    except aiohttp.ClientConnectorError:
        # socket exists but process crashed — clean it up and return 404
        Path(sock).unlink(missing_ok=True)
        raise web.HTTPNotFound()
