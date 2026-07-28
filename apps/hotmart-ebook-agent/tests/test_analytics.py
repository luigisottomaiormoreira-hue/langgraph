"""Análise de desempenho: os números viram sinais certos, sem falso alarme."""

from __future__ import annotations

from hotmart_agent.models import Severity
from hotmart_agent.modules import analytics

from .conftest import make_snapshot, make_window, source


def codes(findings) -> set[str]:
    return {f.code for f in findings}


def test_produto_saudavel_nao_gera_alerta(settings):
    findings = analytics.analyze(make_snapshot(), settings)
    assert codes(findings) == {"ALL_CLEAR"}


def test_taxa_de_reembolso_alta_vira_alerta_critico(settings):
    snapshot = make_snapshot(current=make_window(units=100, refunds=13))
    findings = analytics.analyze(snapshot, settings)
    refund = next(f for f in findings if f.code == "REFUND_RATE_HIGH")
    assert refund.severity is Severity.CRITICAL
    assert refund.impact_brl > 0
    assert "13" in refund.detail


def test_reembolso_precoce_muda_o_diagnostico(settings):
    early = analytics.analyze(
        make_snapshot(current=make_window(units=100, refunds=10, refunds_within_48h=9)),
        settings,
    )
    late = analytics.analyze(
        make_snapshot(current=make_window(units=100, refunds=10, refunds_within_48h=0)),
        settings,
    )
    early_detail = next(f for f in early if f.code == "REFUND_RATE_HIGH").detail
    late_detail = next(f for f in late if f.code == "REFUND_RATE_HIGH").detail
    assert "48h" in early_detail
    assert "abaixo da promessa" in late_detail


def test_volume_baixo_rebaixa_severidade(settings):
    """2 reembolsos em 8 vendas não é uma crise de 25% — é amostra pequena."""
    snapshot = make_snapshot(
        current=make_window(units=8, refunds=2, gross_revenue_brl=776.0)
    )
    findings = analytics.analyze(snapshot, settings)
    refund = next(f for f in findings if f.code == "REFUND_RATE_HIGH")
    assert refund.severity is Severity.INFO


def test_queda_de_conversao_detectada(settings):
    snapshot = make_snapshot(
        current=make_window(units=50, checkout_sessions=500),
        previous=make_window(units=100, checkout_sessions=400),
    )
    findings = analytics.analyze(snapshot, settings)
    conv = next(f for f in findings if f.code == "CONVERSION_DROP")
    assert conv.severity is Severity.CRITICAL
    assert conv.evidence["delta_pct"] < -0.4


def test_receita_zerada_gera_critico(settings):
    snapshot = make_snapshot(
        current=make_window(units=0, gross_revenue_brl=0.0, checkout_sessions=0)
    )
    findings = analytics.analyze(snapshot, settings)
    assert "NO_SALES" in codes(findings)


def test_canal_pago_vencedor_vira_oportunidade(settings):
    current = make_window(
        by_source={
            "meta-ads": source(
                "meta-ads", units=40, revenue_brl=3880.0, ad_spend_brl=800.0
            ),
        }
    )
    findings = analytics.analyze(make_snapshot(current=current), settings)
    scale = next(f for f in findings if f.code == "SCALE_WINNING_SOURCE")
    assert scale.severity is Severity.OPPORTUNITY
    assert scale.evidence["roas"] > 2


def test_boletos_pendentes_viram_oportunidade_de_recuperacao(settings):
    snapshot = make_snapshot(current=make_window(units=50, pending_billets=20))
    findings = analytics.analyze(snapshot, settings)
    pending = next(f for f in findings if f.code == "PENDING_PAYMENT_RECOVERY")
    assert pending.impact_brl > 0


def test_dependencia_de_afiliado(settings):
    snapshot = make_snapshot(
        current=make_window(gross_revenue_brl=10000.0, top_affiliate_revenue_brl=6000.0)
    )
    assert "AFFILIATE_CONCENTRATION" in codes(analytics.analyze(snapshot, settings))


def test_tendencia_separa_queda_de_ruido():
    queda = analytics.detect_trend(
        make_window(daily_revenue_brl=[2000, 1700, 1500, 1200, 900, 700, 400])
    )
    ruido = analytics.detect_trend(
        make_window(daily_revenue_brl=[1400, 900, 1900, 1000, 1800, 1100, 1500])
    )
    assert queda["shape"] == "queda"
    assert queda["slope_pct_per_day"] < -0.1
    assert ruido["shape"] == "estável"
    assert ruido["volatility"] > queda["volatility"] * 0.3


def test_metricas_comparam_com_janela_anterior(settings):
    snapshot = make_snapshot(
        current=make_window(gross_revenue_brl=8000.0),
        previous=make_window(gross_revenue_brl=10000.0),
    )
    metrics = {m.key: m for m in analytics.compute_metrics(snapshot)}
    receita = metrics["gross_revenue"]
    assert receita.delta_pct == -0.2
    assert receita.direction == "down"
    assert receita.is_bad_move is True
    # Reembolso subindo também é ruim, apesar do delta positivo.
    assert metrics["refund_rate"].good_when == "down"


def test_resumo_textual_tem_os_numeros(settings):
    snapshot = make_snapshot()
    resumo = analytics.summarize(snapshot, analytics.compute_metrics(snapshot))
    assert "R$ 9.700,00" in resumo
    assert "100 vendas" in resumo
