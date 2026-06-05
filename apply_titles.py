"""apply_titles.py — Aplica los 5 titles SEO + captura body de errores 400.

Corre en Render Shell con:
  python apply_titles.py
"""
import asyncio
import json
from ml_auth import auth_from_config
from ml_client import MLClient, MLApiError


TITLES = [
    ("MLC958953783", "Scrub Top Cherokee Revolution Ww610 Mujer Uniforme Clinico"),
    ("MLC1119205480", "Scrub Top Cherokee Revolution Ww670 Hombre Uniforme Clinico"),
    ("MLC1078037920", "Scrub Pantalon Mujer Cherokee Revolution Ww110 Clinico"),
    ("MLC977652264", "Scrub Top Mujer Cherokee Infinity 2625a Uniforme Clinico"),
    ("MLC976945657", "Gorro Quirurgico Cherokee Liso 2506 Unisex Clinico Medico"),
]


async def main():
    auth = auth_from_config({})
    client = MLClient(auth)
    for sku, title in TITLES:
        print(f"\n--- {sku} ---")
        # Primero, inspeccionar el item para diagnosticar
        try:
            item = await client.get(f"/items/{sku}")
            print(f"  current_title: {item.get('title')[:70]!r}")
            print(f"  catalog_listing: {item.get('catalog_listing')}")
            print(f"  catalog_product_id: {item.get('catalog_product_id')}")
            print(f"  status: {item.get('status')}")
            print(f"  variations: {len(item.get('variations') or [])}")
        except Exception as e:
            print(f"  GET fail: {e}")
            continue

        # Intentar update
        try:
            r = await client.put(f"/items/{sku}", json={"title": title})
            print(f"  ✓ UPDATED to: {title!r}")
        except MLApiError as e:
            print(f"  ✗ {e.status}: {json.dumps(e.body, ensure_ascii=False)[:500]}")
        except Exception as e:
            print(f"  ✗ {type(e).__name__}: {e}")
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
