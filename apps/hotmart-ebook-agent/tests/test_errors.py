"""Resiliência: retry, circuit breaker e redação de segredos."""

from __future__ import annotations

import pytest

from hotmart_agent.errors import (
    AuthError,
    CircuitBreaker,
    CircuitOpen,
    IntegrationError,
    RateLimitError,
    retry,
)
from hotmart_agent.integrations.base import redact


def test_retry_persiste_ate_o_sucesso():
    tentativas = {"n": 0}

    @retry(attempts=3, sleep=lambda _: None)
    def instavel():
        tentativas["n"] += 1
        if tentativas["n"] < 3:
            raise IntegrationError("x", "instável", retryable=True)
        return "ok"

    assert instavel() == "ok"
    assert tentativas["n"] == 3


def test_erro_de_credencial_nao_e_retentado():
    """Repetir uma senha errada só queima quota."""
    tentativas = {"n": 0}

    @retry(attempts=5, sleep=lambda _: None)
    def sem_permissao():
        tentativas["n"] += 1
        raise AuthError("x")

    with pytest.raises(AuthError):
        sem_permissao()
    assert tentativas["n"] == 1


def test_rate_limit_respeita_o_retry_after():
    esperas: list[float] = []

    @retry(attempts=2, sleep=esperas.append)
    def limitado():
        raise RateLimitError("x", retry_after=5.0)

    with pytest.raises(RateLimitError):
        limitado()
    assert esperas and esperas[0] >= 5.0


def test_circuito_abre_apos_falhas_seguidas():
    breaker = CircuitBreaker("meta", failure_threshold=3, reset_after_s=60)
    for _ in range(3):
        breaker.record_failure()
    assert breaker.is_open
    with pytest.raises(CircuitOpen):
        breaker.guard()


def test_sucesso_zera_o_contador():
    breaker = CircuitBreaker("meta", failure_threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    assert not breaker.is_open


def test_circuito_reabre_para_teste_apos_a_janela():
    breaker = CircuitBreaker("meta", failure_threshold=2, reset_after_s=0.0)
    breaker.record_failure()
    breaker.record_failure()
    assert not breaker.is_open  # half-open: deixa passar uma tentativa
    breaker.guard()


def test_segredos_nunca_aparecem_em_log():
    limpo = redact(
        {
            "access_token": "EAAG-secreto",
            "client_secret": "abc123",
            "nested": {"password": "senha", "ok": "visivel"},
            "lista": [{"authorization": "Bearer x"}],
        }
    )
    assert limpo["access_token"] == "***"
    assert limpo["client_secret"] == "***"
    assert limpo["nested"]["password"] == "***"
    assert limpo["nested"]["ok"] == "visivel"
    assert limpo["lista"][0]["authorization"] == "***"


def test_configuracao_redigida_mostra_presenca_nao_valor(settings):
    from hotmart_agent.config import Credentials

    s = settings.with_(
        credentials=Credentials(
            hotmart_client_id="id", hotmart_client_secret="segredo-real"
        )
    )
    resumo = s.redacted()
    assert resumo["credentials"]["hotmart"] == "set"
    assert resumo["credentials"]["meta_ads"] == "missing"
    assert "segredo-real" not in str(resumo)
