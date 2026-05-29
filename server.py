"""
server.py — MyScrubs MercadoLibre MCP Server (FastMCP).

Punto de entrada del MCP. Registra todas las tools de lectura y escritura,
expone también el "Profit Engine" como tool consultable y orquesta la
inicialización del bridge BSale.

Uso local (stdio):
    python server.py

Uso en Cowork: empaquetado en `myscrubs-ml.plugin`, Cowork lo arranca
automáticamente al instalarse.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Optional

import structlog
from fastmcp import FastMCP

import tools_read as TR
import tools_write as TW
from bsale_bridge import BSaleBridge
from ml_auth import auth_from_config
from ml_client import MLClient
from profit_engine import (
    ProfitInputs,
    calcular_margen,
    evaluar_decision_precio,
    precio_minimo_para_margen,
)

# ---------- bootstrap ----------

structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger("myscrubs_ml")

CONFIG_PATH = Path(
    os.environ.get("MYSCRUBS_ML_CONFIG", "./config.json")
).expanduser()

# En cloud (Render), config.json puede no existir — todo viene de env vars
if CONFIG_PATH.exists():
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
else:
    log.warning("no_config_file_using_env_only", path=str(CONFIG_PATH))
    config = {
        "ml": {
            "site_id": os.environ.get("ML_SITE_ID", "MLC"),
            "user_id_seller": int(os.environ.get("ML_USER_ID_SELLER", "0") or 0),
            "main_categories": (
                os.environ.get("ML_MAIN_CATEGORIES", "MLC1430").split(",")
            ),
            "main_search_terms": (
                os.environ.get(
                    "ML_MAIN_SEARCH_TERMS",
                    "uniforme clinico,uniforme medico,scrub mujer,scrub hombre",
                ).split(",")
            ),
        },
        "bsale": {"use_existing_mcp": False},
        "profit": {
            "iva_pct": float(os.environ.get("PROFIT_IVA_PCT", "19")),
            "ml_commission_pct_by_listing_type": {
                "gold_pro": 17.5, "gold_premium": 13.0,
                "gold_special": 13.0, "free": 0.0,
            },
            "cuotas_sin_interes_cost_pct": {"3": 6, "6": 12, "12": 18},
            "envio_subsidio_estimado_clp": float(
                os.environ.get("PROFIT_ENVIO_SUBSIDIO_CLP", "1500")
            ),
            "target_min_margin_pct": float(
                os.environ.get("PROFIT_MIN_MARGIN_PCT", "20")
            ),
            "target_ideal_margin_pct": float(
                os.environ.get("PROFIT_IDEAL_MARGIN_PCT", "30")
            ),
        },
        "agent": {"guardrails": {
            "max_price_change_pct": float(
                os.environ.get("AGENT_MAX_CHANGE_PCT", "10")
            ),
            "max_changes_per_day": int(
                os.environ.get("AGENT_MAX_CHANGES_DAY", "20")
            ),
            "pause_threshold_days": int(
                os.environ.get("AGENT_PAUSE_DAYS", "7")
            ),
            "auto_respond_questions": (
                os.environ.get("AGENT_AUTO_QUESTIONS", "true").lower() == "true"
            ),
        }},
    }

# ---------- componentes globales ----------

# Token storage: por default ~/.myscrubs_ml/, en Render usa el disco persistente
TOKENS_DIR = Path(os.environ.get("MYSCRUBS_DATA_DIR", str(Path.home() / ".myscrubs_ml")))
TOKENS_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("ML_TOKENS_PATH", str(TOKENS_DIR / "tokens.json"))

auth = auth_from_config(config)
client = MLClient(auth)
bsale = BSaleBridge(
    cache_path=TOKENS_DIR / "bsale_cost_cache.json",
    use_existing_cowork_mcp=config.get("bsale", {}).get(
        "use_existing_mcp", False
    ),
)

SITE_ID: str = config["ml"]["site_id"]
USER_ID: int = int(config["ml"].get("user_id_seller") or auth.user_id or 0)
PROFIT_CFG: dict = config.get("profit", {})
COMMISSION_TABLE = PROFIT_CFG.get(
    "ml_commission_pct_by_listing_type",
    {"gold_pro": 17.5, "gold_premium": 13, "gold_special": 13, "free": 0},
)
CUOTAS_TABLE = {
    int(k): v
    for k, v in PROFIT_CFG.get(
        "cuotas_sin_interes_cost_pct", {"3": 6, "6": 12, "12": 18}
    ).items()
}

# ---------- FastMCP app ----------

mcp = FastMCP(
    name="myscrubs-mercadolibre",
    instructions=(
        "MCP de MercadoLibre Chile para MyScrubs. Exponé lectura de mercado, "
        "métricas propias, escritura de publicaciones/precios/stock, Q&A "
        "post-venta, y cálculo de margen neto por SKU (KPI maestro). "
        "Las acciones de escritura usan dry_run=True por default."
    ),
)


# =========================================================================
# READ TOOLS
# =========================================================================

@mcp.tool()
async def ml_buscar_categoria(
    query: Optional[str] = None,
    category_id: Optional[str] = None,
    limit: int = 50,
    sort: str = "relevance",
) -> dict:
    """Busca top N items en categoría/búsqueda. Base del análisis de mercado.

    Args:
        query: texto, ej "uniforme clinico mujer"
        category_id: id ML, ej "MLC1430"
        limit: max 50
        sort: relevance | price_asc | price_desc
    """
    return await TR.buscar_categoria(
        client, SITE_ID, query=query, category_id=category_id,
        limit=limit, sort=sort,
    )


@mcp.tool()
async def ml_competencia_top_n(query: str, n: int = 20) -> list[dict]:
    """Top N competidores en una búsqueda, excluyendo a MyScrubs."""
    return await TR.competencia_top_n(
        client, SITE_ID, query=query, n=n, exclude_seller_ids=[USER_ID],
    )


@mcp.tool()
async def ml_market_share(category_id: str) -> dict:
    """Estimación de market share por seller en la categoría."""
    return await TR.market_share_categoria(client, SITE_ID, category_id)


@mcp.tool()
async def ml_estadisticas_categoria(query: str) -> dict:
    """Mediana, rango, total vendido para una búsqueda."""
    return await TR.estadisticas_categoria(client, SITE_ID, query)


@mcp.tool()
async def ml_mis_publicaciones(
    status: str = "active", limit: int = 100
) -> list[dict]:
    """Lista mis publicaciones con detalle completo."""
    return await TR.mis_publicaciones(client, USER_ID, status, limit)


@mcp.tool()
async def ml_metricas_visitas(item_id: str, days: int = 7) -> dict:
    """Visitas a una publicación últimos N días."""
    return await TR.metricas_visitas(client, item_id, days)


@mcp.tool()
async def ml_health_publicacion(item_id: str) -> dict:
    """Health score y acciones recomendadas por ML para una publicación."""
    return await TR.health_publicacion(client, item_id)


@mcp.tool()
async def ml_preguntas_pendientes(limit: int = 50) -> list[dict]:
    """Preguntas sin responder."""
    return await TR.preguntas_pendientes(client, USER_ID, limit)


@mcp.tool()
async def ml_ordenes_recientes(days: int = 7, limit: int = 50) -> list[dict]:
    """Órdenes últimos N días con detalle financiero."""
    return await TR.ordenes_recientes(client, USER_ID, days, limit)


@mcp.tool()
async def ml_reclamos_abiertos() -> list[dict]:
    """Reclamos abiertos."""
    return await TR.reclamos_abiertos(client, USER_ID)


@mcp.tool()
async def ml_calcular_envio(item_id: str, zip_code_destino: str) -> dict:
    """Calcula costo de envío estimado."""
    return await TR.calculadora_envio(client, item_id, zip_code_destino)


# =========================================================================
# PROFIT ENGINE TOOLS
# =========================================================================

async def _calcular_margen_sku_helper(
    sku: str,
    pvp_clp: float,
    listing_type_id: str = "gold_pro",
    cuotas_sin_interes: Optional[int] = 3,
    free_shipping: bool = True,
) -> dict:
    """Helper interno (no decorado) para que otras tools puedan invocarlo
    sin depender del wrapper FastMCP."""
    cost = await bsale.get_cost_by_sku(sku)
    if not cost:
        return {
            "error": f"SKU {sku} no encontrado en BSale",
            "hint": "Verifica que el SKU exista o refresca el cache.",
        }
    inp = ProfitInputs(
        sku=sku,
        pvp_clp=pvp_clp,
        costo_neto_clp=cost.costo_neto_clp,
        listing_type_id=listing_type_id,
        cuotas_sin_interes=cuotas_sin_interes,
        free_shipping=free_shipping,
        envio_subsidio_clp=PROFIT_CFG.get("envio_subsidio_estimado_clp", 1500),
        iva_pct=PROFIT_CFG.get("iva_pct", 19),
        commission_table=COMMISSION_TABLE,
        cuotas_cost_table=CUOTAS_TABLE,
    )
    breakdown = calcular_margen(inp)
    return breakdown.to_dict()


@mcp.tool()
async def ml_margen_sku(
    sku: str,
    pvp_clp: float,
    listing_type_id: str = "gold_pro",
    cuotas_sin_interes: Optional[int] = 3,
    free_shipping: bool = True,
) -> dict:
    """
    Calcula el margen neto para un SKU. Llama a BSale por el costo.

    Devuelve el breakdown completo y la decisión sugerida.
    """
    return await _calcular_margen_sku_helper(
        sku, pvp_clp, listing_type_id, cuotas_sin_interes, free_shipping
    )


@mcp.tool()
async def ml_precio_minimo_objetivo(
    sku: str,
    margen_objetivo_pct: float = 25.0,
    listing_type_id: str = "gold_pro",
    cuotas_sin_interes: Optional[int] = 3,
    free_shipping: bool = True,
) -> dict:
    """
    Dado un SKU, devuelve el PVP que asegura un margen objetivo.
    Usar para decidir piso de precio antes de bajar para competir.
    """
    cost = await bsale.get_cost_by_sku(sku)
    if not cost:
        return {"error": f"SKU {sku} no en BSale"}
    inp = ProfitInputs(
        sku=sku,
        pvp_clp=1,  # placeholder, no se usa
        costo_neto_clp=cost.costo_neto_clp,
        listing_type_id=listing_type_id,
        cuotas_sin_interes=cuotas_sin_interes,
        free_shipping=free_shipping,
        envio_subsidio_clp=PROFIT_CFG.get("envio_subsidio_estimado_clp", 1500),
        iva_pct=PROFIT_CFG.get("iva_pct", 19),
        commission_table=COMMISSION_TABLE,
        cuotas_cost_table=CUOTAS_TABLE,
    )
    try:
        precio = precio_minimo_para_margen(inp, margen_objetivo_pct)
    except ValueError as e:
        return {"error": str(e)}
    return {
        "sku": sku,
        "costo_neto_clp": cost.costo_neto_clp,
        "margen_objetivo_pct": margen_objetivo_pct,
        "precio_minimo_clp": round(precio),
        "precio_minimo_sugerido_redondeado": round(precio / 10) * 10,
    }


@mcp.tool()
async def ml_decision_precio(
    sku: str, pvp_actual: float, posicion_ranking: int
) -> dict:
    """
    Heurística: dado un SKU, su precio actual y su posición en el ranking,
    devuelve qué hacer (mantener / subir / bajar / pausar).
    """
    margen = await _calcular_margen_sku_helper(sku, pvp_actual)
    if "error" in margen:
        return margen
    from profit_engine import ProfitBreakdown
    bd = ProfitBreakdown(**{k: v for k, v in margen.items() if k != "notes"})
    bd.notes = margen.get("notes", [])
    return evaluar_decision_precio(
        bd,
        posicion_ranking=posicion_ranking,
        margen_minimo_pct=PROFIT_CFG.get("target_min_margin_pct", 20),
        margen_ideal_pct=PROFIT_CFG.get("target_ideal_margin_pct", 30),
    )


# =========================================================================
# WRITE TOOLS
# =========================================================================

@mcp.tool()
async def ml_crear_publicacion(item_payload: dict, dry_run: bool = True) -> dict:
    """Crea publicación. dry_run=True valida sin crear."""
    return await TW.crear_publicacion(client, item_payload, dry_run)


@mcp.tool()
async def ml_actualizar_precio(
    item_id: str,
    nuevo_precio: float,
    motivo: str,
    max_change_pct: float = 10.0,
    dry_run: bool = True,
) -> dict:
    """Cambia precio con guardrail de %."""
    return await TW.actualizar_precio(
        client, item_id, nuevo_precio, motivo, max_change_pct, dry_run
    )


@mcp.tool()
async def ml_actualizar_stock(
    item_id: str, nuevo_stock: int, dry_run: bool = False
) -> dict:
    """Sync stock. Por design no requiere dry_run."""
    return await TW.actualizar_stock(client, item_id, nuevo_stock, dry_run)


@mcp.tool()
async def ml_actualizar_titulo(
    item_id: str,
    nuevo_titulo: Optional[str] = None,
    nueva_descripcion: Optional[str] = None,
    dry_run: bool = True,
) -> dict:
    """Cambia título y/o descripción."""
    return await TW.actualizar_titulo_descripcion(
        client, item_id, nuevo_titulo, nueva_descripcion, dry_run
    )


@mcp.tool()
async def ml_pausar_publicacion(
    item_id: str, motivo: str, dry_run: bool = True
) -> dict:
    """Pausa una publicación."""
    return await TW.pausar_publicacion(client, item_id, motivo, dry_run)


@mcp.tool()
async def ml_relistar_publicacion(item_id: str, dry_run: bool = True) -> dict:
    """Re-activa una publicación pausada."""
    return await TW.relistar_publicacion(client, item_id, dry_run)


@mcp.tool()
async def ml_responder_pregunta(
    question_id: int, texto: str, dry_run: bool = False
) -> dict:
    """Responde una pregunta a un comprador."""
    return await TW.responder_pregunta(client, question_id, texto, dry_run)


@mcp.tool()
async def ml_responder_mensaje(
    pack_id: str, user_id_buyer: int, texto: str, dry_run: bool = False
) -> dict:
    """Responde mensaje post-venta."""
    return await TW.responder_mensaje(
        client, pack_id, user_id_buyer, texto, dry_run
    )


# =========================================================================
# AGENT TOOLS (invocables on-demand también, no solo cron)
# =========================================================================

@mcp.tool()
async def ml_agente_corrida_diaria(modo: str = "report_only") -> dict:
    """
    Corre el agente diario de optimización.

    Args:
        modo: "report_only" (sin escribir) | "apply_safe" (acciones
              dentro de guardrails) | "apply_all_with_human" (pide
              aprobación para cambios sensibles).
    """
    from agent_daily import correr_agente
    return await correr_agente(
        client=client,
        bsale=bsale,
        user_id=USER_ID,
        site_id=SITE_ID,
        config=config,
        modo=modo,
    )


@mcp.tool()
async def ml_acciones_hoy() -> dict:
    """Resumen de qué hizo el MCP hoy (audit log)."""
    return TW.resumen_acciones_hoy()


@mcp.tool()
async def ml_acciones_periodo(
    days_back: int = 7,
    only_applied: bool = True,
    accion: str | None = None,
    item_id: str | None = None,
    limit: int = 200,
) -> dict:
    """
    Lee el audit log y devuelve TODAS las acciones del MCP en un período.
    Útil para Cowork: "qué hizo el agente en los últimos 7 días?".

    Args:
      days_back: cuántos días atrás mirar (default 7).
      only_applied: True = solo cambios efectivos (default). False = incluye
                    propuestas / dry_run / bloqueadas por guardrail.
      accion: filtro exacto por tipo (ej. "actualizar_precio",
              "actualizar_stock", "responder_pregunta", "pausar").
      item_id: filtro por SKU específico (ej. "MLC958953783").
      limit: máximo de entradas a devolver (default 200).
    """
    return TW.acciones_periodo(
        days_back=days_back,
        only_applied=only_applied,
        accion_filter=accion,
        item_id_filter=item_id,
        limit=limit,
    )


@mcp.tool()
async def ml_cambios_precio_periodo(days_back: int = 7) -> dict:
    """
    Lista compacta de cambios de precio aplicados en los últimos N días.
    Devuelve: SKU, precio antes, precio después, delta %, motivo.
    Perfecto para preguntarle "qué precios cambió el agente esta semana?".
    """
    return TW.cambios_precio_periodo(days_back=days_back)


# =========================================================================
# Run
# =========================================================================

if __name__ == "__main__":
    # MCP_TRANSPORT: "stdio" (Cowork local) | "streamable-http" (Render/cloud)
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    log.info(
        "starting_myscrubs_ml_mcp",
        site=SITE_ID,
        user_id=USER_ID,
        config_path=str(CONFIG_PATH) if CONFIG_PATH.exists() else "(env only)",
        transport=transport,
    )
    if transport == "stdio":
        mcp.run()
    else:
        # En Render, PORT viene como env var
        port = int(os.environ.get("PORT", "8000"))
        host = os.environ.get("HOST", "0.0.0.0")
        mcp.run(
            transport="streamable-http",
            host=host,
            port=port,
            path="/mcp",
        )
