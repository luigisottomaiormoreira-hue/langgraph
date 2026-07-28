from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from hotmart_agent.config import Autonomy, Credentials, Product, Settings
from hotmart_agent.integrations.fakes import (
    FakeHotmartClient,
    FakeMailboxClient,
    FakeMetaAdsClient,
)
from hotmart_agent.models import InboundEmail, SalesSnapshot, SalesWindow, SourceStats


@pytest.fixture
def settings() -> Settings:
    """Configuração de teste: sem credenciais reais, dry-run desligado.

    dry_run=False é intencional — queremos exercitar o caminho de execução
    contra os clientes falsos, não pular tudo como simulação.
    """
    return Settings(
        product=Product(
            name="Ebook Teste",
            price_brl=97.0,
            support_signature="Equipe Teste",
            sales_page_url="https://exemplo.com/ebook",
        ),
        credentials=Credentials(),
        autonomy=Autonomy.APPROVE,
        dry_run=False,
        analysis_window_days=7,
    )


@pytest.fixture
def hotmart(settings) -> FakeHotmartClient:
    return FakeHotmartClient(settings)


@pytest.fixture
def meta(settings) -> FakeMetaAdsClient:
    return FakeMetaAdsClient(settings)


@pytest.fixture
def mailbox(settings) -> FakeMailboxClient:
    return FakeMailboxClient(settings)


def make_window(**overrides) -> SalesWindow:
    """Janela de vendas neutra; sobrescreva só o que o teste precisa."""
    base = dict(
        start=date(2026, 1, 8),
        end=date(2026, 1, 15),
        gross_revenue_brl=9700.0,
        net_revenue_brl=8500.0,
        units=100,
        refunds=3,
        refunded_brl=291.0,
        chargebacks=0,
        checkout_sessions=400,
        page_views=4000,
        pending_billets=5,
        affiliate_units=10,
        top_affiliate_revenue_brl=500.0,
        daily_revenue_brl=[1400.0] * 7,
        by_source={},
        refunds_within_48h=1,
    )
    base.update(overrides)
    return SalesWindow(**base)


def make_snapshot(
    current: SalesWindow | None = None, previous: SalesWindow | None = None
):
    return SalesSnapshot(
        current=current or make_window(),
        previous=previous or make_window(start=date(2026, 1, 1), end=date(2026, 1, 8)),
    )


def make_email(
    subject: str, body: str, sender: str = "cliente@example.com", eid: str = "1"
):
    return InboundEmail(
        id=eid,
        sender=sender,
        subject=subject,
        body=body,
        received_at=datetime.utcnow() - timedelta(hours=1),
        thread_id=f"<{eid}@example.com>",
    )


def source(name: str, **kwargs) -> SourceStats:
    return SourceStats(source=name, **kwargs)
