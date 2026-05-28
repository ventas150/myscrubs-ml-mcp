"""
profit_engine.py — Cálculo de margen neto por SKU/publicación.

ESTA ES LA PIEZA CRÍTICA del sistema. El agente toma decisiones basadas
en GANANCIA, no en ventas brutas. Cualquier error aquí distorsiona toda
la estrategia.

Fórmula:
    PVP_neto = PVP / (1 + IVA)
    comision_ml = PVP * % comisión por tipo de listing
    envio = subsidio_envio_si_aplica
    costo_cuotas = PVP * % financiamiento si tiene cuotas sin interés
    margen_neto = PVP_neto - costo_producto - comision_ml - envio - costo_cuotas
    margen_pct  = margen_neto / PVP_neto * 100
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import structlog

log = structlog.get_logger()


@dataclass
class ProfitInputs:
    """Datos crudos necesarios para calcular margen."""
    sku: str
    pvp_clp: float
    costo_neto_clp: float  # desde BSale
    listing_type_id: str  # "gold_pro" | "gold_premium" | "gold_special" | "free"
    cuotas_sin_interes: Optional[int] = None  # 3, 6, 12 o None
    envio_subsidio_clp: float = 0.0
    free_shipping: bool = False
    price_threshold_free_shipping_clp: float = 9990  # Chile actual
    iva_pct: float = 19.0
    # Tabla de comisiones por listing type (override-able)
    commission_table: dict = field(
        default_factory=lambda: {
            "gold_pro": 17.5,
            "gold_premium": 13.0,
            "gold_special": 13.0,
            "free": 0.0,
        }
    )
    # Tabla de costo cuotas sin interés
    cuotas_cost_table: dict = field(
        default_factory=lambda: {3: 6.0, 6: 12.0, 12: 18.0}
    )


@dataclass
class ProfitBreakdown:
    """Output del cálculo, transparente para auditoría."""
    sku: str
    pvp_clp: float
    pvp_neto_clp: float
    costo_neto_clp: float
    comision_ml_clp: float
    comision_ml_pct: float
    envio_clp: float
    costo_cuotas_clp: float
    costo_cuotas_pct: float
    margen_neto_clp: float
    margen_pct: float
    is_profitable: bool
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "sku": self.sku,
            "pvp_clp": round(self.pvp_clp),
            "pvp_neto_clp": round(self.pvp_neto_clp),
            "costo_neto_clp": round(self.costo_neto_clp),
            "comision_ml_clp": round(self.comision_ml_clp),
            "comision_ml_pct": round(self.comision_ml_pct, 2),
            "envio_clp": round(self.envio_clp),
            "costo_cuotas_clp": round(self.costo_cuotas_clp),
            "costo_cuotas_pct": round(self.costo_cuotas_pct, 2),
            "margen_neto_clp": round(self.margen_neto_clp),
            "margen_pct": round(self.margen_pct, 2),
            "is_profitable": self.is_profitable,
            "notes": self.notes,
        }


def calcular_margen(inp: ProfitInputs) -> ProfitBreakdown:
    """Cálculo principal. Pure function, fácil de testear."""
    notes: list[str] = []

    if inp.pvp_clp <= 0:
        raise ValueError(f"PVP inválido para {inp.sku}: {inp.pvp_clp}")
    if inp.costo_neto_clp < 0:
        raise ValueError(f"Costo inválido para {inp.sku}: {inp.costo_neto_clp}")

    # 1) Quitar IVA
    pvp_neto = inp.pvp_clp / (1 + inp.iva_pct / 100)

    # 2) Comisión ML
    com_pct = inp.commission_table.get(inp.listing_type_id, 17.5)
    if inp.listing_type_id not in inp.commission_table:
        notes.append(
            f"listing_type_id desconocido '{inp.listing_type_id}', "
            f"usando default 17.5%"
        )
    comision_ml = inp.pvp_clp * com_pct / 100

    # 3) Envío: ML subsidia parte del costo si el ítem califica para envío gratis
    envio = 0.0
    if inp.free_shipping or inp.pvp_clp >= inp.price_threshold_free_shipping_clp:
        envio = inp.envio_subsidio_clp
        notes.append(
            f"envío gratis aplica, subsidio estimado ${envio:,.0f}"
        )

    # 4) Costo financiamiento cuotas sin interés
    cuotas_pct = 0.0
    costo_cuotas = 0.0
    if inp.cuotas_sin_interes and inp.cuotas_sin_interes > 1:
        cuotas_pct = inp.cuotas_cost_table.get(inp.cuotas_sin_interes, 0.0)
        if inp.cuotas_sin_interes not in inp.cuotas_cost_table:
            notes.append(
                f"cuotas {inp.cuotas_sin_interes} no en tabla, asumiendo 0%"
            )
        costo_cuotas = inp.pvp_clp * cuotas_pct / 100

    # 5) Margen
    margen_neto = (
        pvp_neto - inp.costo_neto_clp - comision_ml - envio - costo_cuotas
    )
    margen_pct = (margen_neto / pvp_neto * 100) if pvp_neto > 0 else 0.0

    return ProfitBreakdown(
        sku=inp.sku,
        pvp_clp=inp.pvp_clp,
        pvp_neto_clp=pvp_neto,
        costo_neto_clp=inp.costo_neto_clp,
        comision_ml_clp=comision_ml,
        comision_ml_pct=com_pct,
        envio_clp=envio,
        costo_cuotas_clp=costo_cuotas,
        costo_cuotas_pct=cuotas_pct,
        margen_neto_clp=margen_neto,
        margen_pct=margen_pct,
        is_profitable=margen_neto > 0,
        notes=notes,
    )


def precio_minimo_para_margen(
    inp: ProfitInputs, margen_objetivo_pct: float
) -> float:
    """
    Resuelve el PVP que garantiza margen_objetivo_pct dado todo lo demás.

    Algebra:
      margen_pct = (PVP_neto - costo - PVP*com_pct - envio - PVP*cuotas_pct) / PVP_neto
      Sea k = 1 / (1+IVA), PVP_neto = k*PVP
      m*k*PVP = k*PVP - costo - PVP*(com_pct+cuotas_pct) - envio
      PVP*(k - m*k - com_pct - cuotas_pct) = costo + envio
      PVP = (costo + envio) / (k*(1-m) - com_pct - cuotas_pct)

    Donde m = margen_objetivo_pct/100 (sobre PVP neto)

    NOTA: la decisión de envío es iterativa porque si el PVP calculado
    supera el umbral de envío gratis, hay que recalcular incluyendo
    el subsidio. Resolvemos en máximo 2 iteraciones para garantizar
    consistencia exacta con calcular_margen().
    """
    m = margen_objetivo_pct / 100
    k = 1 / (1 + inp.iva_pct / 100)
    com_pct = inp.commission_table.get(inp.listing_type_id, 17.5) / 100
    cuotas_pct = 0.0
    if inp.cuotas_sin_interes:
        cuotas_pct = inp.cuotas_cost_table.get(inp.cuotas_sin_interes, 0.0) / 100
    denom = k * (1 - m) - com_pct - cuotas_pct
    if denom <= 0:
        raise ValueError(
            f"Margen {margen_objetivo_pct}% inalcanzable con esta estructura "
            f"de costos (comisión {com_pct*100}% + cuotas {cuotas_pct*100}% "
            f"+ IVA {inp.iva_pct}% ya superan el target)"
        )

    def _con_envio(envio_val: float) -> float:
        return (inp.costo_neto_clp + envio_val) / denom

    # Iteración 1: asumir el envío según flag explícito o pvp_clp actual
    envio_aplica = inp.free_shipping or (
        inp.pvp_clp >= inp.price_threshold_free_shipping_clp
    )
    envio = inp.envio_subsidio_clp if envio_aplica else 0.0
    precio = _con_envio(envio)

    # Iteración 2: si el precio resultante cruza el umbral en sentido
    # contrario al supuesto inicial, recalcular para mantener simetría
    # exacta con calcular_margen()
    cruza_umbral_alza = (
        not envio_aplica
        and precio >= inp.price_threshold_free_shipping_clp
    )
    cruza_umbral_baja = (
        envio_aplica
        and not inp.free_shipping
        and precio < inp.price_threshold_free_shipping_clp
    )
    if cruza_umbral_alza:
        precio = _con_envio(inp.envio_subsidio_clp)
    elif cruza_umbral_baja:
        precio = _con_envio(0.0)
    return precio


def evaluar_decision_precio(
    breakdown: ProfitBreakdown,
    posicion_ranking: int,
    margen_minimo_pct: float = 20,
    margen_ideal_pct: float = 30,
) -> dict:
    """
    Heurística que el agente usa para decidir qué hacer con el precio.

    Returns dict con: decision, accion_sugerida, razon, nuevo_precio_sugerido
    """
    if not breakdown.is_profitable:
        return {
            "decision": "PAUSAR_O_SUBIR",
            "razon": (
                f"Margen NEGATIVO ${breakdown.margen_neto_clp:,.0f}. "
                "Pierdes plata en cada venta. Subir precio urgente o pausar."
            ),
            "nuevo_precio_sugerido": None,
        }
    if breakdown.margen_pct < margen_minimo_pct:
        return {
            "decision": "SUBIR_PRECIO",
            "razon": (
                f"Margen {breakdown.margen_pct:.1f}% bajo el mínimo "
                f"{margen_minimo_pct}%."
            ),
            "nuevo_precio_sugerido": None,  # calcular afuera con precio_minimo_para_margen
        }
    if (
        breakdown.margen_pct >= margen_ideal_pct
        and 1 <= posicion_ranking <= 3
    ):
        return {
            "decision": "MANTENER_O_SUBIR",
            "razon": (
                f"Top {posicion_ranking} con margen ideal "
                f"{breakdown.margen_pct:.1f}%. Probar +2-5% para "
                f"capturar más margen."
            ),
            "nuevo_precio_sugerido": breakdown.pvp_clp * 1.03,
        }
    if (
        breakdown.margen_pct >= margen_minimo_pct + 5
        and posicion_ranking > 10
    ):
        return {
            "decision": "BAJAR_PRECIO",
            "razon": (
                f"Margen {breakdown.margen_pct:.1f}% con holgura, pero "
                f"posición #{posicion_ranking}. Bajar precio para ganar "
                f"visibilidad sin sacrificar mínimo."
            ),
            "nuevo_precio_sugerido": breakdown.pvp_clp * 0.95,
        }
    return {
        "decision": "MANTENER",
        "razon": (
            f"Margen {breakdown.margen_pct:.1f}% saludable en posición "
            f"#{posicion_ranking}. Sin acción."
        ),
        "nuevo_precio_sugerido": None,
    }
