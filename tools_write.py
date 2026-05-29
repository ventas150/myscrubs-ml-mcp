"""
tools_write.py — Tools de escritura del MCP MyScrubs ML.

REGLAS DE ORO:
1. Toda mutación se loggea en audit.log (append-only).
2. Las que cambian precio/título piden dry_run=True por default.
3. Las que crean publicaciones SIEMPRE requieren confirmación humana
   (validate=False es excepcional, debe pasarse explícitamente).
4. Cada función devuelve un dict con `applied`, `would_apply`, `diff`
   para que el agente y el humano puedan razonar antes de confirmar.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import structlog

from ml_client import MLClient

log = structlog.get_logger()


def _resolve_audit_log_path() -> Path:
    """Resuelve la ruta del audit.log preferiendo disco persistente.

    Orden de prioridad:
    1. env MYSCRUBS_AUDIT_LOG (override explícito)
    2. env MYSCRUBS_DATA_DIR / audit.log (disco persistente en Render)
    3. ~/.myscrubs_ml/audit.log (default local)
    """
    explicit = os.environ.get("MYSCRUBS_AUDIT_LOG")
    if explicit:
        return Path(explicit)
    data_dir = os.environ.get("MYSCRUBS_DATA_DIR")
    if data_dir:
        return Path(data_dir) / "audit.log"
    return Path.home() / ".myscrubs_ml" / "audit.log"


AUDIT_LOG = _resolve_audit_log_path()
AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)


def _audit(action: str, item_id: Optional[str], payload: dict, applied: bool):
    entry = {
        "ts": datetime.utcnow().isoformat(),
        "action": action,
        "item_id": item_id,
        "applied": applied,
        "payload": payload,
    }
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# =========================================================================
# 1. PUBLICACIONES — CREAR
# =========================================================================

async def crear_publicacion(
    client: MLClient,
    item_payload: dict,
    dry_run: bool = True,
) -> dict:
    """
    Crea una nueva publicación en ML.

    item_payload debe seguir el schema oficial ML:
      {
        "title": "...", "category_id": "MLC...", "price": 24990,
        "currency_id": "CLP", "available_quantity": 10,
        "buying_mode": "buy_it_now", "condition": "new",
        "listing_type_id": "gold_pro", "pictures": [{"source": "..."}],
        "attributes": [...], "shipping": {...}, "sale_terms": [...]
      }

    Si dry_run=True (default) usa /items/validate para chequear sin crear.
    """
    if dry_run:
        result = await client.post("/items/validate", json=item_payload)
        _audit("crear_publicacion.validate", None, item_payload, applied=False)
        return {
            "applied": False,
            "would_apply": True,
            "validation": result,
            "next_step": "Llamar de nuevo con dry_run=False para crear.",
        }
    result = await client.post("/items", json=item_payload)
    _audit("crear_publicacion", result.get("id"), item_payload, applied=True)
    return {
        "applied": True,
        "item_id": result.get("id"),
        "permalink": result.get("permalink"),
        "result": result,
    }


# =========================================================================
# 2. PUBLICACIONES — ACTUALIZAR
# =========================================================================

async def actualizar_precio(
    client: MLClient,
    item_id: str,
    nuevo_precio: float,
    motivo: str,
    max_change_pct: float = 10.0,
    dry_run: bool = True,
    variation_prices: Optional[dict[int, float]] = None,
) -> dict:
    """
    Cambia el precio de una publicación con guardrail de % máximo.

    motivo: razón legible (ej. "agente diario: posición #15, margen 23%, bajar 5%")
    max_change_pct: si el cambio excede este %, requiere confirmación explícita
    variation_prices: opcional, dict {variation_id: precio}. Si se omite y el item
                     tiene variantes, aplica `nuevo_precio` uniformemente a todas.

    Importante (MLChile):
    - Si el item tiene `variations`, NO se puede mandar `{"price": X}` a nivel
      parent — devuelve 400. Hay que mandar `{"variations": [{"id": vid,
      "price": X}, ...]}` con TODAS las variantes activas.
    - Si el item no tiene variantes, se manda `{"price": X}` simple.
    - El precio debe ser número entero CLP (sin decimales en mercado chileno).
    """
    current = await client.get(f"/items/{item_id}")
    precio_actual = current.get("price")
    variations = current.get("variations") or []
    if not precio_actual and not variations:
        return {"applied": False, "error": f"Item {item_id} sin precio actual"}

    # Si tiene variantes, el precio "actual" para el delta es el de la primera variante
    ref_precio = (
        precio_actual
        if precio_actual
        else (variations[0].get("price") if variations else 0)
    )
    change_pct = abs(nuevo_precio - ref_precio) / ref_precio * 100 if ref_precio else 0
    diff = {
        "item_id": item_id,
        "precio_antes": ref_precio,
        "precio_despues": nuevo_precio,
        "delta_clp": nuevo_precio - ref_precio,
        "delta_pct": round(change_pct, 2),
        "motivo": motivo,
        "tiene_variations": len(variations) > 0,
        "num_variations": len(variations),
    }
    if change_pct > max_change_pct and dry_run:
        _audit("actualizar_precio.guardrail", item_id, diff, applied=False)
        return {
            "applied": False,
            "would_apply": False,
            "diff": diff,
            "blocked_by": (
                f"Cambio {change_pct:.1f}% supera el guardrail {max_change_pct}%. "
                f"Pasa dry_run=False y max_change_pct={change_pct+1} para forzar."
            ),
        }

    # Construir el payload según si el item tiene variantes o no
    nuevo_precio_int = int(round(nuevo_precio))  # ML Chile usa CLP entero
    if variations:
        # Permitir override per-variation si vino especificado, sino aplicar uniforme
        per_v = variation_prices or {}
        payload = {
            "variations": [
                {
                    "id": v["id"],
                    "price": int(round(per_v.get(v["id"], nuevo_precio_int))),
                }
                for v in variations
            ]
        }
    else:
        payload = {"price": nuevo_precio_int}

    if dry_run:
        diff["payload_a_enviar"] = payload
        _audit("actualizar_precio.dry_run", item_id, diff, applied=False)
        return {"applied": False, "would_apply": True, "diff": diff, "payload": payload}

    try:
        result = await client.put(f"/items/{item_id}", json=payload)
    except Exception as e:
        err_str = str(e)[:400]
        _audit(
            "actualizar_precio.failed",
            item_id,
            {**diff, "payload": payload, "error": err_str},
            applied=False,
        )
        return {
            "applied": False,
            "diff": diff,
            "error": err_str,
            "payload_intentado": payload,
            "hint": (
                "Si error 400 menciona 'variations', revisa que TODAS las variantes "
                "activas estén incluidas en el payload."
            ),
        }
    _audit("actualizar_precio", item_id, {**diff, "payload": payload}, applied=True)
    return {
        "applied": True,
        "diff": diff,
        "result_id": result.get("id") if isinstance(result, dict) else None,
        "payload_used": payload,
    }


async def actualizar_stock(
    client: MLClient,
    item_id: str,
    nuevo_stock: int,
    dry_run: bool = False,
    variation_stocks: Optional[dict[int, int]] = None,
) -> dict:
    """
    Sync de stock. Por design corre AUTOMÁTICO sin dry_run (sync con BSale).

    variation_stocks: opcional, dict {variation_id: stock}. Si se omite y el item
                     tiene variantes, aplica `nuevo_stock` uniformemente.

    ML rechaza `{"available_quantity": X}` a nivel parent cuando el item tiene
    variants — devuelve 400. Hay que usar `{"variations": [{"id": vid,
    "available_quantity": X}, ...]}`.
    """
    current = await client.get(f"/items/{item_id}")
    stock_actual = current.get("available_quantity")
    variations = current.get("variations") or []
    diff = {
        "item_id": item_id,
        "stock_antes": stock_actual,
        "stock_despues": nuevo_stock,
        "delta": nuevo_stock - (stock_actual or 0),
        "tiene_variations": len(variations) > 0,
        "num_variations": len(variations),
    }

    nuevo_stock_int = int(nuevo_stock)
    if variations:
        per_v = variation_stocks or {}
        payload = {
            "variations": [
                {
                    "id": v["id"],
                    "available_quantity": int(
                        per_v.get(v["id"], nuevo_stock_int)
                    ),
                }
                for v in variations
            ]
        }
    else:
        payload = {"available_quantity": nuevo_stock_int}

    if dry_run:
        diff["payload_a_enviar"] = payload
        _audit("actualizar_stock.dry_run", item_id, diff, applied=False)
        return {"applied": False, "would_apply": True, "diff": diff, "payload": payload}

    try:
        result = await client.put(f"/items/{item_id}", json=payload)
    except Exception as e:
        err_str = str(e)[:400]
        _audit(
            "actualizar_stock.failed",
            item_id,
            {**diff, "payload": payload, "error": err_str},
            applied=False,
        )
        return {
            "applied": False,
            "diff": diff,
            "error": err_str,
            "payload_intentado": payload,
        }
    _audit("actualizar_stock", item_id, {**diff, "payload": payload}, applied=True)
    return {
        "applied": True,
        "diff": diff,
        "result_id": result.get("id") if isinstance(result, dict) else None,
        "payload_used": payload,
    }


async def actualizar_titulo_descripcion(
    client: MLClient,
    item_id: str,
    nuevo_titulo: Optional[str] = None,
    nueva_descripcion: Optional[str] = None,
    dry_run: bool = True,
) -> dict:
    """Cambios SEO sensibles, siempre con dry_run por default."""
    payload: dict[str, Any] = {}
    if nuevo_titulo:
        payload["title"] = nuevo_titulo[:60]  # ML max 60
    if dry_run:
        _audit("actualizar_titulo.dry_run", item_id, payload, applied=False)
        return {"applied": False, "would_apply": True, "diff": payload}
    if payload:
        await client.put(f"/items/{item_id}", json=payload)
    if nueva_descripcion is not None:
        await client.put(
            f"/items/{item_id}/description",
            json={"plain_text": nueva_descripcion},
        )
    _audit(
        "actualizar_titulo",
        item_id,
        {"titulo": bool(nuevo_titulo), "desc": bool(nueva_descripcion)},
        applied=True,
    )
    return {"applied": True, "diff": payload}


async def pausar_publicacion(
    client: MLClient, item_id: str, motivo: str, dry_run: bool = True
) -> dict:
    payload = {"status": "paused"}
    if dry_run:
        _audit("pausar.dry_run", item_id, {"motivo": motivo}, applied=False)
        return {"applied": False, "would_apply": True, "motivo": motivo}
    await client.put(f"/items/{item_id}", json=payload)
    _audit("pausar", item_id, {"motivo": motivo}, applied=True)
    return {"applied": True, "motivo": motivo}


async def relistar_publicacion(
    client: MLClient, item_id: str, dry_run: bool = True
) -> dict:
    payload = {"status": "active"}
    if dry_run:
        _audit("relistar.dry_run", item_id, {}, applied=False)
        return {"applied": False, "would_apply": True}
    await client.put(f"/items/{item_id}", json=payload)
    _audit("relistar", item_id, {}, applied=True)
    return {"applied": True}


# =========================================================================
# 3. PREGUNTAS Y MENSAJES
# =========================================================================

async def responder_pregunta(
    client: MLClient,
    question_id: int,
    texto: str,
    dry_run: bool = False,
) -> dict:
    """
    Responde una pregunta. Por design corre automático (dry_run=False) cuando
    el agente usa templates aprobados. Para texto libre conviene dry_run=True.
    """
    payload = {"question_id": question_id, "text": texto[:2000]}
    if dry_run:
        _audit("responder_pregunta.dry_run", None, payload, applied=False)
        return {"applied": False, "would_apply": True, "texto": texto}
    result = await client.post("/answers", json=payload)
    _audit("responder_pregunta", None, payload, applied=True)
    return {"applied": True, "result": result}


async def responder_mensaje(
    client: MLClient,
    pack_id: str,
    user_id: int,
    texto: str,
    dry_run: bool = False,
) -> dict:
    """
    Mensaje post-venta. Endpoint /messages/packs/{pack_id}/sellers/{user_id}
    """
    payload = {"from": {"user_id": user_id}, "text": texto[:2000]}
    if dry_run:
        _audit(
            "responder_mensaje.dry_run", None,
            {"pack_id": pack_id, **payload}, applied=False,
        )
        return {"applied": False, "would_apply": True}
    result = await client.post(
        f"/messages/packs/{pack_id}/sellers/{user_id}",
        json=payload,
    )
    _audit(
        "responder_mensaje", None,
        {"pack_id": pack_id, **payload}, applied=True,
    )
    return {"applied": True, "result": result}


# =========================================================================
# 4. PROMOCIONES / RECLAMOS
# =========================================================================

async def aplicar_promocion_seller(
    client: MLClient,
    promotion_id: str,
    item_id: str,
    deal_price: float,
    dry_run: bool = True,
) -> dict:
    """
    Aplica un item a una promoción del seller.
    Endpoint: /seller-promotions/items/{item_id}
    """
    payload = {"deal_id": promotion_id, "deal_price": deal_price}
    if dry_run:
        _audit("promo.dry_run", item_id, payload, applied=False)
        return {"applied": False, "would_apply": True, "payload": payload}
    result = await client.post(
        f"/seller-promotions/items/{item_id}", json=payload
    )
    _audit("promo", item_id, payload, applied=True)
    return {"applied": True, "result": result}


# =========================================================================
# Resumen del audit log (útil para que el agente vea qué hizo hoy)
# =========================================================================

def resumen_acciones_hoy() -> dict:
    """Cuenta de acciones aplicadas hoy (UTC) agrupadas por tipo."""
    if not AUDIT_LOG.exists():
        return {"total": 0, "por_accion": {}, "audit_path": str(AUDIT_LOG)}
    today = datetime.utcnow().strftime("%Y-%m-%d")
    counts: dict[str, int] = {}
    total = 0
    with AUDIT_LOG.open(encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if entry.get("ts", "").startswith(today) and entry.get("applied"):
                counts[entry["action"]] = counts.get(entry["action"], 0) + 1
                total += 1
    return {"date": today, "total": total, "por_accion": counts, "audit_path": str(AUDIT_LOG)}


def acciones_periodo(
    days_back: int = 7,
    only_applied: bool = True,
    accion_filter: Optional[str] = None,
    item_id_filter: Optional[str] = None,
    limit: int = 200,
) -> dict:
    """
    Lee el audit log y devuelve las acciones dentro de un rango temporal.

    Args:
      days_back: cuántos días atrás mirar (default 7).
      only_applied: si True, sólo cuenta las que efectivamente se aplicaron.
                   Si False, también incluye las que quedaron en dry_run o
                   bloqueadas por guardrail.
      accion_filter: si se pasa, filtra por tipo exacto de acción
                     (ej. "actualizar_precio", "actualizar_stock").
      item_id_filter: si se pasa, filtra por SKU específico.
      limit: máximo de entradas a devolver (orden cronológico reverso).

    Devuelve:
      {
        "audit_path": "...",
        "rango": {"desde": "...", "hasta": "..."},
        "total_encontrados": N,
        "acciones": [
          {"ts":"...", "action":"actualizar_precio", "item_id":"MLC...",
           "applied":true, "diff":{precio_antes:..., precio_despues:...}}
        ],
        "por_accion": {"actualizar_precio": N, ...},
        "por_item": {"MLC123": M, ...}
      }
    """
    if not AUDIT_LOG.exists():
        return {
            "audit_path": str(AUDIT_LOG),
            "total_encontrados": 0,
            "acciones": [],
            "por_accion": {},
            "por_item": {},
            "warning": "Audit log no existe todavía. Acciones se loggean al primer cambio.",
        }

    cutoff = datetime.utcnow() - timedelta(days=max(1, int(days_back)))
    cutoff_iso = cutoff.isoformat()
    hasta_iso = datetime.utcnow().isoformat()
    matches: list[dict] = []
    por_accion: dict[str, int] = {}
    por_item: dict[str, int] = {}

    with AUDIT_LOG.open(encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except Exception:
                continue
            ts = entry.get("ts", "")
            if ts < cutoff_iso:
                continue
            if only_applied and not entry.get("applied"):
                continue
            action = entry.get("action") or ""
            item_id = entry.get("item_id") or ""
            if accion_filter and action != accion_filter:
                continue
            if item_id_filter and item_id != item_id_filter:
                continue
            matches.append(entry)
            por_accion[action] = por_accion.get(action, 0) + 1
            if item_id:
                por_item[item_id] = por_item.get(item_id, 0) + 1

    # Orden cronológico reverso (más reciente primero)
    matches.sort(key=lambda e: e.get("ts", ""), reverse=True)

    return {
        "audit_path": str(AUDIT_LOG),
        "rango": {"desde": cutoff_iso, "hasta": hasta_iso, "days_back": days_back},
        "filtros": {
            "only_applied": only_applied,
            "accion": accion_filter,
            "item_id": item_id_filter,
        },
        "total_encontrados": len(matches),
        "acciones": matches[: max(1, int(limit))],
        "por_accion": por_accion,
        "por_item": por_item,
    }


def cambios_precio_periodo(days_back: int = 7) -> dict:
    """
    Atajo: devuelve solo cambios de precio aplicados en los últimos N días
    con un formato compacto (SKU, antes, después, delta_pct, motivo).
    """
    raw = acciones_periodo(
        days_back=days_back,
        only_applied=True,
        accion_filter="actualizar_precio",
        limit=500,
    )
    compactos = []
    for entry in raw.get("acciones", []):
        payload = entry.get("payload") or {}
        diff = payload if "precio_antes" in payload else payload.get("diff") or payload
        compactos.append({
            "ts": entry.get("ts"),
            "sku": entry.get("item_id"),
            "precio_antes": diff.get("precio_antes"),
            "precio_despues": diff.get("precio_despues"),
            "delta_pct": diff.get("delta_pct"),
            "delta_clp": diff.get("delta_clp"),
            "motivo": diff.get("motivo"),
        })
    return {
        "audit_path": raw.get("audit_path"),
        "rango": raw.get("rango"),
        "total_cambios": len(compactos),
        "cambios": compactos,
    }
