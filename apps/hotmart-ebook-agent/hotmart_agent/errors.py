"""Erros, retry com backoff e circuit breaker.

O princípio é o de degradação graciosa: uma integração fora do ar reduz o escopo
da resposta (e marca o resultado como degradado), mas nunca derruba a execução
inteira do agente.
"""

from __future__ import annotations

import functools
import logging
import random
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class AgentError(Exception):
    """Base de todos os erros do agente."""


class ConfigError(AgentError):
    """Configuração ausente ou inválida (credencial, id de produto...)."""


class IntegrationError(AgentError):
    """Falha ao falar com um sistema externo."""

    def __init__(
        self,
        service: str,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        payload: Any | None = None,
    ) -> None:
        super().__init__(f"[{service}] {message}")
        self.service = service
        self.status = status
        self.retryable = retryable
        self.payload = payload


class AuthError(IntegrationError):
    """Credencial inválida/expirada — nunca é retentável sem intervenção."""

    def __init__(self, service: str, message: str = "credencial inválida") -> None:
        super().__init__(service, message, status=401, retryable=False)


class RateLimitError(IntegrationError):
    """429/limite de API. Retentável, respeitando `retry_after`."""

    def __init__(
        self, service: str, message: str = "rate limit", retry_after: float = 1.0
    ) -> None:
        super().__init__(service, message, status=429, retryable=True)
        self.retry_after = retry_after


class GuardrailViolation(AgentError):
    """Ação bloqueada por um limite de segurança (financeiro ou operacional)."""

    def __init__(self, rule: str, message: str) -> None:
        super().__init__(f"guardrail '{rule}': {message}")
        self.rule = rule


class CircuitOpen(AgentError):
    """Circuito aberto: a integração falhou demais e está em quarentena."""


@dataclass
class CircuitBreaker:
    """Circuit breaker simples por serviço.

    Depois de `failure_threshold` falhas consecutivas o circuito abre por
    `reset_after_s` segundos; a primeira chamada seguinte é um teste (half-open).
    """

    name: str
    failure_threshold: int = 4
    reset_after_s: float = 60.0
    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.reset_after_s:
            # half-open: deixa passar uma tentativa
            self._opened_at = None
            self._failures = self.failure_threshold - 1
            return False
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = time.monotonic()
            logger.warning(
                "circuit_open service=%s failures=%s", self.name, self._failures
            )

    def guard(self) -> None:
        if self.is_open:
            raise CircuitOpen(
                f"integração '{self.name}' indisponível; tente novamente em instantes"
            )


def retry(
    attempts: int = 3,
    *,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    retry_on: Iterable[type[BaseException]] = (IntegrationError,),
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Backoff exponencial com jitter para chamadas de rede.

    Só reexecuta erros marcados como retentáveis — 401 e 400 falham de imediato,
    porque repetir uma credencial inválida só queima quota.
    """
    retry_types = tuple(retry_on)

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            last: BaseException | None = None
            for attempt in range(1, attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except retry_types as exc:  # type: ignore[misc]
                    retryable = getattr(exc, "retryable", True)
                    last = exc
                    if not retryable or attempt == attempts:
                        raise
                    delay = min(max_delay, base_delay * 2 ** (attempt - 1))
                    delay = getattr(exc, "retry_after", delay)
                    delay += random.uniform(0, base_delay)
                    logger.info(
                        "retry fn=%s attempt=%s/%s delay=%.2fs err=%s",
                        fn.__name__,
                        attempt,
                        attempts,
                        delay,
                        exc,
                    )
                    sleep(delay)
            assert last is not None
            raise last

        return wrapper

    return decorator
