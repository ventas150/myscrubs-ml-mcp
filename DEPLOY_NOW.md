# Deploy Now — 3 comandos para tener todo corriendo

> Tiempo total: **~40 minutos** (la mayoría es esperar al build de Render).
> Costo mensual: **~$11-16 USD**.

---

## Pre-vuelo (5 min) — credenciales que necesitás a mano

Tené abiertos en pestañas del navegador:

1. **GitHub PAT** → https://github.com/settings/tokens/new
   - Note: `myscrubs-ml-deploy`
   - Scopes: marcá **`repo`** (full control of private repositories)
   - Click **Generate**, copia el token (empieza con `ghp_` o `github_pat_`)

2. **MercadoLibre Developer credentials** → https://developers.mercadolibre.cl/devcenter
   - Tu **App ID** y **Secret Key** de la app `MyScrubs MCP Agent`
   - Si no creaste la app todavía, mirá `SETUP_ML_DEVELOPERS.md` paso 1

3. **BSale access_token** → BSale → Configuración → API → Acceso

4. **Anthropic API key** → https://console.anthropic.com/settings/keys

---

## Paso 1 — Subir el proyecto a GitHub (un comando, ~30 segundos)

Desde la carpeta `outputs/` donde están todos los archivos del proyecto:

```bash
python deploy_to_github.py
```

El script te pide:
- Tu **GitHub PAT** (el que copiaste arriba)
- Nombre del repo (default: `myscrubs-ml-mcp`)

En ~30 segundos crea el repo privado y sube los 26 archivos. Te imprime la URL final.

> **Alternativa si preferís manual con git:** después de copiar los archivos al repo, `git init && git add . && git commit -m "initial" && git remote add origin <url> && git push -u origin main`.

---

## Paso 2 — Actualizar el redirect URI en MercadoLibre (1 min)

Antes de Render, vamos a MercadoLibre Developers para agregar el callback de Render.

1. https://developers.mercadolibre.cl/devcenter → tu app **MyScrubs MCP Agent** → Editar
2. En **Redirect URI** agregá (sin borrar el de localhost si lo tenías):
   ```
   https://myscrubs-ml-mcp.onrender.com/oauth/callback
   ```
   (Si Render te asigna un sufijo `-XXXX`, vuelvé a editar esta URL después del primer deploy.)
3. Guardá.

---

## Paso 3 — Desplegar en Render (10 min, mayoría de espera)

1. Andá a **https://dashboard.render.com** → **New +** → **Blueprint**
2. Conectá tu GitHub si no está conectado, autorizá el repo `myscrubs-ml-mcp`
3. Render lee `render.yaml` y muestra **3 servicios** que va a crear:
   - `myscrubs-ml-mcp` (Web Service)
   - `myscrubs-agent-daily` (Cron, 06:00 Chile)
   - `myscrubs-agent-questions` (Cron, cada 2h hábil)
4. **Completá los secretos** que Render pide (marca con candado los `sync: false`):

   Web service `myscrubs-ml-mcp`:
   ```
   ML_APP_ID            = <tu App ID ML>
   ML_SECRET_KEY        = <tu Secret Key ML>
   ML_REDIRECT_URI      = https://myscrubs-ml-mcp.onrender.com/oauth/callback
   ML_USER_ID_SELLER    = <tu user_id de vendedor>
   BSALE_ACCESS_TOKEN   = <tu access_token BSale>
   MCP_AUTH_TOKEN       = dejá que Render lo genere (botón "Generate")
   ```

   Cron `myscrubs-agent-daily`:
   ```
   ANTHROPIC_API_KEY    = sk-ant-...
   SMTP_HOST            = smtp.gmail.com (opcional, si querés email)
   SMTP_USER            = tu email
   SMTP_PASSWORD        = Gmail App Password (no la contraseña normal)
   ```

   Cron `myscrubs-agent-questions`:
   ```
   ANTHROPIC_API_KEY    = sk-ant-... (mismo de arriba)
   ```

5. Click **Apply**. Render empieza a buildear las 3 imágenes Docker.
6. Esperá **5-10 minutos**. Mirá los logs en vivo si querés.

---

## Paso 4 — Actualizar `MCP_URL` en los crons (1 min)

Render asigna un URL público al web service. Va a ser algo como:
```
https://myscrubs-ml-mcp.onrender.com
```
(o con un sufijo `-XXXX` si el nombre estaba tomado)

1. En cada cron (`myscrubs-agent-daily` y `myscrubs-agent-questions`):
2. Settings → Environment → editar `MCP_URL`:
   ```
   https://<tu-url-real>.onrender.com/mcp
   ```
3. Save (no requiere redeploy).

---

## Paso 5 — Autorizar la app ML (one-time OAuth, 2 min)

En Render dashboard → web service `myscrubs-ml-mcp` → **Environment** → revelá el valor de `MCP_AUTH_TOKEN`. Copiálo.

Abrí en el navegador (reemplazando `<URL>` y `<TOKEN>`):

```
https://<URL>.onrender.com/oauth/start?setup=<MCP_AUTH_TOKEN>
```

ML te muestra "MyScrubs MCP Agent quiere acceder a tu cuenta". **Autorizá**.

Te redirige al callback, que intercambia el code por tokens y muestra:
```
✓ Tokens guardados
User ID: 12345678
```

A partir de ahora el MCP se auto-renueva cada 6h por 6 meses sin intervención.

Verificá con:
```
https://<URL>.onrender.com/oauth/status?setup=<MCP_AUTH_TOKEN>
```

Debe responder `"status":"ok"`.

---

## Paso 6 — Probar el agente manualmente (3 min)

Antes de esperar al primer cron a las 6 AM, dispará uno manual:

1. Render dashboard → cron `myscrubs-agent-daily` → botón **Trigger Run** (arriba a la derecha)
2. Mirá los logs en vivo. Deberías ver:
   ```
   {"event":"agent_start","mode":"daily_full",...}
   {"event":"tools_discovered","count":22}
   {"event":"agent_turn","turn":0,...}
   ...
   {"event":"agent_done","tool_calls":N,...}
   ```
3. Si setteaste SMTP, te llega un email con el reporte.

---

## ✅ Listo. ¿Y ahora?

- **Cron diario** corre todos los días a las 06:00 Chile.
- **Cron preguntas** corre cada 2h entre 08:00 y 22:00 Chile.
- **El agente vive solo** dentro del MCP en Render.

### Comandos útiles del día a día

- **Pausar el agente**: Render → cron → **Suspend**
- **Cambiar guardrails**: Editar env vars en el web service (`AGENT_MAX_CHANGE_PCT`, etc)
- **Ver acciones del día**: GET a `https://<URL>/mcp` con tool `ml_acciones_hoy` (o instalá el MCP en Cowork y pregúntale)
- **Logs**: Render dashboard → servicio → **Logs**

### Si algo falla
Revisá `RENDER_DEPLOY.md` sección Troubleshooting. Los errores más comunes:
- 401 en /mcp → bearer mal copiado
- Agent corre sin SKUs → mapping `seller_custom_field` ML ↔ `code` BSale
- OAuth falla → redirect URI no coincide exacto
