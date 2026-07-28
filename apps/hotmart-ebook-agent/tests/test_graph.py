"""Grafo ponta a ponta: um comando entra, ações saem — com humano no meio."""

from __future__ import annotations

from dataclasses import replace

from langgraph.types import Command

from hotmart_agent.config import Autonomy
from hotmart_agent.errors import IntegrationError
from hotmart_agent.graph import Clients, build_graph
from hotmart_agent.models import ActionType, ExecutionMode


def run(settings, clients, command, thread="t"):
    graph = build_graph(settings, clients)
    config = {"configurable": {"thread_id": thread}}
    return graph, config, graph.invoke({"command": command}, config=config)


def test_comando_de_analise_gera_relatorio(settings, hotmart, meta, mailbox):
    clients = Clients(settings, hotmart=hotmart, meta=meta, mailbox=mailbox)
    _, _, state = run(settings, clients, "como estão as vendas essa semana?")
    assert state["metrics"]
    assert state["findings"]
    assert "Desempenho" in state["report"]
    assert state["next_step"]


def test_comando_vago_interrompe_e_pergunta(settings, hotmart, meta, mailbox):
    clients = Clients(settings, hotmart=hotmart, meta=meta, mailbox=mailbox)
    graph, config, state = run(settings, clients, "resolve isso", thread="vago")
    interrupcao = state["__interrupt__"][0].value
    assert interrupcao["type"] == "clarification"
    assert "?" in interrupcao["question"]

    final = graph.invoke(Command(resume="quero ver as vendas"), config=config)
    assert final.get("metrics")


def test_resposta_ainda_vaga_assume_briefing(settings, hotmart, meta, mailbox):
    """Perguntar duas vezes trava o usuário — na segunda o agente assume."""
    clients = Clients(settings, hotmart=hotmart, meta=meta, mailbox=mailbox)
    graph, config, _ = run(settings, clients, "resolve isso", thread="vago2")
    final = graph.invoke(Command(resume="sei lá, você que sabe"), config=config)
    assert final["routing"]["intents"]
    assert final.get("report")


def test_acao_de_risco_pausa_para_aprovacao(settings, hotmart, meta, mailbox):
    clients = Clients(settings, hotmart=hotmart, meta=meta, mailbox=mailbox)
    _, _, state = run(
        settings, clients, "revisa as campanhas do meta ads", thread="ads"
    )
    interrupcao = state["__interrupt__"][0].value
    assert interrupcao["type"] == "approval"
    assert interrupcao["actions"]
    assert meta.created == []  # nada foi criado antes de aprovar


def test_rejeicao_nao_executa_nada(settings, hotmart, meta, mailbox):
    clients = Clients(settings, hotmart=hotmart, meta=meta, mailbox=mailbox)
    graph, config, _ = run(settings, clients, "cria campanha no meta ads", thread="rej")
    final = graph.invoke(Command(resume="rejeitar tudo"), config=config)
    assert meta.created == []
    assert any(r["status"] == "rejected" for r in final["results"])


def test_aprovacao_executa_de_verdade(settings, hotmart, meta, mailbox):
    clients = Clients(settings, hotmart=hotmart, meta=meta, mailbox=mailbox)
    graph, config, _ = run(settings, clients, "cria campanha no meta ads", thread="apr")
    final = graph.invoke(Command(resume="aprovar tudo"), config=config)
    assert meta.created
    assert all(p.start_paused for p in meta.created)  # nasce pausada
    assert any(r["status"] == "done" for r in final["results"])


def test_aprovacao_seletiva_por_id(settings, hotmart, meta, mailbox):
    clients = Clients(settings, hotmart=hotmart, meta=meta, mailbox=mailbox)
    graph, config, state = run(
        settings, clients, "cria campanha no meta ads", thread="sel"
    )
    pendentes = state["__interrupt__"][0].value["actions"]
    escolhido = pendentes[0]["id"]
    final = graph.invoke(Command(resume={"approved": [escolhido]}), config=config)
    aprovadas = [r for r in final["results"] if r["status"] == "done"]
    rejeitadas = [r for r in final["results"] if r["status"] == "rejected"]
    assert escolhido in {r["action_id"] for r in aprovadas}
    assert rejeitadas


def test_suporte_responde_e_escalona(settings, hotmart, meta, mailbox):
    """L3: responde o recorrente sozinho, escala o que tem risco jurídico."""
    autonomo = settings.with_(autonomy=Autonomy.AUTONOMOUS)
    clients = Clients(autonomo, hotmart=hotmart, meta=meta, mailbox=mailbox)
    _, _, state = run(autonomo, clients, "responde os e-mails do suporte", thread="sup")

    assert mailbox.sent, "nenhum e-mail recorrente foi respondido"
    assert mailbox.drafts, "nenhum caso sensível foi para rascunho"
    escalados = [label for _, label in mailbox.handled if "escalado" in label]
    assert escalados, "o e-mail com ameaça de chargeback não foi escalado"
    # O cliente hostil nunca recebe resposta automática.
    assert not any("lucas.ferreira" in m["to"] for m in mailbox.sent)


def test_modo_observacao_nao_toca_em_nada(settings, hotmart, meta, mailbox):
    observador = settings.with_(autonomy=Autonomy.OBSERVE)
    clients = Clients(observador, hotmart=hotmart, meta=meta, mailbox=mailbox)
    _, _, state = run(observador, clients, "resumo geral", thread="obs")
    assert "__interrupt__" not in state
    assert mailbox.sent == [] and meta.created == []
    assert all(
        a["mode"] == ExecutionMode.RECOMMEND.value
        for a in state["actions"]
        if a["type"] != ActionType.REPORT.value
    )
    assert state["report"]


def test_dry_run_nao_faz_chamada_externa(settings, hotmart, meta, mailbox):
    simulado = settings.with_(autonomy=Autonomy.AUTONOMOUS, dry_run=True)
    clients = Clients(simulado, hotmart=hotmart, meta=meta, mailbox=mailbox)
    _, _, state = run(simulado, clients, "resumo geral", thread="dry")
    assert mailbox.sent == [] and meta.created == []
    assert any("simulada" in r["detail"] for r in state["results"])


def test_hotmart_fora_do_ar_degrada_sem_derrubar(settings, meta, mailbox):
    class HotmartQuebrado:
        def fetch_snapshot(self, window_days):
            raise IntegrationError("hotmart", "502", retryable=True)

    clients = Clients(settings, hotmart=HotmartQuebrado(), meta=meta, mailbox=mailbox)
    graph, config, state = run(settings, clients, "resumo geral", thread="deg")
    assert any("Hotmart indisponível" in d for d in state["degraded"])
    # As outras frentes continuaram funcionando apesar da falha.
    assert state["email_replies"]
    assert state["ad_account"]

    final = graph.invoke(Command(resume="rejeitar tudo"), config=config)
    assert "Execução parcial" in final["report"]
    assert "Hotmart indisponível" in final["report"]


def test_conta_de_anuncios_bloqueada_nao_propoe_campanha(settings, hotmart, mailbox):
    from hotmart_agent.integrations.fakes import FakeMetaAdsClient

    bloqueada = FakeMetaAdsClient(settings, account_status=2, disable_reason=2)
    clients = Clients(settings, hotmart=hotmart, meta=bloqueada, mailbox=mailbox)
    _, _, state = run(settings, clients, "cria campanha no meta ads", thread="blk")
    assert state["campaign_plans"] == []
    assert any(f["code"] == "AD_ACCOUNT_DISABLED" for f in state["findings"])
    assert bloqueada.created == []


def test_saldo_insuficiente_impede_campanha(settings, hotmart, mailbox):
    from hotmart_agent.integrations.fakes import FakeMetaAdsClient

    magra = FakeMetaAdsClient(settings, prepaid_balance_brl=15.0)
    clients = Clients(settings, hotmart=hotmart, meta=magra, mailbox=mailbox)
    _, _, state = run(
        settings, clients, "cria campanha no meta ads", thread="sem-saldo"
    )
    assert state["campaign_plans"] == []
    assert any(f["code"] == "AD_LOW_BALANCE" for f in state["findings"])


def test_guardrail_bloqueia_orcamento_acima_do_teto(settings, hotmart, meta, mailbox):
    apertado = settings.with_(
        autonomy=Autonomy.AUTONOMOUS,
        guardrails=replace(settings.guardrails, max_daily_budget_brl=5.0),
    )
    clients = Clients(apertado, hotmart=hotmart, meta=meta, mailbox=mailbox)
    _, _, state = run(apertado, clients, "escala as campanhas boas", thread="guard")
    bloqueadas = [
        a for a in state["actions"] if a["mode"] == ExecutionMode.BLOCKED.value
    ]
    assert bloqueadas
    assert all(u[1] <= 5.0 for u in meta.budget_updates)


def test_trilha_de_auditoria_registra_a_execucao(settings, hotmart, meta, mailbox):
    autonomo = settings.with_(autonomy=Autonomy.AUTONOMOUS)
    clients = Clients(autonomo, hotmart=hotmart, meta=meta, mailbox=mailbox)
    _, _, state = run(autonomo, clients, "resumo geral", thread="audit")
    eventos = {e["event"] for e in state["audit"]}
    assert {"command_received", "decision", "report"} <= eventos
    assert all("ts" in e and "actor" in e for e in state["audit"])


def test_estado_sobrevive_entre_invocacoes(settings, hotmart, meta, mailbox):
    """O checkpoint permite aprovar horas depois, sem refazer a coleta."""
    clients = Clients(settings, hotmart=hotmart, meta=meta, mailbox=mailbox)
    graph, config, _ = run(
        settings, clients, "cria campanha no meta ads", thread="persist"
    )
    snapshot = graph.get_state(config)
    assert snapshot.next  # parado, aguardando resposta
    final = graph.invoke(Command(resume="aprovar tudo"), config=config)
    assert final["report"]
