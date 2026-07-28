"""Triagem de e-mail: classificar certo e, principalmente, saber quando calar."""

from __future__ import annotations

import pytest

from hotmart_agent.models import EmailCategory
from hotmart_agent.modules import support

from .conftest import make_email


@pytest.mark.parametrize(
    ("assunto", "corpo", "esperado"),
    [
        (
            "Não recebi o acesso",
            "Comprei ontem e não chegou nada no meu e-mail, já olhei no spam.",
            EmailCategory.ACCESS,
        ),
        (
            "Boleto pago",
            "Paguei o boleto hoje, segue o comprovante. Quando libera?",
            EmailCategory.PAYMENT,
        ),
        (
            "Quero reembolso",
            "Gostaria de solicitar o reembolso dentro da garantia.",
            EmailCategory.REFUND,
        ),
        (
            "Dúvida do capítulo 3",
            "Onde encontro a planilha citada no material?",
            EmailCategory.CONTENT,
        ),
        (
            "Nota fiscal",
            "Preciso da nota fiscal da compra feita pelo CNPJ da empresa.",
            EmailCategory.INVOICE,
        ),
        (
            "Arquivo não abre",
            "O PDF não abre no meu celular, dá erro ao baixar.",
            EmailCategory.TECHNICAL,
        ),
        (
            "Quero divulgar",
            "Como faço para me afiliar e receber comissao pelas vendas?",
            EmailCategory.AFFILIATE,
        ),
    ],
)
def test_classificacao_dos_casos_recorrentes(assunto, corpo, esperado):
    triage = support.classify(make_email(assunto, corpo))
    assert triage.category is esperado
    assert triage.confidence > 0.5


def test_email_sem_sinal_conhecido_fica_como_outro():
    triage = support.classify(make_email("Olá", "Tudo bem com você?"))
    assert triage.category is EmailCategory.OTHER
    assert triage.confidence == 0.0


def test_mencao_a_procon_domina_a_classificacao():
    triage = support.classify(
        make_email(
            "Sobre o acesso",
            "Não recebi meu acesso e se não resolverem hoje eu vou no Procon.",
        )
    )
    assert triage.category is EmailCategory.COMPLAINT
    assert "juridico" in triage.flags
    assert triage.urgency == "alta"


def test_acento_nao_muda_classificacao():
    com = support.classify(make_email("Dúvida", "Não achei o capítulo três."))
    sem = support.classify(make_email("Duvida", "Nao achei o capitulo tres."))
    assert com.category is sem.category is EmailCategory.CONTENT
    assert com.confidence == sem.confidence


def test_ameaca_de_chargeback_nunca_e_respondida_automaticamente(settings):
    mail = make_email(
        "ABSURDO",
        "Faz 3 dias que peço reembolso. Vou fazer chargeback no cartão e abrir "
        "reclamação no Reclame Aqui.",
    )
    triage = support.classify(mail)
    auto, motivo = support.decide_auto_send(triage, mail, settings)
    assert auto is False
    assert "escalonamento" in motivo


def test_cliente_irritado_sem_ameaca_tambem_vai_para_humano(settings):
    mail = make_email(
        "Péssimo", "Serviço péssimo, me sinto enganado com esse material."
    )
    triage = support.classify(mail)
    auto, motivo = support.decide_auto_send(triage, mail, settings)
    assert auto is False
    assert "humana" in motivo


def test_confianca_baixa_bloqueia_envio_automatico(settings):
    mail = make_email("Ajuda", "Preciso de ajuda com o celular.")
    triage = support.classify(mail)
    auto, motivo = support.decide_auto_send(triage, mail, settings)
    assert auto is False
    assert "confiança" in motivo or "categoria" in motivo


def test_categoria_recorrente_e_respondida_sozinha(settings):
    mail = make_email(
        "Não recebi o acesso",
        "Comprei ontem e não chegou nada no e-mail, já procurei no spam.",
    )
    triage = support.classify(mail)
    auto, _ = support.decide_auto_send(triage, mail, settings)
    assert auto is True


def test_reembolso_sempre_passa_por_revisao(settings):
    mail = make_email("Reembolso", "Quero cancelar a compra e receber o estorno.")
    triage = support.classify(mail)
    auto, motivo = support.decide_auto_send(triage, mail, settings)
    assert auto is False
    assert "reembolso" in motivo


def test_resposta_usa_dados_do_produto(settings):
    mail = make_email(
        "Não recebi o acesso",
        "Não chegou nada no meu e-mail.",
        "maria.silva@example.com",
    )
    triage = support.classify(mail)
    corpo = support.render_reply(mail, triage, settings)
    assert "Maria" in corpo
    assert settings.product.name in corpo
    assert settings.product.support_signature in corpo
    assert "{" not in corpo  # nenhum placeholder sobrou


def test_triagem_da_caixa_gera_sinais(settings, mailbox):
    replies, findings = support.triage_inbox(mailbox.fetch_unread(), settings)
    assert len(replies) == 8
    codes = {f.code for f in findings}
    assert "SUPPORT_ESCALATION" in codes
    # A caixa simulada tem 1 e-mail com risco jurídico e nenhum é respondido
    # automaticamente por engano.
    escalados = [r for r in replies if r.triage.flags]
    assert len(escalados) == 1
    assert all(not r.auto_sendable for r in escalados)


def test_limite_de_envios_automaticos_por_execucao(settings, mailbox):
    from hotmart_agent.models import ActionType
    from hotmart_agent.policy import actions_from_emails

    restrito = settings.with_(
        guardrails=settings.guardrails.__class__(max_auto_emails_per_run=1)
    )
    replies, _ = support.triage_inbox(mailbox.fetch_unread(), restrito)
    actions = actions_from_emails(replies, restrito)
    enviados = [a for a in actions if a.type is ActionType.SEND_EMAIL]
    assert len(enviados) == 1


def test_garantia_legal(settings):
    from datetime import datetime, timedelta

    dentro = make_email("x", "y")
    fora = make_email("x", "y")
    fora.received_at = datetime.utcnow() - timedelta(days=30)
    assert support.is_within_guarantee(dentro, settings) is True
    assert support.is_within_guarantee(fora, settings) is False
