# Setup MercadoLibre Developers — Paso a paso

Esta guía te lleva desde cuenta de vendedor MyScrubs a credenciales OAuth funcionando. Tiempo estimado: **20-30 minutos**.

---

## Pre-requisitos

- [x] Cuenta de vendedor MyScrubs activa en MercadoLibre Chile (mercadolibre.cl)
- [x] Acceso al email asociado a esa cuenta
- [x] Un dominio o URL pública (puede ser ngrok temporal) para el redirect URI

---

## Paso 1 — Crear la aplicación en ML Developers

1. Anda a **https://developers.mercadolibre.cl/devcenter** y entra con la cuenta MyScrubs.
2. Click en **"Crear nueva aplicación"**.
3. Completa el formulario:
   - **Nombre**: `MyScrubs MCP Agent`
   - **Descripción corta**: "Agente interno para gestión de catálogo, precios y atención de clientes en MercadoLibre."
   - **URL de la app / sitio web**: `https://myscrubs.cl`
   - **Redirect URI**: `http://localhost:8765/callback`
     - Si no quieres usar localhost, levanta `ngrok http 8765` y usa la URL https que te da.
   - **Scopes**: marca **TODOS** los que aparezcan:
     - `read` — leer datos de cuenta, items, órdenes
     - `write` — modificar items, precios, stock
     - `offline_access` — obtener refresh token (CRÍTICO, sin esto los tokens duran solo 6h)
   - **Tópicos de notificación (webhooks)**: marca al menos
     - `orders_v2` (órdenes nuevas)
     - `items` (cambios en publicaciones)
     - `questions` (preguntas nuevas)
     - `messages` (mensajes post-venta)
     - `claims` (reclamos)
   - **URL de notificaciones (webhooks callback)**: por ahora `https://myscrubs.cl/ml/webhooks` (en Fase 2 lo conectaremos a un endpoint real).
4. Guarda.

Te quedan visibles dos valores **secretos**:
- **App ID** (visible)
- **Secret Key** (click "Mostrar" para ver)

Cópialos a un lugar seguro.

---

## Paso 2 — Configurar credenciales locales

En tu computador, crea el archivo `config.json` en la carpeta donde vive el MCP (te lo entrego como `config.example.json`, solo cambias los valores).

```json
{
  "ml": {
    "site_id": "MLC",
    "app_id": "PEGAR_AQUI_TU_APP_ID",
    "secret_key": "PEGAR_AQUI_TU_SECRET_KEY",
    "redirect_uri": "http://localhost:8765/callback",
    "user_id_seller": "PEGAR_AQUI_TU_USER_ID_DE_VENDEDOR"
  },
  "bsale": {
    "use_existing_mcp": true,
    "comment": "El MCP de MyScrubs ML usa el MCP de BSale ya instalado para obtener costos por SKU."
  },
  "agent": {
    "enabled": true,
    "cron": "0 6 * * *",
    "guardrails": {
      "max_price_change_pct": 10,
      "max_changes_per_day": 20,
      "min_margin_pct": 15,
      "pause_threshold_days": 7
    },
    "notification_email": "ventas@myscrubs.cl",
    "notion_database_id": "PEGAR_SI_QUIERES_REPORTES_EN_NOTION"
  }
}
```

Para encontrar tu **user_id de vendedor**:
- Entra a https://mercadolibre.cl, abre tu perfil
- O ejecuta una vez `curl https://api.mercadolibre.com/users/me?access_token=XXX` después de obtener tu primer token

---

## Paso 3 — Primer authorization code (one-time)

Esto solo se hace **una vez** para obtener el primer refresh_token. Después el MCP se auto-renueva.

### Opción A: Vía script incluido (recomendado)

```bash
python scripts/oauth_setup.py
```

El script:
1. Abre el navegador hacia la URL de autorización de ML.
2. Tú confirmas que MyScrubs autoriza la app.
3. ML te redirige a `http://localhost:8765/callback?code=ABC123`.
4. El script captura el `code` y lo intercambia por `access_token` + `refresh_token`.
5. Guarda los tokens en `~/.myscrubs_ml/tokens.json` con permisos 600.

### Opción B: Manual (si Opción A falla)

1. Construye la URL:
   ```
   https://auth.mercadolibre.cl/authorization?response_type=code&client_id=TU_APP_ID&redirect_uri=http%3A%2F%2Flocalhost%3A8765%2Fcallback
   ```
2. Pégala en el navegador, autoriza.
3. Te redirige a `http://localhost:8765/callback?code=TG-XXXXX`. Copia el código (parte después de `?code=`).
4. Ejecuta:
   ```bash
   curl -X POST https://api.mercadolibre.com/oauth/token \
     -H "Content-Type: application/x-www-form-urlencoded" \
     -d "grant_type=authorization_code&client_id=TU_APP_ID&client_secret=TU_SECRET&code=TG-XXXXX&redirect_uri=http://localhost:8765/callback"
   ```
5. La respuesta trae `access_token`, `refresh_token`, `expires_in`. Guárdala en `~/.myscrubs_ml/tokens.json`:
   ```json
   {
     "access_token": "APP_USR-...",
     "refresh_token": "TG-...",
     "expires_at": 1735000000,
     "user_id": 12345678
   }
   ```

---

## Paso 4 — Verificar que funciona

```bash
python scripts/verify_setup.py
```

Output esperado:
```
✓ Credenciales cargadas
✓ Token válido (expira en 5h 52min)
✓ User ID: 12345678 (MyScrubs)
✓ Site: MLC (Chile)
✓ 47 publicaciones activas detectadas
✓ Categoría principal detectada: MLC1430 (Uniformes médicos)
✓ Conexión BSale OK (via MCP existente)
✓ Setup completo. El MCP está listo para usar desde Cowork.
```

---

## Paso 5 — Instalar el plugin en Cowork

1. En Cowork, anda a **Plugins → Install from file**.
2. Selecciona el archivo `myscrubs-ml.plugin` (te lo entrego en esta sesión).
3. Confirma permisos.
4. El MCP queda activo. Las tools aparecen como `mcp__myscrubs_ml__*` en el chat.

---

## Paso 6 — Activar el agente diario

En Cowork, di:
> "Activa el agente diario de MyScrubs ML a las 6 AM hora Chile"

Esto crea una scheduled-task que corre `agent_daily.py` cada mañana.

---

## Troubleshooting

| Síntoma | Causa probable | Solución |
|---|---|---|
| `invalid_client` al intercambiar code | App ID o Secret mal copiados | Revisar developers.mercadolibre.cl |
| `redirect_uri_mismatch` | URI distinto al registrado | Debe ser EXACTO, incluye http/https y puerto |
| `invalid_grant` al refrescar | Refresh token expirado (6 meses sin uso) | Re-ejecutar Paso 3 |
| Tools devuelven 401 | access_token expiró y refresh falló | Revisar logs en `~/.myscrubs_ml/audit.log` |
| Webhooks no llegan | URL pública mal configurada | Por ahora ignora, en Fase 2 lo arreglamos |

---

## Notas de seguridad

- **NUNCA** subas `config.json` ni `tokens.json` a git ni los compartas por chat.
- El `Secret Key` es como una contraseña: si se filtra, ML te puede suspender la cuenta. Regenera desde el devcenter si pasó.
- El MCP corre **localmente en tu computador**, las credenciales nunca viajan a Anthropic.
