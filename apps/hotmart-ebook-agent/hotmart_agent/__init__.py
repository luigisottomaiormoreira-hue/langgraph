"""Agente de operação para um ebook vendido na Hotmart.

Um comando em linguagem natural entra; análise de desempenho, atendimento e
gestão de mídia paga saem — executados ou recomendados conforme o nível de
autonomia configurado.

    from hotmart_agent import Settings, build_graph

    graph = build_graph(Settings.from_env())
    state = graph.invoke(
        {"command": "como estão as vendas essa semana?"},
        config={"configurable": {"thread_id": "op-1"}},
    )
    print(state["report"])
"""

from .config import Autonomy, Guardrails, Product, Settings, Thresholds
from .graph import Clients, build_graph
from .models import Action, ActionType, ExecutionMode, Finding, Intent, Severity
from .router import route

__all__ = [
    "Action",
    "ActionType",
    "Autonomy",
    "Clients",
    "ExecutionMode",
    "Finding",
    "Guardrails",
    "Intent",
    "Product",
    "Settings",
    "Severity",
    "Thresholds",
    "build_graph",
    "route",
]

__version__ = "0.1.0"
