"""Security response headers and a self-hosted Swagger UI for the agent API.

Every response gets the headers of SECURITY_HEADERS (unless the route set them itself)
and a Content-Security-Policy: API_CSP, which allows nothing, for JSON, PDFs and
everything else, DOCS_CSP for the one HTML page, /docs. /docs loads Swagger UI from
this service (the static files of fastapi-swagger), not from a CDN, and starts it
with an external script, so its CSP needs no 'unsafe-inline' and no SRI hashes.
The Server header comes from uvicorn, which names no version (--no-server-header
removes it).
"""

from importlib.resources import files

from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
        "accelerometer=(), gyroscope=(), magnetometer=()"
    ),
}
API_CSP = "default-src 'none'; frame-ancestors 'none'"
DOCS_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
# Only for HTML: isolate the browsing context (ZAP rule 90004)
HTML_HEADERS = {
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Embedder-Policy": "require-corp",
}

DOCS_STATIC = "/docs/static"
DOCS_HTML = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>Order agent - Swagger UI</title>
    <link rel="icon" href="{DOCS_STATIC}/favicon-32x32.png">
    <link rel="stylesheet" href="{DOCS_STATIC}/swagger-ui.css">
  </head>
  <body>
    <div id="swagger-ui"></div>
    <script src="{DOCS_STATIC}/swagger-ui-bundle.js"></script>
    <script src="/docs/init.js"></script>
  </body>
</html>
"""
DOCS_INIT_JS = """window.ui = SwaggerUIBundle({
  url: "/openapi.json",
  dom_id: "#swagger-ui",
  layout: "BaseLayout",
  presets: [SwaggerUIBundle.presets.apis],
  deepLinking: true,
  validatorUrl: null,
});
"""


class SecurityHeadersMiddleware:
    """Pure ASGI middleware, so it also covers streaming and static file responses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                html = headers.get("content-type", "").startswith("text/html")
                extra = SECURITY_HEADERS | (HTML_HEADERS if html else {})
                extra["Content-Security-Policy"] = DOCS_CSP if html else API_CSP
                for name, value in extra.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


def add_security(app: FastAPI) -> None:
    """Adds the middleware and /docs; create the app with docs_url=None, redoc_url=None."""
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount(
        DOCS_STATIC,
        StaticFiles(directory=str(files("fastapi_swagger") / "resources")),
        name="docs-static",
    )

    @app.get("/docs", include_in_schema=False)
    def docs() -> HTMLResponse:
        return HTMLResponse(DOCS_HTML)

    @app.get("/docs/init.js", include_in_schema=False)
    def docs_init() -> Response:
        return Response(DOCS_INIT_JS, media_type="text/javascript")
