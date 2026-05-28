#!/usr/bin/env python3
"""
deploy_to_github.py — Sube todo el proyecto MyScrubs ML MCP a un repo
nuevo en GitHub, en un solo comando. Sin necesidad de tener git ni gh CLI
instalado.

USO:
    1. Generá un Personal Access Token (PAT) en GitHub:
       https://github.com/settings/tokens/new
       Scopes mínimos: "repo" (Full control of private repositories)
       Copia el token (empieza con ghp_ o github_pat_).

    2. Corré este script desde la carpeta que contiene todos los archivos
       del proyecto (server.py, render.yaml, etc):

       python deploy_to_github.py

    3. Te va a pedir:
       - GitHub username
       - PAT
       - Nombre del repo (default: myscrubs-ml-mcp)

    4. Si todo sale bien, en ~30 segundos imprime la URL del repo nuevo.

Sin dependencias externas — usa solo urllib + json de stdlib.
"""
from __future__ import annotations

import base64
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

GITHUB_API = "https://api.github.com"

# Archivos del proyecto que se suben al repo. Excluimos secretos, caches y temp.
INCLUDE_FILES = [
    # Docs
    "README.md",
    "PLAN.md",
    "SETUP_ML_DEVELOPERS.md",
    "RENDER_DEPLOY.md",
    "DEPLOY_NOW.md",
    # Config / templates
    "config.example.json",
    ".env.example",
    "requirements.txt",
    "plugin.json",
    # Code: MCP server
    "server.py",
    "serve_http.py",
    "ml_auth.py",
    "ml_client.py",
    "profit_engine.py",
    "bsale_bridge.py",
    "bsale_client.py",
    "tools_read.py",
    "tools_write.py",
    "agent_daily.py",
    # Code: Claude agent
    "claude_agent.py",
    # Setup scripts
    "oauth_setup.py",
    "verify_setup.py",
    # Deploy
    "Dockerfile",
    ".dockerignore",
    ".gitignore",
    "render.yaml",
    # Este propio script (útil para re-deploy)
    "deploy_to_github.py",
]

# Estos NUNCA se suben (defensa adicional contra leaks)
NEVER_INCLUDE = {
    "config.json",
    ".env",
    "tokens.json",
    "audit.log",
}


def _req(
    method: str,
    path: str,
    token: str,
    body: Optional[dict] = None,
) -> dict:
    """HTTP request a la API de GitHub con manejo decente de errores."""
    url = f"{GITHUB_API}{path}" if path.startswith("/") else path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "myscrubs-deploy/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json" if data else "",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8"))
        except Exception:
            err_body = {}
        raise RuntimeError(
            f"GitHub API {method} {path} → {e.code}: "
            f"{err_body.get('message', str(e))}\n  {err_body}"
        ) from None


def get_authenticated_user(token: str) -> dict:
    return _req("GET", "/user", token)


def create_repo(
    token: str, name: str, description: str, private: bool = True,
) -> dict:
    return _req(
        "POST",
        "/user/repos",
        token,
        {
            "name": name,
            "description": description,
            "private": private,
            "auto_init": True,  # Crea con README inicial para que ya tenga branch main
            "default_branch": "main",
        },
    )


def get_default_branch_sha(token: str, owner: str, repo: str) -> str:
    info = _req("GET", f"/repos/{owner}/{repo}", token)
    branch = info.get("default_branch", "main")
    ref = _req(
        "GET", f"/repos/{owner}/{repo}/git/ref/heads/{branch}", token
    )
    return ref["object"]["sha"]


def upload_file(
    token: str,
    owner: str,
    repo: str,
    file_path: Path,
    repo_path: str,
    message: str,
) -> None:
    """Sube/actualiza un archivo via Contents API (1 commit por archivo)."""
    if not file_path.exists():
        print(f"  ⚠ skip (no existe): {file_path.name}")
        return
    content_b64 = base64.b64encode(file_path.read_bytes()).decode("ascii")
    # Check si ya existe (para incluir sha en el update)
    sha = None
    try:
        existing = _req(
            "GET",
            f"/repos/{owner}/{repo}/contents/{repo_path}",
            token,
        )
        if isinstance(existing, dict):
            sha = existing.get("sha")
    except RuntimeError:
        pass
    body = {
        "message": message,
        "content": content_b64,
        "branch": "main",
    }
    if sha:
        body["sha"] = sha
    _req(
        "PUT",
        f"/repos/{owner}/{repo}/contents/{repo_path}",
        token,
        body,
    )


def main():
    print("=" * 70)
    print("MyScrubs ML MCP — Auto-deploy a GitHub")
    print("=" * 70)
    print()

    # Validar que estamos en la carpeta correcta
    here = Path(__file__).resolve().parent
    os.chdir(here)
    print(f"Working dir: {here}")
    critical_files = ["server.py", "render.yaml", "claude_agent.py"]
    missing = [f for f in critical_files if not (here / f).exists()]
    if missing:
        print(f"\n✗ Faltan archivos críticos en {here}:")
        for m in missing:
            print(f"    {m}")
        print("\nCorré el script desde la carpeta del proyecto.")
        sys.exit(1)
    print("✓ Archivos del proyecto detectados")
    print()

    # Inputs
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("Tu Personal Access Token de GitHub (no se imprime en pantalla):")
        print("  → si no tenés uno: https://github.com/settings/tokens/new")
        print("  → scope requerido: 'repo'")
        token = getpass.getpass("PAT: ").strip()
    if not token:
        print("✗ Token vacío. Abortando.")
        sys.exit(2)

    # Validar token + identificar al usuario
    try:
        me = get_authenticated_user(token)
    except RuntimeError as e:
        print(f"\n✗ Token inválido o sin permisos: {e}")
        sys.exit(3)
    owner = me["login"]
    print(f"✓ Autenticado como: {owner}  ({me.get('name','')})")
    print()

    default_name = "myscrubs-ml-mcp"
    repo_name = input(f"Nombre del repo [{default_name}]: ").strip() or default_name
    description = (
        "MCP autónomo para MercadoLibre Chile con agente Claude API que "
        "optimiza margen neto por SKU en uniformes clínicos MyScrubs."
    )

    # Crear repo
    print(f"\n→ Creando repo privado {owner}/{repo_name}...")
    try:
        repo = create_repo(token, repo_name, description, private=True)
    except RuntimeError as e:
        if "already exists" in str(e).lower() or "name already exists" in str(e).lower():
            print(f"  ⚠ El repo {owner}/{repo_name} ya existe.")
            ans = input("  ¿Subir archivos a ese repo igual? [y/N]: ").strip().lower()
            if ans != "y":
                print("Abortando.")
                sys.exit(4)
            repo = _req("GET", f"/repos/{owner}/{repo_name}", token)
        else:
            print(f"\n✗ Falló crear repo: {e}")
            sys.exit(5)
    repo_html = repo["html_url"]
    print(f"✓ Repo listo: {repo_html}")
    print()

    # Subir archivos
    print(f"→ Subiendo {len(INCLUDE_FILES)} archivos...")
    uploaded = 0
    skipped = 0
    failed: list[tuple[str, str]] = []
    for name in INCLUDE_FILES:
        if name in NEVER_INCLUDE:
            continue
        local_path = here / name
        if not local_path.exists():
            print(f"  - skip (no existe local): {name}")
            skipped += 1
            continue
        try:
            upload_file(
                token, owner, repo_name, local_path, name,
                f"chore: add {name}",
            )
            print(f"  ✓ {name}")
            uploaded += 1
            time.sleep(0.15)  # Cortesía con la API de GitHub
        except RuntimeError as e:
            print(f"  ✗ {name}: {e}")
            failed.append((name, str(e)))

    print()
    print("=" * 70)
    print(f"Subidos:    {uploaded}")
    print(f"Saltados:   {skipped}")
    print(f"Fallidos:   {len(failed)}")
    if failed:
        print("\nArchivos fallidos:")
        for name, err in failed:
            print(f"  - {name}: {err[:120]}")
    print()
    print(f"Repo: {repo_html}")
    print()
    print("Siguientes pasos (ver RENDER_DEPLOY.md para detalle):")
    print()
    print("  1. Entrá a https://dashboard.render.com → New + → Blueprint")
    print(f"  2. Conectá el repo: {owner}/{repo_name}")
    print("  3. Render detecta render.yaml y crea 3 servicios.")
    print("  4. Completá los 6 secretos pedidos:")
    print("       ML_APP_ID, ML_SECRET_KEY, ML_USER_ID_SELLER,")
    print("       BSALE_ACCESS_TOKEN, ANTHROPIC_API_KEY, SMTP_* (opcional)")
    print("  5. Esperá 5-10 min al primer build.")
    print("  6. Copiá la URL pública que te asignó Render y actualizá MCP_URL")
    print("     en los 2 cron jobs.")
    print("  7. Hacé OAuth abriendo en navegador:")
    print("     https://<tu-url>/oauth/start?setup=<MCP_AUTH_TOKEN>")
    print()
    print("Listo. El agente Claude empieza a correr al próximo cron tick.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nAbortado por usuario.")
        sys.exit(130)
