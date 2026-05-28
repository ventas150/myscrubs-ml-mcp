"""
tools_read.py — Tools de lectura del MCP MyScrubs ML.

Cada función está pensada para ser registrada con @mcp.tool en server.py.
Aquí están como funciones puras async para facilitar testing y reuso
desde el agente diario.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from typing import Any, Optional

import structlog

from ml_client import MLClient

log = structlog.get_logger()


# =========================================================================
# 1. MERCADO / COMPETENCIA
# =========================================================================

async def buscar_categoria(
    client: MLClient,
    site_id: str,
    query: Optional[str] = None,
    category_id: Optional[str] = None,
    limit: int = 50,
    sort: str = "relevance",
    filters: Optional[dict] = None,
) -> dict:
    """
    Top N items en una categoría o búsqueda. Base para todo análisis de mercado.

    Args:
      query: texto de búsqueda, ej "uniforme clinico mujer"
      category_id: ID categoría ML, ej "MLC1430"
      sort: relevance | price_asc | price_desc
      filters: {"condition": "new", "official_store_id": "1234", ...}
    """
    params: dict[str, Any] = {"limit": min(limit, 50), "sort": sort}
    if query:
        params["q"] = query
    if category_id:
        params["category"] = category_id
    if filters:
        params.update(filters)
    return await client.get(f"/sites/{site_id}/search", params=params)


async def competencia_top_n(
    client: MLClient,
    site_id: str,
    query: str,
    n: int = 20,
    exclude_seller_ids: Optional[list[int]] = None,
) -> list[dict]:
    """
    Devuelve los top N items de una búsqueda, excluyendo a un seller
    (ej. excluyes tu propio user_id para ver competencia).
    """
    data = await buscar_categoria(client, site_id, query=query, limit=n)
    items = data.get("results", [])
    if exclude_seller_ids:
        items = [
            it for it in items
            if it.get("seller", {}).get("id") not in exclude_seller_ids
        ]
    return items[:n]


async def evolucion_precios_item(
    client: MLClient,
    item_id: str,
    historial_local_store: Optional[dict] = None,
) -> dict:
    """
    Devuelve el precio actual + snapshot. La evolución histórica se construye
    fuera (en el agente diario, que persiste snapshots y arma el delta).
    """
    item = await client.get(f"/items/{item_id}")
    snapshot = {
        "item_id": item_id,
        "title": item.get("title"),
        "price": item.get("price"),
        "currency_id": item.get("currency_id"),
        "available_quantity": item.get("available_quantity"),
        "sold_quantity": item.get("sold_quantity"),
        "seller_id": item.get("seller_id"),
        "permalink": item.get("permalink"),
        "captured_at": datetime.utcnow().isoformat(),
    }
    if historial_local_store is not None:
        historial_local_store.setdefault(item_id, []).append(snapshot)
    return snapshot


async def market_share_categoria(
    client: MLClient,
    site_id: str,
    category_id: str,
    sample_size: int = 100,
) -> dict:
    """
    Estimación rápida de market share por seller dentro de una categoría.

    Métrica: concentración de top N items + sold_quantity por seller.
    Es un PROXY, no la cifra oficial (ML no expone share oficial).
    """
    data = await buscar_categoria(
        client, site_id, category_id=category_id, limit=50, sort="relevance"
    )
    items = data.get("results", [])
    # Si quisieras 100, paginar 2 veces (ML max 50 por página en search)
    by_seller: dict[int, dict] = {}
    for it in items:
        seller = it.get("seller", {})
        sid = seller.get("id")
        if not sid:
            continue
        entry = by_seller.setdefault(
            sid,
            {
                "seller_id": sid,
                "seller_nickname": seller.get("nickname", "?"),
                "n_items_top": 0,
                "sold_quantity_total": 0,
                "items_in_sample": [],
            },
        )
        entry["n_items_top"] += 1
        entry["sold_quantity_total"] += int(it.get("sold_quantity", 0) or 0)
        entry["items_in_sample"].append(it.get("id"))
    sorted_sellers = sorted(
        by_seller.values(),
        key=lambda x: (x["sold_quantity_total"], x["n_items_top"]),
        reverse=True,
    )
    total_top = sum(s["n_items_top"] for s in sorted_sellers) or 1
    total_sold = sum(s["sold_quantity_total"] for s in sorted_sellers) or 1
    for s in sorted_sellers:
        s["share_top_pct"] = round(s["n_items_top"] / total_top * 100, 2)
        s["share_sold_pct"] = round(
            s["sold_quantity_total"] / total_sold * 100, 2
        )
    return {
        "category_id": category_id,
        "sample_size": len(items),
        "top_sellers": sorted_sellers[:15],
        "captured_at": datetime.utcnow().isoformat(),
    }


async def estadisticas_categoria(
    client: MLClient, site_id: str, query: str
) -> dict:
    """
    Estadísticas agregadas: mediana de precio, rango, top vendidos.
    Útil para detectar si MyScrubs está sobre/bajo el precio de mercado.
    """
    data = await buscar_categoria(client, site_id, query=query, limit=50)
    items = data.get("results", [])
    precios = [it.get("price", 0) for it in items if it.get("price")]
    if not precios:
        return {"query": query, "items": 0, "error": "sin resultados"}
    return {
        "query": query,
        "items": len(items),
        "precio_min": min(precios),
        "precio_max": max(precios),
        "precio_mediana": statistics.median(precios),
        "precio_media": round(statistics.mean(precios)),
        "precio_p25": statistics.quantiles(precios, n=4)[0] if len(precios) >= 4 else None,
        "precio_p75": statistics.quantiles(precios, n=4)[-1] if len(precios) >= 4 else None,
        "total_unidades_vendidas_sample": sum(
            int(it.get("sold_quantity", 0) or 0) for it in items
        ),
        "captured_at": datetime.utcnow().isoformat(),
    }


# =========================================================================
# 2. MIS PUBLICACIONES
# =========================================================================

async def mis_publicaciones(
    client: MLClient,
    user_id: int,
    status: str = "active",
    limit: int = 100,
) -> list[dict]:
    """
    Lista mis publicaciones (active | paused | closed | under_review).
    Devuelve la lista ENRIQUECIDA con detalle de cada item.
    """
    # 1) Obtener IDs de items (paginado)
    ids: list[str] = []
    offset = 0
    while len(ids) < limit:
        page = await client.get(
            f"/users/{user_id}/items/search",
            params={"status": status, "limit": 50, "offset": offset},
        )
        results = page.get("results", [])
        if not results:
            break
        ids.extend(results)
        offset += 50
        if offset >= page.get("paging", {}).get("total", 0):
            break
    ids = ids[:limit]
    if not ids:
        return []
    # 2) Bulk fetch detalles (máx 20 por llamada en /items?ids=)
    items_detail: list[dict] = []
    for i in range(0, len(ids), 20):
        chunk = ids[i:i + 20]
        bulk = await client.get(
            "/items",
            params={"ids": ",".join(chunk)},
        )
        for entry in bulk:
            if entry.get("code") == 200:
                items_detail.append(entry["body"])
    return items_detail


async def metricas_visitas(
    client: MLClient,
    item_id: str,
    days: int = 7,
) -> dict:
    """
    Visitas a una publicación en los últimos N días.
    Endpoint: /items/{id}/visits/time_window
    """
    return await client.get(
        f"/items/{item_id}/visits/time_window",
        params={"last": days, "unit": "day"},
    )


async def health_publicacion(client: MLClient, item_id: str) -> dict:
    """
    Trae health score, reputación de la publicación.
    """
    return await client.get(f"/items/{item_id}/health/actions")


# =========================================================================
# 3. PREGUNTAS Y MENSAJES
# =========================================================================

async def preguntas_pendientes(
    client: MLClient,
    user_id: int,
    limit: int = 50,
) -> list[dict]:
    """
    Preguntas sin responder a publicaciones del seller.
    Estado: UNANSWERED.
    """
    data = await client.get(
        "/questions/search",
        params={
            "seller_id": user_id,
            "status": "UNANSWERED",
            "limit": limit,
            "sort_fields": "date_created",
            "sort_types": "DESC",
        },
    )
    return data.get("questions", [])


async def mensajes_post_venta(
    client: MLClient,
    user_id: int,
    role: str = "seller",
    limit: int = 50,
) -> list[dict]:
    """
    Mensajes del centro de mensajería post-venta.
    """
    return await client.get(
        f"/messages/packs/{user_id}",
        params={"limit": limit, "role": role},
    )


# =========================================================================
# 4. ÓRDENES Y POST-VENTA
# =========================================================================

async def ordenes_recientes(
    client: MLClient,
    user_id: int,
    days: int = 7,
    limit: int = 50,
) -> list[dict]:
    """
    Órdenes del seller en los últimos N días, con detalle financiero por
    line item: precio, fee_ml, shipping_cost, etc.
    """
    desde = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00.000Z")
    data = await client.get(
        "/orders/search",
        params={
            "seller": user_id,
            "order.date_created.from": desde,
            "sort": "date_desc",
            "limit": limit,
        },
    )
    return data.get("results", [])


async def reclamos_abiertos(client: MLClient, user_id: int) -> list[dict]:
    """Reclamos / mediaciones abiertas."""
    data = await client.get(
        "/post-purchase/v1/claims/search",
        params={"resource_role": "respondent", "status": "opened", "limit": 50},
    )
    return data.get("data", [])


# =========================================================================
# 5. ENVÍOS
# =========================================================================

async def calculadora_envio(
    client: MLClient,
    item_id: str,
    zip_code_destino: str,
) -> dict:
    """
    Estima costo de envío para un item a un código postal.
    Útil para entender el COSTO REAL que ML cobra al comprador o subsidia.
    """
    return await client.get(
        f"/items/{item_id}/shipping_options",
        params={"zip_code": zip_code_destino},
    )
