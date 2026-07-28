"""Regras de decisão: prioridade correta e autonomia que não passa do limite."""

from __future__ import annotations

from dataclasses import replace

from hotmart_agent.config import Autonomy
from hotmart_agent.models import (
    Action,
    ActionType,
    Domain,
    ExecutionMode,
    Finding,
    Severity,
)
from hotmart_agent.policy import (
    next_best_action,
    prioritize,
    priority_score,
    rank_actions,
    resolve_mode,
)


def finding(code="X", severity=Severity.WARNING, impact=1000.0, effort=1.0, conf=0.9):
    return Finding(
        code=code,
        domain=Domain.ANALYTICS,
        severity=severity,
        title=code,
        detail="",
        impact_brl=impact,
        effort=effort,
        confidence=conf,
    )


def action(atype=ActionType.REPORT, cost=0.0, impact=0.0, **kwargs):
    return Action(
        id=kwargs.pop("id", "a1"),
        type=atype,
        domain=Domain.ADS,
        title="ação",
        cost_brl=cost,
        expected_impact_brl=impact,
        **kwargs,
    )


# ---------------------------------------------------------------- prioridade


def test_critico_vem_antes_de_oportunidade(settings):
    ordenado = prioritize(
        [
            finding("OPP", Severity.OPPORTUNITY, impact=5000.0),
            finding("CRIT", Severity.CRITICAL, impact=500.0),
        ],
        settings,
    )
    assert ordenado[0].code == "CRIT"


def test_esforco_menor_ganha_no_empate(settings):
    barato = finding("BARATO", effort=0.5)
    caro = finding("CARO", effort=3.0)
    assert priority_score(barato, settings) > priority_score(caro, settings)


def test_impacto_maior_ganha_no_empate(settings):
    assert priority_score(finding(impact=10000.0), settings) > priority_score(
        finding(impact=100.0), settings
    )


def test_confianca_baixa_reduz_prioridade(settings):
    assert priority_score(finding(conf=0.4), settings) < priority_score(
        finding(conf=1.0), settings
    )


def test_sinal_critico_sem_valor_estimado_ainda_pontua(settings):
    assert priority_score(finding("C", Severity.CRITICAL, impact=0.0), settings) > 0


# ---------------------------------------------------------------- autonomia


def test_modo_observacao_nunca_executa(settings):
    s = settings.with_(autonomy=Autonomy.OBSERVE)
    a = resolve_mode(action(ActionType.SEND_EMAIL), s)
    assert a.mode is ExecutionMode.RECOMMEND


def test_modo_aprovacao_executa_baixo_risco_sozinho(settings):
    s = settings.with_(autonomy=Autonomy.APPROVE)
    assert resolve_mode(action(ActionType.DRAFT_EMAIL), s).mode is ExecutionMode.AUTO
    assert resolve_mode(action(ActionType.PAUSE_CAMPAIGN), s).mode is ExecutionMode.AUTO


def test_modo_aprovacao_pede_confirmacao_para_gasto(settings):
    s = settings.with_(autonomy=Autonomy.APPROVE)
    a = resolve_mode(action(ActionType.CREATE_CAMPAIGN, cost=50.0), s)
    assert a.mode is ExecutionMode.APPROVAL
    assert "aprovação humana" in a.mode_reason


def test_modo_autonomo_executa_gasto_dentro_do_limite(settings):
    s = settings.with_(autonomy=Autonomy.AUTONOMOUS)
    assert (
        resolve_mode(action(ActionType.CREATE_CAMPAIGN, cost=50.0), s).mode
        is ExecutionMode.AUTO
    )


def test_guardrail_financeiro_bloqueia_ate_no_modo_autonomo(settings):
    """Autonomia total não é permissão para estourar o orçamento."""
    s = settings.with_(autonomy=Autonomy.AUTONOMOUS)
    a = resolve_mode(action(ActionType.CREATE_CAMPAIGN, cost=10_000.0), s)
    assert a.mode is ExecutionMode.BLOCKED
    assert "teto" in a.mode_reason


def test_reembolso_alto_sempre_pede_aprovacao(settings):
    s = settings.with_(autonomy=Autonomy.AUTONOMOUS)
    a = action(ActionType.ISSUE_REFUND)
    a.payload = {"value_brl": 5000.0}
    assert resolve_mode(a, s).mode is ExecutionMode.APPROVAL


def test_reembolso_dentro_do_limite_passa_no_modo_autonomo(settings):
    s = settings.with_(autonomy=Autonomy.AUTONOMOUS)
    a = action(ActionType.ISSUE_REFUND)
    a.payload = {"value_brl": 97.0}
    assert resolve_mode(a, s).mode is ExecutionMode.AUTO


def test_envio_de_email_exige_aprovacao_em_l2(settings):
    """E-mail enviado não volta: risco médio não passa em L2."""
    s = settings.with_(autonomy=Autonomy.APPROVE)
    assert resolve_mode(action(ActionType.SEND_EMAIL), s).mode is ExecutionMode.APPROVAL


def test_dry_run_e_sinalizado_no_motivo(settings):
    s = settings.with_(autonomy=Autonomy.APPROVE, dry_run=True)
    assert "dry-run" in resolve_mode(action(ActionType.DRAFT_EMAIL), s).mode_reason


def test_limite_customizado_de_reembolso(settings):
    s = settings.with_(
        autonomy=Autonomy.AUTONOMOUS,
        guardrails=replace(settings.guardrails, refund_auto_approve_max_brl=50.0),
    )
    a = action(ActionType.ISSUE_REFUND)
    a.payload = {"value_brl": 97.0}
    assert resolve_mode(a, s).mode is ExecutionMode.APPROVAL


# ---------------------------------------------------------------- ordenação


def test_escalonamento_de_cliente_vem_na_frente(settings):
    acoes = rank_actions(
        [
            action(ActionType.CREATE_CAMPAIGN, cost=50.0, impact=5000.0, id="camp"),
            action(ActionType.ESCALATE, id="esc"),
        ],
        [],
        settings,
    )
    assert acoes[0].id == "esc"


def test_proximo_passo_aponta_o_critico(settings):
    passo = next_best_action([], [finding("CRIT", Severity.CRITICAL)])
    assert "Prioridade agora" in passo


def test_proximo_passo_sem_nada_pendente(settings):
    assert "Nenhuma ação pendente" in next_best_action([], [])


def test_proximo_passo_menciona_aprovacao_pendente(settings):
    a = resolve_mode(action(ActionType.CREATE_CAMPAIGN, cost=50.0), settings)
    assert "aprovação" in next_best_action([a], [])
