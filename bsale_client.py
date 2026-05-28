"""
bsale_client.py — Cliente HTTP standalone para la API de BSale.

Usa el access_token de BSale (BSale > Configuración > API Acceso).
Es independiente del MCP de BSale de Cowork: puede correr en la nube
sin requerir Cowork ni el MCP local.

Endpoints utilizados:
  GET /v1/products.json?code={sku}            → datos producto + costo
  GET /v1/variants.json?code={sku}            → variantes con costo
  GET /v1/stocks.json?variantid={id}          → stock agregado por variante
  GET /v1/products/{id}.json                  → detalle producto

BSale tiene rate limit ~10 req/s. Usamos un rate limiter local.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Optional

import httpx
import structlog
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = structlog.get_logger()

BSALE_API_BASE = "https://api.bsale.io/v1"


class BSaleError(Exception):
    def __init__(self, status: int, message: str, body: Any = None):
        self.status = status
        self.message = message
        self.body = body
        super().__init__(f"BSale API {status}: {message}")


class BSaleClient:
    """Cliente async para BSale. Lee access_token de env BSALE_ACCESS_TOKEN."""

    def __init__(
        self,
        access_token: Optional[str] = None,
        rate_limit_rps: int = 8,
        timeout: float = 30.0,
    ):
        token = access_token or os.environ.get("BSALE_ACCESS_TOKEN")
        if not token:
            raise RuntimeError(
                "BSALE_ACCESS_TOKEN no seteado. Generalo en "
                "BSale > Configuración > API Acceso."
            )
        self._token = token
        self._http = httpx.AsyncClient(
            base_url=BSALE_API_BASE,
            timeout=timeout,
            headers={
                "access_token": token,
                "Accept": "application/json",
                "User-Agent": "MyScrubs-ML-MCP/1.0",
            },
        )
        self._rl_semaphore = asyncio.Semaphore(rate_limit_rps)

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(
        self, method: str, path: str, params: Optional[dict] = None,
    ) -> Any:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type((httpx.TimeoutException, BSaleError)),
            reraise=True,
        ):
            with attempt:
                async with self._rl_semaphore:
                    r = await self._http.request(method, path, params=params)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise BSaleError(r.status_code, "Retryable", body=r.text[:300])
                if r.status_code >= 400:
                    try:
                        body = r.json()
                    except Exception:
                        body = r.text
                    raise BSaleError(
                        r.status_code, f"{method} {path} failed", body=body
                    )
                if r.status_code == 204 or not r.content:
                    return None
                return r.json()

    # ---------- productos / variantes ----------

    async def buscar_producto_por_sku(self, sku: str) -> Optional[dict]:
        """Busca producto por code/SKU. Devuelve el primer match."""
        data = await self._request(
            "GET", "/products.json", params={"code": sku, "limit": 1},
        )
        items = (data or {}).get("items", []) if isinstance(data, dict) else []
        return items[0] if items else None

    async def buscar_variante_por_sku(self, sku: str) -> Optional[dict]:
        """Busca variante por code/SKU. La variante trae el costo real."""
        data = await self._request(
            "GET", "/variants.json", params={"code": sku, "limit": 1},
        )
        items = (data or {}).get("items", []) if isinstance(data, dict) else []
        return items[0] if items else None

    async def stock_por_variante(self, variant_id: int) -> int:
        """Stock agregado de una variante (suma sobre todas las sucursales)."""
        data = await self._request(
            "GET", "/stocks.json", params={"variantid": variant_id, "limit": 50},
        )
        items = (data or {}).get("items", []) if isinstance(data, dict) else []
        return sum(int(s.get("quantityAvailable", 0) or 0) for s in items)

    async def obtener_costo_y_stock(self, sku: str) -> Optional[dict]:
        """
        Pipeline completo: variante → costo → stock agregado.
        Devuelve dict con `costo_neto`, `stock_total`, `nombre`, `variant_id`.
        """
        variant = await self.buscar_variante_por_sku(sku)
        if not variant:
            # Fallback: buscar como producto (caso productos sin variantes)
            prod = await self.buscar_producto_por_sku(sku)
            if not prod:
                return None
            return {
                "costo_neto": float(prod.get("cost", 0) or 0),
                "stock_total": 0,
                "nombre": prod.get("name", sku),
                "variant_id": None,
                "categoria_bsale": (
                    (prod.get("product_type") or {}).get("name", "")
                ),
            }
        stock = await self.stock_por_variante(int(variant["id"]))
        return {
            "costo_neto": float(variant.get("cost", 0) or 0),
            "stock_total": stock,
            "nombre": variant.get("description") or variant.get("code") or sku,
            "variant_id": int(variant["id"]),
            "categoria_bsale": None,
        }
