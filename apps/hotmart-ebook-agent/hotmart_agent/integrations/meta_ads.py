"""Integração com a Meta Marketing API (Facebook/Instagram Ads).

Responsabilidades:
1. Ler a saúde financeira da conta (saldo, teto de gasto, status, meio de pagamento).
2. Ler performance das campanhas ativas (insights).
3. Criar campanha/conjunto/anúncio — sempre pausados por padrão.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from ..config import Settings
from ..errors import ConfigError
from ..models import AdAccountStatus, CampaignPerformance, CampaignPlan
from .base import HttpClient

logger = logging.getLogger(__name__)

GRAPH_VERSION = "v21.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

ACCOUNT_FIELDS = ",".join(
    [
        "account_status",
        "disable_reason",
        "currency",
        "balance",
        "spend_cap",
        "amount_spent",
        "funding_source_details",
    ]
)

# Códigos de `disable_reason` da Meta que exigem ação humana.
DISABLE_REASONS = {
    1: "conta desativada por violação de política",
    2: "conta desativada por problema de pagamento (IP_CENTRAL)",
    3: "conta desativada por atividade suspeita",
    4: "conta em análise de anúncios",
    5: "conta desativada por risco (RISK_PAYMENT)",
    7: "conta fechada pelo anunciante",
    8: "revisão de política pendente",
    9: "conta desativada por gasto não pago",
}


@runtime_checkable
class MetaAdsClient(Protocol):
    def get_account_status(self) -> AdAccountStatus: ...

    def list_campaign_performance(self, days: int) -> list[CampaignPerformance]: ...

    def create_campaign(self, plan: CampaignPlan) -> dict[str, Any]: ...

    def update_daily_budget(
        self, adset_id: str, budget_brl: float
    ) -> dict[str, Any]: ...

    def pause_campaign(self, campaign_id: str) -> dict[str, Any]: ...


class MetaAdsAPIClient:
    """Cliente real. Valores monetários da Graph API vêm em centavos (string)."""

    def __init__(self, settings: Settings) -> None:
        creds = settings.credentials
        if not creds.has_meta():
            raise ConfigError("META_ACCESS_TOKEN/META_AD_ACCOUNT_ID não configurados")
        self.settings = settings
        self.creds = creds
        self.account = (
            creds.meta_ad_account_id
            if creds.meta_ad_account_id.startswith("act_")
            else f"act_{creds.meta_ad_account_id}"
        )
        self.http = HttpClient(
            "meta-ads",
            GRAPH_BASE,
            timeout=settings.http_timeout_s,
            max_retries=settings.http_max_retries,
        )

    def _params(self, **extra: Any) -> dict[str, Any]:
        return {"access_token": self.creds.meta_access_token, **extra}

    @staticmethod
    def _cents(value: Any) -> float:
        try:
            return float(value) / 100.0
        except (TypeError, ValueError):
            return 0.0

    # ------------------------------------------------------------------
    def get_account_status(self) -> AdAccountStatus:
        data = self.http.get(
            f"/{self.account}", params=self._params(fields=ACCOUNT_FIELDS)
        )
        funding = data.get("funding_source_details") or {}
        # `balance` é o saldo devedor no pós-pago e o crédito no pré-pago;
        # o tipo do meio de pagamento diz qual leitura vale.
        funding_type = str(funding.get("type", ""))
        is_prepaid = (
            funding_type in {"20", "prepaid"}
            or "prepaid" in str(funding.get("display_string", "")).lower()
        )
        balance = self._cents(data.get("balance"))
        return AdAccountStatus(
            account_id=self.account,
            account_status=int(data.get("account_status", 1)),
            disable_reason=int(data.get("disable_reason", 0)),
            currency=data.get("currency", "BRL"),
            balance_brl=0.0 if is_prepaid else balance,
            prepaid_balance_brl=balance if is_prepaid else 0.0,
            spend_cap_brl=self._cents(data.get("spend_cap")),
            amount_spent_brl=self._cents(data.get("amount_spent")),
            has_valid_funding=bool(funding),
            funding_source=str(funding.get("display_string", "")),
        )

    def list_campaign_performance(self, days: int) -> list[CampaignPerformance]:
        insights = self.http.get(
            f"/{self.account}/insights",
            params=self._params(
                level="campaign",
                date_preset=self._date_preset(days),
                fields="campaign_id,campaign_name,spend,impressions,clicks,actions,action_values",
                limit=200,
            ),
        )
        campaigns = self.http.get(
            f"/{self.account}/campaigns",
            params=self._params(
                fields="id,name,status,objective,daily_budget", limit=200
            ),
        )
        meta_by_id = {c["id"]: c for c in campaigns.get("data", [])}

        out: list[CampaignPerformance] = []
        for row in insights.get("data", []):
            cid = row.get("campaign_id", "")
            meta = meta_by_id.get(cid, {})
            purchases = self._action_value(row.get("actions"), "purchase")
            revenue = self._action_value(row.get("action_values"), "purchase")
            out.append(
                CampaignPerformance(
                    campaign_id=cid,
                    name=row.get("campaign_name", meta.get("name", "")),
                    status=meta.get("status", "UNKNOWN"),
                    objective=meta.get("objective", ""),
                    daily_budget_brl=self._cents(meta.get("daily_budget")),
                    spend_brl=float(row.get("spend", 0) or 0),
                    impressions=int(row.get("impressions", 0) or 0),
                    clicks=int(row.get("clicks", 0) or 0),
                    purchases=int(purchases),
                    revenue_brl=revenue,
                )
            )
        return out

    @staticmethod
    def _date_preset(days: int) -> str:
        if days <= 1:
            return "today"
        if days <= 7:
            return "last_7d"
        if days <= 14:
            return "last_14d"
        return "last_30d"

    @staticmethod
    def _action_value(actions: Any, action_type: str) -> float:
        for item in actions or []:
            if action_type in str(item.get("action_type", "")):
                try:
                    return float(item.get("value", 0))
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    # ------------------------------------------------------------------
    def create_campaign(self, plan: CampaignPlan) -> dict[str, Any]:
        """Cria campanha + conjunto. O anúncio reaproveita criativo existente.

        `status=PAUSED` é o default deliberado: o agente monta a estrutura, um
        humano (ou uma regra L3 explícita) liga.
        """
        status = "PAUSED" if plan.start_paused else "ACTIVE"
        campaign = self.http.post(
            f"/{self.account}/campaigns",
            params=self._params(),
            json_body={
                "name": plan.name,
                "objective": plan.objective,
                "status": status,
                "special_ad_categories": [],
                "access_token": self.creds.meta_access_token,
            },
        )
        campaign_id = campaign.get("id", "")
        adset = self.http.post(
            f"/{self.account}/adsets",
            params=self._params(),
            json_body={
                "name": f"{plan.name} — {plan.audience}",
                "campaign_id": campaign_id,
                "daily_budget": int(plan.daily_budget_brl * 100),
                "billing_event": "IMPRESSIONS",
                "optimization_goal": plan.optimization_goal,
                "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                "targeting": plan.audience_spec,
                "promoted_object": {
                    "pixel_id": self.creds.meta_pixel_id,
                    "custom_event_type": "PURCHASE",
                },
                "status": status,
                "access_token": self.creds.meta_access_token,
            },
        )
        return {
            "campaign_id": campaign_id,
            "adset_id": adset.get("id", ""),
            "status": status,
        }

    def update_daily_budget(self, adset_id: str, budget_brl: float) -> dict[str, Any]:
        return self.http.post(
            f"/{adset_id}",
            params=self._params(),
            json_body={
                "daily_budget": int(budget_brl * 100),
                "access_token": self.creds.meta_access_token,
            },
        )

    def pause_campaign(self, campaign_id: str) -> dict[str, Any]:
        return self.http.post(
            f"/{campaign_id}",
            params=self._params(),
            json_body={
                "status": "PAUSED",
                "access_token": self.creds.meta_access_token,
            },
        )


def build_meta_client(settings: Settings) -> MetaAdsClient:
    from .fakes import FakeMetaAdsClient

    if settings.credentials.has_meta() and not settings.dry_run:
        return MetaAdsAPIClient(settings)
    logger.info("meta-ads: usando cliente simulado (dry-run ou credencial ausente)")
    return FakeMetaAdsClient(settings)
