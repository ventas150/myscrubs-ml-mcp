"""
oauth_setup.py — Asistente interactivo para obtener el primer
refresh_token de MercadoLibre.

Uso:
  python oauth_setup.py

Flujo:
  1. Lee config.json (debe tener app_id, secret_key, redirect_uri)
  2. Abre el navegador hacia la URL de autorización ML
  3. Levanta un mini-server en localhost:8765 para capturar el ?code=
  4. Intercambia el code por access + refresh tokens
  5. Guarda tokens en ~/.myscrubs_ml/tokens.json
"""
from __future__ import annotations

import asyncio
import json
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import httpx

CONFIG = Path("./config.json")

if not CONFIG.exists():
    print("ERROR: No existe config.json. Copia config.example.json y edítalo.")
    sys.exit(1)

cfg = json.loads(CONFIG.read_text(encoding="utf-8"))["ml"]
APP_ID = cfg["app_id"]
SECRET = cfg["secret_key"]
REDIRECT = cfg["redirect_uri"]
SITE = cfg.get("site_id", "MLC").lower()

if APP_ID.startswith("REEMPLAZAR"):
    print("ERROR: Edita config.json con tu APP_ID real.")
    sys.exit(1)

# ML Chile usa auth.mercadolibre.cl
auth_host = f"https://auth.mercadolibre.{ {'mlc':'cl','mla':'com.ar','mlb':'com.br','mlm':'com.mx'}.get(SITE, 'cl') }"
AUTH_URL = (
    f"{auth_host}/authorization?response_type=code"
    f"&client_id={APP_ID}"
    f"&redirect_uri={urllib.parse.quote(REDIRECT, safe='')}"
)

print("=" * 70)
print("MyScrubs ML — Setup OAuth")
print("=" * 70)
print()
print("Voy a abrir tu navegador hacia ML. Confirmá que MyScrubs autoriza")
print("la app. Te va a redirigir a:")
print(f"  {REDIRECT}")
print()
print("Mantengo un mini-server local escuchando ahí para capturar el code.")
print()
print("URL de autorización:")
print(f"  {AUTH_URL}")
print()

captured_code: dict = {}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if "/callback" in self.path:
            qs = urllib.parse.urlparse(self.path).query
            params = dict(urllib.parse.parse_qsl(qs))
            captured_code["code"] = params.get("code")
            captured_code["error"] = params.get("error")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = (
                "<h1>OK</h1><p>Code capturado. Volvé a la terminal.</p>"
                if captured_code.get("code")
                else f"<h1>Error</h1><p>{captured_code.get('error')}</p>"
            )
            self.wfile.write(msg.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args, **kwargs):  # silenciar logs
        return


def run_server():
    server = HTTPServer(("127.0.0.1", 8765), Handler)
    server.handle_request()  # 1 sola vez


t = Thread(target=run_server, daemon=True)
t.start()

webbrowser.open(AUTH_URL)
print("⏳ Esperando que autorices en el navegador...")
t.join(timeout=300)

if not captured_code.get("code"):
    print(f"ERROR: no se capturó code. {captured_code.get('error', 'timeout')}")
    sys.exit(2)

code = captured_code["code"]
print(f"✓ Code recibido: {code[:12]}...")
print()
print("Intercambiando code por tokens...")


async def exchange():
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            "https://api.mercadolibre.com/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": APP_ID,
                "client_secret": SECRET,
                "code": code,
                "redirect_uri": REDIRECT,
            },
            headers={"Accept": "application/json"},
        )
        r.raise_for_status()
        return r.json()


import time

data = asyncio.run(exchange())
out = {
    "access_token": data["access_token"],
    "refresh_token": data["refresh_token"],
    "expires_at": time.time() + data["expires_in"] - 60,
    "user_id": data.get("user_id"),
}
target = Path.home() / ".myscrubs_ml" / "tokens.json"
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(out, indent=2))
try:
    import os, stat as st
    os.chmod(target, st.S_IRUSR | st.S_IWUSR)
except Exception:
    pass

print(f"✓ Tokens guardados en {target}")
print(f"✓ User ID: {out['user_id']}")
print(f"✓ access_token válido por {int(data['expires_in']/60)} minutos")
print(f"✓ refresh_token vigente por 6 meses con uso")
print()
print("Setup completo. Ya podés correr `python server.py` o instalar el plugin.")
