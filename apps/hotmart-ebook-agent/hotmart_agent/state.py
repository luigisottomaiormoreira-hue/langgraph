"""Estado do grafo.

Tudo no estado é JSON-serializável (dicts, listas, primitivos) para que o
checkpointer possa persistir e retomar a execução — inclusive depois de uma
pausa para aprovação humana que pode durar horas.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any

from typing_extensions import TypedDict


def _merge_dicts(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {**left, **right}


class AgentState(TypedDict, total=False):
    """Contrato de estado entre os nós."""

    # entrada
    command: str
    history: Annotated[list[dict[str, str]], operator.add]

    # interpretação
    routing: dict[str, Any]
    clarification: str | None
    clarified: bool

    # coleta e análise (escritos em paralelo — daí os reducers)
    snapshot: dict[str, Any]
    metrics: list[dict[str, Any]]
    findings: Annotated[list[dict[str, Any]], operator.add]
    ad_account: dict[str, Any]
    campaigns: list[dict[str, Any]]
    campaign_plans: list[dict[str, Any]]
    plan_notes: Annotated[list[str], operator.add]
    email_replies: list[dict[str, Any]]

    # decisão e execução
    actions: list[dict[str, Any]]
    approvals: Annotated[dict[str, str], _merge_dicts]
    results: Annotated[list[dict[str, Any]], operator.add]

    # saída
    report: str
    next_step: str

    # operação
    degraded: Annotated[list[str], operator.add]
    audit: Annotated[list[dict[str, Any]], operator.add]
