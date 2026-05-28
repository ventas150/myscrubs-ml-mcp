"""
bsale_bridge.py — Puente con el MCP de BSale ya instalado en Cowork.

Roberto ya tiene un MCP de BSale corriendo. En vez de duplicar lógica,
este módulo es un cliente DELGADO que:

1. En modo "standalone" (testing): llama directo a la API BSale
2. En modo "via_cowork_mcp" (producción): delega al MCP existente

Para producción, los tools del MCP ML que necesitan costo simplemente
invocan al MCP de BSale via stdio (Cowork hace de bus).

NOTA: Este módulo expone una INTERFACE estándar (`get_cost_by_sku`,
`get_stock_by_sku`) para que el resto del MCP no sepa de dónde viene
el dato. Si mañana cambias de BSale a otro POS, solo cambias esto.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger()


@dataclass
class SkuCost:
    sku: str
    costo_neto_clp: float
    nombre: str
    categoria_bsale: Optional[str]
    stock_total: int
    last_updated: float


class BSaleBridge:
    """
    Bridge hacia BSale. Cachea costos para evitar hammering.

    Métodos públicos:
      - get_cost_by_sku(sku) -> SkuCost | None
      - bulk_costs(skus) -> dict[str, SkuCost]
      - get_stock_by_sku(sku) -> int
      - refresh_cache() -> int (cuántos SKUs cacheados)
    """

    def __init__(
        self,
        cache_path: Optional[Path] = None,
        cache_ttl_hours: int = 24,
        use_existing_cowork_mcp: bool = True,
    ):
        self.cache_path = (
            cache_path or Path.home() / ".myscrubs_ml" / "bsale_cost_cache.json"
        )
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_ttl = cache_ttl_hours * 3600
        self.use_cowork = use_existing_cowork_mcp
        self._cache: dict[str, SkuCost] = {}
        self._load_cache()

    # ---------- cache ----------

    def _load_cache(self) -> None:
        if not self.cache_path.exists():
            return
        try:
            data = json.loads(self.cache_path.read_text())
            for sku, payload in data.items():
                self._cache[sku] = SkuCost(**payload)
            log.info("bsale_cache_loaded", count=len(self._cache))
        except Exception as e:
            log.warning("bsale_cache_load_failed", error=str(e))

    def _save_cache(self) -> None:
        payload = {sku: vars(cost) for sku, cost in self._cache.items()}
        self.cache_path.write_text(json.dumps(payload, indent=2))

    def _is_fresh(self, cost: SkuCost) -> bool:
        return (time.time() - cost.last_updated) < self.cache_ttl

    # ---------- API pública ----------

    async def get_cost_by_sku(self, sku: str) -> Optional[SkuCost]:
        cached = self._cache.get(sku)
        if cached and self._is_fresh(cached):
            return cached

        fresh = await self._fetch_one(sku)
        if fresh:
            self._cache[sku] = fresh
            self._save_cache()
        return fresh

    async def bulk_costs(self, skus: list[str]) -> dict[str, SkuCost]:
        """Obtiene varios SKUs en paralelo (limitado para no saturar BSale)."""
        sem = asyncio.Semaphore(5)

        async def one(sku: str) -> tuple[str, Optional[SkuCost]]:
            async with sem:
                return sku, await self.get_cost_by_sku(sku)

        results = await asyncio.gather(*[one(s) for s in skus])
        return {sku: cost for sku, cost in results if cost is not None}

    async def get_stock_by_sku(self, sku: str) -> int:
        cost = await self.get_cost_by_sku(sku)
        return cost.stock_total if cost else 0

    async def refresh_cache(self, skus: list[str]) -> int:
        """Fuerza refresh de un set de SKUs. Útil para corrida diaria."""
        for sku in skus:
            self._cache.pop(sku, None)
        results = await self.bulk_costs(skus)
        return len(results)

    # ---------- backend ----------

    async def _fetch_one(self, sku: str) -> Optional[SkuCost]:
        """
        Por default usa BSaleClient standalone (api.bsale.io directo).
        En Cowork se reemplaza por CoworkBSaleDelegate que enruta al
        MCP de BSale ya conectado.
        """
        # Importación tardía para no requerir BSALE_ACCESS_TOKEN si
        # el bridge se usa solo en Cowork con delegate.
        from bsale_client import BSaleClient
        if not hasattr(self, "_standalone_client"):
            self._standalone_client = BSaleClient()
        data = await self._standalone_client.obtener_costo_y_stock(sku)
        if not data:
            return None
        return SkuCost(
            sku=sku,
            costo_neto_clp=float(data["costo_neto"]),
            nombre=data["nombre"],
            categoria_bsale=data.get("categoria_bsale"),
            stock_total=int(data["stock_total"]),
            last_updated=time.time(),
        )


# ---------- delegate hook ----------

class CoworkBSaleDelegate(BSaleBridge):
    """
    Variante que se usa cuando el MCP corre dentro de Cowork y puede
    invocar el MCP de BSale como un servicio peer.

    El "delegate_fn" recibe nombre de tool BSale + args, y devuelve la
    respuesta cruda. Lo provee el shell de Cowork al inicializar.
    """

    def __init__(self, delegate_fn, **kwargs):
        super().__init__(**kwargs)
        self._delegate = delegate_fn

    async def _fetch_one(self, sku: str) -> Optional[SkuCost]:
        try:
            # 1) Buscar producto en BSale por SKU/code
            prod = await self._delegate(
                "bsale_listar_productos", {"code": sku, "limit": 1}
            )
            items = prod.get("items", []) if isinstance(prod, dict) else []
            if not items:
                return None
            p = items[0]
            # 2) Obtener stock agregado
            stock_data = await self._delegate(
                "bsale_stock_agregado", {"sku": sku}
            )
            stock_total = (
                stock_data.get("total_quantity", 0)
                if isinstance(stock_data, dict) else 0
            )
            return SkuCost(
                sku=sku,
                costo_neto_clp=float(p.get("cost", 0) or 0),
                nombre=p.get("name", ""),
                categoria_bsale=str(p.get("product_type", {}).get("name", ""))
                if isinstance(p.get("product_type"), dict) else None,
                stock_total=int(stock_total),
                last_updated=time.time(),
            )
        except Exception as e:
            log.error("bsale_delegate_failed", sku=sku, error=str(e))
            return None
