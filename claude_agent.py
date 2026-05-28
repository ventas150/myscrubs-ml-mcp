"""
claude_agent.py — Agente Claude que vive en el MCP de MyScrubs ML.

Este script es lo que corre en el cron de Render. Usa la API de Anthropic
para razonar sobre el catálogo y la competencia, e invoca las tools del
MCP HTTP via tool_use loop.

Dos modos (env var AGENT_MODE):
  - "daily_full": pipeline completa de optimización (1× al día)
  - "questions_sweep": revisa preguntas pendientes cada 2h y las responde

Env vars requeridas:
  - ANTHROPIC_API_KEY
  - MCP_URL          ej. https://myscrubs-ml-mcp.onrender.com/mcp
  - MCP_AUTH_TOKEN   bearer token compartido con el MCP server
  - AGENT_MODE       "daily_full" | "questions_sweep"
  - AGENT_MODEL      ej. "claude-sonnet-4-6" (default)
  - REPORT_WEBHOOK   (opcional) URL para POST del reporte JSON
  - REPORT_EMAIL_TO  (opcional) email (usado solo si SMTP_* setteado)
  - SMTP_*           (opcional) credenciales SMTP para enviar reporte
"""
from __future__ import annotations

import asyncio
import json
import os
import smtplib
import sys
import time
from datetime import datetime
from email.mime.text import MIMEText
from typing import Any, Optional

import httpx
import structlog
from anthropic import AsyncAnthropic

structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger("claude_agent")


# =========================================================================
# Cliente MCP HTTP minimalista (JSON-RPC sobre Streamable HTTP)
# =========================================================================

class MCPHttpClient:
    """Cliente JSON-RPC para un servidor MCP via Streamable HTTP.

    Implementa lo mínimo: initialize, tools/list, tools/call. No usa SSE
    streaming porque para tool calls discretas no es necesario.
    """

    def __init__(self, url: str, auth_token: str, timeout: float = 120.0):
        self.url = url.rstrip("/")
        self._http = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {auth_token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        self._req_id = 0
        self._session_id: Optional[str] = None
        self._initialized = False

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def close(self) -> None:
        await self._http.aclose()

    async def _rpc(self, method: str, params: dict | None = None) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        headers = {}
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        r = await self._http.post(self.url, json=payload, headers=headers)
        # MCP puede devolver session-id en el header en la primera respuesta
        sid = r.headers.get("mcp-session-id")
        if sid and not self._session_id:
            self._session_id = sid
        if r.status_code >= 400:
            raise RuntimeError(f"MCP RPC {method} failed [{r.status_code}]: {r.text[:300]}")
        # Si es event-stream (SSE), parseamos el primer evento
        ctype = r.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            data = _parse_first_sse_event(r.text)
        else:
            data = r.json()
        if isinstance(data, dict) and "error" in data:
            raise RuntimeError(f"MCP RPC error: {data['error']}")
        return data.get("result") if isinstance(data, dict) else data

    async def initialize(self) -> dict:
        result = await self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "myscrubs-claude-agent", "version": "1.0.0"},
        })
        # Enviar notification "initialized" (sin id, sin esperar respuesta)
        await self._http.post(
            self.url,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={"mcp-session-id": self._session_id} if self._session_id else {},
        )
        self._initialized = True
        return result

    async def list_tools(self) -> list[dict]:
        if not self._initialized:
            await self.initialize()
        result = await self._rpc("tools/list")
        return result.get("tools", []) if isinstance(result, dict) else []

    async def call_tool(self, name: str, arguments: dict) -> dict:
        if not self._initialized:
            await self.initialize()
        return await self._rpc("tools/call", {
            "name": name, "arguments": arguments,
        })


def _parse_first_sse_event(text: str) -> dict:
    """Parsea un SSE chunk y devuelve el primer `data:` como JSON."""
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload:
                return json.loads(payload)
    return {}


# =========================================================================
# Conversión tools MCP → schema Anthropic
# =========================================================================

def mcp_tools_to_anthropic(tools: list[dict]) -> list[dict]:
    """Convierte el formato MCP tool al formato Anthropic Tools API."""
    out = []
    for t in tools:
        out.append({
            "name": t["name"],
            "description": t.get("description", ""),
            "input_schema": t.get("inputSchema", {"type": "object", "properties": {}}),
        })
    return out


# =========================================================================
# Prompts del agente
# =========================================================================

SYSTEM_PROMPT_DAILY = """Eres el agente autónomo de MyScrubs, una marca chilena de uniformes clínicos que vende en MercadoLibre Chile. Tu misión es MAXIMIZAR EL MARGEN NETO mensual del catálogo, no las ventas brutas.

Tienes acceso a tools del MCP que te permiten:
- Leer mercado/competencia (ml_buscar_categoria, ml_competencia_top_n, ml_market_share, ml_estadisticas_categoria)
- Ver tu catálogo y métricas (ml_mis_publicaciones, ml_metricas_visitas, ml_health_publicacion)
- Calcular margen REAL por SKU (ml_margen_sku, ml_precio_minimo_objetivo, ml_decision_precio)
- Modificar publicaciones (ml_actualizar_precio, ml_actualizar_stock, ml_actualizar_titulo, ml_pausar_publicacion)
- Atender clientes (ml_preguntas_pendientes, ml_responder_pregunta)
- Ver órdenes/reclamos (ml_ordenes_recientes, ml_reclamos_abiertos)

Pipeline que debes ejecutar HOY:

1. Snapshot competitivo: para cada término de búsqueda principal ("uniforme clinico", "scrub mujer", "scrub hombre", "ambo clinico"), obtén top 20 competidores. Anota mediana de precio y rango.

2. Snapshot propio: lista todas mis publicaciones activas. Para cada una calcula su margen real (ml_margen_sku) y su posición estimada vs competencia.

3. Decisiones de pricing (UNA por SKU):
   - Margen NEGATIVO: marca para pausa (ml_pausar_publicacion con dry_run=True; NO pauses solo, RECOMIENDA)
   - Margen < 20%: usa ml_precio_minimo_objetivo para calcular precio que asegure 25% margen. Si el cambio es ≤10%, aplica con ml_actualizar_precio dry_run=False. Si es >10%, déjalo como pendiente de aprobación humana.
   - Margen ≥ 30% y posición top 3: prueba subir 2-3% (dentro de guardrail ±10%)
   - Margen ≥ 25% pero posición >10: baja 3-5% para ganar visibilidad sin sacrificar mínimo
   - Caso intermedio: mantener

4. Responde preguntas pendientes: usa ml_preguntas_pendientes. Para cada pregunta:
   - Stock/disponibilidad → responde confirmando si hay stock
   - Tallas → indica que están en variantes
   - Envío/despacho → "1 día hábil despacho, 1-5 días entrega según comuna"
   - Factura → confirma que emiten factura electrónica
   - Cualquier otra cosa compleja → NO respondas, déjala para humano

5. Reporta al final un JSON con:
   {
     "fecha": "...",
     "resumen": "...",
     "kpis": {"margen_total_estimado_mes": ..., "items_no_rentables": ..., "items_top": [...]},
     "acciones_ejecutadas": [...],
     "pendientes_aprobacion": [...],
     "preguntas_respondidas": ...,
     "alertas": [...]
   }

GUARDRAILS ABSOLUTOS:
- Máximo 20 cambios de precio por corrida
- Nunca cambies precio si el delta es >10% sin marcarlo como pendiente
- Nunca pauses una publicación automáticamente (siempre recomienda)
- Si una tool da error 3 veces seguidas, omítela y continúa

Sé eficiente: usa las tools en paralelo cuando puedas, no hagas lecturas redundantes."""

SYSTEM_PROMPT_QUESTIONS = """Eres el agente de atención al cliente de MyScrubs en MercadoLibre Chile. Tu única tarea ahora es revisar preguntas pendientes y responderlas con tono cálido, profesional y conciso.

Tools disponibles: ml_preguntas_pendientes, ml_mis_publicaciones (para contexto del item), ml_responder_pregunta.

Para CADA pregunta pendiente:
1. Lee el texto.
2. Si encaja con un patrón conocido (stock, tallas, envío, factura, talla específica), responde con el template apropiado adaptado al item.
3. Si es compleja, ambigua, queja, o requiere decisión de negocio (descuento, devolución, cambio), NO RESPONDAS — déjala para humano y márcala en el reporte.

Tono: "¡Hola! ... ¡Saludos!". Tutea, sé breve (máx 3 oraciones).

Al final reporta JSON: {"respondidas": N, "pendientes_humano": [...], "errores": [...]}."""


# =========================================================================
# Agent loop
# =========================================================================

async def run_agent(mode: str) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY no setada")
    mcp_url = os.environ.get("MCP_URL")
    mcp_token = os.environ.get("MCP_AUTH_TOKEN")
    if not mcp_url or not mcp_token:
        raise RuntimeError("MCP_URL y MCP_AUTH_TOKEN requeridos")
    model = os.environ.get("AGENT_MODEL", "claude-sonnet-4-6")
    max_turns = int(os.environ.get("AGENT_MAX_TURNS", "40"))

    mcp = MCPHttpClient(mcp_url, mcp_token)
    log.info("agent_start", mode=mode, model=model, mcp_url=mcp_url)

    # Descubrir tools
    tools = await mcp.list_tools()
    anth_tools = mcp_tools_to_anthropic(tools)
    log.info("tools_discovered", count=len(anth_tools))

    system_prompt = (
        SYSTEM_PROMPT_DAILY if mode == "daily_full" else SYSTEM_PROMPT_QUESTIONS
    )
    user_msg = (
        "Ejecuta tu pipeline diaria de optimización completa para HOY."
        if mode == "daily_full"
        else "Revisa preguntas pendientes y respondé las que correspondan."
    )

    client = AsyncAnthropic(api_key=api_key)
    messages = [{"role": "user", "content": user_msg}]
    tool_calls_log: list[dict] = []
    final_text = ""
    turn = -1  # init para que turn+1 no falle si max_turns=0

    for turn in range(max_turns):
        log.info("agent_turn", turn=turn, msgs=len(messages))
        resp = await client.messages.create(
            model=model,
            max_tokens=8192,
            system=system_prompt,
            tools=anth_tools,
            messages=messages,
        )
        # Acumular respuesta del assistant
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "end_turn":
            # Extrae texto final
            for block in resp.content:
                if block.type == "text":
                    final_text += block.text + "\n"
            break

        if resp.stop_reason == "tool_use":
            tool_results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                t0 = time.monotonic()
                try:
                    tool_response = await mcp.call_tool(block.name, block.input or {})
                    # Las respuestas MCP traen content[].text o structuredContent
                    if isinstance(tool_response, dict):
                        if "structuredContent" in tool_response:
                            result_str = json.dumps(
                                tool_response["structuredContent"],
                                ensure_ascii=False,
                            )
                        else:
                            content = tool_response.get("content", [])
                            result_str = "\n".join(
                                c.get("text", "")
                                for c in content
                                if isinstance(c, dict) and c.get("type") == "text"
                            ) or json.dumps(tool_response, ensure_ascii=False)
                    else:
                        result_str = json.dumps(tool_response, ensure_ascii=False)
                    is_error = bool(
                        isinstance(tool_response, dict)
                        and tool_response.get("isError")
                    )
                except Exception as e:
                    log.error(
                        "tool_call_failed", tool=block.name, error=str(e)
                    )
                    result_str = json.dumps({"error": str(e)})
                    is_error = True
                dur_ms = int((time.monotonic() - t0) * 1000)
                tool_calls_log.append({
                    "tool": block.name,
                    "input": block.input,
                    "dur_ms": dur_ms,
                    "error": is_error,
                })
                # Trunca con marker para no romper JSON ni confundir al modelo
                if len(result_str) > 25000:
                    result_str = result_str[:24900] + "\n...[truncated, output too large]"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_str,
                    "is_error": is_error,
                })
            messages.append({"role": "user", "content": tool_results})
            continue

        # Otros stop_reasons (max_tokens, pause_turn, etc.)
        log.warning("unexpected_stop_reason", reason=resp.stop_reason)
        break

    await mcp.close()

    # Intentar parsear JSON del último texto
    report_json: Optional[dict] = None
    try:
        # Buscar bloque JSON en el texto final
        start = final_text.find("{")
        end = final_text.rfind("}")
        if start >= 0 and end > start:
            report_json = json.loads(final_text[start:end + 1])
    except Exception:
        pass

    summary = {
        "mode": mode,
        "model": model,
        "turns": turn + 1,
        "tool_calls": len(tool_calls_log),
        "tool_calls_detail": tool_calls_log,
        "final_text": final_text.strip(),
        "report_json": report_json,
        "completed_at": datetime.utcnow().isoformat(),
    }
    log.info(
        "agent_done",
        mode=mode,
        turns=turn + 1,
        tool_calls=len(tool_calls_log),
        has_report_json=report_json is not None,
    )
    return summary


# =========================================================================
# Notificación de resultado
# =========================================================================

async def notify(summary: dict) -> None:
    webhook = os.environ.get("REPORT_WEBHOOK")
    if webhook:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                await c.post(webhook, json=summary)
            log.info("webhook_sent", url=webhook)
        except Exception as e:
            log.error("webhook_failed", error=str(e))

    email_to = os.environ.get("REPORT_EMAIL_TO")
    smtp_host = os.environ.get("SMTP_HOST")
    if email_to and smtp_host:
        try:
            body = (
                f"MyScrubs ML — corrida {summary['mode']}\n"
                f"Turnos: {summary['turns']}\n"
                f"Tool calls: {summary['tool_calls']}\n\n"
                f"{summary['final_text']}\n"
            )
            msg = MIMEText(body, _charset="utf-8")
            msg["Subject"] = (
                f"[MyScrubs ML] Reporte {summary['mode']} "
                f"— {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
            )
            msg["From"] = os.environ.get("SMTP_FROM", "noreply@myscrubs.cl")
            msg["To"] = email_to
            with smtplib.SMTP_SSL(
                smtp_host, int(os.environ.get("SMTP_PORT", "465"))
            ) as s:
                s.login(
                    os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"]
                )
                s.send_message(msg)
            log.info("email_sent", to=email_to)
        except Exception as e:
            log.error("email_failed", error=str(e))


# =========================================================================
# Entry point
# =========================================================================

async def main():
    mode = os.environ.get("AGENT_MODE", "daily_full")
    if mode not in ("daily_full", "questions_sweep"):
        print(f"ERROR: AGENT_MODE inválido: {mode}")
        sys.exit(1)
    summary = await run_agent(mode)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    await notify(summary)


if __name__ == "__main__":
    asyncio.run(main())
