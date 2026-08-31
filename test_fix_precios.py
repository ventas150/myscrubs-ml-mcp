"""
test_fix_precios.py — Regresion del incidente del 7-12 de agosto de 2026.

Cubre los dos bugs que hacian que el agente propusiera bajar el precio de
todo el catalogo:
  1. posicion desconocida devuelta como 99 (== "voy ultimo")
  2. costo 0 de BSale tomado como costo valido (margen inflado ~73%)

Correr: python test_fix_precios.py
"""
from agent_daily import _estimar_posicion
from profit_engine import ProfitInputs, calcular_margen, evaluar_decision_precio

fallos = []


def check(nombre, condicion, detalle=""):
    if condicion:
        print(f"  OK   {nombre}")
    else:
        print(f"  FALLA {nombre} {detalle}")
        fallos.append(nombre)


def inputs(costo, pvp=30690):
    return ProfitInputs(
        sku="TEST", pvp_clp=pvp, costo_neto_clp=costo,
        listing_type_id="gold_special", cuotas_sin_interes=3,
        free_shipping=True, envio_subsidio_clp=1500, iva_pct=19,
        commission_table={"gold_special": 13.0}, cuotas_cost_table={3: 6.0},
    )


print("\n1. Costo 0 (el caso real: 46 publicaciones sin SKU de BSale)")
bd = calcular_margen(inputs(0))
check("marca costo_confiable=False", bd.costo_confiable is False)
d = evaluar_decision_precio(bd, posicion_ranking=99, margen_minimo_pct=20,
                            margen_ideal_pct=30)
check("NO propone bajar precio", d["decision"] != "BAJAR_PRECIO", d["decision"])
check("decision es SIN_DATOS_COSTO", d["decision"] == "SIN_DATOS_COSTO")
check("sin precio sugerido", d["nuevo_precio_sugerido"] is None)

print("\n2. Costo real + posicion DESCONOCIDA (mercado con 403)")
bd = calcular_margen(inputs(9000))
check("costo_confiable=True", bd.costo_confiable is True)
check("margen realista <60%", bd.margen_pct < 60, f"{bd.margen_pct:.1f}%")
d = evaluar_decision_precio(bd, posicion_ranking=None, margen_minimo_pct=20,
                            margen_ideal_pct=30)
check("NO propone bajar precio", d["decision"] != "BAJAR_PRECIO", d["decision"])
check("sin precio sugerido", d["nuevo_precio_sugerido"] is None)

print("\n3. Costo real + posicion CONOCIDA mala (la heuristica debe seguir viva)")
d = evaluar_decision_precio(calcular_margen(inputs(6000)), posicion_ranking=25,
                            margen_minimo_pct=20, margen_ideal_pct=30)
check("SI propone bajar precio", d["decision"] == "BAJAR_PRECIO", d["decision"])
check("con precio sugerido", d["nuevo_precio_sugerido"] is not None)

print("\n4. Costo real + margen bajo el minimo (no depende de posicion)")
bd_bajo = calcular_margen(inputs(14000))  # margen positivo pero ~17%
check("margen entre 0 y 20%", 0 < bd_bajo.margen_pct < 20,
      f"{bd_bajo.margen_pct:.1f}%")
d = evaluar_decision_precio(bd_bajo, posicion_ranking=None,
                            margen_minimo_pct=20, margen_ideal_pct=30)
check("propone SUBIR_PRECIO", d["decision"] == "SUBIR_PRECIO", d["decision"])

print("\n5. Margen negativo (no depende de posicion)")
d = evaluar_decision_precio(calcular_margen(inputs(40000)), posicion_ranking=None,
                            margen_minimo_pct=20, margen_ideal_pct=30)
check("propone PAUSAR_O_SUBIR", d["decision"] == "PAUSAR_O_SUBIR", d["decision"])

print("\n6. _estimar_posicion")
item = {"id": "MLC1", "title": "Top Clinico Cherokee Revolution", "price": 30690}
check("mercado vacio -> None", _estimar_posicion(item, {}) is None)
check("terminos con listas vacias -> None",
      _estimar_posicion(item, {"top clinico": [], "scrub mujer": []}) is None)
check("titulo que no matchea -> None",
      _estimar_posicion(item, {"zapato": [{"id": "X", "price": 1000}]}) is None)
mercado = {"top clinico": [
    {"id": "A", "price": 20000}, {"id": "B", "price": 25000},
    {"id": "C", "price": 40000},
]}
check("con datos -> rank 3", _estimar_posicion(item, mercado) == 3,
      str(_estimar_posicion(item, mercado)))

print("\n7. Cache de BSale envenenado")
import json, tempfile, time
from pathlib import Path
tmp = Path(tempfile.mkdtemp()) / "cache.json"
tmp.write_text(json.dumps({
    "SKU_CERO": {"sku": "SKU_CERO", "costo_neto_clp": 0.0, "nombre": "x",
                 "categoria_bsale": None, "stock_total": 0,
                 "last_updated": time.time()},
    "SKU_FUTURO": {"sku": "SKU_FUTURO", "costo_neto_clp": 5000.0, "nombre": "y",
                   "categoria_bsale": None, "stock_total": 3,
                   "last_updated": time.time() + 999999},
    "SKU_BUENO": {"sku": "SKU_BUENO", "costo_neto_clp": 9000.0, "nombre": "z",
                  "categoria_bsale": None, "stock_total": 5,
                  "last_updated": time.time()},
}))
from bsale_bridge import BSaleBridge
b = BSaleBridge(cache_path=tmp, use_existing_cowork_mcp=False)
check("descarta la entrada con costo 0", "SKU_CERO" not in b._cache)
check("descarta el timestamp futuro", "SKU_FUTURO" not in b._cache)
check("conserva la entrada sana", "SKU_BUENO" in b._cache)

print()
if fallos:
    print(f"FALLARON {len(fallos)}: {fallos}")
    raise SystemExit(1)
print("TODOS LOS TESTS PASARON")
