from aiohttp import web

from mypds.web import render

APP_NAME = "hello"
NSID     = None

routes = web.RouteTableDef()


@routes.get("/hello")
async def hello_page(request: web.Request):
    return render(request, "plugin/hello/main.html", {})

if __name__ == "__main__":
    from mypds.plugin_runner import run_plugin
    run_plugin(routes, APP_NAME)
