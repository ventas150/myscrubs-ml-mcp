"""apply_titles.py — Aplica los 5 titles SEO directo via MLClient.

Corre en Render Shell con:
  python apply_titles.py
"""
import asyncio
from ml_auth import auth_from_config
from ml_client import MLClient
from tools_write import actualizar_titulo_descripcion


TITLES = [
    ("MLC958953783", "Scrub Top Cherokee Revolution Ww610 Mujer Uniforme Clinico"),
    ("MLC1119205480", "Scrub Top Cherokee Revolution Ww670 Hombre Uniforme Clinico"),
    ("MLC1078037920", "Scrub Pantalon Mujer Cherokee Revolution Ww110 Clinico"),
    ("MLC977652264", "Scrub Top Mujer Cherokee Infinity 2625a Uniforme Clinico"),
    ("MLC976945657", "Gorro Quirurgico Cherokee Liso 2506 Unisex Clinico Medico"),
]


async def main():
    # auth_from_config lee env vars (ML_APP_ID, ML_SECRET_KEY, etc.) si están
    auth = auth_from_config({})
    client = MLClient(auth)
    for sku, title in TITLES:
        try:
            result = await actualizar_titulo_descripcion(
                client=client,
                item_id=sku,
                nuevo_titulo=title,
                dry_run=False,
            )
            applied = result.get("applied")
            print(f"{'OK ' if applied else 'FAIL'} {sku}: {result}")
        except Exception as e:
            print(f"FAIL {sku}: {type(e).__name__}: {e}")
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
