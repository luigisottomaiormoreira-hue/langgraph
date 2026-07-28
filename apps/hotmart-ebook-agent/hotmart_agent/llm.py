"""Camada de LLM — opcional por design.

O agente funciona inteiro sem modelo: roteamento, análise e respostas têm
caminho determinístico. O LLM entra para (a) desambiguar comandos livres e
(b) melhorar a redação de e-mails e do relatório. Se ele não estiver
disponível, ou falhar, o agente degrada para as regras e registra o motivo —
nunca inventa número nem envia resposta sem base.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .config import Settings

logger = logging.getLogger(__name__)

_UNAVAILABLE_LOGGED = False


def get_model(settings: Settings) -> Any | None:
    """Instancia o modelo de chat, ou devolve None se não houver como."""
    global _UNAVAILABLE_LOGGED
    try:
        from langchain.chat_models import init_chat_model
    except ImportError:
        try:
            from langchain_core.language_models import (  # type: ignore[attr-defined]
                init_chat_model,
            )
        except ImportError:
            if not _UNAVAILABLE_LOGGED:
                logger.info("llm indisponível: usando caminho determinístico")
                _UNAVAILABLE_LOGGED = True
            return None
    try:
        return init_chat_model(settings.model, temperature=0)
    except Exception as exc:  # credencial ausente, provider desconhecido...
        if not _UNAVAILABLE_LOGGED:
            logger.info("llm indisponível (%s): usando caminho determinístico", exc)
            _UNAVAILABLE_LOGGED = True
        return None


def complete_json(
    settings: Settings, system: str, user: str, *, timeout_note: str = ""
) -> dict[str, Any] | None:
    """Pede JSON ao modelo e devolve None em qualquer falha.

    Nenhuma exceção de LLM sobe para o grafo: a ausência de resposta é sempre
    tratada como "use a regra".
    """
    model = get_model(settings)
    if model is None:
        return None
    try:
        response = model.invoke(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
        )
        content = getattr(response, "content", "")
        if isinstance(content, list):  # blocos de conteúdo
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        content = str(content).strip()
        if content.startswith("```"):
            content = content.strip("`")
            content = content.split("\n", 1)[-1] if "\n" in content else content
        start, end = content.find("{"), content.rfind("}")
        if start == -1 or end == -1:
            return None
        parsed = json.loads(content[start : end + 1])
        return parsed if isinstance(parsed, dict) else None
    except Exception as exc:
        logger.warning("llm falhou (%s)%s; caindo para regras", exc, timeout_note)
        return None


def polish_text(settings: Settings, instruction: str, draft: str) -> str:
    """Melhora a redação mantendo o conteúdo. Em falha, devolve o original."""
    model = get_model(settings)
    if model is None:
        return draft
    try:
        response = model.invoke(
            [
                {
                    "role": "system",
                    "content": (
                        "Você revisa textos de suporte de um infoproduto. Tom: "
                        f"{settings.product.tone}. Não invente fatos, políticas, "
                        "prazos ou valores que não estejam no rascunho. Não use "
                        "emojis. Mantenha a estrutura e devolva apenas o texto final."
                    ),
                },
                {"role": "user", "content": f"{instruction}\n\n---\n{draft}"},
            ]
        )
        content = getattr(response, "content", "")
        text = content if isinstance(content, str) else draft
        return text.strip() or draft
    except Exception as exc:
        logger.warning("revisão por llm falhou (%s); mantendo rascunho", exc)
        return draft
