"""
ml_client.py — Cliente HTTP async para la API de MercadoLibre.

- Auto-refresh de token via MLAuth
- Retry con backoff exponencial en 429 / 5xx
- Rate-limit local (max 8 req/s por defecto, ML permite ~10)
- Logging estructurado de cada request
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import httpx
import structlog
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ml_auth import MLAuth

log = structlog.get_logger()

ML_API_BASE = "https://api.mercadolibre.com"
DEFAULT_TIMEOUT = 30.0
DEFAULT_RATE_LIMIT_RPS = 8


class MLApiError(Exception):
    def __init__(self, status: int, message: str, body: Any = None):
        self.status = status
        self.message = message
        self.body = body
        super().__init__(f"ML API {status}: {message}")


class _RateLimiter:
    """Token bucket simple: max N req/s."""

    def __init__(self, rps: int):
        self.rps = rps
        self._tokens = rps
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self.rps, self._tokens + elapsed * self.rps)
            self._last_refill = now
            if self._tokens < 1:
                wait = (1 - self._tokens) / self.rps
                await asyncio.sleep(wait)
                self._tokens = 0
            else:
                self._tokens -= 1


class MLClient:
    """Cliente principal para la API de MercadoLibre."""

    def __init__(
        self,
        auth: MLAuth,
        rate_limit_rps: int = DEFAULT_RATE_LIMIT_RPS,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.auth = auth
        self._rl = _RateLimiter(rate_limit_rps)
        self._timeout = timeout
        self._http = httpx.AsyncClient(
            base_url=ML_API_BASE,
            timeout=timeout,
            headers={
                "Accept": "application/json",
                "User-Agent": "MyScrubs-ML-MCP/1.0",
            },
        )

    async def close(self) -> None:
        await self._http.aclose()

    # ---------- core request ----------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json: Optional[dict] = None,
        auth_required: bool = True,
    ) -> Any:
        await self._rl.acquire()
        headers = {}
        if auth_required:
            token = await self.auth.get_access_token()
            headers["Authorization"] = f"Bearer {token}"

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=1, min=1, max=30),
            retry=retry_if_exception_type((httpx.TimeoutException, MLApiError)),
            reraise=True,
        ):
            with attempt:
                r = await self._http.request(
                    method, path, params=params, json=json, headers=headers
                )
                log.debug(
                    "ml_request",
                    method=method,
                    path=path,
                    status=r.status_code,
                )
                if r.status_code == 401 and auth_required:
                    # Token rechazado, forzar refresh y reintentar 1 vez
                    log.warning("ml_401_forcing_refresh")
                    await self.auth._refresh()  # noqa: SLF001
                    headers["Authorization"] = (
                        f"Bearer {await self.auth.get_access_token()}"
                    )
                    raise MLApiError(401, "Token rejected, retrying")
                if r.status_code in (429, 500, 502, 503, 504):
                    raise MLApiError(r.status_code, "Retryable", body=r.text[:300])
                if r.status_code >= 400:
                    try:
                        body = r.json()
                    except Exception:
                        body = r.text
                    raise MLApiError(
                        r.status_code,
                        f"{method} {path} failed",
                        body=body,
                    )
                if r.status_code == 204:
                    return None
                return r.json()

    # ---------- atajos comunes ----------

    async def get(self, path: str, **kwargs) -> Any:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs) -> Any:
        return await self.request("POST", path, **kwargs)

    async def put(self, path: str, **kwargs) -> Any:
        return await self.request("PUT", path, **kwargs)

    async def delete(self, path: str, **kwargs) -> Any:
        return await self.request("DELETE", path, **kwargs)

    # ---------- helpers de paginación ----------

    async def paginate(
        self,
        path: str,
        params: Optional[dict] = None,
        max_pages: int = 50,
        page_size: int = 50,
    ):
        """Iterador async sobre páginas ML (offset/limit)."""
        params = dict(params or {})
        params["limit"] = page_size
        offset = 0
        pages = 0
        while pages < max_pages:
            params["offset"] = offset
            data = await self.get(path, params=params)
            results = data.get("results", [])
            if not results:
                return
            for item in results:
                yield item
            paging = data.get("paging", {})
            total = paging.get("total", 0)
            offset += page_size
            pages += 1
            if offset >= total:
                return
