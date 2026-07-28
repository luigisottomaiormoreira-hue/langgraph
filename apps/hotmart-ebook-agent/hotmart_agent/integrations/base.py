"""Cliente HTTP mínimo com retry, timeout, circuit breaker e redação de segredos.

Usa apenas a stdlib (`urllib`) de propósito: o agente precisa rodar em um worker
enxuto, e cada dependência extra é mais superfície de manutenção.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..errors import (
    AuthError,
    CircuitBreaker,
    IntegrationError,
    RateLimitError,
    retry,
)

logger = logging.getLogger(__name__)

_REDACT_KEYS = {
    "access_token",
    "client_secret",
    "password",
    "authorization",
    "refresh_token",
}


def redact(data: Any) -> Any:
    """Remove segredos antes de qualquer log."""
    if isinstance(data, dict):
        return {
            k: ("***" if k.lower() in _REDACT_KEYS else redact(v))
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [redact(v) for v in data]
    return data


class HttpClient:
    """Wrapper HTTP por serviço, com breaker próprio.

    Cada serviço externo tem seu próprio breaker: a Meta cair não pode arrastar a
    Hotmart junto.
    """

    def __init__(
        self,
        service: str,
        base_url: str = "",
        *,
        timeout: float = 20.0,
        max_retries: int = 3,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        self.service = service
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.default_headers = default_headers or {}
        self.breaker = CircuitBreaker(name=service)

    # ------------------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        form_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.breaker.guard()

        @retry(attempts=self.max_retries, retry_on=(IntegrationError,))
        def _call() -> dict[str, Any]:
            return self._raw_request(
                method,
                path,
                params=params,
                json_body=json_body,
                form_body=form_body,
                headers=headers,
            )

        try:
            result = _call()
        except IntegrationError:
            self.breaker.record_failure()
            raise
        self.breaker.record_success()
        return result

    # ------------------------------------------------------------------
    def _raw_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        form_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            url = f"{url}?{urllib.parse.urlencode(clean, doseq=True)}"

        data: bytes | None = None
        all_headers = {"Accept": "application/json", **self.default_headers}
        if headers:
            all_headers.update(headers)
        if json_body is not None:
            data = json.dumps(json_body).encode()
            all_headers["Content-Type"] = "application/json"
        elif form_body is not None:
            data = urllib.parse.urlencode(form_body).encode()
            all_headers["Content-Type"] = "application/x-www-form-urlencoded"

        req = urllib.request.Request(
            url, data=data, headers=all_headers, method=method.upper()
        )
        logger.debug(
            "http_request service=%s method=%s url=%s body=%s",
            self.service,
            method,
            url.split("?")[0],
            redact(json_body or form_body or {}),
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8") or "{}"
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, dict) else {"data": parsed}
        except urllib.error.HTTPError as exc:  # noqa: PERF203
            body = exc.read().decode("utf-8", errors="replace")[:800]
            if exc.code in (401, 403):
                raise AuthError(self.service, f"{exc.code}: {body}") from exc
            if exc.code == 429:
                retry_after = float(exc.headers.get("Retry-After", "2") or 2)
                raise RateLimitError(
                    self.service, f"429: {body}", retry_after=retry_after
                ) from exc
            raise IntegrationError(
                self.service,
                f"HTTP {exc.code}: {body}",
                status=exc.code,
                retryable=exc.code >= 500,
            ) from exc
        except urllib.error.URLError as exc:
            raise IntegrationError(
                self.service, f"falha de rede: {exc.reason}", retryable=True
            ) from exc
        except json.JSONDecodeError as exc:
            raise IntegrationError(
                self.service, "resposta não-JSON", retryable=False
            ) from exc

    def get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return self.request("POST", path, **kwargs)
