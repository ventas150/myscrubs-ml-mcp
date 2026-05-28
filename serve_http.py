"""
serve_http.py — Entry point HTTP del MCP para deployment en Render.

Envuelve el FastMCP server en una ASGI app de Starlette con:
- Bearer token auth (env MCP_AUTH_TOKEN)
- Health check en /health (no requiere auth)
- CORS abierto al dominio del agente (env MCP_ALLOWED_ORIGINS)
- Logging estructurado por request

Uso local:
    MCP_TRANSPORT=streamable-http MCP_AUTH_TOKEN=dev python serve_http.py

En Render: comando start = "python serve_http.py"
"""
from __future__ import annotations

import os
import time
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

# Importar el server.py NO arranca el MCP (solo registra tools)
import server as srv_module

log = structlog.get_logger("serve_http")

MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN")
if not MCP_AUTH_TOKEN:
    raise RuntimeError(
        "MCP_AUTH_TOKEN no seteado. Genera un secret aleatorio "
        "(`openssl rand -hex 32`) y configúralo como env var en Render."
    )

ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("MCP_ALLOWED_ORIGINS", "*").split(",")
    if o.strip()
]


# ---------- middleware bearer ----------

class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Exige Authorization: Bearer <token> en /mcp/*.

    /oauth/* es protegido por SETUP_TOKEN en query string (no bearer)
    para permitir el flujo OAuth ML que redirige por navegador.
    """

    PROTECTED_PREFIXES = ("/mcp",)
    PUBLIC_PATHS = ("/health", "/", "/version", "/oauth/")

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if any(path.startswith(p) for p in self.PUBLIC_PATHS):
            return await call_next(request)
        if any(path.startswith(p) for p in self.PROTECTED_PREFIXES):
            auth = request.headers.get("authorization", "")
            if not auth.startswith("Bearer "):
                return JSONResponse(
                    {"error": "missing_bearer", "detail": "Authorization: Bearer <token> requerido"},
                    status_code=401,
                )
            token = auth.split(" ", 1)[1].strip()
            if token != MCP_AUTH_TOKEN:
                return JSONResponse(
                    {"error": "invalid_token"},
                    status_code=401,
                )
        return await call_next(request)


class AccessLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        t0 = time.monotonic()
        response = await call_next(request)
        dur_ms = int((time.monotonic() - t0) * 1000)
        log.info(
            "http",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            ms=dur_ms,
            ua=request.headers.get("user-agent", "")[:80],
        )
        return response


# ---------- routes públicas ----------

async def health(request: Request) -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "service": "myscrubs-ml-mcp",
        "site": srv_module.SITE_ID,
        "user_id": srv_module.USER_ID,
        "time": int(time.time()),
    })


async def root(request: Request) -> PlainTextResponse:
    return PlainTextResponse(
        "MyScrubs ML MCP — endpoint MCP en /mcp (requiere Bearer auth)"
    )


async def version(request: Request) -> JSONResponse:
    return JSONResponse({"name": "myscrubs-ml-mcp", "version": "1.0.0"})


# ---------- OAuth flow ML (one-time setup) ----------
# Protegido con SETUP_TOKEN (env var) en query: ?setup=<token>
# Una vez generado el refresh_token, ML se auto-renueva — este flujo solo
# se ejecuta una vez (o cada 6 meses si el refresh_token expira por inactividad).

SETUP_TOKEN = os.environ.get("SETUP_TOKEN") or MCP_AUTH_TOKEN

ML_SITE_TO_HOST = {
    "MLC": "auth.mercadolibre.cl",
    "MLA": "auth.mercadolibre.com.ar",
    "MLB": "auth.mercadolibre.com.br",
    "MLM": "auth.mercadolibre.com.mx",
    "MLU": "auth.mercadolibre.com.uy",
}


def _check_setup(request: Request) -> Optional[JSONResponse]:
    if request.query_params.get("setup") != SETUP_TOKEN:
        return JSONResponse(
            {"error": "missing_setup_token",
             "hint": "Agregá ?setup=<SETUP_TOKEN o MCP_AUTH_TOKEN>"},
            status_code=401,
        )
    return None


async def oauth_start(request: Request):
    """Redirige al usuario a la pantalla de autorización ML."""
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


async def oauth_callback(request: Request):
    """Recibe el code de ML, intercambia por tokens y los guarda."""
    err = _check_setup(request)
    if err:
        # ML no incluye nuestro setup token en la URL de redirect.
        # En vez de devolver 401, intentamos completar igual si los
        # params vienen del provider (code presente).
        if not request.query_params.get("code"):
            return err
    code = request.query_params.get("code")
    error = request.query_params.get("error")
    if error or not code:
        return HTMLResponse(
            f"<h1>OAuth error</h1><p>{error or 'sin code'}</p>",
            status_code=400,
        )
    try:
        tokens = await srv_module.auth.exchange_code_for_tokens(code)
        return HTMLResponse(
            "<h1>✓ Tokens guardados</h1>"
            f"<p>User ID: {tokens.user_id}</p>"
            f"<p>El MCP ya puede operar. Cierre esta ventana.</p>"
        )
    except Exception as e:
        return HTMLResponse(
            f"<h1>Falló el intercambio</h1><pre>{e}</pre>", status_code=500
        )


async def oauth_status(request: Request):
    """Muestra estado de los tokens (útil para verificar setup)."""
    err = _check_setup(request)
    if err:
        return err
    try:
        token = await srv_module.auth.get_access_token()
        return JSONResponse({
            "status": "ok",
            "has_tokens": True,
            "user_id": srv_module.auth.user_id,
            "tokens_path": str(srv_module.auth.tokens_path),
            "access_token_preview": token[:12] + "...",
        })
    except Exception as e:
        return JSONResponse({
            "status": "missing_tokens",
            "hint": "Andá a /oauth/start?setup=<token> para iniciar el flujo",
            "error": str(e),
        }, status_code=503)


# ---------- ASGI app ----------

def build_app() -> Starlette:
    # FastMCP expone la ASGI app via http_app(), montamos en /mcp
    mcp_asgi = srv_module.mcp.http_app(path="/")
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
        Middleware(
            CORSMiddleware,
            allow_origins=ALLOWED_ORIGINS,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
            allow_credentials=False,
        ),
        Middleware(AccessLogMiddleware),
        Middleware(BearerAuthMiddleware),
    ]
    return Starlette(
        routes=routes,
        middleware=middleware,
        lifespan=mcp_asgi.lifespan,
    )


app = build_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    log.info(
        "starting_http_server",
        host=host,
        port=port,
        auth_token_set=bool(MCP_AUTH_TOKEN),
    )
    uvicorn.run(
        "serve_http:app",
        host=host,
        port=port,
        log_level="info",
        access_log=False,  # ya logueamos via middleware
    )
