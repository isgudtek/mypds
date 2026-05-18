from aiohttp import web

APP_NAME = "aturi"
NSID = None

routes = web.RouteTableDef()

if __name__ == "__main__":
    from mypds.plugin_runner import run_plugin
    run_plugin(routes, APP_NAME)
