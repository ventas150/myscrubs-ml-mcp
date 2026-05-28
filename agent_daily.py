"""
agent_daily.py — El agente que vive en el MCP.

Pipeline diaria:

  1. Snapshot mercado (top items por búsqueda principal)
  2. Snapshot propio (mis publicaciones + métricas)
  3. Snapshot financiero (margen por SKU, requiere BSale)
  4. Análisis SWOT
  5. Plan de acciones (autoaplicables + pendientes)
  6. Ejecución dentro de guardrails
  7. Reporte ejecutivo

Modos:
  - "report_only": no escribe nada, solo recomienda
  - "apply_safe": ejecuta lo que cumple guardrails, deja pendiente lo demás
  - "apply_all_with_human": ejecuta safe + emite cola de pendientes
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

import tools_read as TR
import tools_write as TW
from bsale_bridge import BSaleBridge
from ml_client import MLClient
from profit_engine import (
    ProfitInputs,
    calcular_margen,
    evaluar_decision_precio,
    precio_minimo_para_margen,
)

log = structlog.get_logger("agente_diario")

REPORT_DIR = Path.home() / ".myscrubs_ml" / "reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)


async def correr_agente(
    *,
    client: MLClient,
    bsale: BSaleBridge,
    user_id: int,
    site_id: str,
    config: dict,
    modo: str = "report_only",
) -> dict:
    t0 = time.monotonic()
    guardrails = config.get("agent", {}).get("guardrails", {})
    profit_cfg = config.get("profit", {})
    ml_cfg = config.get("ml", {})

    report: dict[str, Any] = {
        "started_at": datetime.utcnow().isoformat(),
        "modo": modo,
        "passos": {},
        "decisiones": [],
        "ejecutadas": [],
        "pendientes_aprobacion": [],
        "alertas": [],
        "kpi": {},
    }

    # ----- Paso 1: snapshot mercado -----
    log.info("step.market_snapshot")
    market: dict[str, list[dict]] = {}
    for term in ml_cfg.get("main_search_terms", []):
        try:
            top = await TR.competencia_top_n(
                client, site_id, query=term, n=20,
                exclude_seller_ids=[user_id],
            )
            market[term] = top
        except Exception as e:
            report["alertas"].append(
                f"Snapshot mercado '{term}' falló: {e}"
            )
    report["passos"]["market_snapshot"] = {
        "search_terms": len(market),
        "total_items_observados": sum(len(v) for v in market.values()),
    }

    # ----- Paso 2: snapshot propio -----
    log.info("step.own_snapshot")
    mis_items = await TR.mis_publicaciones(
        client, user_id, status="active", limit=200
    )
    report["passos"]["own_snapshot"] = {"items_activos": len(mis_items)}

    # ----- Paso 3: snapshot financiero -----
    log.info("step.profit_snapshot")
    # Recolecta SKUs de mis items
    skus = list({(it.get("seller_custom_field") or it.get("id")) for it in mis_items})
    costs_map = await bsale.bulk_costs([s for s in skus if s])
    margenes: dict[str, dict] = {}
    margen_total_estimado = 0.0
    items_sin_costo: list[str] = []
    for it in mis_items:
        item_id = it["id"]
        sku = it.get("seller_custom_field") or item_id
        cost = costs_map.get(sku)
        if not cost:
            items_sin_costo.append(item_id)
            continue
        inp = ProfitInputs(
            sku=sku,
            pvp_clp=float(it.get("price", 0)),
            costo_neto_clp=cost.costo_neto_clp,
            listing_type_id=it.get("listing_type_id", "gold_pro"),
            cuotas_sin_interes=3,
            free_shipping=it.get("shipping", {}).get("free_shipping", False),
            envio_subsidio_clp=profit_cfg.get("envio_subsidio_estimado_clp", 1500),
            iva_pct=profit_cfg.get("iva_pct", 19),
            commission_table=profit_cfg.get(
                "ml_commission_pct_by_listing_type",
                {"gold_pro": 17.5, "gold_premium": 13},
            ),
            cuotas_cost_table={
                int(k): v for k, v in profit_cfg.get(
                    "cuotas_sin_interes_cost_pct", {"3": 6, "6": 12}
                ).items()
            },
        )
        try:
            bd = calcular_margen(inp)
            margenes[item_id] = bd.to_dict()
            margen_total_estimado += bd.margen_neto_clp * int(
                it.get("sold_quantity", 0) or 0
            )
        except Exception as e:
            report["alertas"].append(f"Margen falla item {item_id}: {e}")
    report["passos"]["profit_snapshot"] = {
        "items_con_margen": len(margenes),
        "items_sin_costo_bsale": len(items_sin_costo),
        "margen_acumulado_estimado_lifetime_clp": round(margen_total_estimado),
    }
    if items_sin_costo:
        report["alertas"].append(
            f"{len(items_sin_costo)} items sin costo en BSale (mapping faltante)"
        )

    # ----- Paso 4: análisis competitivo + decisiones -----
    log.info("step.swot")
    # Construye mapa item_id -> posición observada en el mercado para cada query
    # (heurística: el menor índice donde aparece un competidor cercano)
    decisiones: list[dict] = []
    for it in mis_items:
        item_id = it["id"]
        if item_id not in margenes:
            continue
        pvp = float(it.get("price", 0))
        margen_data = margenes[item_id]
        # Posición proxy: cuántos items con precio MENOR existen en su query principal
        posicion = _estimar_posicion(it, market)
        from profit_engine import ProfitBreakdown
        bd = ProfitBreakdown(**{k: v for k, v in margen_data.items() if k != "notes"})
        bd.notes = margen_data.get("notes", [])
        dec = evaluar_decision_precio(
            bd,
            posicion_ranking=posicion,
            margen_minimo_pct=profit_cfg.get("target_min_margin_pct", 20),
            margen_ideal_pct=profit_cfg.get("target_ideal_margin_pct", 30),
        )
        dec.update({
            "item_id": item_id,
            "sku": it.get("seller_custom_field") or item_id,
            "title": it.get("title"),
            "pvp_actual": pvp,
            "posicion_estimada": posicion,
            "margen_actual_pct": margen_data["margen_pct"],
        })
        decisiones.append(dec)

    report["decisiones"] = decisiones

    # ----- Paso 5: planificar ejecución según modo -----
    max_change_pct = guardrails.get("max_price_change_pct", 10)
    max_changes_day = guardrails.get("max_changes_per_day", 20)
    auto_questions = guardrails.get("auto_respond_questions", True)
    pause_threshold_days = guardrails.get("pause_threshold_days", 7)

    changes_count = 0
    for dec in decisiones:
        if changes_count >= max_changes_day:
            break
        if dec["decision"] == "PAUSAR_O_SUBIR":
            # No pausa auto, solo recomienda — pausa requiere historia 7d
            report["pendientes_aprobacion"].append({
                "tipo": "pausar_publicacion",
                "item_id": dec["item_id"],
                "razon": dec["razon"],
            })
            continue
        if dec["decision"] in ("BAJAR_PRECIO", "MANTENER_O_SUBIR"):
            if dec.get("nuevo_precio_sugerido") is None:
                continue
            cambio_pct = abs(
                dec["nuevo_precio_sugerido"] - dec["pvp_actual"]
            ) / dec["pvp_actual"] * 100
            accion = {
                "tipo": "actualizar_precio",
                "item_id": dec["item_id"],
                "precio_actual": dec["pvp_actual"],
                "precio_nuevo": round(dec["nuevo_precio_sugerido"] / 10) * 10,
                "razon": dec["razon"],
                "delta_pct": round(cambio_pct, 2),
            }
            if cambio_pct > max_change_pct:
                report["pendientes_aprobacion"].append(accion)
                continue
            if modo == "report_only":
                report["pendientes_aprobacion"].append(accion)
                continue
            # apply_safe / apply_all_with_human → ejecuta
            res = await TW.actualizar_precio(
                client,
                item_id=dec["item_id"],
                nuevo_precio=accion["precio_nuevo"],
                motivo=f"agente_diario: {dec['razon']}",
                max_change_pct=max_change_pct,
                dry_run=False,
            )
            accion["resultado"] = res
            report["ejecutadas"].append(accion)
            changes_count += 1
        if dec["decision"] == "SUBIR_PRECIO":
            # Calcula precio mínimo para volver al margen objetivo
            # IMPORTANTE: usar el SKU real (seller_custom_field), no el item_id
            # porque costs_map se construyó con SKUs de BSale.
            sku = dec["sku"]
            cost = costs_map.get(sku)
            if not cost:
                continue
            inp = ProfitInputs(
                sku=sku, pvp_clp=dec["pvp_actual"],
                costo_neto_clp=cost.costo_neto_clp,
                listing_type_id="gold_pro", cuotas_sin_interes=3,
                free_shipping=True,
                envio_subsidio_clp=profit_cfg.get("envio_subsidio_estimado_clp", 1500),
                iva_pct=profit_cfg.get("iva_pct", 19),
                commission_table=profit_cfg.get(
                    "ml_commission_pct_by_listing_type", {"gold_pro": 17.5}
                ),
                cuotas_cost_table={
                    int(k): v for k, v in profit_cfg.get(
                        "cuotas_sin_interes_cost_pct", {"3": 6}
                    ).items()
                },
            )
            try:
                precio_min = precio_minimo_para_margen(
                    inp,
                    profit_cfg.get("target_min_margin_pct", 20),
                )
            except ValueError as e:
                report["alertas"].append(
                    f"item {sku}: imposible alcanzar margen — {e}"
                )
                continue
            cambio_pct = (precio_min - dec["pvp_actual"]) / dec["pvp_actual"] * 100
            accion = {
                "tipo": "actualizar_precio",
                "item_id": dec["item_id"],
                "precio_actual": dec["pvp_actual"],
                "precio_nuevo": round(precio_min / 10) * 10,
                "razon": (
                    f"{dec['razon']} → subir a precio mínimo para margen "
                    f"{profit_cfg.get('target_min_margin_pct', 20)}%"
                ),
                "delta_pct": round(cambio_pct, 2),
            }
            # Subidas son siempre conservadoras → ir a pendientes
            report["pendientes_aprobacion"].append(accion)

    # ----- Paso 6: responder preguntas pendientes (si auto) -----
    if auto_questions and modo != "report_only":
        preguntas = await TR.preguntas_pendientes(client, user_id, limit=20)
        for q in preguntas:
            template = _template_respuesta(q, mis_items)
            if not template:
                continue
            res = await TW.responder_pregunta(
                client, q["id"], template, dry_run=False
            )
            report["ejecutadas"].append({
                "tipo": "responder_pregunta",
                "question_id": q["id"],
                "respuesta": template,
                "resultado": res,
            })

    # ----- Paso 7: KPIs ejecutivos -----
    if margenes:
        all_margenes = list(margenes.values())
        report["kpi"] = {
            "items_analizados": len(all_margenes),
            "margen_promedio_pct": round(
                sum(m["margen_pct"] for m in all_margenes) / len(all_margenes), 2
            ),
            "items_no_rentables": sum(1 for m in all_margenes if not m["is_profitable"]),
            "items_top_margen": sorted(
                all_margenes, key=lambda x: x["margen_neto_clp"], reverse=True
            )[:5],
            "items_bottom_margen": sorted(
                all_margenes, key=lambda x: x["margen_neto_clp"]
            )[:5],
        }

    report["duracion_segundos"] = round(time.monotonic() - t0, 2)
    report["finished_at"] = datetime.utcnow().isoformat()

    # Persistir reporte
    report_file = REPORT_DIR / f"daily-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json"
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    log.info(
        "agent_done",
        decisiones=len(decisiones),
        ejecutadas=len(report["ejecutadas"]),
        pendientes=len(report["pendientes_aprobacion"]),
        alertas=len(report["alertas"]),
        report_path=str(report_file),
    )
    return report


# =========================================================================
# Helpers privados
# =========================================================================

def _estimar_posicion(my_item: dict, market: dict[str, list[dict]]) -> int:
    """
    Proxy de posición de ranking: cuenta cuántos items competidores tienen
    precio MENOR al mío para cada term donde el título de mi item matchea
    palabras claves. Si nunca aparece, devuelve 99.
    """
    title = (my_item.get("title") or "").lower()
    pvp = float(my_item.get("price", 0))
    min_rank = 99
    for term, items in market.items():
        if not any(w in title for w in term.lower().split()):
            continue
        cheaper = sum(
            1 for it in items
            if float(it.get("price", 0)) < pvp and it.get("id") != my_item["id"]
        )
        rank = cheaper + 1
        if rank < min_rank:
            min_rank = rank
    return min_rank


def _template_respuesta(question: dict, my_items: list[dict]) -> str:
    """
    Templates conservadores. Solo responde preguntas frecuentes y seguras.
    Para preguntas complejas devuelve "" → quedan pendientes para humano.
    """
    text = (question.get("text") or "").lower()
    item_id = question.get("item_id")
    item = next((i for i in my_items if i["id"] == item_id), None)

    # Pregunta común: ¿hay stock? ¿disponible?
    if any(k in text for k in ["stock", "disponib", "hay", "queda"]):
        if item and item.get("available_quantity", 0) > 0:
            qty = item["available_quantity"]
            return (
                f"¡Hola! Sí, tenemos {qty} unidades disponibles. "
                f"Si lo compras ahora, despachamos al día siguiente hábil. "
                f"Cualquier consulta sobre tallas, escríbenos. ¡Saludos!"
            )
        return ""

    # Pregunta común: tallas
    if any(k in text for k in ["talla", "tallas", "size"]):
        return (
            "¡Hola! Las tallas disponibles aparecen en las variantes de la "
            "publicación, al seleccionar el producto te muestra cuáles "
            "tenemos en stock. Si necesitas una talla específica que no "
            "veas, escríbenos y revisamos. ¡Saludos!"
        )

    # Pregunta común: tiempo entrega / despacho
    if any(k in text for k in ["envio", "despach", "llega", "demora", "cuanto"]):
        return (
            "¡Hola! Despachamos al día siguiente hábil después de tu compra. "
            "El tiempo de entrega depende de tu comuna y de MercadoEnvíos: "
            "RM 1-2 días hábiles, regiones 2-5 días hábiles. ¡Saludos!"
        )

    # Pregunta común: factura
    if any(k in text for k in ["factura", "boleta"]):
        return (
            "¡Hola! Emitimos boleta o factura electrónica. Si necesitas "
            "factura, escríbenos por mensaje interno con razón social, RUT "
            "y giro al momento de la compra. ¡Saludos!"
        )

    # Si no matchea, no responder automáticamente
    return ""
