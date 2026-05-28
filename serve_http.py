"""
serve_http.py — Entry point HTTP del MCP para deployment en Render (FIXED lifespan).
"""
from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from typing import Optional

import structlog
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, HTMLResponse
from starlette.routing import Mount, Route

import urllib.parse as _urlparse

import server as srv_module

log = structlog.get_logger("serve_http")

MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN")
if not MCP_AUTH_TOKEN:
    raise RuntimeError("MCP_AUTH_TOKEN no seteado.")

ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("MCP_ALLOWED_ORIGINS", "*").split(",")
    if o.strip()
]


class BearerAuthMiddleware(BaseHTTPMiddleware):
    PROTECTED_PREFIXES = ("/mcp",)
    PUBLIC_PATHS = ("/health", "/", "/version", "/oauth/")

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if any(path.startswith(p) for p in self.PUBLIC_PATHS):
            return await call_next(request)
        if any(path.startswith(p) for p in self.PROTECTED_PREFIXES):
            auth = request.headers.get("authorization", "")
            if not auth.startswith("Bearer "):
                return JSONResponse({"error": "missing_bearer"}, status_code=401)
            token = auth.split(" ", 1)[1].strip()
            if token != MCP_AUTH_TOKEN:
                return JSONResponse({"error": "invalid_token"}, status_code=401)
        return await call_next(request)


class AccessLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        t0 = time.monotonic()
        response = await call_next(request)
        dur_ms = int((time.monotonic() - t0) * 1000)
        log.info("http", method=request.method, path=request.url.path, status=response.status_code, ms=dur_ms)
        return response


async def health(request):
    return JSONResponse({
        "status": "ok",
        "service": "myscrubs-ml-mcp",
        "site": srv_module.SITE_ID,
        "user_id": srv_module.USER_ID,
        "time": int(time.time()),
    })


async def root(request):
    return PlainTextResponse("MyScrubs ML MCP - /mcp (Bearer auth)")


async def version(request):
    return JSONResponse({"name": "myscrubs-ml-mcp", "version": "1.0.1"})


SETUP_TOKEN = os.environ.get("SETUP_TOKEN") or MCP_AUTH_TOKEN

ML_SITE_TO_HOST = {
    "MLC": "auth.mercadolibre.cl",
    "MLA": "auth.mercadolibre.com.ar",
    "MLB": "auth.mercadolibre.com.br",
    "MLM": "auth.mercadolibre.com.mx",
}


def _check_setup(request):
    if request.query_params.get("setup") != SETUP_TOKEN:
        return JSONResponse({"error": "missing_setup_token"}, status_code=401)
    return None


async def oauth_start(request):
    err = _check_setup(request)
    if err:
        return err
    creds = srv_module.auth.credentials
    site_host = ML_SITE_TO_HOST.get(creds.site_id, "auth.mercadolibre.cl")
    auth_url = (
        f"https://{site_host}/authorization?response_type=code"
        f"&client_id={creds.app_id}"
        f"&redirect_uri={_urlparse.quote(creds.redirect_uri, safe='')}"
    )
    return RedirectResponse(auth_url, status_code=302)


async def oauth_callback(request):
    err = _check_setup(request)
    if err and not request.query_params.get("code"):
        return err
    code = request.query_params.get("code")
    error = request.query_params.get("error")
    if error or not code:
        return HTMLResponse(f"<h1>OAuth error</h1><p>{error or 'sin code'}</p>", status_code=400)
    try:
        tokens = await srv_module.auth.exchange_code_for_tokens(code)
        return HTMLResponse(f"<h1>Tokens guardados</h1><p>User ID: {tokens.user_id}</p><p>El MCP ya puede operar.</p>")
    except Exception as e:
        return HTMLResponse(f"<h1>Fallo intercambio</h1><pre>{e}</pre>", status_code=500)


async def oauth_status(request):
    err = _check_setup(request)
    if err:
        return err
    try:
        token = await srv_module.auth.get_access_token()
        return JSONResponse({"status": "ok", "has_tokens": True, "user_id": srv_module.auth.user_id})
    except Exception as e:
        return JSONResponse({"status": "missing_tokens", "error": str(e)}, status_code=503)


def build_app() -> Starlette:
    mcp_asgi = srv_module.mcp.http_app(path="/")

    @asynccontextmanager
    async def _combined_lifespan(app):
        # CRITICAL FIX: arranca el lifespan del MCP sub-app (StreamableHTTP session manager)
        async with mcp_asgi.router.lifespan_context(mcp_asgi):
            yield

    routes = [
        Route("/", root),
        Route("/health", health),
        Route("/version", version),
        Route("/oauth/start", oauth_start),
        Route("/oauth/callback", oauth_callback),
        Route("/oauth/status", oauth_status),
        Mount("/mcp", app=mcp_asgi),
    ]
    middleware = [
        Middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["*"], allow_credentials=False),
        Middleware(AccessLogMiddleware),
        Middleware(BearerAuthMiddleware),
    ]
    return Starlette(routes=routes, middleware=middleware, lifespan=_combined_lifespan)


app = build_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    log.info("starting_http_server", host=host, port=port)
    uvicorn.run("serve_http:app", host=host, port=port, log_level="info", access_log=False)
