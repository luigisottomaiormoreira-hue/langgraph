"""Clientes simulados: dry-run, demonstração e testes determinísticos.

O cenário padrão ("baseline") foi construído para exercitar os caminhos
interessantes do agente: queda de conversão, reembolso acima do limite, boletos
pendentes, um canal com ROAS alto e subinvestido, e uma caixa de entrada com os
tipos de e-mail que realmente chegam num produto da Hotmart.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from typing import Any

from ..config import Settings
from ..models import (
    AdAccountStatus,
    CampaignPerformance,
    CampaignPlan,
    InboundEmail,
    SalesSnapshot,
    SalesWindow,
    SourceStats,
)


class FakeHotmartClient:
    """Gera um snapshot de vendas coerente e determinístico."""

    def __init__(self, settings: Settings, scenario: str = "baseline", seed: int = 7):
        self.settings = settings
        self.scenario = scenario
        self.rng = random.Random(seed)
        self.refund_calls: list[dict[str, Any]] = []
        self.resend_calls: list[str] = []

    def fetch_snapshot(self, window_days: int) -> SalesSnapshot:
        today = date.today()
        cur_start = today - timedelta(days=window_days)
        prev_start = cur_start - timedelta(days=window_days)
        if self.scenario == "healthy":
            current = self._window(cur_start, today, units=140, refunds=4, mult=1.2)
            previous = self._window(prev_start, cur_start, units=120, refunds=4)
        elif self.scenario == "crash":
            current = self._window(cur_start, today, units=18, refunds=6, mult=0.4)
            previous = self._window(prev_start, cur_start, units=110, refunds=5)
        else:  # baseline: saudável na receita, com problemas localizados
            current = self._window(cur_start, today, units=86, refunds=9, mult=0.85)
            previous = self._window(prev_start, cur_start, units=104, refunds=5)
        return SalesSnapshot(current=current, previous=previous)

    def _window(
        self, start: date, end: date, *, units: int, refunds: int, mult: float = 1.0
    ) -> SalesWindow:
        price = self.settings.product.price_brl
        days = max(1, (end - start).days)
        gross = units * price
        daily = [
            round(gross / days * self.rng.uniform(0.7, 1.3), 2) for _ in range(days)
        ]

        sources = {
            "meta-ads": SourceStats(
                source="meta-ads",
                units=int(units * 0.52),
                revenue_brl=round(gross * 0.52, 2),
                clicks=int(units * 52 * mult),
                checkout_sessions=int(units * 3.1),
                ad_spend_brl=round(gross * 0.52 / 1.8, 2),
            ),
            "organico-instagram": SourceStats(
                source="organico-instagram",
                units=int(units * 0.22),
                revenue_brl=round(gross * 0.22, 2),
                clicks=int(units * 14),
                checkout_sessions=int(units * 0.9),
            ),
            "email-lista": SourceStats(
                source="email-lista",
                units=int(units * 0.16),
                revenue_brl=round(gross * 0.16, 2),
                clicks=int(units * 5),
                checkout_sessions=int(units * 0.4),
                ad_spend_brl=0.0,
            ),
            "afiliados": SourceStats(
                source="afiliados",
                units=int(units * 0.10),
                revenue_brl=round(gross * 0.10, 2),
                clicks=int(units * 6),
                checkout_sessions=int(units * 0.5),
            ),
        }
        return SalesWindow(
            start=start,
            end=end,
            gross_revenue_brl=round(gross, 2),
            net_revenue_brl=round(gross * 0.88, 2),
            units=units,
            refunds=refunds,
            refunded_brl=round(refunds * price, 2),
            chargebacks=1 if units > 60 else 0,
            checkout_sessions=int(units * 4.6 / max(mult, 0.5)),
            page_views=int(units * 41 / max(mult, 0.5)),
            pending_billets=int(units * 0.18),
            affiliate_units=int(units * 0.10),
            top_affiliate_revenue_brl=round(gross * 0.07, 2),
            daily_revenue_brl=daily,
            by_source=sources,
            refunds_within_48h=max(0, refunds - 3),
        )

    def find_purchase(self, email: str) -> dict[str, Any] | None:
        if "desconhecido" in email:
            return None
        return {
            "purchase": {
                "transaction": "HP" + str(abs(hash(email)) % 10_000_000),
                "status": "APPROVED",
                "order_date": datetime.utcnow().isoformat(),
                "price": {"value": self.settings.product.price_brl},
            },
            "buyer": {"email": email},
        }

    def request_refund(self, transaction: str, reason: str) -> dict[str, Any]:
        self.refund_calls.append({"transaction": transaction, "reason": reason})
        return {"status": "refund_requested", "transaction": transaction}

    def resend_access(self, email: str) -> dict[str, Any]:
        self.resend_calls.append(email)
        return {"status": "queued", "email": email}


class FakeMetaAdsClient:
    """Conta de anúncios simulada, com saldo suficiente para 1 campanha."""

    def __init__(
        self,
        settings: Settings,
        *,
        prepaid_balance_brl: float = 420.0,
        account_status: int = 1,
        disable_reason: int = 0,
    ):
        self.settings = settings
        self.prepaid_balance_brl = prepaid_balance_brl
        self.account_status = account_status
        self.disable_reason = disable_reason
        self.created: list[CampaignPlan] = []
        self.budget_updates: list[tuple[str, float]] = []
        self.paused: list[str] = []

    def get_account_status(self) -> AdAccountStatus:
        return AdAccountStatus(
            account_id=self.settings.credentials.meta_ad_account_id or "act_demo",
            account_status=self.account_status,
            disable_reason=self.disable_reason,
            currency="BRL",
            prepaid_balance_brl=self.prepaid_balance_brl,
            spend_cap_brl=0.0,
            amount_spent_brl=1980.0,
            has_valid_funding=True,
            funding_source="Pré-pago (PIX)",
        )

    def list_campaign_performance(self, days: int) -> list[CampaignPerformance]:
        return [
            CampaignPerformance(
                campaign_id="23851",
                name="[VENDAS] Público frio — amplo",
                status="ACTIVE",
                objective="OUTCOME_SALES",
                daily_budget_brl=60.0,
                spend_brl=60.0 * days,
                impressions=41_000,
                clicks=980,
                purchases=int(3.2 * days),
                revenue_brl=3.2 * days * self.settings.product.price_brl,
            ),
            CampaignPerformance(
                campaign_id="23852",
                name="[VENDAS] Remarketing checkout 7d",
                status="ACTIVE",
                objective="OUTCOME_SALES",
                daily_budget_brl=15.0,
                spend_brl=15.0 * days,
                impressions=6_200,
                clicks=310,
                purchases=int(2.4 * days),
                revenue_brl=2.4 * days * self.settings.product.price_brl,
            ),
            CampaignPerformance(
                campaign_id="23853",
                name="[TRÁFEGO] Blog — leitura",
                status="ACTIVE",
                objective="OUTCOME_TRAFFIC",
                daily_budget_brl=25.0,
                spend_brl=25.0 * days,
                impressions=88_000,
                clicks=2_400,
                purchases=int(0.3 * days),
                revenue_brl=0.3 * days * self.settings.product.price_brl,
            ),
        ]

    def create_campaign(self, plan: CampaignPlan) -> dict[str, Any]:
        self.created.append(plan)
        return {
            "campaign_id": f"sim-{len(self.created)}",
            "adset_id": f"sim-as-{len(self.created)}",
            "status": "PAUSED" if plan.start_paused else "ACTIVE",
        }

    def update_daily_budget(self, adset_id: str, budget_brl: float) -> dict[str, Any]:
        self.budget_updates.append((adset_id, budget_brl))
        return {"status": "updated", "adset_id": adset_id, "daily_budget": budget_brl}

    def pause_campaign(self, campaign_id: str) -> dict[str, Any]:
        self.paused.append(campaign_id)
        return {"status": "paused", "campaign_id": campaign_id}


_SAMPLE_EMAILS: list[tuple[str, str, str]] = [
    (
        "maria.silva@example.com",
        "Não recebi o acesso ao ebook",
        "Boa tarde! Comprei ontem pelo cartão e até agora não chegou nada no meu "
        "e-mail. Já olhei no spam. Podem me ajudar? Obrigada.",
    ),
    (
        "joao.pereira@example.com",
        "Quero meu dinheiro de volta",
        "Comprei anteontem e não era o que eu esperava. Gostaria de solicitar o "
        "reembolso dentro da garantia, por favor.",
    ),
    (
        "carla.souza@example.com",
        "Boleto pago e não liberou",
        "Paguei o boleto hoje de manhã, segue o comprovante. Quando libera o acesso?",
    ),
    (
        "rodrigo.lima@example.com",
        "Dúvida sobre o capítulo 4",
        "No capítulo 4 você fala sobre a planilha de controle, mas não achei o link "
        "para baixar. Onde encontro?",
    ),
    (
        "financeiro@empresa.example.com",
        "Nota fiscal da compra",
        "Precisamos da nota fiscal referente à compra feita pelo CNPJ. Como proceder?",
    ),
    (
        "ana.martins@example.com",
        "Quero ser afiliada",
        "Olá! Tenho uma audiência de 30 mil seguidores no nicho. Como faço para "
        "divulgar o ebook como afiliada?",
    ),
    (
        "lucas.ferreira@example.com",
        "ABSURDO - vou no Procon",
        "Faz 3 dias que peço reembolso e ninguém responde. Se não resolverem hoje "
        "eu abro reclamação no Procon e no Reclame Aqui e faço chargeback no cartão.",
    ),
    (
        "promo@marketingblast.example",
        "🔥 Aumente suas vendas com nosso robô de tráfego",
        "Oferta imperdível! Clique aqui e triplique seu faturamento em 7 dias.",
    ),
]


class FakeMailboxClient:
    """Caixa de entrada simulada com casos representativos."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sent: list[dict[str, Any]] = []
        self.drafts: list[dict[str, Any]] = []
        self.handled: list[tuple[str, str]] = []

    def fetch_unread(self, limit: int = 50) -> list[InboundEmail]:
        now = datetime.utcnow()
        return [
            InboundEmail(
                id=str(i + 1),
                sender=sender,
                subject=subject,
                body=body,
                received_at=now - timedelta(hours=i * 3),
                thread_id=f"<thread-{i + 1}@example.com>",
            )
            for i, (sender, subject, body) in enumerate(_SAMPLE_EMAILS[:limit])
        ]

    def send_reply(
        self, to: str, subject: str, body: str, in_reply_to: str
    ) -> dict[str, Any]:
        self.sent.append({"to": to, "subject": subject, "body": body})
        return {"status": "sent", "to": to}

    def save_draft(self, to: str, subject: str, body: str) -> dict[str, Any]:
        self.drafts.append({"to": to, "subject": subject, "body": body})
        return {"status": "drafted", "to": to}

    def mark_handled(self, email_id: str, label: str) -> None:
        self.handled.append((email_id, label))
