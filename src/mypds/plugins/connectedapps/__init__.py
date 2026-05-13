from aiohttp import web

from mypds.app_util import get_db
from mypds.web import render, get_session, get_web_store

APP_NAME = "connectedapps"
NSID     = None  # no ATProto records — internal tracking only

routes = web.RouteTableDef()


@routes.get("/connected-apps")
async def connected_apps_page(request: web.Request):
    session = get_session(request)
    if not session:
        raise web.HTTPFound("/login")
    ws = get_web_store(request)
    apps = ws.get_app_logins()
    return render(request, "plugin/connectedapps/main.html", {"connected_apps": apps})
