# MyScrubs MercadoLibre MCP

> MCP completo (nivel Supermetrics) para MercadoLibre Chile, con un agente que vive adentro y trabaja para que MyScrubs sea **líder en margen** de la categoría Uniformes Clínicos.

---

## 🎯 Por qué este MCP

Los MCPs estándar de MercadoLibre te dan tools sueltas. Este es distinto: **es un sistema completo que decide y actúa**, optimizando **ganancia neta por SKU** (no ventas brutas), porque vender mucho con margen 5% es perder.

Combina:
- Lectura profunda del mercado y la competencia
- Escritura controlada sobre tus publicaciones, precios, stock, Q&A y post-venta
- Un motor financiero que calcula tu margen real (costo BSale + comisión ML + envío + cuotas + IVA)
- Un agente diario que ejecuta dentro de guardrails y te entrega un reporte

---

## 📦 Archivos del proyecto

| Archivo | Rol |
|---|---|
| `PLAN.md` | Arquitectura completa del sistema |
| `SETUP_ML_DEVELOPERS.md` | Cómo registrar la app en ML developers + OAuth |
| `config.example.json` | Template de configuración |
| `requirements.txt` | Dependencias Python |
| `ml_auth.py` | OAuth 2.0 + refresh automático |
| `ml_client.py` | HTTP client async con rate-limit + retries |
| `profit_engine.py` | **El corazón**: cálculo de margen por SKU |
| `bsale_bridge.py` | Puente con tu MCP de BSale para costos |
| `tools_read.py` | Tools de lectura (mercado, métricas, preguntas) |
| `tools_write.py` | Tools de escritura (precio, stock, publicaciones, Q&A) |
| `server.py` | Servidor FastMCP que une todo |
| `agent_daily.py` | El agente autónomo |
| `oauth_setup.py` | Script one-time para obtener primer refresh_token |
| `verify_setup.py` | Diagnóstico end-to-end |
| `plugin.json` | Manifest para instalar como plugin en Cowork |

---

## 🚀 Setup rápido

### 1. Registrá la app en ML Developers
Sigue `SETUP_ML_DEVELOPERS.md` paso a paso (~20 min).

### 2. Configurá credenciales
```bash
cp config.example.json config.json
# editar config.json con tu APP_ID, SECRET, user_id_seller
```

### 3. Instalá dependencias Python
```bash
pip install -r requirements.txt
```

### 4. Obtené el primer refresh_token
```bash
python oauth_setup.py
# abre el navegador, autoriza, listo
```

### 5. Verificá que todo funciona
```bash
python verify_setup.py
```

Output esperado:
```
✓ config.json cargado
✓ tokens.json encontrado
✓ access_token válido (APP_USR-1234...)
✓ Identidad ML: myscrubs_oficial (id=12345678)
  Site: MLC
  Reputación: 5_green
✓ 47 publicaciones activas
✓ Categoría principal: MLC1430 → Uniformes Médicos
ℹ BSale: configurado para usar MCP existente en Cowork
✓ Setup completo.
```

### 6. Corré el servidor MCP
```bash
python server.py
```

Quedará escuchando vía stdio para que Cowork lo conecte.

### 7. Instalá como plugin en Cowork
Empacá la carpeta como `myscrubs-ml.plugin` (zip con extensión cambiada) e instalá desde Cowork → Plugins.

### 8. Activá el agente diario
En Cowork:
> "Activa el agente diario de MyScrubs ML a las 6 AM hora Chile en modo apply_safe"

---

## 🧠 Cómo "vive" el agente en el MCP

El agente NO es un proceso separado. Es una **tool del MCP** (`ml_agente_corrida_diaria`) que se invoca:

1. **Por cron** vía scheduled-task de Cowork (recomendado)
2. **On-demand** desde el chat ("corre el agente ahora")
3. **Por evento** vía webhook ML (Fase 2)

Esto permite que:
- Puedas pausar/cambiar comportamiento sin redeploy
- El agente comparta estado (tokens, cache BSale, audit log) con todas las otras tools
- Cowork sea el "centro de control" donde tú apruebas, revisás y le pedís ajustes

### Pipeline diario

```
06:00 │ Snapshot mercado (top 20 items por cada búsqueda principal)
06:30 │ Snapshot propio (mis publicaciones + métricas últimas 24h)
06:45 │ Snapshot financiero (recalcula margen por SKU vía BSale)
07:00 │ Análisis SWOT por SKU
07:15 │ Plan: precio↑ / precio↓ / mantener / pausar / pendiente
07:20 │ Ejecuta autoaplicables (dentro de ±10%, max 20 cambios/día)
07:25 │ Responde preguntas pendientes con templates seguros
07:30 │ Reporte → audit log + JSON en ~/.myscrubs_ml/reports/
```

---

## 💰 Por qué el cálculo de margen es el KPI maestro

Ejemplo real: dos SKUs con misma venta mensual.

| | SKU A | SKU B |
|---|---|---|
| PVP | $24.990 | $24.990 |
| Costo BSale | $8.500 | $14.500 |
| Comisión ML 13% | -$3.249 | -$3.249 |
| Envío gratis subsidiado | -$1.500 | -$1.500 |
| Cuotas 3x sin interés (6%) | -$1.499 | -$1.499 |
| IVA fuera | -$3.991 | -$3.991 |
| **Margen** | **$6.251 (29.7%)** | **$251 (1.2%)** |

Vender 100 unidades del SKU A = **$625.100 de ganancia real**.
Vender 100 unidades del SKU B = **$25.100 de ganancia real**.

Un dashboard "tradicional" te diría que ambos venden igual. **Este agente te dice que B te está consumiendo capital de trabajo sin pagar nada**, y propone subir precio, renegociar costo con proveedor, o pausar.

---

## 🛡️ Guardrails del agente

Configurables en `config.json → agent.guardrails`:

| Guardrail | Default | Por qué |
|---|---|---|
| `max_price_change_pct` | 10 | Evita carrera al fondo y volatilidad penalizada por ML |
| `max_changes_per_day` | 20 | Limita daño si hay bug |
| `min_hours_between_changes_same_sku` | 24 | ML penaliza cambios frecuentes |
| `pause_threshold_days` | 7 | No pausa por margen negativo de 1 día (puede ser ruido) |
| `auto_respond_questions` | true | Templates conservadores, complejas → humano |

---

## 🛠️ Tools expuestas

### Lectura
- `ml_buscar_categoria(query, category_id, limit, sort)`
- `ml_competencia_top_n(query, n)` — excluye automáticamente a MyScrubs
- `ml_market_share(category_id)`
- `ml_estadisticas_categoria(query)` — mediana, P25, P75
- `ml_mis_publicaciones(status, limit)`
- `ml_metricas_visitas(item_id, days)`
- `ml_health_publicacion(item_id)`
- `ml_preguntas_pendientes(limit)`
- `ml_ordenes_recientes(days, limit)`
- `ml_reclamos_abiertos()`
- `ml_calcular_envio(item_id, zip_destino)`

### Profit Engine
- `ml_margen_sku(sku, pvp_clp, listing_type, cuotas, free_shipping)`
- `ml_precio_minimo_objetivo(sku, margen_objetivo_pct, ...)`
- `ml_decision_precio(sku, pvp_actual, posicion_ranking)`

### Escritura (todas con `dry_run` por default donde corresponde)
- `ml_crear_publicacion(item_payload, dry_run)`
- `ml_actualizar_precio(item_id, nuevo_precio, motivo, max_change_pct, dry_run)`
- `ml_actualizar_stock(item_id, nuevo_stock, dry_run)`
- `ml_actualizar_titulo(item_id, nuevo_titulo, nueva_descripcion, dry_run)`
- `ml_pausar_publicacion(item_id, motivo, dry_run)`
- `ml_relistar_publicacion(item_id, dry_run)`
- `ml_responder_pregunta(question_id, texto, dry_run)`
- `ml_responder_mensaje(pack_id, user_id_buyer, texto, dry_run)`

### Agente
- `ml_agente_corrida_diaria(modo)` — modo: `report_only` | `apply_safe` | `apply_all_with_human`
- `ml_acciones_hoy()` — qué hizo el MCP hoy

---

## 📈 Roadmap

Ver `PLAN.md` sección 5 — Fase 2 (semana 1), Fase 3 (mes 1), Fase 4 (mes 3).

---

## 🆘 Soporte

Logs en `~/.myscrubs_ml/audit.log` y `~/.myscrubs_ml/reports/`.
Si algo falla, primero `python verify_setup.py`.

---

## ⚖️ Notas legales y de uso

- El MCP respeta los rate-limits de ML (10 req/s default, configurable).
- Toda acción se loggea en append-only audit log con timestamp.
- Credenciales en chmod 600.
- Los cálculos de margen son **estimaciones** basadas en la tabla de comisiones declarada en `config.json`. Para cifras contables exactas, cruzar con tu liquidación mensual de ML.
