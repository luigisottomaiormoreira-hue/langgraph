"""Suporte ao cliente: triagem e resposta de e-mails recorrentes.

Estratégia em duas camadas:

1. Classificador por regras (determinístico, auditável, custo zero). Resolve os
   casos recorrentes — que são a maioria absoluta em produto de infoproduto.
2. LLM como desempate, acionado apenas quando as regras ficam abaixo do limiar
   de confiança. Se o modelo não estiver disponível, o e-mail vai para revisão
   humana — nunca para uma resposta chutada.

Um e-mail com sinal jurídico, ameaça de chargeback ou hostilidade nunca é
respondido automaticamente, independentemente da confiança.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta

from ..config import Settings
from ..formatting import pct
from ..llm import complete_json
from ..models import (
    Domain,
    EmailCategory,
    EmailReply,
    Finding,
    InboundEmail,
    Severity,
    Triage,
)

# --------------------------------------------------------------------------
# Léxico de classificação. Peso maior = sinal mais específico da categoria.
# --------------------------------------------------------------------------

KEYWORDS: dict[EmailCategory, list[tuple[str, float]]] = {
    EmailCategory.ACCESS: [
        ("nao recebi", 3.0),
        ("nao chegou", 2.5),
        ("acesso", 2.0),
        ("login", 2.0),
        ("senha", 1.5),
        ("liberar", 1.5),
        ("como faco para baixar", 2.0),
        ("link do ebook", 2.5),
        ("area de membros", 2.5),
        ("hotmart club", 2.0),
        ("spam", 1.0),
    ],
    EmailCategory.PAYMENT: [
        ("boleto", 3.0),
        ("pix", 2.5),
        ("comprovante", 2.5),
        ("paguei", 2.5),
        ("cartao recusado", 3.0),
        ("cobranca duplicada", 3.0),
        ("cobrado duas vezes", 3.0),
        ("parcelamento", 2.0),
        ("nao liberou", 1.5),
    ],
    EmailCategory.REFUND: [
        ("reembolso", 3.5),
        ("estorno", 3.0),
        ("dinheiro de volta", 3.0),
        ("cancelar a compra", 2.5),
        ("garantia", 2.0),
        ("desistir", 2.0),
        ("arrependimento", 2.0),
    ],
    EmailCategory.CONTENT: [
        ("capitulo", 2.5),
        ("pagina do ebook", 2.0),
        ("duvida sobre", 2.0),
        ("planilha", 2.0),
        ("exercicio", 1.5),
        ("bonus", 1.5),
        ("como aplicar", 1.5),
    ],
    EmailCategory.INVOICE: [
        ("nota fiscal", 3.5),
        ("nf-e", 3.0),
        ("cnpj", 2.0),
        ("recibo", 2.0),
        ("comprovante fiscal", 2.5),
    ],
    EmailCategory.TECHNICAL: [
        ("pdf nao abre", 3.0),
        ("erro ao baixar", 3.0),
        ("arquivo corrompido", 3.0),
        ("nao consigo abrir", 2.5),
        ("kindle", 2.0),
        ("epub", 2.0),
        ("celular", 1.0),
    ],
    EmailCategory.AFFILIATE: [
        ("afiliad", 3.5),
        ("divulgar", 2.0),
        ("comissao", 2.5),
        ("seguidores", 1.5),
    ],
    EmailCategory.PARTNERSHIP: [
        ("parceria", 3.0),
        ("permuta", 2.5),
        ("publieditorial", 2.5),
        ("proposta comercial", 2.0),
    ],
    EmailCategory.COMPLAINT: [
        ("procon", 4.0),
        ("reclame aqui", 4.0),
        ("advogado", 4.0),
        ("processo", 3.0),
        ("golpe", 3.5),
        ("estelionato", 4.0),
        ("absurdo", 2.0),
        ("pessimo", 2.0),
        ("chargeback", 3.5),
    ],
    EmailCategory.SPAM: [
        ("oferta imperdivel", 3.0),
        ("clique aqui e", 2.5),
        ("aumente suas vendas", 3.0),
        ("robo de trafego", 3.0),
        ("triplique seu faturamento", 3.0),
        ("marketing digital gratis", 2.5),
    ],
}

# Sinais que travam qualquer envio automático, venham na categoria que vierem.
ESCALATION_TERMS = {
    "procon": "juridico",
    "advogado": "juridico",
    "processo": "juridico",
    "justica": "juridico",
    "estelionato": "juridico",
    "golpe": "reputacao",
    "reclame aqui": "reputacao",
    "imprensa": "reputacao",
    "jornalista": "reputacao",
    "chargeback": "chargeback",
    "contestar no cartao": "chargeback",
    "lgpd": "privacidade",
    "excluir meus dados": "privacidade",
}

HOSTILE_TERMS = {"absurdo", "vergonha", "palhacada", "ladrao", "pessimo", "enganado"}

# Categorias que o agente pode responder sozinho: resposta padronizada, baixo
# risco e alto volume.
AUTO_REPLYABLE = {
    EmailCategory.ACCESS,
    EmailCategory.PAYMENT,
    EmailCategory.CONTENT,
    EmailCategory.TECHNICAL,
    EmailCategory.INVOICE,
    EmailCategory.SPAM,
}


def normalize(text: str) -> str:
    """Minúsculas sem acento — o cliente escreve 'duvida' e 'dúvida'."""
    lowered = text.lower()
    stripped = unicodedata.normalize("NFKD", lowered)
    return "".join(c for c in stripped if not unicodedata.combining(c))


def classify(mail: InboundEmail) -> Triage:
    """Classificação por pontuação de termos, com confiança calibrada.

    A confiança é a distância relativa entre o primeiro e o segundo colocado:
    dois candidatos empatados devolvem confiança baixa e caem para revisão.
    """
    haystack = normalize(f"{mail.subject}\n{mail.body}")
    scores: dict[EmailCategory, float] = {}
    matched: dict[EmailCategory, list[str]] = {}

    for category, terms in KEYWORDS.items():
        total = 0.0
        hits: list[str] = []
        for term, weight in terms:
            if normalize(term) in haystack:
                total += weight
                hits.append(term)
        if total:
            scores[category] = total
            matched[category] = hits

    if not scores:
        return Triage(
            email_id=mail.id,
            category=EmailCategory.OTHER,
            confidence=0.0,
            sentiment="neutro",
        )

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top, top_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    separation = (top_score - runner_up) / top_score
    # Duas parcelas: o quanto o vencedor se destaca (separação) e o quanto de
    # evidência absoluta existe. Um único termo fraco não passa do limiar de
    # envio automático, mesmo sem concorrente.
    confidence = min(0.95, 0.30 + 0.35 * separation + min(top_score, 7.0) / 23)

    flags = sorted(
        {flag for term, flag in ESCALATION_TERMS.items() if normalize(term) in haystack}
    )
    hostile = any(normalize(t) in haystack for t in HOSTILE_TERMS)
    sentiment = "hostil" if hostile and flags else ("negativo" if hostile else "neutro")
    urgency = "alta" if flags or top == EmailCategory.COMPLAINT else "normal"

    # Reclamação com sinal jurídico domina qualquer outra categoria.
    if flags and top not in {EmailCategory.COMPLAINT, EmailCategory.REFUND}:
        top = EmailCategory.COMPLAINT
        confidence = max(confidence, 0.9)

    return Triage(
        email_id=mail.id,
        category=top,
        confidence=round(confidence, 3),
        sentiment=sentiment,
        urgency=urgency,
        flags=flags,
        matched_terms=matched.get(top, [])[:6],
        classifier="rules",
    )


# --------------------------------------------------------------------------
# Templates de resposta
# --------------------------------------------------------------------------


def _greeting(mail: InboundEmail) -> str:
    local = mail.sender.split("@")[0]
    name = re.split(r"[._\-+]", local)[0]
    return name.capitalize() if name.isalpha() and len(name) > 2 else "Olá"


TEMPLATES: dict[EmailCategory, str] = {
    EmailCategory.ACCESS: (
        "Olá, {name}!\n\n"
        "Localizamos a sua compra do {product} e o acesso já foi reenviado para "
        "este mesmo e-mail. Se não aparecer em até 10 minutos:\n\n"
        "1. Verifique as abas Promoções e Spam;\n"
        "2. Acesse {club_url} e entre com o e-mail usado na compra;\n"
        '3. Use a opção "Esqueci minha senha" para definir uma nova.\n\n'
        "Se ainda assim não conseguir, responda esta mensagem informando o e-mail "
        "usado na compra que liberamos o acesso manualmente.\n\n"
        "{signature}"
    ),
    EmailCategory.PAYMENT: (
        "Olá, {name}!\n\n"
        "Obrigado pelo aviso. Pagamentos por boleto levam de 1 a 3 dias úteis para "
        "compensar, e por PIX a liberação costuma ser em minutos. Assim que a "
        "Hotmart confirmar, o acesso ao {product} é enviado automaticamente para "
        "o seu e-mail.\n\n"
        "Se já se passaram mais de 3 dias úteis do pagamento, responda esta "
        "mensagem com o comprovante que verificamos diretamente com a plataforma.\n\n"
        "{signature}"
    ),
    EmailCategory.CONTENT: (
        "Olá, {name}!\n\n"
        "Obrigado pela pergunta sobre o {product} — é justamente esse tipo de dúvida "
        "que mostra que o material está sendo aplicado.\n\n"
        "Todos os materiais complementares (planilhas, checklists e bônus) ficam na "
        "área de materiais dentro de {club_url}, no mesmo módulo do capítulo "
        "correspondente.\n\n"
        "Se a sua dúvida for sobre a aplicação prática do conteúdo, responda com um "
        "pouco mais de detalhe do seu caso que orientamos o próximo passo.\n\n"
        "{signature}"
    ),
    EmailCategory.TECHNICAL: (
        "Olá, {name}!\n\n"
        "Vamos resolver. O {product} é entregue em PDF e funciona em qualquer "
        "leitor. Se o arquivo não abrir:\n\n"
        "1. Baixe novamente pelo computador (downloads pelo navegador do celular "
        "às vezes vêm incompletos);\n"
        "2. Abra com Adobe Acrobat Reader ou com o próprio navegador;\n"
        "3. Confirme que o download chegou ao fim antes de abrir.\n\n"
        "Se o problema continuar, informe o aparelho e o programa que está usando "
        "que enviamos uma versão alternativa do arquivo.\n\n"
        "{signature}"
    ),
    EmailCategory.INVOICE: (
        "Olá, {name}!\n\n"
        "A nota fiscal da compra do {product} é emitida pela Hotmart, que é a "
        "processadora do pagamento. Para obtê-la:\n\n"
        "1. Acesse hotmart.com/pt-br e entre com o e-mail da compra;\n"
        "2. Vá em Minhas compras e selecione o pedido;\n"
        "3. Clique em Nota fiscal / Comprovante.\n\n"
        "Se precisar da nota em CNPJ e a compra foi feita em CPF, responda com os "
        "dados corretos que solicitamos o ajuste junto à plataforma.\n\n"
        "{signature}"
    ),
    EmailCategory.REFUND: (
        "Olá, {name}!\n\n"
        "Sem problema — a sua solicitação de reembolso do {product} foi registrada "
        "e será processada pela Hotmart. O valor volta pelo mesmo meio de pagamento; "
        "no cartão, costuma aparecer na fatura seguinte.\n\n"
        "Se quiser, responda em uma linha o que faltou no material. A informação é "
        "usada para melhorar as próximas versões e não muda em nada o seu reembolso.\n\n"
        "{signature}"
    ),
    EmailCategory.AFFILIATE: (
        "Olá, {name}!\n\n"
        "Que bom o interesse em divulgar o {product}. O programa de afiliados é "
        "gerenciado pela Hotmart: basta buscar o produto no Mercado de Afiliados e "
        "solicitar afiliação — a aprovação sai em até 48h úteis e o material de "
        "divulgação é liberado junto com o link.\n\n"
        "{signature}"
    ),
    EmailCategory.COMPLAINT: (
        "Olá, {name}!\n\n"
        "Sinto muito pela experiência. Assumo o caso pessoalmente e ele já está "
        "como prioridade máxima aqui.\n\n"
        "Para resolver hoje: se o pedido for reembolso, ele será processado "
        "imediatamente, sem burocracia. Se for acesso, libero manualmente agora.\n\n"
        "Responda confirmando qual das duas opções resolve para você.\n\n"
        "{signature}"
    ),
    EmailCategory.OTHER: (
        "Olá, {name}!\n\n"
        "Recebemos a sua mensagem sobre o {product} e ela já está com a nossa "
        "equipe. Respondemos em até 1 dia útil.\n\n"
        "{signature}"
    ),
}

SUBJECT_PREFIX = {
    EmailCategory.ACCESS: "Seu acesso ao",
    EmailCategory.PAYMENT: "Sobre o seu pagamento —",
    EmailCategory.REFUND: "Sua solicitação de reembolso —",
    EmailCategory.CONTENT: "Sobre sua dúvida —",
    EmailCategory.INVOICE: "Nota fiscal —",
    EmailCategory.TECHNICAL: "Suporte técnico —",
    EmailCategory.AFFILIATE: "Programa de afiliados —",
    EmailCategory.COMPLAINT: "Vamos resolver —",
    EmailCategory.OTHER: "Sobre sua mensagem —",
}


def render_reply(mail: InboundEmail, triage: Triage, settings: Settings) -> str:
    product = settings.product
    template = TEMPLATES.get(triage.category, TEMPLATES[EmailCategory.OTHER])
    return template.format(
        name=_greeting(mail),
        product=product.name,
        club_url=product.sales_page_url or "https://hotmart.com/pt-br/club",
        signature=product.support_signature,
    )


def decide_auto_send(
    triage: Triage, mail: InboundEmail, settings: Settings
) -> tuple[bool, str]:
    """Regra de envio automático. Na dúvida, rascunho — nunca envio."""
    g = settings.guardrails
    if triage.flags:
        return False, f"sinal de escalonamento ({', '.join(triage.flags)})"
    if triage.sentiment in {"hostil", "negativo"}:
        return False, "tom negativo do cliente exige resposta humana"
    if triage.category not in AUTO_REPLYABLE:
        return False, f"categoria '{triage.category.value}' exige revisão humana"
    if triage.confidence < g.auto_reply_min_confidence:
        return (
            False,
            f"confiança {pct(triage.confidence, 0)} abaixo do mínimo "
            f"({pct(g.auto_reply_min_confidence, 0)})",
        )
    if triage.category is EmailCategory.SPAM:
        return False, "spam: arquivar sem resposta"
    return True, "categoria recorrente com resposta padronizada"


CLASSIFIER_SYSTEM = """Você classifica e-mails de suporte de um ebook vendido na Hotmart.
Devolva SOMENTE um JSON com as chaves:
  category: uma de ["acesso","pagamento","reembolso","duvida_conteudo","nota_fiscal","tecnico","afiliado","parceria","reclamacao","spam","outro"]
  confidence: número entre 0 e 1
  sentiment: um de "positivo","neutro","negativo","hostil"
  urgency: um de "baixa","normal","alta"
Não invente informação sobre a compra. Se o e-mail mencionar Procon, advogado,
Reclame Aqui ou chargeback, a categoria é "reclamacao"."""


def classify_email(mail: InboundEmail, settings: Settings) -> Triage:
    """Regras primeiro; LLM só como desempate quando a confiança está baixa.

    Um acerto do LLM nunca eleva a confiança acima do limiar de envio
    automático: se as regras não reconheceram o caso, ele merece olho humano.
    """
    triage = classify(mail)
    threshold = settings.guardrails.auto_reply_min_confidence
    if triage.confidence >= threshold or triage.flags:
        return triage

    parsed = complete_json(
        settings,
        CLASSIFIER_SYSTEM,
        f"Assunto: {mail.subject}\n\nCorpo:\n{mail.body[:2000]}",
    )
    if not parsed:
        return triage
    try:
        category = EmailCategory(parsed.get("category", ""))
    except ValueError:
        return triage

    triage.category = category
    triage.classifier = "llm"
    triage.confidence = round(
        min(float(parsed.get("confidence") or 0.5), threshold - 0.01), 3
    )
    triage.sentiment = str(parsed.get("sentiment") or triage.sentiment)
    triage.urgency = str(parsed.get("urgency") or triage.urgency)
    return triage


def triage_inbox(
    emails: list[InboundEmail], settings: Settings
) -> tuple[list[EmailReply], list[Finding]]:
    """Classifica a caixa inteira e devolve respostas + sinais para o produto."""
    replies: list[EmailReply] = []
    counts: dict[EmailCategory, int] = {}

    for mail in emails:
        triage = classify_email(mail, settings)
        counts[triage.category] = counts.get(triage.category, 0) + 1
        auto, reason = decide_auto_send(triage, mail, settings)
        replies.append(
            EmailReply(
                email_id=mail.id,
                to=mail.sender,
                subject=(
                    f"{SUBJECT_PREFIX.get(triage.category, 'Sobre sua mensagem —')} "
                    f"{settings.product.name}"
                ),
                body=render_reply(mail, triage, settings),
                triage=triage,
                auto_sendable=auto,
                reason=reason,
            )
        )

    return replies, inbox_findings(replies, counts, settings)


def inbox_findings(
    replies: list[EmailReply],
    counts: dict[EmailCategory, int],
    settings: Settings,
) -> list[Finding]:
    """A caixa de entrada é um sensor do produto: o que dói aparece nela primeiro."""
    findings: list[Finding] = []
    total = max(1, len(replies))
    margin = settings.product.contribution_margin_brl

    access = counts.get(EmailCategory.ACCESS, 0)
    if access / total >= 0.25 and access >= 2:
        findings.append(
            Finding(
                code="SUPPORT_ACCESS_SPIKE",
                domain=Domain.SUPPORT,
                severity=Severity.WARNING,
                title=f"{access} de {total} e-mails são sobre acesso",
                detail=(
                    "Volume alto de problema de entrega indica que o e-mail de "
                    "liberação está caindo em spam ou que a instrução pós-compra "
                    "não está clara."
                ),
                evidence={"count": access, "total": total},
                recommendation=(
                    "Adicionar as instruções de acesso na página de obrigado e "
                    "configurar SPF/DKIM no domínio do remetente."
                ),
                impact_brl=round(access * margin * 4, 2),
                effort=1.0,
            )
        )

    escalations = [r for r in replies if r.triage.flags]
    if escalations:
        findings.append(
            Finding(
                code="SUPPORT_ESCALATION",
                domain=Domain.SUPPORT,
                severity=Severity.CRITICAL,
                title=f"{len(escalations)} e-mail(s) com risco jurídico ou de reputação",
                detail=(
                    "Mensagens com menção a Procon, advogado, Reclame Aqui ou "
                    "chargeback. Resposta humana em até 4h reduz materialmente a "
                    "chance de contestação."
                ),
                evidence={
                    "ids": [r.email_id for r in escalations],
                    "flags": sorted({f for r in escalations for f in r.triage.flags}),
                },
                recommendation="Responder pessoalmente e resolver na primeira interação.",
                impact_brl=round(len(escalations) * margin * 3, 2),
                effort=0.5,
                suggested_actions=["responder_manualmente"],
            )
        )

    refunds = counts.get(EmailCategory.REFUND, 0)
    if refunds / total >= 0.2 and refunds >= 2:
        findings.append(
            Finding(
                code="SUPPORT_REFUND_PRESSURE",
                domain=Domain.SUPPORT,
                severity=Severity.WARNING,
                title=f"{refunds} pedidos de reembolso na caixa de entrada",
                detail=(
                    "Pedidos por e-mail antecipam o número que vai aparecer no "
                    "painel da Hotmart nos próximos dias."
                ),
                evidence={"count": refunds, "total": total},
                recommendation=(
                    "Processar rápido (evita chargeback) e investigar a causa "
                    "comum nas mensagens."
                ),
                impact_brl=round(refunds * margin * 4, 2),
                effort=1.0,
            )
        )

    content = counts.get(EmailCategory.CONTENT, 0)
    if content >= 2:
        findings.append(
            Finding(
                code="SUPPORT_CONTENT_GAP",
                domain=Domain.PRODUCT,
                severity=Severity.OPPORTUNITY,
                title=f"{content} dúvidas recorrentes sobre o conteúdo",
                detail=(
                    "Perguntas repetidas apontam um trecho pouco claro ou material "
                    "complementar difícil de achar."
                ),
                evidence={"count": content},
                recommendation=(
                    "Criar um FAQ na área de membros e um e-mail automático no D+2 "
                    "cobrindo as dúvidas mais frequentes."
                ),
                impact_brl=round(content * margin * 2, 2),
                effort=1.0,
            )
        )
    return findings


def is_within_guarantee(mail: InboundEmail, settings: Settings) -> bool:
    """Se o pedido está dentro da garantia legal, o reembolso é obrigatório."""
    limit = datetime.utcnow() - timedelta(days=settings.product.guarantee_days)
    return mail.received_at >= limit
