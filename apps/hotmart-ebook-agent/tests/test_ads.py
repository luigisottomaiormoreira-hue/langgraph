"""Mídia paga: verificar a conta antes de gastar, e gastar dentro do limite."""

from __future__ import annotations

from dataclasses import replace

from hotmart_agent.models import AdAccountStatus, CampaignPerformance, Severity
from hotmart_agent.modules import ads

from .conftest import make_snapshot, make_window, source


def conta(**kwargs) -> AdAccountStatus:
    base = dict(
        account_id="act_1",
        account_status=1,
        disable_reason=0,
        prepaid_balance_brl=500.0,
        spend_cap_brl=0.0,
        amount_spent_brl=1000.0,
        has_valid_funding=True,
    )
    base.update(kwargs)
    return AdAccountStatus(**base)


def campanha(**kwargs) -> CampaignPerformance:
    base = dict(
        campaign_id="1",
        name="Teste",
        status="ACTIVE",
        objective="OUTCOME_SALES",
        daily_budget_brl=50.0,
        spend_brl=350.0,
        impressions=20000,
        clicks=600,
        purchases=10,
        revenue_brl=970.0,
    )
    base.update(kwargs)
    return CampaignPerformance(**base)


# ---------------------------------------------------------------- saldo


def test_conta_desativada_bloqueia_tudo(settings):
    status = conta(account_status=2, disable_reason=2)
    findings = ads.check_funding(status, settings)
    assert findings[0].code == "AD_ACCOUNT_DISABLED"
    assert findings[0].severity is Severity.CRITICAL

    catalogo = ads.build_campaign_catalog(None, [], settings)
    aprovados, notas = ads.select_campaigns(catalogo, status, settings)
    assert aprovados == []
    assert "inativa" in notas[0]


def test_saldo_baixo_gera_alerta(settings):
    findings = ads.check_funding(conta(prepaid_balance_brl=20.0), settings)
    assert findings[0].code == "AD_LOW_BALANCE"


def test_conta_sem_meio_de_pagamento(settings):
    status = conta(prepaid_balance_brl=0.0, has_valid_funding=False)
    assert ads.check_funding(status, settings)[0].code == "AD_NO_FUNDING"


def test_conta_saudavel_nao_gera_alerta(settings):
    assert ads.check_funding(conta(), settings) == []


def test_saldo_disponivel_respeita_o_teto_de_gasto():
    status = conta(
        prepaid_balance_brl=0.0, spend_cap_brl=1200.0, amount_spent_brl=1000.0
    )
    assert status.available_brl == 200.0


def test_pos_pago_sem_teto_nao_limita_orcamento():
    status = conta(prepaid_balance_brl=0.0, spend_cap_brl=0.0)
    assert status.available_brl == float("inf")


# ---------------------------------------------------------------- diagnóstico


def test_campanha_sem_conversao_e_critica(settings):
    findings = ads.diagnose_campaigns(
        [campanha(purchases=0, revenue_brl=0.0)], settings
    )
    assert findings[0].code == "CAMPAIGN_NO_CONVERSION"
    assert findings[0].severity is Severity.CRITICAL


def test_campanha_com_roas_ruim_e_critica(settings):
    findings = ads.diagnose_campaigns(
        [campanha(spend_brl=1000.0, purchases=5, revenue_brl=485.0)], settings
    )
    assert any(f.code == "CAMPAIGN_ROAS_CRITICAL" for f in findings)


def test_campanha_boa_vira_oportunidade_de_escala(settings):
    findings = ads.diagnose_campaigns(
        [campanha(spend_brl=200.0, purchases=10, revenue_brl=970.0)], settings
    )
    escala = next(f for f in findings if f.code == "CAMPAIGN_SCALABLE")
    assert escala.severity is Severity.OPPORTUNITY
    assert escala.evidence["campaign_id"] == "1"  # permite virar ação direta


def test_campanha_sem_gasto_minimo_nao_e_julgada(settings):
    """Julgar campanha com R$ 20 gastos é ler ruído."""
    assert (
        ads.diagnose_campaigns([campanha(spend_brl=20.0, purchases=0)], settings) == []
    )


def test_campanha_pausada_e_ignorada(settings):
    assert (
        ads.diagnose_campaigns([campanha(status="PAUSED", purchases=0)], settings) == []
    )


def test_objetivo_errado_e_apontado(settings):
    findings = ads.diagnose_campaigns(
        [
            campanha(
                objective="OUTCOME_TRAFFIC", clicks=5000, purchases=1, revenue_brl=97.0
            )
        ],
        settings,
    )
    assert any(f.code == "CAMPAIGN_WRONG_OBJECTIVE" for f in findings)


# ---------------------------------------------------------------- planejamento


def test_abandono_alto_prioriza_remarketing(settings):
    snapshot = make_snapshot(current=make_window(units=50, checkout_sessions=1000))
    catalogo = ads.build_campaign_catalog(snapshot, [], settings)
    assert catalogo[0].blueprint == "retarget_checkout_7d"


def test_cpa_caro_promove_captura_de_leads(settings):
    """Quando a venda direta fica cara, o caminho é vender pela lista."""
    snapshot = make_snapshot(current=make_window(units=100, checkout_sessions=130))

    def score_da_isca(campanhas):
        catalogo = ads.build_campaign_catalog(snapshot, campanhas, settings)
        return next(p for p in catalogo if p.blueprint == "lead_magnet_capture").score

    barato = score_da_isca([campanha(spend_brl=200.0, purchases=10, revenue_brl=970.0)])
    caro = score_da_isca([campanha(spend_brl=900.0, purchases=10, revenue_brl=970.0)])
    assert caro > barato

    catalogo = ads.build_campaign_catalog(
        snapshot, [campanha(spend_brl=900.0, purchases=10, revenue_brl=970.0)], settings
    )
    assert [p.blueprint for p in catalogo].index("lead_magnet_capture") <= 2


def test_selecao_respeita_o_saldo_da_conta(settings):
    """Cada plano precisa de 3 dias de veiculação; sem isso, fica de fora."""
    catalogo = ads.build_campaign_catalog(make_snapshot(), [], settings)
    aprovados, notas = ads.select_campaigns(
        catalogo, conta(prepaid_balance_brl=60.0), settings
    )
    assert len(aprovados) <= 1
    assert any("disponíveis" in n for n in notas)


def test_selecao_respeita_o_teto_de_campanhas_por_execucao(settings):
    catalogo = ads.build_campaign_catalog(make_snapshot(), [], settings)
    aprovados, _ = ads.select_campaigns(
        catalogo, conta(prepaid_balance_brl=100000.0), settings
    )
    assert len(aprovados) == settings.guardrails.max_new_campaigns_per_run


def test_orcamento_e_cortado_no_teto_diario(settings):
    apertado = settings.with_(
        guardrails=replace(settings.guardrails, max_daily_budget_brl=10.0)
    )
    catalogo = ads.build_campaign_catalog(make_snapshot(), [], apertado)
    aprovados, _ = ads.select_campaigns(
        catalogo, conta(prepaid_balance_brl=100000.0), apertado
    )
    assert all(p.daily_budget_brl <= 10.0 for p in aprovados)


def test_campanhas_novas_nascem_pausadas(settings):
    catalogo = ads.build_campaign_catalog(make_snapshot(), [], settings)
    assert all(p.start_paused for p in catalogo)


def test_plano_usa_o_canal_organico_que_mais_converte(settings):
    current = make_window(
        by_source={"email-lista": source("email-lista", units=30, revenue_brl=2910.0)}
    )
    catalogo = ads.build_campaign_catalog(make_snapshot(current=current), [], settings)
    lookalike = next(p for p in catalogo if p.blueprint == "lookalike_purchasers_1")
    assert "email-lista" in lookalike.creative_angle
