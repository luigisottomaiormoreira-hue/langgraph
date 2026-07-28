"""Configuração central do agente.

Todo comportamento sensível (limites financeiros, thresholds de alerta, nível de
autonomia) é declarado aqui e pode ser sobrescrito por variável de ambiente.
Segredos nunca são impressos: `Settings.redacted()` produz a versão segura para
log.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any


class Autonomy(str, Enum):
    """Quanto o agente pode fazer sem um humano no meio."""

    OBSERVE = "L0"  # apenas coleta e reporta
    RECOMMEND = "L1"  # propõe ações, nunca executa
    APPROVE = "L2"  # executa somente após aprovação humana (padrão)
    AUTONOMOUS = "L3"  # executa dentro dos limites (guardrails) sem aprovação


AUTONOMY_ORDER = {
    Autonomy.OBSERVE: 0,
    Autonomy.RECOMMEND: 1,
    Autonomy.APPROVE: 2,
    Autonomy.AUTONOMOUS: 3,
}


class Risk(str, Enum):
    """Risco de uma ação — usado junto com a autonomia para liberar execução."""

    NONE = "none"  # leitura pura
    LOW = "low"  # reversível, sem custo (rascunho, pausa de campanha)
    MEDIUM = "medium"  # visível ao cliente, reversível com esforço (e-mail enviado)
    HIGH = "high"  # gasta dinheiro ou é irreversível (subir campanha, reembolso)


RISK_ORDER = {Risk.NONE: 0, Risk.LOW: 1, Risk.MEDIUM: 2, Risk.HIGH: 3}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, float(default)))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on", "sim"}


@dataclass(frozen=True)
class Product:
    """Contexto do produto — usado em prompts, e-mails e escolha de campanha."""

    name: str = "Ebook"
    hotmart_product_id: str = ""
    price_brl: float = 97.0
    cogs_brl: float = 0.0  # ebook: custo marginal ~0
    platform_fee_rate: float = 0.099  # taxa Hotmart aproximada
    affiliate_commission_rate: float = 0.40
    sales_page_url: str = ""
    checkout_url: str = ""
    support_email: str = ""
    support_signature: str = "Equipe de Suporte"
    guarantee_days: int = 7  # garantia legal (CDC art. 49)
    delivery: str = "Hotmart Club / e-mail de acesso"
    tone: str = "profissional, direto e cordial, em português do Brasil"

    @property
    def contribution_margin_brl(self) -> float:
        """Margem por venda usada para estimar impacto financeiro."""
        return max(
            0.0,
            self.price_brl * (1 - self.platform_fee_rate) - self.cogs_brl,
        )


@dataclass(frozen=True)
class Thresholds:
    """Gatilhos de alerta da análise de desempenho."""

    refund_rate_warn: float = 0.07
    refund_rate_critical: float = 0.12
    chargeback_rate_warn: float = 0.005
    chargeback_rate_critical: float = 0.01
    conversion_drop_warn: float = 0.20  # queda relativa vs. janela anterior
    conversion_drop_critical: float = 0.40
    revenue_drop_warn: float = 0.20
    revenue_drop_critical: float = 0.40
    traffic_drop_warn: float = 0.25
    roas_target: float = 2.0
    roas_critical: float = 1.0
    cpa_target_ratio: float = 0.35  # CPA aceitável = 35% do preço
    cpa_critical_ratio: float = 0.60
    checkout_abandonment_warn: float = 0.75
    pending_billet_ratio_warn: float = 0.15
    affiliate_concentration_warn: float = 0.50  # % da receita em 1 afiliado
    zero_sales_hours: int = 48
    min_units_for_stats: int = 20  # abaixo disso, sinais viram "informativos"


@dataclass(frozen=True)
class Guardrails:
    """Limites duros. Nenhuma ação automática ultrapassa estes valores."""

    max_daily_budget_brl: float = 150.0
    max_new_campaigns_per_run: int = 2
    max_budget_increase_pct: float = 0.30  # regra clássica: +30% por vez
    min_account_balance_brl: float = 50.0
    min_roas_to_scale: float = 2.0
    max_auto_emails_per_run: int = 25
    auto_reply_min_confidence: float = 0.80
    refund_auto_approve_days: int = 7
    refund_auto_approve_max_brl: float = 300.0
    new_campaigns_start_paused: bool = True
    require_approval_above_brl: float = 0.0  # qualquer gasto pede aprovação em L2


@dataclass(frozen=True)
class Credentials:
    """Segredos. Carregados do ambiente; nunca serializados em log."""

    hotmart_client_id: str = ""
    hotmart_client_secret: str = ""
    hotmart_basic_token: str = ""
    meta_access_token: str = ""
    meta_ad_account_id: str = ""
    meta_page_id: str = ""
    meta_pixel_id: str = ""
    imap_host: str = ""
    imap_user: str = ""
    imap_password: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""

    @classmethod
    def from_env(cls) -> Credentials:
        return cls(
            hotmart_client_id=os.getenv("HOTMART_CLIENT_ID", ""),
            hotmart_client_secret=os.getenv("HOTMART_CLIENT_SECRET", ""),
            hotmart_basic_token=os.getenv("HOTMART_BASIC_TOKEN", ""),
            meta_access_token=os.getenv("META_ACCESS_TOKEN", ""),
            meta_ad_account_id=os.getenv("META_AD_ACCOUNT_ID", ""),
            meta_page_id=os.getenv("META_PAGE_ID", ""),
            meta_pixel_id=os.getenv("META_PIXEL_ID", ""),
            imap_host=os.getenv("IMAP_HOST", ""),
            imap_user=os.getenv("IMAP_USER", ""),
            imap_password=os.getenv("IMAP_PASSWORD", ""),
            smtp_host=os.getenv("SMTP_HOST", ""),
            smtp_port=_env_int("SMTP_PORT", 587),
            smtp_user=os.getenv("SMTP_USER", ""),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
        )

    def has_hotmart(self) -> bool:
        return bool(self.hotmart_client_id and self.hotmart_client_secret)

    def has_meta(self) -> bool:
        return bool(self.meta_access_token and self.meta_ad_account_id)

    def has_mailbox(self) -> bool:
        return bool(self.imap_host and self.imap_user and self.imap_password)


@dataclass(frozen=True)
class Settings:
    """Configuração completa de uma execução do agente."""

    product: Product = field(default_factory=Product)
    thresholds: Thresholds = field(default_factory=Thresholds)
    guardrails: Guardrails = field(default_factory=Guardrails)
    credentials: Credentials = field(default_factory=Credentials)
    autonomy: Autonomy = Autonomy.APPROVE
    dry_run: bool = True
    analysis_window_days: int = 7
    model: str = "anthropic:claude-sonnet-5"
    timezone: str = "America/Sao_Paulo"
    currency: str = "BRL"
    http_timeout_s: float = 20.0
    http_max_retries: int = 3

    @classmethod
    def from_env(cls) -> Settings:
        autonomy_raw = os.getenv("HOTMART_AGENT_AUTONOMY", Autonomy.APPROVE.value)
        try:
            autonomy = Autonomy(autonomy_raw.upper())
        except ValueError:
            autonomy = Autonomy.APPROVE
        product = Product(
            name=os.getenv("PRODUCT_NAME", Product.name),
            hotmart_product_id=os.getenv("HOTMART_PRODUCT_ID", ""),
            price_brl=_env_float("PRODUCT_PRICE_BRL", Product.price_brl),
            sales_page_url=os.getenv("PRODUCT_SALES_PAGE_URL", ""),
            checkout_url=os.getenv("PRODUCT_CHECKOUT_URL", ""),
            support_email=os.getenv("SUPPORT_EMAIL", ""),
            support_signature=os.getenv("SUPPORT_SIGNATURE", Product.support_signature),
        )
        return cls(
            product=product,
            credentials=Credentials.from_env(),
            autonomy=autonomy,
            dry_run=_env_bool("HOTMART_AGENT_DRY_RUN", True),
            analysis_window_days=_env_int("HOTMART_AGENT_WINDOW_DAYS", 7),
            model=os.getenv("HOTMART_AGENT_MODEL", cls.model),
            http_timeout_s=_env_float("HOTMART_AGENT_HTTP_TIMEOUT", 20.0),
            http_max_retries=_env_int("HOTMART_AGENT_HTTP_RETRIES", 3),
        )

    def with_(self, **changes: Any) -> Settings:
        """Cópia imutável com sobrescritas — usado por CLI e testes."""
        return replace(self, **changes)

    def redacted(self) -> dict[str, Any]:
        """Visão segura para log/telemetria: presença de segredo, nunca o valor."""
        c = self.credentials
        return {
            "product": self.product.name,
            "autonomy": self.autonomy.value,
            "dry_run": self.dry_run,
            "window_days": self.analysis_window_days,
            "model": self.model,
            "credentials": {
                "hotmart": "set" if c.has_hotmart() else "missing",
                "meta_ads": "set" if c.has_meta() else "missing",
                "mailbox": "set" if c.has_mailbox() else "missing",
            },
        }


DEFAULT_SETTINGS = Settings()
