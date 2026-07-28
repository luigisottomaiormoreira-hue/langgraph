"""Interpretação de comando: intenção certa a partir de uma frase solta."""

from __future__ import annotations

import pytest

from hotmart_agent.models import Intent
from hotmart_agent.router import route_rules


@pytest.mark.parametrize(
    ("comando", "esperado"),
    [
        ("como estão as vendas essa semana?", Intent.ANALYZE_PERFORMANCE),
        ("o faturamento caiu, o que houve?", Intent.ANALYZE_PERFORMANCE),
        ("responde os e-mails do suporte", Intent.HANDLE_EMAILS),
        ("olha a caixa de entrada", Intent.HANDLE_EMAILS),
        ("cria uma campanha nova no meta ads", Intent.MANAGE_ADS),
        ("tem saldo na conta de anúncio?", Intent.CHECK_AD_BUDGET),
    ],
)
def test_intencao_principal(comando, esperado, settings):
    decision = route_rules(comando, settings)
    assert esperado in decision.intents
    assert decision.confidence >= 0.5


def test_comando_vago_pede_esclarecimento(settings):
    decision = route_rules("resolve isso", settings)
    assert decision.intents == [Intent.CLARIFY]
    assert decision.clarification
    assert "?" in decision.clarification


def test_comando_vazio_pede_esclarecimento(settings):
    assert route_rules("   ", settings).intents == [Intent.CLARIFY]


def test_briefing_dispara_as_tres_frentes(settings):
    decision = route_rules("me dá um resumo geral do produto", settings)
    assert Intent.ANALYZE_PERFORMANCE in decision.intents
    assert Intent.HANDLE_EMAILS in decision.intents
    assert Intent.MANAGE_ADS in decision.intents


def test_comando_com_dois_objetos_dispara_dois_modulos(settings):
    decision = route_rules("vê as vendas e as campanhas", settings)
    assert Intent.ANALYZE_PERFORMANCE in decision.intents
    assert Intent.MANAGE_ADS in decision.intents


def test_falar_de_campanha_implica_verificar_saldo(settings):
    """Nunca planejar mídia sem antes olhar se a conta pode gastar."""
    decision = route_rules("sobe uma campanha de remarketing", settings)
    assert Intent.MANAGE_ADS in decision.intents
    assert Intent.CHECK_AD_BUDGET in decision.intents


@pytest.mark.parametrize(
    ("comando", "dias"),
    [
        ("como foram as vendas hoje", 1),
        ("desempenho da semana", 7),
        ("faturamento do mês", 30),
        ("receita dos últimos 45 dias", 45),
        ("receita das últimas 2 semanas", 14),
    ],
)
def test_extracao_da_janela_temporal(comando, dias, settings):
    assert route_rules(comando, settings).params["window_days"] == dias


def test_janela_padrao_quando_nao_especificada(settings):
    decision = route_rules("como está a conversão", settings)
    assert decision.params["window_days"] == settings.analysis_window_days


def test_verbo_de_acao_marca_execucao(settings):
    assert route_rules("cria a campanha", settings).execute is True
    assert route_rules("como estão as campanhas", settings).execute is False


@pytest.mark.parametrize(
    ("comando", "modo"),
    [
        ("pausa a campanha ruim", "pause"),
        ("aumenta o orçamento da melhor campanha", "scale"),
        ("cria uma campanha nova", "create"),
    ],
)
def test_modo_de_operacao_de_midia(comando, modo, settings):
    assert route_rules(comando, settings).params["ads_mode"] == modo


def test_pergunta_desconhecida_vira_pergunta_aberta(settings):
    decision = route_rules("qual a cor do céu no ebook", settings)
    assert Intent.ANSWER_QUESTION in decision.intents
