"""Log estruturado e trilha de auditoria.

Toda ação que toca o mundo externo (e-mail enviado, campanha criada, reembolso
solicitado) gera um registro de auditoria no estado do grafo. Como o estado é
persistido pelo checkpointer, a trilha sobrevive ao processo — é o que permite
responder "por que o agente fez isso?" depois.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Any

LOGGER_NAME = "hotmart_agent"


def setup_logging(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    return logger


def audit_entry(
    event: str,
    *,
    actor: str = "agent",
    detail: str = "",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Registro imutável de auditoria."""
    from .integrations.base import redact

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "actor": actor,
        "detail": detail,
        "payload": redact(payload or {}),
    }
