from pathlib import Path
import logging
from aiohttp import web

from mypds import static_config
from mypds.plugins.federation import routes, APP_NAME, _start_peer_on_startup
from mypds.plugin_runner import build_subprocess_app

sock_path = static_config.DATA_DIR + f"/plugins/{APP_NAME}.sock"
Path(sock_path).parent.mkdir(parents=True, exist_ok=True)
Path(sock_path).unlink(missing_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format=f"[plugin:{APP_NAME}] %(levelname)s %(name)s %(message)s",
)

app = build_subprocess_app(routes, APP_NAME)
app.on_startup.append(_start_peer_on_startup)
web.run_app(app, path=sock_path, print=lambda *_: None)
