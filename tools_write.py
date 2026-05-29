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
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import structlog

from ml_client import MLClient

log = structlog.get_logger()

AUDIT_LOG = Path.home() / ".myscrubs_ml" / "audit.log"
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
    if not AUDIT_LOG.exists():
        return {"total": 0, "por_accion": {}}
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
    return {"date": today, "total": total, "por_accion": counts}
