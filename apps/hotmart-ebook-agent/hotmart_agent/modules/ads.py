"""Mídia paga (Meta Ads): verificação de verba, diagnóstico e plano de campanha.

Ordem de operação inegociável:

    saldo/status da conta  ->  diagnóstico do que já roda  ->  novo plano

Verificar a conta antes de qualquer coisa evita o erro clássico de "criar
campanha em conta bloqueada" e evita gastar chamada de API à toa.
"""

from __future__ import annotations

from ..config import Settings
from ..formatting import brl, pct, times
from ..integrations.meta_ads import DISABLE_REASONS
from ..models import (
    AdAccountStatus,
    CampaignPerformance,
    CampaignPlan,
    Domain,
    Finding,
    SalesSnapshot,
    Severity,
)


def check_funding(status: AdAccountStatus, settings: Settings) -> list[Finding]:
    """Primeiro portão: a conta pode gastar? Se não, nada mais importa."""
    g = settings.guardrails
    findings: list[Finding] = []

    if not status.is_active:
        reason = DISABLE_REASONS.get(
            status.disable_reason, f"motivo {status.disable_reason}"
        )
        findings.append(
            Finding(
                code="AD_ACCOUNT_DISABLED",
                domain=Domain.ADS,
                severity=Severity.CRITICAL,
                title="Conta de anúncios inativa",
                detail=(
                    f"Status {status.account_status}: {reason}. Nenhuma campanha "
                    "pode ser criada ou veiculada até a regularização."
                ),
                evidence=status.to_dict(),
                recommendation=(
                    "Abrir a Central de Contas do Meta Business, revisar a "
                    "pendência e solicitar revisão. Enquanto isso, redirecionar "
                    "esforço para canais orgânicos e lista de e-mail."
                ),
                effort=1.0,
                suggested_actions=["regularizar_conta_meta"],
            )
        )
        return findings

    available = status.available_brl
    if not status.has_valid_funding:
        findings.append(
            Finding(
                code="AD_NO_FUNDING",
                domain=Domain.ADS,
                severity=Severity.CRITICAL,
                title="Conta sem meio de pagamento válido",
                detail="Não há cartão nem saldo pré-pago associado à conta.",
                evidence=status.to_dict(),
                recommendation="Cadastrar meio de pagamento antes de subir campanha.",
                effort=0.5,
            )
        )
    elif available != float("inf") and available < g.min_account_balance_brl:
        findings.append(
            Finding(
                code="AD_LOW_BALANCE",
                domain=Domain.ADS,
                severity=Severity.WARNING,
                title=f"Saldo disponível de {brl(available)}",
                detail=(
                    f"Abaixo do mínimo operacional de {brl(g.min_account_balance_brl)}. "
                    "Campanhas podem parar de "
                    "entregar no meio do dia."
                ),
                evidence=status.to_dict(),
                recommendation="Recarregar a conta antes de aprovar novos planos.",
                effort=0.5,
                suggested_actions=["recarregar_conta"],
            )
        )
    return findings


def diagnose_campaigns(
    campaigns: list[CampaignPerformance], settings: Settings
) -> list[Finding]:
    """Cada campanha ativa vira um sinal: escalar, corrigir ou cortar."""
    t = settings.thresholds
    price = settings.product.price_brl
    cpa_target = price * t.cpa_target_ratio
    cpa_critical = price * t.cpa_critical_ratio
    findings: list[Finding] = []

    for c in campaigns:
        if c.status != "ACTIVE":
            continue
        # Sem gasto mínimo, não há dado suficiente para julgar.
        if c.spend_brl < price:
            continue

        if c.purchases == 0:
            findings.append(
                Finding(
                    code="CAMPAIGN_NO_CONVERSION",
                    domain=Domain.ADS,
                    severity=Severity.CRITICAL,
                    title=f"'{c.name}' gastou {brl(c.spend_brl)} sem venda",
                    detail=(
                        "Gasto acima de um ticket sem nenhuma conversão. Pode ser "
                        "pixel mal configurado, público errado ou objetivo "
                        "inadequado para venda direta."
                    ),
                    evidence=c.to_dict(),
                    recommendation="Pausar e validar o evento de compra no pixel.",
                    impact_brl=round(c.spend_brl * 4, 2),
                    effort=0.5,
                    suggested_actions=["pausar_campanha"],
                )
            )
            continue

        if c.roas < t.roas_critical:
            findings.append(
                Finding(
                    code="CAMPAIGN_ROAS_CRITICAL",
                    domain=Domain.ADS,
                    severity=Severity.CRITICAL,
                    title=f"'{c.name}' com ROAS {times(c.roas)}",
                    detail=(
                        f"Cada real investido devolve {brl(c.roas)}. "
                        f"CPA de {brl(c.cpa_brl)} contra ticket de {brl(price)}."
                    ),
                    evidence=c.to_dict(),
                    recommendation="Pausar ou reduzir orçamento e refazer criativo/público.",
                    impact_brl=round(c.spend_brl * (1 - c.roas) * 4, 2),
                    effort=1.0,
                    suggested_actions=["pausar_campanha"],
                )
            )
        elif c.cpa_brl > cpa_critical:
            findings.append(
                Finding(
                    code="CAMPAIGN_CPA_HIGH",
                    domain=Domain.ADS,
                    severity=Severity.WARNING,
                    title=f"'{c.name}' com CPA de {brl(c.cpa_brl)}",
                    detail=(
                        f"Acima do teto de {brl(cpa_critical)} "
                        f"({pct(t.cpa_critical_ratio, 0)} do ticket)."
                    ),
                    evidence=c.to_dict(),
                    recommendation="Testar novo criativo antes de mexer no orçamento.",
                    impact_brl=round((c.cpa_brl - cpa_target) * c.purchases * 4, 2),
                    effort=1.0,
                )
            )
        elif c.roas >= settings.guardrails.min_roas_to_scale:
            findings.append(
                Finding(
                    code="CAMPAIGN_SCALABLE",
                    domain=Domain.ADS,
                    severity=Severity.OPPORTUNITY,
                    title=f"'{c.name}' com ROAS {times(c.roas)} — pronta para escalar",
                    detail=(
                        f"CPA de {brl(c.cpa_brl)} e {c.purchases} vendas. "
                        f"Aumento de até "
                        f"{pct(settings.guardrails.max_budget_increase_pct, 0)} preserva a "
                        "fase de aprendizado."
                    ),
                    evidence=c.to_dict(),
                    recommendation=(
                        f"Subir orçamento diário de {brl(c.daily_budget_brl)} para "
                        f"{brl(c.daily_budget_brl * (1 + settings.guardrails.max_budget_increase_pct))} "
                        "e reavaliar em 72h."
                    ),
                    impact_brl=round(
                        c.revenue_brl * settings.guardrails.max_budget_increase_pct * 4,
                        2,
                    ),
                    effort=0.3,
                    suggested_actions=["escalar_orcamento"],
                )
            )

        if c.objective == "OUTCOME_TRAFFIC" and c.purchases / max(c.clicks, 1) < 0.002:
            findings.append(
                Finding(
                    code="CAMPAIGN_WRONG_OBJECTIVE",
                    domain=Domain.ADS,
                    severity=Severity.WARNING,
                    title=f"'{c.name}' otimiza tráfego, não venda",
                    detail=(
                        "Campanhas de tráfego compram cliques baratos de quem não "
                        "compra. Para produto de ticket baixo, otimizar por compra "
                        "quase sempre sai mais barato por venda."
                    ),
                    evidence=c.to_dict(),
                    recommendation="Migrar para objetivo de vendas com otimização por compra.",
                    impact_brl=round(c.spend_brl * 4 * 0.6, 2),
                    effort=1.0,
                )
            )
    return findings


# --------------------------------------------------------------------------
# Blueprints de campanha
# --------------------------------------------------------------------------


def _audience_spec(kind: str, settings: Settings) -> dict:
    """Targeting da Marketing API. Público personalizado depende do pixel."""
    pixel = settings.credentials.meta_pixel_id or "PIXEL_ID"
    base = {"geo_locations": {"countries": ["BR"]}, "age_min": 22, "age_max": 55}
    if kind == "retarget_checkout":
        base["custom_audiences"] = [{"id": f"ca:initiate_checkout_7d:{pixel}"}]
    elif kind == "lookalike_purchasers":
        base["custom_audiences"] = [{"id": f"lal:1pct:purchasers_180d:{pixel}"}]
    elif kind == "leads_nao_compradores":
        base["custom_audiences"] = [{"id": f"ca:leads_60d:{pixel}"}]
        base["excluded_custom_audiences"] = [{"id": f"ca:purchasers_180d:{pixel}"}]
    elif kind == "broad":
        base["targeting_automation"] = {"advantage_audience": 1}
    return base


def build_campaign_catalog(
    snapshot: SalesSnapshot | None,
    campaigns: list[CampaignPerformance],
    settings: Settings,
) -> list[CampaignPlan]:
    """Catálogo de campanhas candidatas, pontuadas pelo contexto do produto.

    A pontuação é o que faz o agente sugerir a campanha certa para o momento e
    não sempre a mesma: retargeting sobe quando há abandono de checkout, captura
    de leads sobe quando o CPA direto está caro, e assim por diante.
    """
    price = settings.product.price_brl
    g = settings.guardrails
    cur = snapshot.current if snapshot else None
    running = {c.name for c in campaigns if c.status == "ACTIVE"}

    abandonment = 0.0
    if cur and cur.checkout_sessions:
        abandonment = 1 - (cur.units / cur.checkout_sessions)
    pending = cur.pending_billets if cur else 0
    best_organic = ""
    if cur:
        organic = [s for s in cur.by_source.values() if s.ad_spend_brl == 0]
        if organic:
            best_organic = max(organic, key=lambda s: s.units).source
    avg_cpa = 0.0
    active = [c for c in campaigns if c.status == "ACTIVE" and c.purchases]
    if active:
        avg_cpa = sum(c.spend_brl for c in active) / sum(c.purchases for c in active)

    budget_cap = g.max_daily_budget_brl
    catalog: list[CampaignPlan] = []

    # 1. Remarketing de checkout — quase sempre o melhor CPA disponível.
    catalog.append(
        CampaignPlan(
            blueprint="retarget_checkout_7d",
            name=f"[VENDAS] Remarketing checkout 7d — {settings.product.name}",
            objective="OUTCOME_SALES",
            optimization_goal="OFFSITE_CONVERSIONS",
            daily_budget_brl=min(budget_cap * 0.25, max(15.0, price * 0.2)),
            audience="Quem iniciou o checkout nos últimos 7 dias e não comprou",
            audience_spec=_audience_spec("retarget_checkout", settings),
            creative_angle=(
                "Quebra de objeção direta: garantia de "
                f"{settings.product.guarantee_days} dias + prova social"
            ),
            expected_cpa_brl=round(price * 0.18, 2),
            expected_roas=round(1 / 0.18, 2),
            score=3.0 + 4.0 * abandonment,
            rationale=(
                f"Abandono de checkout em {pct(abandonment, 0)}: público quente, "
                "volume baixo e CPA historicamente o menor do funil."
                if cur
                else "Público quente e de menor CPA do funil. Sem dados de venda "
                "nesta execução, a estimativa de volume é conservadora."
            ),
        )
    )

    # 2. Lookalike de compradores — motor de escala.
    catalog.append(
        CampaignPlan(
            blueprint="lookalike_purchasers_1",
            name=f"[VENDAS] Lookalike 1% compradores — {settings.product.name}",
            objective="OUTCOME_SALES",
            optimization_goal="OFFSITE_CONVERSIONS",
            daily_budget_brl=min(budget_cap * 0.4, max(30.0, price * 0.5)),
            audience="Público semelhante (1%) aos compradores dos últimos 180 dias",
            audience_spec=_audience_spec("lookalike_purchasers", settings),
            creative_angle=(
                f"Ângulo do canal que mais converte hoje ({best_organic or 'orgânico'})"
            ),
            expected_cpa_brl=round(price * 0.35, 2),
            expected_roas=round(1 / 0.35, 2),
            score=2.5 + (1.5 if cur and cur.units >= 100 else 0.0),
            rationale=(
                "Base de compradores já é suficiente para gerar semelhante estável."
                if cur and cur.units >= 100
                else "Base ainda pequena; o semelhante fica instável, mas serve "
                "como teste controlado."
            ),
        )
    )

    # 3. Público amplo — descoberta.
    catalog.append(
        CampaignPlan(
            blueprint="broad_prospecting",
            name=f"[VENDAS] Público amplo (Advantage+) — {settings.product.name}",
            objective="OUTCOME_SALES",
            optimization_goal="OFFSITE_CONVERSIONS",
            daily_budget_brl=min(budget_cap * 0.5, max(40.0, price * 0.6)),
            audience="Amplo com automação de público, Brasil, 22–55 anos",
            audience_spec=_audience_spec("broad", settings),
            creative_angle="3 variações de criativo para o algoritmo escolher",
            expected_cpa_brl=round(price * 0.45, 2),
            expected_roas=round(1 / 0.45, 2),
            score=2.0 if not running else 1.2,
            rationale=(
                "Descoberta de novo público. Só faz sentido com verba folgada e "
                "pixel maduro."
            ),
        )
    )

    # 4. Captura de leads — quando a venda direta está cara demais.
    lead_score = 1.0
    if avg_cpa and avg_cpa > price * settings.thresholds.cpa_critical_ratio:
        lead_score = 3.8
    catalog.append(
        CampaignPlan(
            blueprint="lead_magnet_capture",
            name=f"[LEADS] Isca digital — {settings.product.name}",
            objective="OUTCOME_LEADS",
            optimization_goal="LEAD_GENERATION",
            daily_budget_brl=min(budget_cap * 0.3, 25.0),
            audience="Interesses do nicho, Brasil, excluindo compradores",
            audience_spec=_audience_spec("broad", settings),
            creative_angle="Capítulo gratuito em troca do e-mail, venda por sequência",
            expected_cpa_brl=round(price * 0.08, 2),
            expected_roas=0.0,
            score=lead_score,
            rationale=(
                f"CPA direto em {brl(avg_cpa)}, acima do teto: vender por lista "
                "reduz o custo de aquisição por venda."
                if lead_score > 2
                else "Alternativa de médio prazo; não gera receita imediata."
            ),
        )
    )

    # 5. Reativação de leads não compradores.
    catalog.append(
        CampaignPlan(
            blueprint="reactivation_leads",
            name=f"[VENDAS] Reativação de leads — {settings.product.name}",
            objective="OUTCOME_SALES",
            optimization_goal="OFFSITE_CONVERSIONS",
            daily_budget_brl=min(budget_cap * 0.2, 15.0),
            audience="Leads dos últimos 60 dias que não compraram",
            audience_spec=_audience_spec("leads_nao_compradores", settings),
            creative_angle="Oferta por tempo limitado com bônus exclusivo",
            expected_cpa_brl=round(price * 0.22, 2),
            expected_roas=round(1 / 0.22, 2),
            score=1.8 + (1.2 if pending >= 5 else 0.0),
            rationale=(
                f"{pending} pagamentos pendentes indicam intenção não concluída "
                "na base."
                if pending >= 5
                else "Público morno de baixo custo."
            ),
        )
    )

    # Penaliza blueprint já em veiculação: duplicar campanha canibaliza leilão.
    for plan in catalog:
        if any(plan.blueprint.split("_")[0] in name.lower() for name in running):
            plan.score -= 0.8
        plan.start_paused = g.new_campaigns_start_paused

    return sorted(catalog, key=lambda p: p.score, reverse=True)


def select_campaigns(
    catalog: list[CampaignPlan], status: AdAccountStatus, settings: Settings
) -> tuple[list[CampaignPlan], list[str]]:
    """Corta o catálogo pelo que a conta realmente banca.

    Devolve os planos aprovados e as razões de exclusão — o usuário precisa
    saber por que a campanha que ele esperava não entrou.
    """
    g = settings.guardrails
    notes: list[str] = []
    approved: list[CampaignPlan] = []

    if not status.is_active:
        return [], ["conta de anúncios inativa: nenhum plano pode ser executado"]

    available = status.available_brl
    # Um plano só entra se a conta banca pelo menos 3 dias de veiculação —
    # campanha que morre no dia 1 não sai da fase de aprendizado.
    min_runway_days = 3
    budget_used = 0.0

    for plan in catalog:
        if len(approved) >= g.max_new_campaigns_per_run:
            notes.append(
                f"'{plan.name}' fora: limite de {g.max_new_campaigns_per_run} "
                "campanhas novas por execução"
            )
            continue
        if plan.daily_budget_brl > g.max_daily_budget_brl:
            plan.daily_budget_brl = g.max_daily_budget_brl
            notes.append(
                f"'{plan.name}': orçamento reduzido ao teto de "
                f"{brl(g.max_daily_budget_brl)}"
            )
        needed = (budget_used + plan.daily_budget_brl) * min_runway_days
        if available != float("inf") and needed > available:
            notes.append(
                f"'{plan.name}' fora: exige {brl(needed)} para {min_runway_days} "
                f"dias e há {brl(available)} disponíveis"
            )
            continue
        if plan.score <= 0:
            notes.append(
                f"'{plan.name}' fora: pontuação insuficiente no contexto atual"
            )
            continue
        approved.append(plan)
        budget_used += plan.daily_budget_brl

    return approved, notes
