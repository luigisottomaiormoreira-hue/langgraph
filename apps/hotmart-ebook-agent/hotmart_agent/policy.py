"""Regras de decisão: o que priorizar e o que o agente pode fazer sozinho.

Duas perguntas, deliberadamente separadas:

- `priority_score` -> "isso importa quanto?" (ordena o backlog)
- `resolve_mode`   -> "posso executar?"      (autonomia x risco x guardrail)

Separar as duas evita o erro clássico de deixar uma ação cara ser executada sem
aprovação só porque era urgente.
"""

from __future__ import annotations

import uuid

from .config import AUTONOMY_ORDER, RISK_ORDER, Autonomy, Risk, Settings
from .formatting import brl
from .models import (
    SEVERITY_WEIGHT,
    Action,
    ActionType,
    CampaignPlan,
    Domain,
    EmailReply,
    ExecutionMode,
    Finding,
    Severity,
)

# Risco intrínseco de cada tipo de ação.
ACTION_RISK: dict[ActionType, Risk] = {
    ActionType.REPORT: Risk.NONE,
    ActionType.DRAFT_EMAIL: Risk.LOW,
    ActionType.PAUSE_CAMPAIGN: Risk.LOW,
    ActionType.ESCALATE: Risk.LOW,
    ActionType.SEND_EMAIL: Risk.MEDIUM,
    ActionType.UPDATE_BUDGET: Risk.HIGH,
    ActionType.CREATE_CAMPAIGN: Risk.HIGH,
    ActionType.ISSUE_REFUND: Risk.HIGH,
}

# Risco máximo que cada nível de autonomia executa sem humano.
AUTONOMY_MAX_RISK: dict[Autonomy, Risk] = {
    Autonomy.OBSERVE: Risk.NONE,
    Autonomy.RECOMMEND: Risk.NONE,
    Autonomy.APPROVE: Risk.LOW,
    Autonomy.AUTONOMOUS: Risk.HIGH,
}


def priority_score(finding: Finding, settings: Settings) -> float:
    """Prioridade = gravidade x impacto financeiro x confiança / esforço.

    O impacto entra em escala logarítmica comprimida (R$ 5.000 importa mais que
    R$ 500, mas não dez vezes mais) para que um único número grande não domine a
    fila inteira.
    """
    base = SEVERITY_WEIGHT.get(finding.severity, 0.2)
    reference = max(settings.product.price_brl * 10, 1.0)
    impact = (
        min(3.0, (finding.impact_brl / reference) ** 0.5) if finding.impact_brl else 0.0
    )
    effort = max(0.3, finding.effort)
    # +0.5 garante que um sinal crítico sem valor estimado ainda pontue.
    return round(base * (0.5 + impact) * finding.confidence / effort, 4)


SEVERITY_RANK = {
    Severity.CRITICAL: 0,
    Severity.WARNING: 1,
    Severity.OPPORTUNITY: 2,
    Severity.INFO: 3,
}


def prioritize(findings: list[Finding], settings: Settings) -> list[Finding]:
    """Ordena por faixa de gravidade e, dentro da faixa, por prioridade.

    A gravidade domina de propósito: uma oportunidade de R$ 5.000 não pode
    passar na frente de um risco de bloqueio do produto na Hotmart, por maior
    que seja o número. Perder receita é recuperável; perder o checkout, não.
    """
    return sorted(
        findings,
        key=lambda f: (
            SEVERITY_RANK.get(f.severity, 9),
            -priority_score(f, settings),
            -f.impact_brl,
        ),
    )


def resolve_mode(action: Action, settings: Settings) -> Action:
    """Decide entre executar, pedir aprovação, apenas recomendar ou bloquear."""
    g = settings.guardrails
    risk = ACTION_RISK.get(action.type, Risk.HIGH)
    allowed = AUTONOMY_MAX_RISK[settings.autonomy]

    if settings.autonomy is Autonomy.OBSERVE and action.type is not ActionType.REPORT:
        action.mode = ExecutionMode.RECOMMEND
        action.mode_reason = "modo observação (L0): o agente apenas relata"
        return action

    # Guardrails financeiros travam antes de qualquer consideração de autonomia.
    if action.cost_brl > g.max_daily_budget_brl:
        action.mode = ExecutionMode.BLOCKED
        action.mode_reason = (
            f"custo diário de {brl(action.cost_brl)} acima do teto de "
            f"{brl(g.max_daily_budget_brl)}"
        )
        return action

    if action.type is ActionType.ISSUE_REFUND:
        value = float(action.payload.get("value_brl", 0.0))
        if value > g.refund_auto_approve_max_brl:
            action.mode = ExecutionMode.APPROVAL
            action.mode_reason = (
                f"reembolso de {brl(value)} acima do limite automático de "
                f"{brl(g.refund_auto_approve_max_brl)}"
            )
            return action

    if RISK_ORDER[risk] <= RISK_ORDER[allowed]:
        action.mode = ExecutionMode.AUTO
        action.mode_reason = (
            f"risco {risk.value} dentro do permitido para autonomia "
            f"{settings.autonomy.value}"
        )
    elif AUTONOMY_ORDER[settings.autonomy] >= AUTONOMY_ORDER[Autonomy.APPROVE]:
        action.mode = ExecutionMode.APPROVAL
        action.mode_reason = f"risco {risk.value} exige aprovação humana"
    else:
        action.mode = ExecutionMode.RECOMMEND
        action.mode_reason = f"risco {risk.value} acima da autonomia configurada"

    if settings.dry_run and action.mode is ExecutionMode.AUTO:
        action.mode_reason += " (dry-run: nada será enviado de fato)"
    return action


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def actions_from_emails(replies: list[EmailReply], settings: Settings) -> list[Action]:
    """Converte a triagem da caixa de entrada em ações executáveis."""
    g = settings.guardrails
    actions: list[Action] = []
    auto_budget = g.max_auto_emails_per_run

    for reply in replies:
        if reply.triage.flags or reply.triage.sentiment == "hostil":
            actions.append(
                Action(
                    id=_new_id("esc"),
                    type=ActionType.ESCALATE,
                    domain=Domain.SUPPORT,
                    title=f"Escalar para humano: {reply.to}",
                    payload={
                        "email_id": reply.email_id,
                        "to": reply.to,
                        "flags": reply.triage.flags,
                        "draft": reply.body,
                    },
                    rationale=reply.reason,
                    expected_impact_brl=settings.product.contribution_margin_brl * 3,
                    reversible=True,
                    source_finding="SUPPORT_ESCALATION",
                )
            )
            continue

        if reply.auto_sendable and auto_budget > 0:
            auto_budget -= 1
            actions.append(
                Action(
                    id=_new_id("mail"),
                    type=ActionType.SEND_EMAIL,
                    domain=Domain.SUPPORT,
                    title=f"Responder '{reply.triage.category.value}' para {reply.to}",
                    payload={
                        "email_id": reply.email_id,
                        "to": reply.to,
                        "subject": reply.subject,
                        "body": reply.body,
                        "category": reply.triage.category.value,
                        "confidence": reply.triage.confidence,
                    },
                    rationale=reply.reason,
                    reversible=False,  # e-mail enviado não volta
                )
            )
        else:
            actions.append(
                Action(
                    id=_new_id("draft"),
                    type=ActionType.DRAFT_EMAIL,
                    domain=Domain.SUPPORT,
                    title=f"Rascunho para revisão — {reply.to}",
                    payload={
                        "email_id": reply.email_id,
                        "to": reply.to,
                        "subject": reply.subject,
                        "body": reply.body,
                        "category": reply.triage.category.value,
                    },
                    rationale=reply.reason
                    if not reply.auto_sendable
                    else "limite de respostas automáticas por execução atingido",
                )
            )
    return actions


def actions_from_campaigns(
    plans: list[CampaignPlan], settings: Settings
) -> list[Action]:
    """Cada plano aprovado vira uma ação de criação de campanha."""
    return [
        Action(
            id=_new_id("camp"),
            type=ActionType.CREATE_CAMPAIGN,
            domain=Domain.ADS,
            title=f"Criar campanha: {plan.name}",
            payload=plan.to_dict(),
            rationale=plan.rationale,
            expected_impact_brl=round(
                plan.daily_budget_brl * 30 * max(plan.expected_roas, 0.0), 2
            ),
            cost_brl=plan.daily_budget_brl,
            reversible=True,  # pausar é barato
            source_finding="ADS_PLAN",
        )
        for plan in plans
    ]


def actions_from_findings(findings: list[Finding], settings: Settings) -> list[Action]:
    """Sinais que sugerem ação direta sobre campanhas já em veiculação."""
    actions: list[Action] = []
    for f in findings:
        evidence = f.evidence or {}
        if "pausar_campanha" in f.suggested_actions and evidence.get("campaign_id"):
            actions.append(
                Action(
                    id=_new_id("pause"),
                    type=ActionType.PAUSE_CAMPAIGN,
                    domain=Domain.ADS,
                    title=f"Pausar campanha '{evidence.get('name', '')}'",
                    payload={"campaign_id": evidence["campaign_id"]},
                    rationale=f.detail,
                    expected_impact_brl=f.impact_brl,
                    reversible=True,
                    source_finding=f.code,
                )
            )
        elif "escalar_orcamento" in f.suggested_actions and evidence.get("campaign_id"):
            current = float(evidence.get("daily_budget_brl", 0.0))
            target = round(
                current * (1 + settings.guardrails.max_budget_increase_pct), 2
            )
            actions.append(
                Action(
                    id=_new_id("budget"),
                    type=ActionType.UPDATE_BUDGET,
                    domain=Domain.ADS,
                    title=(
                        f"Aumentar orçamento de '{evidence.get('name', '')}' para "
                        f"{brl(target)}/dia"
                    ),
                    payload={
                        "campaign_id": evidence["campaign_id"],
                        "current_brl": current,
                        "target_brl": target,
                    },
                    rationale=f.recommendation,
                    expected_impact_brl=f.impact_brl,
                    cost_brl=max(0.0, target - current),
                    reversible=True,
                    source_finding=f.code,
                )
            )
    return actions


def rank_actions(
    actions: list[Action], findings: list[Finding], settings: Settings
) -> list[Action]:
    """Prioriza ações herdando a urgência do sinal que as originou."""
    by_code = {f.code: f for f in findings}
    for action in actions:
        source = by_code.get(action.source_finding or "")
        base = priority_score(source, settings) if source else 0.3
        # Contato com cliente irritado corre na frente de otimização de mídia.
        if action.type is ActionType.ESCALATE:
            base += 1.5
        elif action.type is ActionType.SEND_EMAIL:
            base += 0.6
        roi = action.expected_impact_brl / max(action.cost_brl, 1.0)
        action.priority = round(base + min(roi / 50, 1.0), 4)
        resolve_mode(action, settings)
    return sorted(actions, key=lambda a: -a.priority)


def next_best_action(actions: list[Action], findings: list[Finding]) -> str:
    """A frase que fecha toda resposta do agente: o próximo passo concreto."""
    critical = [f for f in findings if f.severity is Severity.CRITICAL]
    if critical:
        top = critical[0]
        return f"Prioridade agora: {top.title.lower()} — {top.recommendation}"
    if actions:
        top = actions[0]
        if top.mode is ExecutionMode.APPROVAL:
            return f"Aguardando sua aprovação para: {top.title}."
        if top.mode is ExecutionMode.AUTO:
            return f"Executando em seguida: {top.title}."
        return f"Recomendação: {top.title} — {top.rationale}"
    opportunities = [f for f in findings if f.severity is Severity.OPPORTUNITY]
    if opportunities:
        return f"Sem urgências. Melhor uso do tempo: {opportunities[0].recommendation}"
    return "Nenhuma ação pendente. Reavaliar no próximo ciclo."
