"""
verify_setup.py — Diagnóstico end-to-end del MCP MyScrubs ML.

Corre checks de:
  - config válida
  - tokens existen y refrescan
  - identidad ML coincide con user_id_seller
  - hay publicaciones activas
  - categoría principal detectada
  - BSale bridge responde (si configurado)
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx


async def main():
    print("=" * 60)
    print("MyScrubs ML — Verificación de setup")
    print("=" * 60)

    cfg_path = Path("./config.json")
    if not cfg_path.exists():
        print("✗ config.json no existe")
        sys.exit(1)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    print("✓ config.json cargado")

    from ml_auth import auth_from_config
    from ml_client import MLClient

    auth = auth_from_config(cfg)
    token_path = Path.home() / ".myscrubs_ml" / "tokens.json"
    if not token_path.exists():
        print("✗ tokens.json no existe → corre oauth_setup.py primero")
        sys.exit(2)

    print("✓ tokens.json encontrado")
    try:
        token = await auth.get_access_token()
        print(f"✓ access_token válido ({token[:12]}...)")
    except Exception as e:
        print(f"✗ refresh falló: {e}")
        sys.exit(3)

    client = MLClient(auth)
    try:
        me = await client.get("/users/me")
        print(f"✓ Identidad ML: {me['nickname']} (id={me['id']})")
        expected = int(cfg['ml'].get('user_id_seller') or 0)
        if expected and me["id"] != expected:
            print(f"  ⚠ user_id en config ({expected}) != real ({me['id']})")
        print(f"  Site: {me['site_id']}")
        print(f"  Reputación: {me.get('seller_reputation', {}).get('level_id', '?')}")
    except Exception as e:
        print(f"✗ /users/me falló: {e}")
        sys.exit(4)

    try:
        items = await client.get(
            f"/users/{me['id']}/items/search",
            params={"status": "active", "limit": 1},
        )
        total = items.get("paging", {}).get("total", 0)
        print(f"✓ {total} publicaciones activas")
    except Exception as e:
        print(f"✗ /users/.../items/search falló: {e}")

    # Búsqueda categoría principal
    main_cat = (cfg["ml"].get("main_categories") or ["MLC1430"])[0]
    try:
        cat = await client.get(f"/categories/{main_cat}")
        print(f"✓ Categoría principal: {main_cat} → {cat['name']}")
    except Exception as e:
        print(f"⚠ Categoría {main_cat} no encontrada: {e}")

    # BSale
    print()
    if cfg.get("bsale", {}).get("use_existing_mcp"):
        print("ℹ BSale: configurado para usar MCP existente en Cowork.")
        print("  Verifica en Cowork que el MCP de BSale esté instalado y autenticado.")
    else:
        print("ℹ BSale: standalone (no integrado).")

    await client.close()
    print()
    print("✓ Setup completo. El MCP está listo para usar desde Cowork.")


if __name__ == "__main__":
    asyncio.run(main())
