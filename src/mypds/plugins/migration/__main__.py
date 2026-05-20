from pathlib import Path
import logging
import apsw
from aiohttp import web

from mypds import static_config
from mypds.plugins.migration import routes, APP_NAME
from mypds.plugin_runner import build_subprocess_app

sock_path = static_config.DATA_DIR + f"/plugins/{APP_NAME}.sock"
Path(sock_path).parent.mkdir(parents=True, exist_ok=True)
Path(sock_path).unlink(missing_ok=True)

# Plugin owns its table — core PDS knows nothing about it
con = apsw.Connection(static_config.MAIN_DB_PATH)
con.execute("""
    CREATE TABLE IF NOT EXISTS migration_preflight(
        token TEXT PRIMARY KEY,
        did TEXT NOT NULL,
        handle TEXT NOT NULL,
        bsky_token TEXT NOT NULL,
        verify_code TEXT NOT NULL,
        ts REAL NOT NULL,
        imported INTEGER NOT NULL DEFAULT 0
    ) STRICT
""")
con.close()

logging.basicConfig(
    level=logging.INFO,
    format=f"[plugin:{APP_NAME}] %(levelname)s %(name)s %(message)s",
)

app = build_subprocess_app(routes, APP_NAME)
web.run_app(app, path=sock_path, print=lambda *_: None)
