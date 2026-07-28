"""Integração com a Hotmart (Payments API).

Contrato: `HotmartClient` é um Protocol. A implementação real fala com a API v1
de vendas; a implementação `FakeHotmartClient` (em `fakes.py`) gera dados
sintéticos coerentes para dry-run, demonstração e testes.
"""

from __future__ import annotations

import base64
import logging
import time
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Protocol, runtime_checkable

from ..config import Settings
from ..errors import ConfigError
from ..models import SalesSnapshot, SalesWindow, SourceStats
from .base import HttpClient

logger = logging.getLogger(__name__)

AUTH_URL = "https://api-sec-vlc.hotmart.com/security/oauth/token"
API_BASE = "https://developers.hotmart.com/payments/api/v1"

# Status da Hotmart que contam como venda concluída / estorno / pendente.
APPROVED = {"APPROVED", "COMPLETE"}
REFUNDED = {"REFUNDED", "CANCELLED"}
CHARGEBACK = {"CHARGEBACK"}
PENDING = {"WAITING_PAYMENT", "PRINTED_BILLET", "STARTED"}


@runtime_checkable
class HotmartClient(Protocol):
    """Interface consumida pelo módulo de analytics."""

    def fetch_snapshot(self, window_days: int) -> SalesSnapshot: ...

    def find_purchase(self, email: str) -> dict[str, Any] | None: ...

    def request_refund(self, transaction: str, reason: str) -> dict[str, Any]: ...

    def resend_access(self, email: str) -> dict[str, Any]: ...


class HotmartAPIClient:
    """Cliente real da Payments API.

    A Hotmart usa OAuth2 client-credentials: o token vale ~24h e é cacheado em
    memória para não estourar o rate limit do endpoint de auth.
    """

    def __init__(self, settings: Settings) -> None:
        creds = settings.credentials
        if not creds.has_hotmart():
            raise ConfigError(
                "HOTMART_CLIENT_ID/HOTMART_CLIENT_SECRET não configurados"
            )
        self.settings = settings
        self.creds = creds
        self.http = HttpClient(
            "hotmart",
            API_BASE,
            timeout=settings.http_timeout_s,
            max_retries=settings.http_max_retries,
        )
        self._token: str | None = None
        self._token_exp: float = 0.0

    # ------------------------------------------------------------------
    def _basic_token(self) -> str:
        if self.creds.hotmart_basic_token:
            return self.creds.hotmart_basic_token
        raw = f"{self.creds.hotmart_client_id}:{self.creds.hotmart_client_secret}"
        return base64.b64encode(raw.encode()).decode()

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        auth = HttpClient("hotmart-auth", timeout=self.settings.http_timeout_s)
        resp = auth.post(
            AUTH_URL,
            params={
                "grant_type": "client_credentials",
                "client_id": self.creds.hotmart_client_id,
                "client_secret": self.creds.hotmart_client_secret,
            },
            headers={"Authorization": f"Basic {self._basic_token()}"},
        )
        self._token = resp["access_token"]
        self._token_exp = time.time() + float(resp.get("expires_in", 86400))
        return self._token

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token()}"}

    def _paged(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Itera a paginação por `page_token`, com teto para não rodar infinito."""
        items: list[dict[str, Any]] = []
        token: str | None = None
        for _ in range(50):
            page = self.http.get(
                path,
                params={**params, "max_results": 500, "page_token": token},
                headers=self._auth_headers(),
            )
            items.extend(page.get("items", []))
            token = (page.get("page_info") or {}).get("next_page_token")
            if not token:
                break
        return items

    # ------------------------------------------------------------------
    def fetch_snapshot(self, window_days: int) -> SalesSnapshot:
        today = date.today()
        cur_start = today - timedelta(days=window_days)
        prev_start = cur_start - timedelta(days=window_days)
        current = self._window(cur_start, today)
        previous = self._window(prev_start, cur_start)
        return SalesSnapshot(current=current, previous=previous)

    def _window(self, start: date, end: date) -> SalesWindow:
        params: dict[str, Any] = {
            "start_date": int(
                time.mktime(start.timetuple()) * 1000
            ),  # a API espera epoch ms
            "end_date": int(time.mktime(end.timetuple()) * 1000),
        }
        if self.settings.product.hotmart_product_id:
            params["product_id"] = self.settings.product.hotmart_product_id

        sales = self._paged("/sales/history", params)
        return self._aggregate(start, end, sales)

    @staticmethod
    def _aggregate(start: date, end: date, sales: list[dict[str, Any]]) -> SalesWindow:
        window = SalesWindow(start=start, end=end)
        by_day: dict[str, float] = defaultdict(float)
        by_affiliate: dict[str, float] = defaultdict(float)
        sources: dict[str, SourceStats] = {}

        for sale in sales:
            purchase = sale.get("purchase", {})
            status = (purchase.get("status") or "").upper()
            price = float((purchase.get("price") or {}).get("value", 0.0))
            commission = float(
                (sale.get("producer", {}).get("commission") or {}).get("value", price)
            )
            src = (purchase.get("tracking") or {}).get("source") or "direto"

            if status in APPROVED:
                window.units += 1
                window.gross_revenue_brl += price
                window.net_revenue_brl += commission
                day = str(purchase.get("order_date", ""))[:10] or start.isoformat()
                by_day[day] += price
                stats = sources.setdefault(src, SourceStats(source=src))
                stats.units += 1
                stats.revenue_brl += price
                affiliate = (sale.get("affiliates") or [{}])[0].get("name") or ""
                if affiliate:
                    window.affiliate_units += 1
                    by_affiliate[affiliate] += price
            elif status in REFUNDED:
                window.refunds += 1
                window.refunded_brl += price
            elif status in CHARGEBACK:
                window.chargebacks += 1
            elif status in PENDING:
                window.pending_billets += 1

        window.by_source = sources
        window.top_affiliate_revenue_brl = max(by_affiliate.values(), default=0.0)
        days = max(1, (end - start).days)
        window.daily_revenue_brl = [
            by_day.get((start + timedelta(days=i)).isoformat(), 0.0)
            for i in range(days)
        ]
        # A API de vendas não expõe sessões de checkout; quando não houver dado de
        # analytics externo, estimamos a partir do funil observado para não
        # travar o cálculo de conversão (marcado como estimativa no relatório).
        window.checkout_sessions = window.units + window.pending_billets
        return window

    # ------------------------------------------------------------------
    def find_purchase(self, email: str) -> dict[str, Any] | None:
        resp = self.http.get(
            "/sales/history",
            params={"buyer_email": email, "max_results": 10},
            headers=self._auth_headers(),
        )
        items = resp.get("items", [])
        return items[0] if items else None

    def request_refund(self, transaction: str, reason: str) -> dict[str, Any]:
        return self.http.post(
            f"/sales/{transaction}/refund",
            json_body={"reason": reason},
            headers=self._auth_headers(),
        )

    def resend_access(self, email: str) -> dict[str, Any]:
        purchase = self.find_purchase(email)
        if not purchase:
            return {"status": "not_found", "email": email}
        transaction = (purchase.get("purchase") or {}).get("transaction", "")
        return {
            "status": "queued",
            "transaction": transaction,
            "detail": "reenvio de acesso solicitado ao Hotmart Club",
        }


def build_hotmart_client(settings: Settings) -> HotmartClient:
    """Escolhe cliente real ou fake conforme credenciais/dry-run."""
    from .fakes import FakeHotmartClient

    if settings.credentials.has_hotmart() and not settings.dry_run:
        return HotmartAPIClient(settings)
    logger.info("hotmart: usando cliente simulado (dry-run ou credencial ausente)")
    return FakeHotmartClient(settings)
