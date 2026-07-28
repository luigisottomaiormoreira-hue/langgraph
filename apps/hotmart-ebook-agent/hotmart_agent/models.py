"""Modelos de domínio compartilhados entre os módulos do agente.

Tudo aqui é `dataclass` serializável: o estado do grafo precisa atravessar
checkpointers (SQLite/Postgres) e virar JSON no relatório final.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any


class Severity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"
    OPPORTUNITY = "opportunity"


SEVERITY_WEIGHT = {
    Severity.CRITICAL: 1.0,
    Severity.WARNING: 0.6,
    Severity.OPPORTUNITY: 0.45,
    Severity.INFO: 0.15,
}


class Domain(str, Enum):
    ANALYTICS = "analytics"
    SUPPORT = "support"
    ADS = "ads"
    PRODUCT = "product"


class Intent(str, Enum):
    """Intenções que o roteador sabe extrair de um comando em linguagem natural."""

    ANALYZE_PERFORMANCE = "analyze_performance"
    HANDLE_EMAILS = "handle_emails"
    MANAGE_ADS = "manage_ads"
    CHECK_AD_BUDGET = "check_ad_budget"
    DAILY_BRIEFING = "daily_briefing"
    ANSWER_QUESTION = "answer_question"
    CLARIFY = "clarify"


class ActionType(str, Enum):
    """Ações executáveis. O tipo determina risco e nível de aprovação."""

    REPORT = "report"  # só informa
    SEND_EMAIL = "send_email"
    DRAFT_EMAIL = "draft_email"
    ISSUE_REFUND = "issue_refund"
    CREATE_CAMPAIGN = "create_campaign"
    UPDATE_BUDGET = "update_budget"
    PAUSE_CAMPAIGN = "pause_campaign"
    ESCALATE = "escalate"


class ExecutionMode(str, Enum):
    AUTO = "auto"  # executa sozinho
    APPROVAL = "approval"  # pede aprovação humana antes
    RECOMMEND = "recommend"  # só recomenda, humano executa
    BLOCKED = "blocked"  # guardrail impediu


@dataclass
class Metric:
    """Um KPI com comparação temporal já embutida."""

    key: str
    label: str
    value: float
    unit: str = ""  # "BRL", "%", "un"
    previous: float | None = None
    delta_pct: float | None = None
    direction: str = "flat"  # up | down | flat
    good_when: str = "up"  # up | down — para saber se a variação é boa

    @property
    def is_bad_move(self) -> bool:
        if self.delta_pct is None or abs(self.delta_pct) < 0.05:
            return False
        return (self.delta_pct < 0) if self.good_when == "up" else (self.delta_pct > 0)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Finding:
    """Um sinal produzido por um módulo: alerta, risco ou oportunidade."""

    code: str
    domain: Domain
    severity: Severity
    title: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)
    recommendation: str = ""
    impact_brl: float = 0.0  # estimativa de ganho/perda mensal
    effort: float = 1.0  # 0.5 barato .. 3 caro — divide a prioridade
    confidence: float = 0.8
    suggested_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["domain"] = self.domain.value
        d["severity"] = self.severity.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Finding:
        """Reconstrói a partir do estado serializado do grafo (checkpoint)."""
        payload = dict(data)
        payload["domain"] = Domain(payload["domain"])
        payload["severity"] = Severity(payload["severity"])
        return cls(**payload)


@dataclass
class Action:
    """Uma ação concreta que o agente pode executar ou recomendar."""

    id: str
    type: ActionType
    domain: Domain
    title: str
    payload: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    expected_impact_brl: float = 0.0
    cost_brl: float = 0.0
    reversible: bool = True
    priority: float = 0.0
    mode: ExecutionMode = ExecutionMode.RECOMMEND
    mode_reason: str = ""
    source_finding: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.value
        d["domain"] = self.domain.value
        d["mode"] = self.mode.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Action:
        payload = dict(data)
        payload["type"] = ActionType(payload["type"])
        payload["domain"] = Domain(payload["domain"])
        payload["mode"] = ExecutionMode(payload["mode"])
        return cls(**payload)


@dataclass
class ActionResult:
    """Resultado da tentativa de execução de uma ação."""

    action_id: str
    status: str  # done | skipped | failed | pending_approval | rejected
    detail: str = ""
    output: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# Analytics
# --------------------------------------------------------------------------


@dataclass
class SourceStats:
    """Desempenho por origem de tráfego (src/utm da Hotmart)."""

    source: str
    units: int = 0
    revenue_brl: float = 0.0
    clicks: int = 0
    checkout_sessions: int = 0
    ad_spend_brl: float = 0.0

    @property
    def conversion_rate(self) -> float:
        return self.units / self.checkout_sessions if self.checkout_sessions else 0.0

    @property
    def roas(self) -> float:
        return self.revenue_brl / self.ad_spend_brl if self.ad_spend_brl else 0.0

    @property
    def cpa_brl(self) -> float:
        return self.ad_spend_brl / self.units if self.units else 0.0


@dataclass
class SalesWindow:
    """Agregado de vendas de uma janela de tempo."""

    start: date
    end: date
    gross_revenue_brl: float = 0.0
    net_revenue_brl: float = 0.0
    units: int = 0
    refunds: int = 0
    refunded_brl: float = 0.0
    chargebacks: int = 0
    checkout_sessions: int = 0
    page_views: int = 0
    pending_billets: int = 0
    affiliate_units: int = 0
    top_affiliate_revenue_brl: float = 0.0
    daily_revenue_brl: list[float] = field(default_factory=list)
    by_source: dict[str, SourceStats] = field(default_factory=dict)
    refunds_within_48h: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["start"] = self.start.isoformat()
        d["end"] = self.end.isoformat()
        return d


@dataclass
class SalesSnapshot:
    """Janela atual + janela anterior de mesmo tamanho, para comparação."""

    current: SalesWindow
    previous: SalesWindow
    fetched_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "current": self.current.to_dict(),
            "previous": self.previous.to_dict(),
            "fetched_at": self.fetched_at.isoformat(),
        }


# --------------------------------------------------------------------------
# Suporte
# --------------------------------------------------------------------------


class EmailCategory(str, Enum):
    ACCESS = "acesso"
    PAYMENT = "pagamento"
    REFUND = "reembolso"
    CONTENT = "duvida_conteudo"
    INVOICE = "nota_fiscal"
    TECHNICAL = "tecnico"
    AFFILIATE = "afiliado"
    PARTNERSHIP = "parceria"
    COMPLAINT = "reclamacao"
    SPAM = "spam"
    OTHER = "outro"


@dataclass
class InboundEmail:
    id: str
    sender: str
    subject: str
    body: str
    received_at: datetime
    thread_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["received_at"] = self.received_at.isoformat()
        return d


@dataclass
class Triage:
    """Classificação de um e-mail + decisão de tratamento."""

    email_id: str
    category: EmailCategory
    confidence: float
    sentiment: str = "neutro"  # positivo | neutro | negativo | hostil
    urgency: str = "normal"  # baixa | normal | alta
    flags: list[str] = field(default_factory=list)  # juridico, chargeback, imprensa
    matched_terms: list[str] = field(default_factory=list)
    classifier: str = "rules"  # rules | llm

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["category"] = self.category.value
        return d


@dataclass
class EmailReply:
    email_id: str
    to: str
    subject: str
    body: str
    triage: Triage
    auto_sendable: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["triage"] = self.triage.to_dict()
        return d


# --------------------------------------------------------------------------
# Meta Ads
# --------------------------------------------------------------------------


@dataclass
class AdAccountStatus:
    """Saúde financeira/operacional da conta de anúncios."""

    account_id: str
    account_status: int = 1  # 1 = ACTIVE
    disable_reason: int = 0
    currency: str = "BRL"
    balance_brl: float = 0.0  # saldo devedor acumulado (pós-pago)
    prepaid_balance_brl: float = 0.0  # saldo pré-pago disponível
    spend_cap_brl: float = 0.0  # 0 = sem teto
    amount_spent_brl: float = 0.0
    has_valid_funding: bool = True
    funding_source: str = ""

    @property
    def is_active(self) -> bool:
        return self.account_status == 1 and self.disable_reason == 0

    @property
    def available_brl(self) -> float:
        """Quanto ainda pode ser gasto: mínimo entre pré-pago e teto restante."""
        cap_left = (
            self.spend_cap_brl - self.amount_spent_brl
            if self.spend_cap_brl > 0
            else float("inf")
        )
        prepaid = (
            self.prepaid_balance_brl
            if self.prepaid_balance_brl > 0
            else (float("inf") if self.has_valid_funding else 0.0)
        )
        return max(0.0, min(cap_left, prepaid))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["is_active"] = self.is_active
        available = self.available_brl
        d["available_brl"] = None if available == float("inf") else available
        return d


@dataclass
class CampaignPerformance:
    campaign_id: str
    name: str
    status: str
    objective: str
    daily_budget_brl: float
    spend_brl: float
    impressions: int
    clicks: int
    purchases: int
    revenue_brl: float

    @property
    def roas(self) -> float:
        return self.revenue_brl / self.spend_brl if self.spend_brl else 0.0

    @property
    def cpa_brl(self) -> float:
        return self.spend_brl / self.purchases if self.purchases else 0.0

    @property
    def ctr(self) -> float:
        return self.clicks / self.impressions if self.impressions else 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.update({"roas": self.roas, "cpa_brl": self.cpa_brl, "ctr": self.ctr})
        return d


@dataclass
class CampaignPlan:
    """Blueprint de campanha pronto para virar payload da Marketing API."""

    blueprint: str
    name: str
    objective: str  # OUTCOME_SALES, OUTCOME_LEADS, ...
    optimization_goal: str
    daily_budget_brl: float
    audience: str
    audience_spec: dict[str, Any] = field(default_factory=dict)
    creative_angle: str = ""
    expected_cpa_brl: float = 0.0
    expected_roas: float = 0.0
    score: float = 0.0
    rationale: str = ""
    start_paused: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
