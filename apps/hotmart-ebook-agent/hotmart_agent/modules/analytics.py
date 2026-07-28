"""Análise de desempenho do ebook na Hotmart.

Toda a lógica aqui é determinística e pura: recebe um `SalesSnapshot`, devolve
KPIs e `Finding`s. Isso é proposital — números não devem depender de amostragem
de LLM. O modelo entra depois, só para narrar o resultado.
"""

from __future__ import annotations

from statistics import mean, pstdev

from ..config import Settings
from ..formatting import brl, pct, signed_pct, times
from ..models import (
    Domain,
    Finding,
    Metric,
    SalesSnapshot,
    SalesWindow,
    Severity,
)


def _safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def _delta_pct(current: float, previous: float) -> float | None:
    if previous == 0:
        return None
    return (current - previous) / previous


def _direction(delta: float | None) -> str:
    if delta is None or abs(delta) < 0.02:
        return "flat"
    return "up" if delta > 0 else "down"


def _metric(
    key: str,
    label: str,
    cur: float,
    prev: float,
    unit: str = "",
    good_when: str = "up",
) -> Metric:
    delta = _delta_pct(cur, prev)
    return Metric(
        key=key,
        label=label,
        value=round(cur, 4),
        unit=unit,
        previous=round(prev, 4),
        delta_pct=None if delta is None else round(delta, 4),
        direction=_direction(delta),
        good_when=good_when,
    )


def compute_metrics(snapshot: SalesSnapshot) -> list[Metric]:
    """KPIs do produto, sempre comparados com a janela anterior."""
    cur, prev = snapshot.current, snapshot.previous
    return [
        _metric(
            "gross_revenue",
            "Receita bruta",
            cur.gross_revenue_brl,
            prev.gross_revenue_brl,
            "BRL",
        ),
        _metric("units", "Vendas aprovadas", cur.units, prev.units, "un"),
        _metric(
            "aov",
            "Ticket médio",
            _safe_div(cur.gross_revenue_brl, cur.units),
            _safe_div(prev.gross_revenue_brl, prev.units),
            "BRL",
        ),
        _metric(
            "checkout_conversion",
            "Conversão do checkout",
            _safe_div(cur.units, cur.checkout_sessions),
            _safe_div(prev.units, prev.checkout_sessions),
            "%",
        ),
        _metric(
            "page_conversion",
            "Conversão da página de vendas",
            _safe_div(cur.units, cur.page_views),
            _safe_div(prev.units, prev.page_views),
            "%",
        ),
        _metric(
            "refund_rate",
            "Taxa de reembolso",
            _safe_div(cur.refunds, cur.units),
            _safe_div(prev.refunds, prev.units),
            "%",
            good_when="down",
        ),
        _metric(
            "chargeback_rate",
            "Taxa de chargeback",
            _safe_div(cur.chargebacks, cur.units),
            _safe_div(prev.chargebacks, prev.units),
            "%",
            good_when="down",
        ),
        _metric(
            "pending_billets",
            "Boletos/PIX aguardando pagamento",
            cur.pending_billets,
            prev.pending_billets,
            "un",
            good_when="down",
        ),
        _metric("traffic", "Visitas na página", cur.page_views, prev.page_views, "un"),
        _metric(
            "ad_spend",
            "Investimento em mídia",
            sum(s.ad_spend_brl for s in cur.by_source.values()),
            sum(s.ad_spend_brl for s in prev.by_source.values()),
            "BRL",
            good_when="down",
        ),
        _metric(
            "roas",
            "ROAS geral",
            _safe_div(
                cur.gross_revenue_brl,
                sum(s.ad_spend_brl for s in cur.by_source.values()),
            ),
            _safe_div(
                prev.gross_revenue_brl,
                sum(s.ad_spend_brl for s in prev.by_source.values()),
            ),
            "x",
        ),
    ]


def detect_trend(window: SalesWindow) -> dict[str, float | str]:
    """Tendência intra-janela: inclinação da receita diária e volatilidade.

    Uma queda de 20% na semana pode ser ruído; uma inclinação negativa
    consistente dia após dia é sintoma. Separar as duas coisas evita reagir a
    barulho.
    """
    series = window.daily_revenue_brl
    if len(series) < 3:
        return {"slope_pct_per_day": 0.0, "volatility": 0.0, "shape": "insuficiente"}
    n = len(series)
    xs = list(range(n))
    x_mean, y_mean = mean(xs), mean(series)
    denom = sum((x - x_mean) ** 2 for x in xs) or 1.0
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, series)) / denom
    slope_pct = slope / y_mean if y_mean else 0.0
    vol = _safe_div(pstdev(series), y_mean)
    if slope_pct > 0.05:
        shape = "crescimento"
    elif slope_pct < -0.05:
        shape = "queda"
    else:
        shape = "estável"
    return {
        "slope_pct_per_day": round(slope_pct, 4),
        "volatility": round(vol, 4),
        "shape": shape,
    }


def analyze(snapshot: SalesSnapshot, settings: Settings) -> list[Finding]:
    """Traduz números em sinais acionáveis (alertas, riscos e oportunidades)."""
    t = settings.thresholds
    margin = settings.product.contribution_margin_brl
    cur, prev = snapshot.current, snapshot.previous
    findings: list[Finding] = []
    low_volume = cur.units < t.min_units_for_stats

    # Com pouca amostra, todo sinal vira informativo: 2 reembolsos em 8 vendas
    # não são uma "crise de 25%".
    def sev(level: Severity) -> Severity:
        return Severity.INFO if low_volume and level != Severity.OPPORTUNITY else level

    # ---------------- reembolso ----------------
    refund_rate = _safe_div(cur.refunds, cur.units)
    if refund_rate >= t.refund_rate_warn:
        critical = refund_rate >= t.refund_rate_critical
        early = _safe_div(cur.refunds_within_48h, max(cur.refunds, 1))
        cause = (
            "maioria dos pedidos ocorre nas primeiras 48h, o que aponta para "
            "expectativa desalinhada na página de vendas ou falha na entrega"
            if early >= 0.5
            else "pedidos distribuídos ao longo da garantia, o que aponta para "
            "conteúdo abaixo da promessa"
        )
        findings.append(
            Finding(
                code="REFUND_RATE_HIGH",
                domain=Domain.ANALYTICS,
                severity=sev(Severity.CRITICAL if critical else Severity.WARNING),
                title=f"Taxa de reembolso em {pct(refund_rate, 1)}",
                detail=(
                    f"{cur.refunds} reembolsos em {cur.units} vendas. {cause.capitalize()}. "
                    "Acima de 10% a Hotmart passa a monitorar o produto e pode "
                    "restringir o checkout."
                ),
                evidence={
                    "refunds": cur.refunds,
                    "units": cur.units,
                    "rate": round(refund_rate, 4),
                    "within_48h": cur.refunds_within_48h,
                    "previous_rate": round(_safe_div(prev.refunds, prev.units), 4),
                },
                recommendation=(
                    "Revisar as 3 primeiras telas da página de vendas e o e-mail de "
                    "boas-vindas; incluir um vídeo de 60s de onboarding no Club."
                ),
                impact_brl=round(cur.refunds * margin * 4, 2),  # projeção mensal
                effort=1.5,
                suggested_actions=["auditar_pagina_vendas", "melhorar_onboarding"],
            )
        )

    # ---------------- chargeback ----------------
    cb_rate = _safe_div(cur.chargebacks, cur.units)
    if cb_rate >= t.chargeback_rate_warn:
        findings.append(
            Finding(
                code="CHARGEBACK_RATE_HIGH",
                domain=Domain.ANALYTICS,
                severity=sev(
                    Severity.CRITICAL
                    if cb_rate >= t.chargeback_rate_critical
                    else Severity.WARNING
                ),
                title=f"Chargeback em {pct(cb_rate, 2)}",
                detail=(
                    f"{cur.chargebacks} contestações. Acima de 1% há risco real de "
                    "bloqueio do meio de pagamento pela adquirente."
                ),
                evidence={"chargebacks": cur.chargebacks, "units": cur.units},
                recommendation=(
                    "Responder solicitações de reembolso em até 24h — reembolso "
                    "voluntário custa menos que contestação — e revisar o nome que "
                    "aparece na fatura do cartão."
                ),
                impact_brl=round(cur.chargebacks * margin * 4, 2),
                effort=1.0,
                suggested_actions=["priorizar_fila_reembolso"],
            )
        )

    # ---------------- conversão ----------------
    conv_cur = _safe_div(cur.units, cur.checkout_sessions)
    conv_prev = _safe_div(prev.units, prev.checkout_sessions)
    conv_delta = _delta_pct(conv_cur, conv_prev)
    if conv_delta is not None and conv_delta <= -t.conversion_drop_warn:
        critical = conv_delta <= -t.conversion_drop_critical
        findings.append(
            Finding(
                code="CONVERSION_DROP",
                domain=Domain.ANALYTICS,
                severity=sev(Severity.CRITICAL if critical else Severity.WARNING),
                title=f"Conversão do checkout caiu {pct(abs(conv_delta), 0)}",
                detail=(
                    f"De {pct(conv_prev, 2)} para {pct(conv_cur, 2)}. Verificar nesta ordem: "
                    "checkout no ar, oferta/preço alterados, mudança de criativo e "
                    "qualidade do tráfego de origem."
                ),
                evidence={
                    "current": round(conv_cur, 4),
                    "previous": round(conv_prev, 4),
                    "delta_pct": round(conv_delta, 4),
                },
                recommendation=(
                    "Testar o fluxo de compra ponta a ponta e comparar a conversão "
                    "por origem antes de mexer no investimento."
                ),
                impact_brl=round(
                    max(0.0, (conv_prev - conv_cur) * cur.checkout_sessions * margin)
                    * 4,
                    2,
                ),
                effort=1.0,
                suggested_actions=["testar_checkout", "comparar_por_origem"],
            )
        )

    # ---------------- receita e tráfego ----------------
    rev_delta = _delta_pct(cur.gross_revenue_brl, prev.gross_revenue_brl)
    if rev_delta is not None and rev_delta <= -t.revenue_drop_warn:
        trend = detect_trend(cur)
        findings.append(
            Finding(
                code="REVENUE_DROP",
                domain=Domain.ANALYTICS,
                severity=sev(
                    Severity.CRITICAL
                    if rev_delta <= -t.revenue_drop_critical
                    else Severity.WARNING
                ),
                title=f"Receita caiu {pct(abs(rev_delta), 0)} na janela",
                detail=(
                    f"{brl(cur.gross_revenue_brl)} contra {brl(prev.gross_revenue_brl)}. "
                    f"Tendência interna da janela: {trend['shape']} "
                    f"({signed_pct(trend['slope_pct_per_day'], 1)} ao dia)."
                ),
                evidence={
                    "current_brl": cur.gross_revenue_brl,
                    "previous_brl": prev.gross_revenue_brl,
                    "trend": trend,
                },
                recommendation=(
                    "Se a tendência interna for de queda contínua, o problema é "
                    "estrutural (oferta/tráfego). Se for pontual, provavelmente é "
                    "sazonalidade ou pausa de campanha."
                ),
                impact_brl=round(
                    max(0.0, prev.gross_revenue_brl - cur.gross_revenue_brl) * 4, 2
                ),
                effort=2.0,
                suggested_actions=["revisar_campanhas", "checar_sazonalidade"],
            )
        )

    traffic_delta = _delta_pct(cur.page_views, prev.page_views)
    if traffic_delta is not None and traffic_delta <= -t.traffic_drop_warn:
        findings.append(
            Finding(
                code="TRAFFIC_DROP",
                domain=Domain.ANALYTICS,
                severity=sev(Severity.WARNING),
                title=f"Tráfego caiu {pct(abs(traffic_delta), 0)}",
                detail=(
                    f"{cur.page_views} visitas contra {prev.page_views}. Costuma ser "
                    "campanha pausada, orçamento esgotado, anúncio reprovado ou "
                    "queda de alcance orgânico."
                ),
                evidence={"current": cur.page_views, "previous": prev.page_views},
                recommendation="Checar status e entrega das campanhas no Meta Ads.",
                impact_brl=0.0,
                effort=0.5,
                suggested_actions=["verificar_campanhas_ativas"],
            )
        )

    # ---------------- boletos/PIX pendentes ----------------
    pending_ratio = _safe_div(cur.pending_billets, cur.units + cur.pending_billets)
    if pending_ratio >= t.pending_billet_ratio_warn and cur.pending_billets >= 3:
        recoverable = cur.pending_billets * 0.35  # taxa típica de recuperação
        findings.append(
            Finding(
                code="PENDING_PAYMENT_RECOVERY",
                domain=Domain.ANALYTICS,
                severity=Severity.OPPORTUNITY,
                title=f"{cur.pending_billets} pagamentos aguardando compensação",
                detail=(
                    f"{pct(pending_ratio, 0)} dos pedidos estão como boleto/PIX não pago. "
                    "Sequência de lembrete em 2h/24h/48h costuma recuperar ~1/3."
                ),
                evidence={
                    "pending": cur.pending_billets,
                    "ratio": round(pending_ratio, 4),
                },
                recommendation=(
                    "Disparar régua de recuperação por e-mail/WhatsApp com o link de "
                    "segunda via."
                ),
                impact_brl=round(recoverable * margin * 4, 2),
                effort=0.5,
                confidence=0.7,
                suggested_actions=["regua_recuperacao"],
            )
        )

    # ---------------- concentração em afiliado ----------------
    concentration = _safe_div(cur.top_affiliate_revenue_brl, cur.gross_revenue_brl)
    if concentration >= t.affiliate_concentration_warn:
        findings.append(
            Finding(
                code="AFFILIATE_CONCENTRATION",
                domain=Domain.ANALYTICS,
                severity=Severity.WARNING,
                title=f"{pct(concentration, 0)} da receita vem de um único afiliado",
                detail=(
                    "Dependência alta: se esse parceiro parar de divulgar, a receita "
                    "cai junto."
                ),
                evidence={"share": round(concentration, 4)},
                recommendation="Recrutar 3 a 5 afiliados no mesmo nicho e reforçar canal próprio.",
                impact_brl=0.0,
                effort=2.0,
            )
        )

    # ---------------- oportunidade: canal campeão subinvestido ----------------
    paid = [s for s in cur.by_source.values() if s.ad_spend_brl > 0 and s.units > 0]
    organic = [s for s in cur.by_source.values() if s.ad_spend_brl == 0 and s.units > 0]
    for src in sorted(paid, key=lambda s: s.roas, reverse=True)[:1]:
        if src.roas >= max(t.roas_target, settings.guardrails.min_roas_to_scale):
            findings.append(
                Finding(
                    code="SCALE_WINNING_SOURCE",
                    domain=Domain.ADS,
                    severity=Severity.OPPORTUNITY,
                    title=f"Origem '{src.source}' com ROAS {times(src.roas)}",
                    detail=(
                        f"{src.units} vendas, CPA de {brl(src.cpa_brl)} contra "
                        f"ticket de {brl(settings.product.price_brl)}. Há espaço "
                        "para escalar com aumento gradual de orçamento."
                    ),
                    evidence={
                        "source": src.source,
                        "roas": round(src.roas, 2),
                        "cpa_brl": round(src.cpa_brl, 2),
                        "spend_brl": src.ad_spend_brl,
                    },
                    recommendation=(
                        f"Aumentar o orçamento em até "
                        f"{pct(settings.guardrails.max_budget_increase_pct, 0)} e reavaliar em 72h."
                    ),
                    impact_brl=round(
                        src.units
                        * margin
                        * settings.guardrails.max_budget_increase_pct
                        * 4,
                        2,
                    ),
                    effort=0.5,
                    suggested_actions=["escalar_orcamento"],
                )
            )

    for src in sorted(organic, key=lambda s: s.conversion_rate, reverse=True)[:1]:
        if src.conversion_rate > 0 and src.units >= 3:
            findings.append(
                Finding(
                    code="ORGANIC_CHANNEL_STRONG",
                    domain=Domain.ANALYTICS,
                    severity=Severity.OPPORTUNITY,
                    title=f"'{src.source}' converte a {pct(src.conversion_rate, 1)} sem mídia",
                    detail=(
                        f"{src.units} vendas sem custo de aquisição. Serve de base "
                        "para público semelhante e para escolher o ângulo criativo."
                    ),
                    evidence={
                        "source": src.source,
                        "conversion_rate": round(src.conversion_rate, 4),
                        "units": src.units,
                    },
                    recommendation="Transformar o conteúdo que mais converte em criativo pago.",
                    impact_brl=round(src.units * margin, 2),
                    effort=1.0,
                    suggested_actions=["criar_campanha_lookalike"],
                )
            )

    # ---------------- silêncio de vendas ----------------
    if cur.units == 0:
        findings.append(
            Finding(
                code="NO_SALES",
                domain=Domain.ANALYTICS,
                severity=Severity.CRITICAL,
                title="Nenhuma venda aprovada na janela",
                detail="Verificar checkout, status do produto e entrega de campanhas.",
                evidence={"window_days": (cur.end - cur.start).days},
                recommendation="Testar a compra do próprio produto agora.",
                impact_brl=round(prev.gross_revenue_brl * 4, 2),
                effort=0.5,
                suggested_actions=["testar_checkout"],
            )
        )

    if not findings:
        findings.append(
            Finding(
                code="ALL_CLEAR",
                domain=Domain.ANALYTICS,
                severity=Severity.INFO,
                title="Nenhum sinal de alerta na janela",
                detail="Indicadores dentro dos limites configurados.",
                evidence={},
                recommendation="Manter a operação e reavaliar no próximo ciclo.",
            )
        )
    return findings


def summarize(snapshot: SalesSnapshot, metrics: list[Metric]) -> str:
    """Resumo textual determinístico (usado quando não há LLM disponível)."""
    cur = snapshot.current
    by_key = {m.key: m for m in metrics}
    rev = by_key["gross_revenue"]
    delta = f"{signed_pct(rev.delta_pct, 0)}" if rev.delta_pct is not None else "n/d"
    trend = detect_trend(cur)
    return (
        f"Janela de {cur.start.isoformat()} a {cur.end.isoformat()}: "
        f"{brl(cur.gross_revenue_brl)} em {cur.units} vendas ({delta} vs. período "
        f"anterior), conversão de checkout em "
        f"{pct(by_key['checkout_conversion'].value, 2)}, reembolso em "
        f"{pct(by_key['refund_rate'].value, 1)}. Tendência interna: {trend['shape']}."
    )
