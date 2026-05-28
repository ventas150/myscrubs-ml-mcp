# Deploy de MyScrubs ML en Render — Guía paso a paso

Tiempo estimado: **35-45 minutos** la primera vez.

Al final vas a tener:
- **MCP HTTP** corriendo 24/7 en `https://myscrubs-ml-mcp.onrender.com`
- **Cron diario** que ejecuta el agente Claude a las 06:00 hora Chile
- **Cron cada 2h** que responde preguntas en horario hábil

---

## Pre-requisitos (todo listo según confirmaste)

- [x] Cuenta Render plan Starter activa
- [x] Cuenta de developer en developers.mercadolibre.cl con app creada
- [x] BSale `access_token` (BSale → Configuración → API → Acceso)
- [x] Anthropic API key con crédito
- [x] Cuenta Git (GitHub o GitLab) para conectar a Render
- [x] (Opcional) SMTP para reportes por email — recomiendo Gmail con app password

---

## Paso 1 — Preparar el repo Git

1. Crea un repo privado en GitHub: `myscrubs-ml-mcp`
2. Copia TODOS los archivos del proyecto a la raíz del repo:
   ```
   server.py
   serve_http.py
   ml_auth.py
   ml_client.py
   profit_engine.py
   bsale_bridge.py
   bsale_client.py
   tools_read.py
   tools_write.py
   agent_daily.py
   claude_agent.py
   oauth_setup.py
   verify_setup.py
   requirements.txt
   Dockerfile
   .dockerignore
   .gitignore
   render.yaml
   .env.example
   README.md
   PLAN.md
   SETUP_ML_DEVELOPERS.md
   RENDER_DEPLOY.md
   plugin.json
   config.example.json
   ```
3. **NO commitees `.env` ni `config.json` reales** (el `.gitignore` ya los excluye).
4. Push al repo: `git add . && git commit -m "initial" && git push`

---

## Paso 2 — Ajustar el redirect URI de la app ML

Antes del deploy, vamos a registrar el redirect URI público en MercadoLibre Developers.

1. Anda a developers.mercadolibre.cl → tu app `MyScrubs MCP Agent` → **Editar**.
2. En **Redirect URI**, agrega:
   ```
   https://myscrubs-ml-mcp.onrender.com/oauth/callback
   ```
   (Reemplazá `myscrubs-ml-mcp` por el nombre que vas a usar en Render si vas a usar otro.)
3. Guardá.

> Nota: ML permite múltiples redirect URIs. Podés dejar también el `http://localhost:8765/callback` para desarrollo.

---

## Paso 3 — Deploy en Render con Blueprint

1. Entrá a **dashboard.render.com → New + → Blueprint**.
2. Conectá el repo que creaste.
3. Render detecta `render.yaml` y muestra los 3 servicios:
   - `myscrubs-ml-mcp` (Web Service)
   - `myscrubs-agent-daily` (Cron)
   - `myscrubs-agent-questions` (Cron)
4. Antes de crear, Render te pide completar las **env vars marcadas con `sync: false`**:

   **Web service `myscrubs-ml-mcp`:**
   - `ML_APP_ID` → tu App ID de ML developers
   - `ML_SECRET_KEY` → tu Secret Key
   - `ML_REDIRECT_URI` → `https://myscrubs-ml-mcp.onrender.com/oauth/callback`
   - `ML_USER_ID_SELLER` → tu user_id de vendedor (numérico)
   - `BSALE_ACCESS_TOKEN` → tu access_token BSale
   - `MCP_AUTH_TOKEN` → dejá que Render lo genere automáticamente (es un secret)

   **Cron `myscrubs-agent-daily`:**
   - `ANTHROPIC_API_KEY` → `sk-ant-...`
   - `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD` → opcional, para emails
   - `REPORT_WEBHOOK` → opcional, URL para POST del JSON
   - `MCP_URL` → **dejalo en `CHANGEME` por ahora**, lo arreglamos en el paso 5

   **Cron `myscrubs-agent-questions`:**
   - `ANTHROPIC_API_KEY` → mismo que arriba
   - `MCP_URL` → **dejalo en `CHANGEME` por ahora**

5. Click **Apply** / **Create**.
6. Render empieza a buildear las 3 imágenes Docker. Toma 5-10 min la primera vez.

---

## Paso 4 — Una vez el MCP esté UP, verificar health

Render te asigna un URL público al web service. Va a ser algo como:
```
https://myscrubs-ml-mcp-XXXX.onrender.com
```
(El sufijo `-XXXX` puede aparecer si el nombre estaba tomado.)

Verificá que está vivo:
```bash
curl https://myscrubs-ml-mcp-XXXX.onrender.com/health
# Esperado:
# {"status":"ok","service":"myscrubs-ml-mcp","site":"MLC","user_id":12345678,"time":...}
```

Si devuelve un error 503 con `missing_tokens`, es normal — todavía no hicimos OAuth.

---

## Paso 5 — Actualizar `MCP_URL` en los crons

1. En Render dashboard, anda a cada cron job (`myscrubs-agent-daily` y `myscrubs-agent-questions`).
2. **Settings → Environment** → edita `MCP_URL`:
   ```
   https://myscrubs-ml-mcp-XXXX.onrender.com/mcp
   ```
   (Reemplazá `XXXX` por el sufijo real que te asignó Render.)
3. Guardá. No hace falta redeploy porque los crons leen env vars al ejecutar.

---

## Paso 6 — Hacer OAuth ML (one-time)

Ahora autorizamos a la app ML para que pueda actuar a nombre de MyScrubs.

1. Abrí en tu navegador (reemplazando `XXXX` y `<MCP_AUTH_TOKEN>`):
   ```
   https://myscrubs-ml-mcp-XXXX.onrender.com/oauth/start?setup=<MCP_AUTH_TOKEN>
   ```
   El `MCP_AUTH_TOKEN` lo ves en Render dashboard → `myscrubs-ml-mcp` → Environment → revela el valor generado.

2. ML te muestra: "MyScrubs MCP Agent quiere acceder a tu cuenta MyScrubs". Confirmá.

3. ML te redirige a `/oauth/callback?code=...`. El server intercambia el code por tokens y muestra:
   ```
   ✓ Tokens guardados
   User ID: 12345678
   ```

4. Verificá:
   ```bash
   curl "https://myscrubs-ml-mcp-XXXX.onrender.com/oauth/status?setup=<MCP_AUTH_TOKEN>"
   # Esperado:
   # {"status":"ok","has_tokens":true,"user_id":12345678,...}
   ```

A partir de ahora el MCP se auto-refresca cada 6h sin intervención humana, durante 6 meses. Si en 6 meses no hubo ninguna corrida, hay que repetir este paso.

---

## Paso 7 — Test manual del agente

Antes de esperar al primer cron, corré el agente a mano para validar.

En Render dashboard → cron `myscrubs-agent-daily` → **Trigger Run** (botón arriba a la derecha).

Mirá los logs en vivo. Deberías ver algo así:
```
{"event":"agent_start","mode":"daily_full","model":"claude-sonnet-4-6",...}
{"event":"tools_discovered","count":22}
{"event":"agent_turn","turn":0,"msgs":1}
{"event":"agent_turn","turn":1,"msgs":3}
...
{"event":"agent_done","mode":"daily_full","turns":15,"tool_calls":47,...}
```

Si setteaste `REPORT_EMAIL_TO` + `SMTP_*`, recibís el reporte en tu inbox.

---

## Paso 8 — Activar el cron

Los crons están definidos en `render.yaml` con schedule:
- **`myscrubs-agent-daily`**: `0 10 * * *` → 10:00 UTC = **06:00 hora Chile**
- **`myscrubs-agent-questions`**: cada 2h entre 12:00 y 02:00 UTC = **08:00–22:00 hora Chile**

Render los ejecuta automáticamente. Podés verificar las últimas corridas en el dashboard de cada cron.

> **Nota sobre horario Chile**: Chile cambia entre UTC-3 (verano) y UTC-4 (invierno) por horario de verano. Los schedules de arriba asumen UTC-4 (invierno). Si querés precisión absoluta, ajustá ±1h en los meses de horario de verano.

---

## Costo mensual estimado

| Item | Costo USD/mes |
|---|---|
| Render Starter web service | $7 |
| Render Persistent Disk 1GB | $1 |
| Render Cron (incluido en plan) | $0 |
| Anthropic Claude (Sonnet 4.6 daily + Haiku 4.5 questions) | $3-8 |
| **Total** | **~$11-16** |

---

## Operación día a día

### Ver qué hizo el agente hoy
```bash
curl -H "Authorization: Bearer <MCP_AUTH_TOKEN>" \
     -X POST https://myscrubs-ml-mcp-XXXX.onrender.com/mcp \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"ml_acciones_hoy","arguments":{}}}'
```

O más simple: usá Cowork con el plugin Cowork instalado y preguntá "qué hizo el MCP hoy".

### Pausar el agente temporalmente
En Render dashboard → cron → **Suspend**. Reactivar = **Resume**.

### Cambiar guardrails
Editar env vars en el web service: `AGENT_MAX_CHANGE_PCT`, `AGENT_MAX_CHANGES_DAY`, etc. No requiere redeploy si solo cambiás env (Render reinicia el servicio automáticamente).

### Ver logs del MCP
Dashboard → `myscrubs-ml-mcp` → **Logs**. Filtrá por `event:tool_call_failed` para spotting de errores.

### Backup de tokens
Los tokens viven en el disco persistente. Si vas a destruir el servicio, primero descargá `/var/data/myscrubs/tokens.json` vía SSH (Starter incluye shell access).

---

## Troubleshooting

| Síntoma | Solución |
|---|---|
| `health` devuelve 503 con `missing_tokens` | Hacé el paso 6 (OAuth) |
| Agente diario corre pero no encuentra mis SKUs | Revisar que `seller_custom_field` en ML coincida con `code` en BSale. El agente loggea `bsale_fetch_not_wired` o `bsale_delegate_failed` |
| `MCP RPC initialize failed [401]` | El cron tiene mal el `MCP_AUTH_TOKEN`. Volvé a vincular `fromService` |
| Render mata el cron a media corrida | Aumentá `AGENT_MAX_TURNS` o pasá a plan Standard (más timeout). También revisá si Claude está pidiendo demasiadas tools por turno |
| Email no llega | Para Gmail necesitás un **App Password** (no la contraseña normal). Generálo en https://myaccount.google.com/apppasswords |
| El refresh_token venció (6 meses sin uso) | Repetí paso 6 |

---

## ¿Cómo conectar Cowork al MCP cloud también?

Si querés además poder pedirle al MCP desde Cowork (no solo via el agente cron):

1. En Cowork → Plugins → **Install MCP Server (custom)**.
2. Configurá:
   ```
   Name: myscrubs-ml-cloud
   URL:  https://myscrubs-ml-mcp-XXXX.onrender.com/mcp
   Auth: Bearer <MCP_AUTH_TOKEN>
   ```

Ahora podés decirle a Cowork "muéstrame mi margen de hoy" y va a llamar al MCP cloud directo.

---

## Próximos pasos sugeridos

1. **Conectar webhooks ML** (Fase 2): el MCP recibe eventos de orders/questions/claims y el agente reacciona en tiempo real. Requiere agregar endpoint `POST /webhooks/ml` y suscribirse en ML developers.
2. **Postgres en Render** para el audit log e histórico de decisiones (en vez de JSON files). $7/mes adicional.
3. **Dashboard live**: crear un Cowork artifact que muestre KPIs en tiempo real consultando el MCP.
4. **Calibrar guardrails** después de la primera semana en producción según los pendientes_aprobacion que se acumulen.
