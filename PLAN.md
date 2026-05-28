# MyScrubs MercadoLibre MCP — Plan Arquitectónico

> **Objetivo**: Dominar la categoría "Uniformes Clínicos" en MercadoLibre Chile, maximizando **ganancia neta** (no ventas brutas) mediante un agente que vive dentro del MCP.

---

## 1. Misión del sistema

| Pilar | Definición |
|---|---|
| **Norte estrella** | Margen neto mensual por SKU > target, no Volumen de Venta |
| **Métrica de éxito** | Posición #1 en búsquedas top de la categoría + margen neto > 25% por SKU |
| **Modo de operación** | Agente programado diario + on-demand desde Cowork |
| **Decisiones automáticas** | Repricing dentro de banda, respuesta a preguntas, pausa de SKUs no rentables |
| **Decisiones que requieren aprobación** | Cambios de precio > ±10%, nuevas publicaciones, eliminación de SKUs |

---

## 2. Arquitectura de capas

```
Cowork (Claude) ←─── usuario consulta, aprueba, revisa reportes
        │  stdio MCP
        ▼
MyScrubs ML MCP Server (Python / FastMCP)
├── Read Tools       (mercado, competencia, métricas, preguntas)
├── Write Tools      (publicaciones, precios, stock, Q&A, post-venta)
├── Profit Engine    (margen por SKU: BSale + comisión ML + envío + cuotas)
├── ML API Client    (OAuth, retry, rate-limit)
└── BSale Bridge     (costos por SKU desde tu sistema actual)
        │  HTTPS
        ▼
api.mercadolibre.com

Agente Diario (cron / scheduled-task de Cowork)
06:00 → snapshot competencia + métricas propias
06:30 → recalcula margen por SKU
07:00 → genera plan: repricing, pausas, oportunidades
07:15 → ejecuta acciones automáticas (dentro de guardrails)
07:30 → publica reporte en Notion + email
Cada 2h → barre preguntas pendientes y las responde
```

---

## 3. Componentes principales

### 3.1 Auth (OAuth 2.0)
- ML usa OAuth 2.0 con access_token de 6h + refresh_token de 6 meses.
- Almacenamiento cifrado en `~/.myscrubs_ml/tokens.json` (chmod 600).
- Refresh automático cuando faltan menos de 30 min para expirar.
- Sitio: **MLC** (Chile).

### 3.2 Profit Engine — el corazón del sistema

Cada SKU tiene una ficha unificada con esta lógica:

```
SKU: SCR-MUJER-M-AZUL-01
├── Costo neto BSale: $8.500 CLP
├── PVP MercadoLibre:  $24.990 CLP
├── Cálculo margen:
│   PVP neto (sin IVA):       $24.990 / 1.19 = $20.999
│   - Costo neto:                              -$8.500
│   - Comisión ML Clásica (13%): $24.990×0.13 = -$3.249
│   - Envío Flex subsidiado*:                  -$1.500
│   - Costo cuotas s/int (3x, 6%): $24.990×0.06 = -$1.499
│   ─────────────────────────────────────────────────
│   = Margen neto: $6.251  (29.7% sobre PVP neto)
```

(*) El subsidio de envío depende del umbral de "envío gratis" (en MLC suele activarse sobre $9.990 con costos variables según peso/volumen).

### 3.3 Read Tools — instrumentos del agente

| Tool | Función | Frecuencia |
|---|---|---|
| `ml_buscar_categoria` | Top N items en categoría con filtros | Diaria |
| `ml_competencia_top_n` | Tracking de competidores específicos | Diaria |
| `ml_evolucion_precios` | Histórico precios competencia | Diaria |
| `ml_market_share` | Estimación participación por seller | Semanal |
| `ml_mis_publicaciones` | Lista todas mis publicaciones activas | Diaria |
| `ml_visitas_metricas` | Visitas, preguntas, conversión por listing | Diaria |
| `ml_health_publicacion` | Health, reputación, mora del listing | Diaria |
| `ml_preguntas_pendientes` | Preguntas sin responder | Cada 30 min |
| `ml_ordenes_recientes` | Órdenes últimos N días con detalle financiero | Diaria |
| `ml_reclamos_abiertos` | Reclamos / mediaciones abiertas | Diaria |
| `ml_calculadora_envio` | Costo de envío estimado por item | On-demand |

### 3.4 Write Tools — acciones del agente

| Tool | Función | Requiere aprobación |
|---|---|---|
| `ml_crear_publicacion` | Crea nueva publicación | Sí |
| `ml_actualizar_precio` | Cambia precio (con cálculo margen previo) | Solo si ∆ > ±10% |
| `ml_actualizar_stock` | Sincroniza stock con BSale | No (automático) |
| `ml_actualizar_titulo` | Optimiza título para SEO ML | Sí |
| `ml_actualizar_fotos` | Reordena/agrega fotos | Sí |
| `ml_pausar_publicacion` | Pausa SKU no rentable | Solo si margen<0 por 7 días |
| `ml_relistar_publicacion` | Re-publica pausada | Sí |
| `ml_responder_pregunta` | Responde pregunta con plantilla/IA | No (automático con voz brand) |
| `ml_responder_mensaje` | Responde mensaje post-venta | No (automático) |
| `ml_gestionar_reclamo` | Inicia flujo de reclamo | Sí |
| `ml_aplicar_promocion` | Aplica promoción de seller | Sí |

### 3.5 Agente diario — el cerebro

Pipeline en cada corrida:

1. **Snapshot mercado** → fotografía categoría MLC1430 (Uniformes médicos) top 100 items
2. **Snapshot propio** → mis publicaciones + métricas últimas 24h
3. **Snapshot financiero** → recalcula margen por SKU con precios actuales
4. **Análisis SWOT**:
   - SKUs en posición #1-3 con margen >30%: mantener, considerar subir precio 2-5%
   - SKUs en posición #1-3 con margen <15%: ajustar (subir precio o renegociar costo BSale)
   - SKUs en posición #10+ con margen >20%: bajar precio dentro de banda hasta entrar al top 5
   - SKUs con margen negativo: pausar y reportar
   - Gaps de mercado (búsquedas sin oferta competitiva): proponer nuevas publicaciones
5. **Ejecución**:
   - Acciones automáticas (dentro de guardrails)
   - Acciones que requieren aprobación: cola pendiente en Notion + notif
6. **Reporte**:
   - Dashboard Notion: margen total día, top 5 SKUs por ganancia, top 3 alertas
   - Email diario a Roberto con resumen ejecutivo

### 3.6 Guardrails del agente

| Acción | Límite | Por qué |
|---|---|---|
| Cambio precio sin aprobación | ±10% | Evita carrera al fondo o quemar margen |
| Cambios totales por día | máx 20 SKUs | Detectar drift, mantener estabilidad |
| Mismo SKU | máx 1 cambio cada 24h | Penalización ML por volatilidad |
| Pausar SKU | margen < 0 por 7 días consecutivos | Evitar pausas prematuras |
| Auto-responder preguntas | Solo con templates aprobados | Voz consistente |
| Stock | Sync automático cada 4h con BSale | Sobreventa = reputación |

---

## 4. Seguridad

| Item | Storage | Cifrado |
|---|---|---|
| client_id, client_secret ML | `~/.myscrubs_ml/credentials.json` | chmod 600 |
| access_token, refresh_token | `~/.myscrubs_ml/tokens.json` | chmod 600 + Fernet opcional |
| Costos BSale | Cache local 24h, refresh diario | No (no PII) |
| Logs de cambios | `~/.myscrubs_ml/audit.log` | Append-only, rotación 30d |

---

## 5. Roadmap

### Fase 1 — MVP (esta sesión)
- Plan arquitectónico (este documento)
- Guía de setup app ML Developers
- Scaffold FastMCP + OAuth + refresh automático
- Profit Engine (cálculo margen)
- Read tools básicos
- Write tools básicos
- Agente diario v1
- Plugin empaquetado para Cowork

### Fase 2 — Semana 1 post-instalación
- Sincronización full con BSale (todos los SKUs mapeados)
- Templates de respuesta a preguntas afinados
- Dashboard live en Cowork artifact
- Calibración guardrails con datos reales

### Fase 3 — Mes 1
- "Intelligent listing": copy generado por LLM con A/B testing
- Optimización de fotos con IA (background removal, lifestyle generation)
- Modelo de elasticidad de precio por categoría
- Anticipación de quiebres cruzando con `bsale_quiebres_proyectados`

### Fase 4 — Mes 3
- Expansión a categorías adyacentes (dental, veterinaria)
- Cross-channel: mismo catálogo en Falabella/Paris/Ripley
- Loyalty: campañas a compradores repetidos

---

## 6. Stack técnico

| Capa | Tecnología | Razón |
|---|---|---|
| Lenguaje | Python 3.11+ | Fluido para APIs, integra con pandas |
| MCP Framework | FastMCP | Standard, simple, async-first |
| HTTP client | httpx (async) | Async + retries + timeouts |
| Validación | Pydantic v2 | Schemas tipados |
| Storage tokens | JSON cifrado con cryptography.Fernet | Sin DB |
| Scheduler | scheduled-tasks Cowork + opción cron Linux | Doble vía |
| Logging | structlog | JSON estructurado |
| Tests | pytest + pytest-httpx | Mocks HTTP confiables |

---

## 7. KPIs del agente (orden de prioridad)

1. **Margen neto mensual total** — la métrica norte
2. **Margen neto promedio por SKU** — eficiencia por unidad
3. **% SKUs en top 10 de su búsqueda principal** — visibilidad
4. **Tiempo promedio respuesta a preguntas** — < 30 min objetivo
5. **Reputación ML** — verde "MercadoLider" sostenido

> Volumen de ventas es una métrica secundaria, **no un objetivo**.
