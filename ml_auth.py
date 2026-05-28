"""
ml_auth.py — OAuth 2.0 para MercadoLibre con refresh automático.

Maneja:
- Carga de credenciales desde config.json
- Persistencia de tokens en ~/.myscrubs_ml/tokens.json (chmod 600)
- Refresh automático cuando faltan <30 min de vida útil
- Bloqueo con asyncio.Lock para evitar refresh paralelos
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
import structlog

log = structlog.get_logger()

ML_OAUTH_URL = "https://api.mercadolibre.com/oauth/token"
TOKEN_REFRESH_THRESHOLD_SEC = 1800  # refrescar si quedan <30 min


@dataclass
class MLCredentials:
    app_id: str
    secret_key: str
    redirect_uri: str
    site_id: str = "MLC"


@dataclass
class MLTokens:
    access_token: str
    refresh_token: str
    expires_at: float  # epoch seconds
    user_id: Optional[int] = None

    @property
    def expires_in_sec(self) -> float:
        return self.expires_at - time.time()

    @property
    def needs_refresh(self) -> bool:
        return self.expires_in_sec < TOKEN_REFRESH_THRESHOLD_SEC


class MLAuth:
    """Gestor de credenciales y tokens OAuth para MercadoLibre."""

    def __init__(
        self,
        credentials: MLCredentials,
        tokens_path: Optional[Path] = None,
    ):
        self.credentials = credentials
        self.tokens_path = tokens_path or Path.home() / ".myscrubs_ml" / "tokens.json"
        self.tokens_path.parent.mkdir(parents=True, exist_ok=True)
        self._tokens: Optional[MLTokens] = None
        self._refresh_lock = asyncio.Lock()
        self._load_tokens()

    # ---------- persistence ----------

    def _load_tokens(self) -> None:
        if not self.tokens_path.exists():
            log.warning("tokens_file_not_found", path=str(self.tokens_path))
            return
        data = json.loads(self.tokens_path.read_text())
        self._tokens = MLTokens(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=float(data["expires_at"]),
            user_id=data.get("user_id"),
        )
        log.info("tokens_loaded", expires_in_min=int(self._tokens.expires_in_sec / 60))

    def _save_tokens(self) -> None:
        if not self._tokens:
            return
        payload = {
            "access_token": self._tokens.access_token,
            "refresh_token": self._tokens.refresh_token,
            "expires_at": self._tokens.expires_at,
            "user_id": self._tokens.user_id,
        }
        self.tokens_path.write_text(json.dumps(payload, indent=2))
        # chmod 600
        try:
            os.chmod(self.tokens_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            # En Windows chmod no aplica igual, no es crítico
            pass

    # ---------- flujo inicial ----------

    async def exchange_code_for_tokens(self, code: str) -> MLTokens:
        """Primer intercambio: authorization_code -> tokens. Se llama una vez."""
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                ML_OAUTH_URL,
                data={
                    "grant_type": "authorization_code",
                    "client_id": self.credentials.app_id,
                    "client_secret": self.credentials.secret_key,
                    "code": code,
                    "redirect_uri": self.credentials.redirect_uri,
                },
                headers={"Accept": "application/json"},
            )
            r.raise_for_status()
            data = r.json()
        self._tokens = MLTokens(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=time.time() + data["expires_in"] - 60,
            user_id=data.get("user_id"),
        )
        self._save_tokens()
        log.info("oauth_initial_exchange_ok", user_id=self._tokens.user_id)
        return self._tokens

    # ---------- refresh ----------

    async def _refresh(self) -> MLTokens:
        if not self._tokens:
            raise RuntimeError(
                "Sin tokens. Ejecuta scripts/oauth_setup.py primero."
            )
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                ML_OAUTH_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": self.credentials.app_id,
                    "client_secret": self.credentials.secret_key,
                    "refresh_token": self._tokens.refresh_token,
                },
                headers={"Accept": "application/json"},
            )
            if r.status_code != 200:
                log.error(
                    "refresh_failed",
                    status=r.status_code,
                    body=r.text[:300],
                )
                r.raise_for_status()
            data = r.json()
        self._tokens = MLTokens(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", self._tokens.refresh_token),
            expires_at=time.time() + data["expires_in"] - 60,
            user_id=self._tokens.user_id,
        )
        self._save_tokens()
        log.info(
            "tokens_refreshed",
            expires_in_min=int(self._tokens.expires_in_sec / 60),
        )
        return self._tokens

    async def get_access_token(self) -> str:
        if not self._tokens:
            raise RuntimeError(
                "Sin tokens en disco. Corre primero scripts/oauth_setup.py."
            )
        if self._tokens.needs_refresh:
            async with self._refresh_lock:
                # double-check después del lock
                if self._tokens.needs_refresh:
                    await self._refresh()
        return self._tokens.access_token

    @property
    def user_id(self) -> Optional[int]:
        return self._tokens.user_id if self._tokens else None


# ---------- helper para construir desde config ----------

def auth_from_config(config: dict, tokens_path: Optional[Path] = None) -> MLAuth:
    """Construye MLAuth desde dict de config.

    Cada valor admite override por env var (prioridad: env > config):
      - ML_APP_ID, ML_SECRET_KEY, ML_REDIRECT_URI, ML_SITE_ID, ML_TOKENS_PATH
    """
    ml_cfg = config.get("ml", {})
    creds = MLCredentials(
        app_id=os.environ.get("ML_APP_ID") or ml_cfg.get("app_id", ""),
        secret_key=os.environ.get("ML_SECRET_KEY") or ml_cfg.get("secret_key", ""),
        redirect_uri=os.environ.get("ML_REDIRECT_URI")
            or ml_cfg.get("redirect_uri", ""),
        site_id=os.environ.get("ML_SITE_ID") or ml_cfg.get("site_id", "MLC"),
    )
    if not creds.app_id or not creds.secret_key:
        raise RuntimeError(
            "Faltan credenciales ML. Setea ML_APP_ID y ML_SECRET_KEY como "
            "env vars o en config.json"
        )
    path_override = os.environ.get("ML_TOKENS_PATH")
    if path_override:
        tokens_path = Path(path_override)
    return MLAuth(creds, tokens_path=tokens_path)
