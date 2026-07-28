"""Interpretação do comando do usuário.

Recebe uma frase solta ("como estão as vendas?", "sobe campanha nova") e devolve
as intenções a executar, os parâmetros extraídos e — quando o comando é vago
demais para agir com segurança — uma pergunta objetiva de esclarecimento.

O caminho principal é determinístico (padrões de linguagem). O LLM só é
consultado quando as regras não atingem confiança suficiente.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .llm import complete_json
from .models import Intent


def _norm(text: str) -> str:
    lowered = text.lower().strip()
    stripped = unicodedata.normalize("NFKD", lowered)
    return "".join(c for c in stripped if not unicodedata.combining(c))


# Padrões por intenção. Peso alto = expressão inequívoca.
PATTERNS: dict[Intent, list[tuple[str, float]]] = {
    Intent.ANALYZE_PERFORMANCE: [
        (r"\bvend(a|as|eu|endo)\b", 2.0),
        (r"\bfaturamento\b", 2.5),
        (r"\breceita\b", 2.0),
        (r"\bdesempenho\b", 2.5),
        (r"\bperformance\b", 2.5),
        (r"\bmetrica", 2.0),
        (r"\bconversao\b", 2.0),
        (r"\breembolso", 1.5),
        (r"\bcomo (esta|estao|foi|vai)\b", 1.5),
        (r"\bcaiu\b|\bcaindo\b|\bqueda\b", 2.0),
        (r"\banalis", 2.0),
        (r"\brelatorio\b", 2.0),
        (r"\bnumeros\b", 1.5),
    ],
    Intent.HANDLE_EMAILS: [
        (r"\be-?mails?\b", 2.5),
        (r"\bcaixa de entrada\b", 3.0),
        (r"\bsuporte\b", 2.0),
        (r"\bresponder\b", 2.0),
        (r"\bmensagens? d[eo]s? client", 2.5),
        (r"\bduvidas? d[eo]s? client", 2.0),
        (r"\binbox\b", 2.5),
        (r"\breclamac", 1.5),
    ],
    Intent.MANAGE_ADS: [
        (r"\bcampanha", 3.0),
        (r"\banunci", 2.5),
        (r"\bmeta ads\b|\bfacebook ads\b|\binstagram ads\b", 3.0),
        (r"\btrafego pago\b", 2.5),
        (r"\bimpulsion", 2.0),
        (r"\bescalar\b", 2.0),
        (r"\bpublico\b", 1.5),
        (r"\bcriativo", 1.5),
        (r"\broas\b|\bcpa\b", 2.0),
    ],
    Intent.CHECK_AD_BUDGET: [
        (r"\bsaldo\b", 3.0),
        (r"\bcredito\b", 2.5),
        (r"\borcamento\b", 2.0),
        (r"\bverba\b", 2.5),
        (r"\btem (dinheiro|grana)\b", 2.5),
        (r"\bconta d[eo] anuncio", 2.5),
    ],
    Intent.DAILY_BRIEFING: [
        (r"\bresumo\b", 2.5),
        (r"\bbriefing\b", 3.0),
        (r"\bbom dia\b", 2.0),
        (r"\bstatus geral\b", 3.0),
        (r"\bpanorama\b", 2.5),
        (r"\btudo\b.*\bproduto\b", 1.5),
        (r"\bo que (eu )?(preciso|devo) (fazer|saber)\b", 3.0),
        (r"\bcuida d[eo] (tudo|produto)\b", 3.0),
    ],
}

# Comandos vagos demais para agir sem confirmar.
AMBIGUOUS = [
    r"^\s*(ajuda|help|oi|ola|e ai|hey)\s*[!?.]*\s*$",
    r"^\s*(resolve|arruma|conserta|melhora|otimiza)\s*(isso|ai|tudo)?\s*[!?.]*\s*$",
    r"^\s*(faz|fazer)\s+(algo|alguma coisa)\s*[!?.]*\s*$",
]

WINDOW_PATTERNS = [
    (r"\bhoje\b", 1),
    (r"\bontem\b", 2),
    (r"\besta semana\b|\bsemana\b|\b7 dias\b|\bsete dias\b", 7),
    (r"\bquinzena\b|\b15 dias\b|\bquinze dias\b", 15),
    (r"\bmes\b|\b30 dias\b|\btrinta dias\b|\bmensal\b", 30),
    (r"\btrimestre\b|\b90 dias\b", 90),
]

# Verbos que indicam "faça", não "me diga".
EXECUTION_VERBS = r"\b(cria|criar|sobe|subir|lanca|lancar|ativa|ativar|pausa|pausar|responde|responder|envia|enviar|aumenta|aumentar|escala|escalar|executa|executar)\b"


@dataclass
class RoutingDecision:
    intents: list[Intent] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    execute: bool = False  # usuário pediu ação, não apenas informação
    clarification: str | None = None
    reasoning: str = ""
    router: str = "rules"

    def to_dict(self) -> dict[str, Any]:
        return {
            "intents": [i.value for i in self.intents],
            "params": self.params,
            "confidence": self.confidence,
            "execute": self.execute,
            "clarification": self.clarification,
            "reasoning": self.reasoning,
            "router": self.router,
        }


def _extract_window(text: str, default_days: int) -> int:
    explicit = re.search(
        r"\bultim[oa]s?\s+(\d{1,3})\s*(dias?|semanas?|mes(es)?)\b", text
    )
    if explicit:
        qty = int(explicit.group(1))
        unit = explicit.group(2)
        if unit.startswith("semana"):
            return qty * 7
        if unit.startswith("mes"):
            return qty * 30
        return qty
    for pattern, days in WINDOW_PATTERNS:
        if re.search(pattern, text):
            return days
    return default_days


def route_rules(command: str, settings: Settings) -> RoutingDecision:
    """Roteador determinístico: rápido, previsível e sem custo."""
    text = _norm(command)
    if not text:
        return RoutingDecision(
            intents=[Intent.CLARIFY],
            clarification="O que você precisa que eu faça com o produto agora?",
            reasoning="comando vazio",
        )

    if any(re.search(p, text) for p in AMBIGUOUS):
        return RoutingDecision(
            intents=[Intent.CLARIFY],
            confidence=0.2,
            clarification=(
                "Posso agir em três frentes: (1) analisar o desempenho das vendas, "
                "(2) responder a caixa de entrada do suporte ou (3) revisar as "
                "campanhas do Meta Ads. Qual delas você quer agora — ou quer o "
                "resumo geral das três?"
            ),
            reasoning="comando genérico sem objeto definido",
        )

    scores: dict[Intent, float] = {}
    for intent, patterns in PATTERNS.items():
        total = sum(weight for pattern, weight in patterns if re.search(pattern, text))
        if total:
            scores[intent] = total

    if not scores:
        return RoutingDecision(
            intents=[Intent.ANSWER_QUESTION],
            confidence=0.3,
            params={
                "window_days": _extract_window(text, settings.analysis_window_days)
            },
            reasoning="nenhum padrão conhecido; tratado como pergunta aberta",
        )

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_intent, top_score = ranked[0]

    # Briefing e comandos amplos disparam as três frentes.
    if top_intent is Intent.DAILY_BRIEFING:
        intents = [
            Intent.ANALYZE_PERFORMANCE,
            Intent.HANDLE_EMAILS,
            Intent.MANAGE_ADS,
        ]
    else:
        # Intenções secundárias entram se tiverem ao menos 60% do topo:
        # "vê as vendas e as campanhas" é um comando só, com dois objetos.
        intents = [i for i, s in ranked if s >= top_score * 0.6]

    # Falar de campanha implica saber o saldo antes.
    if Intent.MANAGE_ADS in intents and Intent.CHECK_AD_BUDGET not in intents:
        intents.append(Intent.CHECK_AD_BUDGET)

    confidence = min(0.97, 0.5 + min(top_score, 6.0) / 12)
    execute = bool(re.search(EXECUTION_VERBS, text))

    params: dict[str, Any] = {
        "window_days": _extract_window(text, settings.analysis_window_days)
    }
    if re.search(r"\bpausa|pausar\b", text):
        params["ads_mode"] = "pause"
    elif re.search(r"\bescala|escalar|aumenta|aumentar\b", text):
        params["ads_mode"] = "scale"
    elif re.search(r"\bcria|criar|sobe|subir|lanca|lancar\b", text):
        params["ads_mode"] = "create"

    return RoutingDecision(
        intents=intents,
        params=params,
        confidence=round(confidence, 3),
        execute=execute,
        reasoning=(
            "padrões encontrados: "
            + ", ".join(f"{i.value}({s:.1f})" for i, s in ranked[:3])
        ),
    )


ROUTER_SYSTEM = """Você roteia comandos de um operador de infoproduto (ebook na Hotmart).
Devolva SOMENTE um JSON com as chaves:
  intents: lista, subconjunto de ["analyze_performance","handle_emails","manage_ads","check_ad_budget","answer_question","clarify"]
  window_days: inteiro (janela de análise pedida; 7 se não houver indicação)
  execute: booleano (true se o usuário pediu para EXECUTAR algo, false se pediu informação)
  ads_mode: um de "create","scale","pause" ou null
  clarification: pergunta curta e objetiva, ou null se o comando estiver claro
  confidence: número entre 0 e 1
Se o comando for vago a ponto de qualquer ação ser um chute, use intents=["clarify"] e escreva a pergunta."""


def route(command: str, settings: Settings) -> RoutingDecision:
    """Roteia o comando; consulta o LLM apenas quando as regras hesitam."""
    decision = route_rules(command, settings)
    needs_llm = decision.confidence < 0.6 or Intent.ANSWER_QUESTION in decision.intents
    if not needs_llm:
        return decision

    parsed = complete_json(settings, ROUTER_SYSTEM, command)
    if not parsed:
        return decision

    try:
        intents = [
            Intent(i)
            for i in parsed.get("intents", [])
            if i in Intent._value2member_map_
        ]
    except ValueError:
        intents = []
    if not intents:
        return decision

    params = dict(decision.params)
    params["window_days"] = int(
        parsed.get("window_days") or params.get("window_days", 7)
    )
    if parsed.get("ads_mode"):
        params["ads_mode"] = parsed["ads_mode"]
    if Intent.MANAGE_ADS in intents and Intent.CHECK_AD_BUDGET not in intents:
        intents.append(Intent.CHECK_AD_BUDGET)

    return RoutingDecision(
        intents=intents,
        params=params,
        confidence=float(parsed.get("confidence") or 0.7),
        execute=bool(parsed.get("execute")),
        clarification=parsed.get("clarification"),
        reasoning="roteado pelo modelo após baixa confiança das regras",
        router="llm",
    )
