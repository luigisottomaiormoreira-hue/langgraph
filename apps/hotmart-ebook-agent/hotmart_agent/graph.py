"""Grafo do agente (LangGraph).

    START
      │
      ▼
  interpretar ──(comando vago)──► esclarecer ──┐
      │                                        │ (resposta do usuário)
      │◄───────────────────────────────────────┘
      ▼
    planejar ──┬──► analytics ──┐
               ├──► suporte  ───┤
               └──► ads ────────┤
                                ▼
                             decidir
                                │
                       (ação de risco?)
                                ▼
                            aprovar ──(interrupt)──► humano
                                │
                                ▼
                            executar
                                │
                                ▼
                            relatar ──► END

Cada módulo roda em paralelo e escreve `findings` através de um reducer, de modo
que a falha de um não impede os outros — ela só marca a execução como degradada.
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .config import Autonomy, Settings
from .errors import AgentError, CircuitOpen, GuardrailViolation, IntegrationError
from .formatting import brl, num, pct, signed_pct, times
from .integrations import (
    build_hotmart_client,
    build_mailbox_client,
    build_meta_client,
)
from .llm import polish_text
from .models import (
    Action,
    ActionResult,
    ActionType,
    ExecutionMode,
    Finding,
    Intent,
    SalesSnapshot,
    Severity,
)
from .modules import ads as ads_module
from .modules import analytics as analytics_module
from .modules import support as support_module
from .observability import audit_entry
from .policy import (
    actions_from_campaigns,
    actions_from_emails,
    actions_from_findings,
    next_best_action,
    prioritize,
    rank_actions,
)
from .router import route
from .state import AgentState

logger = logging.getLogger("hotmart_agent.graph")


class Clients:
    """Injeção de dependências: permite trocar tudo por fakes nos testes."""

    def __init__(self, settings: Settings, *, hotmart=None, meta=None, mailbox=None):
        self.settings = settings
        self._hotmart = hotmart
        self._meta = meta
        self._mailbox = mailbox

    @property
    def hotmart(self):
        if self._hotmart is None:
            self._hotmart = build_hotmart_client(self.settings)
        return self._hotmart

    @property
    def meta(self):
        if self._meta is None:
            self._meta = build_meta_client(self.settings)
        return self._meta

    @property
    def mailbox(self):
        if self._mailbox is None:
            self._mailbox = build_mailbox_client(self.settings)
        return self._mailbox


def build_graph(
    settings: Settings,
    clients: Clients | None = None,
    *,
    checkpointer: Any | None = None,
):
    """Compila o grafo do agente para um dado conjunto de configurações."""
    clients = clients or Clients(settings)

    # ------------------------------------------------------------------
    # 1. Interpretar o comando
    # ------------------------------------------------------------------
    def interpretar(state: AgentState) -> dict[str, Any]:
        command = state.get("command", "")
        decision = route(command, settings)
        return {
            "routing": decision.to_dict(),
            "clarification": decision.clarification,
            "audit": [
                audit_entry(
                    "command_received",
                    actor="user",
                    detail=command[:200],
                    payload=decision.to_dict(),
                )
            ],
        }

    def esclarecer(state: AgentState) -> dict[str, Any]:
        """Pausa o grafo e devolve a pergunta ao usuário.

        `interrupt` congela a execução no checkpoint; a resposta chega no
        retomar, sem que nada precise ser refeito.
        """
        question = state.get("clarification") or "Pode detalhar o que você precisa?"
        answer = interrupt(
            {
                "type": "clarification",
                "question": question,
                "options": [
                    "analisar desempenho das vendas",
                    "responder e-mails do suporte",
                    "revisar campanhas do Meta Ads",
                    "resumo geral (as três frentes)",
                ],
            }
        )
        answer_text = answer if isinstance(answer, str) else str(answer)
        combined = f"{state.get('command', '')} {answer_text}".strip()
        decision = route(combined, settings)
        if Intent.CLARIFY in decision.intents:
            # Segunda tentativa vaga: assume o briefing completo em vez de
            # perguntar de novo e travar o usuário num laço.
            decision.intents = [
                Intent.ANALYZE_PERFORMANCE,
                Intent.HANDLE_EMAILS,
                Intent.MANAGE_ADS,
                Intent.CHECK_AD_BUDGET,
            ]
            decision.clarification = None
            decision.reasoning = "resposta ainda ampla: assumindo briefing completo"
        return {
            "command": combined,
            "routing": decision.to_dict(),
            "clarification": None,
            "clarified": True,
            "history": [{"role": "user", "content": answer_text}],
        }

    def rota_pos_interpretacao(state: AgentState) -> str:
        routing = state.get("routing", {})
        if Intent.CLARIFY.value in routing.get("intents", []) and not state.get(
            "clarified"
        ):
            return "esclarecer"
        return "planejar"

    # ------------------------------------------------------------------
    # 2. Planejar quais módulos rodam
    # ------------------------------------------------------------------
    def planejar(state: AgentState) -> dict[str, Any]:
        routing = state.get("routing", {})
        intents = routing.get("intents", [])
        return {
            "audit": [
                audit_entry(
                    "plan",
                    detail=f"módulos: {', '.join(intents) or 'nenhum'}",
                    payload={"params": routing.get("params", {})},
                )
            ]
        }

    def despachar(state: AgentState) -> list[str]:
        """Fan-out: devolve os nós que devem rodar em paralelo."""
        intents = set(state.get("routing", {}).get("intents", []))
        targets: list[str] = []
        if {
            Intent.ANALYZE_PERFORMANCE.value,
            Intent.ANSWER_QUESTION.value,
        } & intents:
            targets.append("analytics")
        if Intent.HANDLE_EMAILS.value in intents:
            targets.append("suporte")
        if {Intent.MANAGE_ADS.value, Intent.CHECK_AD_BUDGET.value} & intents:
            targets.append("ads")
        return targets or ["analytics"]

    # ------------------------------------------------------------------
    # 3. Módulos (executam em paralelo)
    # ------------------------------------------------------------------
    def analytics(state: AgentState) -> dict[str, Any]:
        window = int(
            state.get("routing", {})
            .get("params", {})
            .get("window_days", settings.analysis_window_days)
        )
        try:
            snapshot: SalesSnapshot = clients.hotmart.fetch_snapshot(window)
        except (IntegrationError, CircuitOpen, AgentError) as exc:
            logger.warning("analytics degradado: %s", exc)
            return {
                "degraded": [f"Hotmart indisponível: {exc}"],
                "findings": [],
                "audit": [audit_entry("analytics_failed", detail=str(exc))],
            }

        metrics = analytics_module.compute_metrics(snapshot)
        findings = analytics_module.analyze(snapshot, settings)
        return {
            "snapshot": snapshot.to_dict(),
            "metrics": [m.to_dict() for m in metrics],
            "findings": [f.to_dict() for f in findings],
            "audit": [
                audit_entry(
                    "analytics_done",
                    detail=analytics_module.summarize(snapshot, metrics),
                    payload={"window_days": window, "findings": len(findings)},
                )
            ],
        }

    def suporte(state: AgentState) -> dict[str, Any]:
        try:
            emails = clients.mailbox.fetch_unread(
                limit=settings.guardrails.max_auto_emails_per_run * 2
            )
        except (IntegrationError, CircuitOpen, AgentError) as exc:
            logger.warning("suporte degradado: %s", exc)
            return {
                "degraded": [f"Caixa de entrada indisponível: {exc}"],
                "findings": [],
                "audit": [audit_entry("support_failed", detail=str(exc))],
            }

        replies, findings = support_module.triage_inbox(emails, settings)
        return {
            "email_replies": [r.to_dict() for r in replies],
            "findings": [f.to_dict() for f in findings],
            "audit": [
                audit_entry(
                    "support_triaged",
                    detail=f"{len(replies)} e-mails classificados",
                    payload={
                        "auto": sum(1 for r in replies if r.auto_sendable),
                        "revisao": sum(1 for r in replies if not r.auto_sendable),
                    },
                )
            ],
        }

    def ads(state: AgentState) -> dict[str, Any]:
        """Ordem obrigatória: saldo/status -> desempenho -> plano."""
        try:
            status = clients.meta.get_account_status()
        except (IntegrationError, CircuitOpen, AgentError) as exc:
            logger.warning("ads degradado: %s", exc)
            return {
                "degraded": [f"Meta Ads indisponível: {exc}"],
                "findings": [],
                "audit": [audit_entry("ads_failed", detail=str(exc))],
            }

        findings = ads_module.check_funding(status, settings)
        campaigns = []
        try:
            campaigns = clients.meta.list_campaign_performance(
                int(state.get("routing", {}).get("params", {}).get("window_days", 7))
            )
            findings += ads_module.diagnose_campaigns(campaigns, settings)
        except (IntegrationError, CircuitOpen) as exc:
            logger.warning("insights indisponíveis: %s", exc)

        # O plano de campanha depende também dos dados de venda, que chegam do
        # nó de analytics em paralelo — por isso ele é montado em `decidir`,
        # depois do fan-in. Aqui só coletamos e diagnosticamos.
        return {
            "ad_account": status.to_dict(),
            "campaigns": [c.to_dict() for c in campaigns],
            "findings": [f.to_dict() for f in findings],
            "audit": [
                audit_entry(
                    "ads_reviewed",
                    detail=(
                        f"conta {'ativa' if status.is_active else 'inativa'}; "
                        f"{len(campaigns)} campanha(s) analisada(s)"
                    ),
                    payload={"available_brl": status.to_dict().get("available_brl")},
                )
            ],
        }

    # ------------------------------------------------------------------
    # 4. Decidir
    # ------------------------------------------------------------------
    def decidir(state: AgentState) -> dict[str, Any]:
        """Fan-in: junta os sinais dos módulos, monta e prioriza as ações.

        É aqui que o plano de mídia é fechado, porque só neste ponto existem ao
        mesmo tempo os dados de venda (Hotmart) e o estado da conta (Meta).
        """
        findings = [Finding.from_dict(f) for f in state.get("findings", [])]

        plans: list[Any] = []
        notes: list[str] = []
        account_raw = state.get("ad_account")
        wants_ads = Intent.MANAGE_ADS.value in state.get("routing", {}).get(
            "intents", []
        )
        if account_raw and wants_ads:
            status = _account_from_dict(account_raw)
            campaigns = [_campaign_from_dict(c) for c in state.get("campaigns") or []]
            snapshot = _snapshot_from_dict(state.get("snapshot") or {})
            catalog = ads_module.build_campaign_catalog(snapshot, campaigns, settings)
            plans, notes = ads_module.select_campaigns(catalog, status, settings)

        ranked = prioritize(findings, settings)

        actions: list[Action] = []
        replies = state.get("email_replies") or []
        if replies:
            actions += actions_from_emails(
                [_reply_from_dict(r) for r in replies], settings
            )
        if plans:
            actions += actions_from_campaigns(plans, settings)
        actions += actions_from_findings(ranked, settings)

        ordered = rank_actions(actions, ranked, settings)
        return {
            "campaign_plans": [p.to_dict() for p in plans],
            "plan_notes": notes,
            "actions": [a.to_dict() for a in ordered],
            "next_step": next_best_action(ordered, ranked),
            "audit": [
                audit_entry(
                    "decision",
                    detail=f"{len(ranked)} sinais, {len(ordered)} ações propostas",
                    payload={
                        "auto": sum(1 for a in ordered if a.mode is ExecutionMode.AUTO),
                        "aprovacao": sum(
                            1 for a in ordered if a.mode is ExecutionMode.APPROVAL
                        ),
                    },
                )
            ],
        }

    # ------------------------------------------------------------------
    # 5. Aprovar (human-in-the-loop)
    # ------------------------------------------------------------------
    def precisa_aprovacao(state: AgentState) -> str:
        actions = state.get("actions", [])
        pending = [a for a in actions if a["mode"] == ExecutionMode.APPROVAL.value]
        executable = [a for a in actions if a["mode"] == ExecutionMode.AUTO.value]
        if pending:
            return "aprovar"
        return "executar" if executable else "relatar"

    def aprovar(state: AgentState) -> dict[str, Any]:
        pending = [
            a
            for a in state.get("actions", [])
            if a["mode"] == ExecutionMode.APPROVAL.value
        ]
        response = interrupt(
            {
                "type": "approval",
                "message": (
                    "Estas ações têm risco financeiro ou impacto externo e precisam "
                    "da sua confirmação."
                ),
                "actions": [
                    {
                        "id": a["id"],
                        "title": a["title"],
                        "cost_brl": a["cost_brl"],
                        "expected_impact_brl": a["expected_impact_brl"],
                        "rationale": a["rationale"],
                        "reversible": a["reversible"],
                    }
                    for a in pending
                ],
                "how_to_reply": (
                    "Responda com 'aprovar tudo', 'rejeitar tudo' ou "
                    "{'approved': ['id1', ...]}"
                ),
            }
        )

        approvals = _parse_approval(response, [a["id"] for a in pending])
        return {
            "approvals": approvals,
            "audit": [
                audit_entry(
                    "approval",
                    actor="user",
                    detail=f"{sum(1 for v in approvals.values() if v == 'approved')} "
                    f"aprovada(s) de {len(pending)}",
                    payload=approvals,
                )
            ],
        }

    # ------------------------------------------------------------------
    # 6. Executar
    # ------------------------------------------------------------------
    def executar(state: AgentState) -> dict[str, Any]:
        approvals = state.get("approvals", {})
        results: list[ActionResult] = []
        audit: list[dict[str, Any]] = []
        degraded: list[str] = []

        for raw in state.get("actions", []):
            action = Action.from_dict(raw)
            decision = approvals.get(action.id)

            if action.mode is ExecutionMode.BLOCKED:
                results.append(
                    ActionResult(
                        action.id, "skipped", f"bloqueada: {action.mode_reason}"
                    )
                )
                continue
            if action.mode is ExecutionMode.RECOMMEND:
                results.append(ActionResult(action.id, "skipped", "apenas recomendada"))
                continue
            if action.mode is ExecutionMode.APPROVAL:
                if decision != "approved":
                    results.append(
                        ActionResult(
                            action.id,
                            "rejected"
                            if decision == "rejected"
                            else "pending_approval",
                            action.mode_reason,
                        )
                    )
                    continue

            if settings.dry_run:
                results.append(
                    ActionResult(
                        action.id,
                        "done",
                        "simulada (dry-run): nenhuma chamada externa foi feita",
                        {"payload": action.payload},
                    )
                )
                audit.append(audit_entry("action_simulated", detail=action.title))
                continue

            try:
                output = _perform(action, clients, settings)
                results.append(ActionResult(action.id, "done", action.title, output))
                audit.append(
                    audit_entry("action_executed", detail=action.title, payload=output)
                )
            except GuardrailViolation as exc:
                results.append(ActionResult(action.id, "skipped", str(exc)))
                audit.append(audit_entry("action_blocked", detail=str(exc)))
            except (IntegrationError, CircuitOpen, AgentError) as exc:
                logger.warning("ação falhou id=%s err=%s", action.id, exc)
                results.append(ActionResult(action.id, "failed", str(exc)))
                degraded.append(f"Falha ao executar '{action.title}': {exc}")
                audit.append(audit_entry("action_failed", detail=str(exc)))

        return {
            "results": [r.to_dict() for r in results],
            "audit": audit,
            "degraded": degraded,
        }

    # ------------------------------------------------------------------
    # 7. Relatar
    # ------------------------------------------------------------------
    def relatar(state: AgentState) -> dict[str, Any]:
        report = render_report(state, settings)
        if settings.autonomy is not Autonomy.OBSERVE:
            report = polish_text(
                settings,
                "Revise a fluidez do relatório abaixo sem alterar nenhum número, "
                "nome de campanha ou recomendação.",
                report,
            )
        return {
            "report": report,
            "history": [{"role": "assistant", "content": report}],
            "audit": [audit_entry("report", detail="relatório entregue")],
        }

    # ------------------------------------------------------------------
    builder = StateGraph(AgentState)
    builder.add_node("interpretar", interpretar)
    builder.add_node("esclarecer", esclarecer)
    builder.add_node("planejar", planejar)
    builder.add_node("analytics", analytics)
    builder.add_node("suporte", suporte)
    builder.add_node("ads", ads)
    builder.add_node("decidir", decidir)
    builder.add_node("aprovar", aprovar)
    builder.add_node("executar", executar)
    builder.add_node("relatar", relatar)

    builder.add_edge(START, "interpretar")
    builder.add_conditional_edges(
        "interpretar", rota_pos_interpretacao, ["esclarecer", "planejar"]
    )
    builder.add_edge("esclarecer", "planejar")
    builder.add_conditional_edges(
        "planejar", despachar, ["analytics", "suporte", "ads"]
    )
    for node in ("analytics", "suporte", "ads"):
        builder.add_edge(node, "decidir")
    builder.add_conditional_edges(
        "decidir", precisa_aprovacao, ["aprovar", "executar", "relatar"]
    )
    builder.add_edge("aprovar", "executar")
    builder.add_edge("executar", "relatar")
    builder.add_edge("relatar", END)

    return builder.compile(checkpointer=checkpointer or InMemorySaver())


# --------------------------------------------------------------------------
# Auxiliares
# --------------------------------------------------------------------------


def _parse_approval(response: Any, pending_ids: list[str]) -> dict[str, str]:
    """Aceita 'aprovar tudo', 'rejeitar tudo', lista de ids ou dict explícito.

    Default seguro: o que não foi explicitamente aprovado fica rejeitado.
    """
    if isinstance(response, str):
        normalized = response.strip().lower()
        if normalized in {"aprovar tudo", "aprovar", "sim", "ok", "approve all", "y"}:
            return {aid: "approved" for aid in pending_ids}
        if normalized in {"rejeitar tudo", "rejeitar", "nao", "não", "no", "n"}:
            return {aid: "rejected" for aid in pending_ids}
        picked = {aid for aid in pending_ids if aid in normalized}
        return {
            aid: ("approved" if aid in picked else "rejected") for aid in pending_ids
        }
    if isinstance(response, dict):
        approved = set(response.get("approved") or [])
        if response.get("approve_all"):
            approved = set(pending_ids)
        return {
            aid: ("approved" if aid in approved else "rejected") for aid in pending_ids
        }
    if isinstance(response, list):
        approved = set(response)
        return {
            aid: ("approved" if aid in approved else "rejected") for aid in pending_ids
        }
    return {aid: "rejected" for aid in pending_ids}


def _perform(action: Action, clients: Clients, settings: Settings) -> dict[str, Any]:
    """Efeito colateral real de cada tipo de ação."""
    p = action.payload
    if action.type is ActionType.SEND_EMAIL:
        result = clients.mailbox.send_reply(
            p["to"], p["subject"], p["body"], p.get("thread_id", "")
        )
        clients.mailbox.mark_handled(p["email_id"], "agente/respondido")
        return result
    if action.type is ActionType.DRAFT_EMAIL:
        return clients.mailbox.save_draft(p["to"], p["subject"], p["body"])
    if action.type is ActionType.ESCALATE:
        clients.mailbox.mark_handled(p["email_id"], "agente/escalado")
        return {"status": "escalated", "email_id": p["email_id"]}
    if action.type is ActionType.CREATE_CAMPAIGN:
        from .models import CampaignPlan

        return clients.meta.create_campaign(CampaignPlan(**p))
    if action.type is ActionType.UPDATE_BUDGET:
        target = float(p["target_brl"])
        if target > settings.guardrails.max_daily_budget_brl:
            raise GuardrailViolation(
                "max_daily_budget_brl",
                f"{brl(target)} acima do teto configurado",
            )
        return clients.meta.update_daily_budget(p["campaign_id"], target)
    if action.type is ActionType.PAUSE_CAMPAIGN:
        return clients.meta.pause_campaign(p["campaign_id"])
    if action.type is ActionType.ISSUE_REFUND:
        return clients.hotmart.request_refund(p["transaction"], p.get("reason", ""))
    return {"status": "noop"}


def _snapshot_from_dict(data: dict[str, Any]) -> SalesSnapshot | None:
    """Reconstrói o snapshot vindo do estado (usado pelo módulo de ads)."""
    from datetime import date, datetime

    from .models import SalesWindow, SourceStats

    def window(raw: dict[str, Any]) -> SalesWindow:
        payload = dict(raw)
        payload["start"] = date.fromisoformat(payload["start"])
        payload["end"] = date.fromisoformat(payload["end"])
        payload["by_source"] = {
            k: SourceStats(**v) for k, v in (payload.get("by_source") or {}).items()
        }
        return SalesWindow(**payload)

    try:
        return SalesSnapshot(
            current=window(data["current"]),
            previous=window(data["previous"]),
            fetched_at=datetime.fromisoformat(data["fetched_at"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _reply_from_dict(data: dict[str, Any]):
    from .models import EmailCategory, EmailReply, Triage

    triage_raw = dict(data["triage"])
    triage_raw["category"] = EmailCategory(triage_raw["category"])
    payload = dict(data)
    payload["triage"] = Triage(**triage_raw)
    return EmailReply(**payload)


def _account_from_dict(data: dict[str, Any]):
    """Descarta as chaves derivadas que `to_dict` acrescenta."""
    from .models import AdAccountStatus

    fields = {f for f in AdAccountStatus.__dataclass_fields__}
    return AdAccountStatus(**{k: v for k, v in data.items() if k in fields})


def _campaign_from_dict(data: dict[str, Any]):
    from .models import CampaignPerformance

    fields = {f for f in CampaignPerformance.__dataclass_fields__}
    return CampaignPerformance(**{k: v for k, v in data.items() if k in fields})


class _Counter:
    """Numerador de seções do relatório."""

    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> int:
        self.n += 1
        return self.n


def render_report(state: AgentState, settings: Settings) -> str:
    """Relatório determinístico em markdown — a fonte da verdade da resposta."""
    lines: list[str] = []
    product = settings.product.name
    lines.append(f"# {product} — resultado da execução")
    # Seções são numeradas na ordem em que aparecem: um comando só de mídia não
    # deve produzir um relatório que pula do "1" para o "5".
    section = _Counter()

    if state.get("degraded"):
        lines.append("\n## ⚠ Execução parcial")
        for item in state["degraded"]:
            lines.append(f"- {item}")

    metrics = state.get("metrics") or []
    if metrics:
        snapshot = state.get("snapshot", {})
        window = snapshot.get("current", {})
        lines.append(
            f"\n## {section()}. Desempenho "
            f"({window.get('start', '')} a {window.get('end', '')})"
        )
        lines.append("| Indicador | Atual | Anterior | Variação |")
        lines.append("|---|---:|---:|---:|")
        for m in metrics:
            lines.append(
                f"| {m['label']} | {_fmt(m['value'], m['unit'])} | "
                f"{_fmt(m.get('previous'), m['unit'])} | {_fmt_delta(m.get('delta_pct'))} |"
            )

    findings = state.get("findings") or []
    if findings:
        ordered = prioritize([Finding.from_dict(f) for f in findings], settings)
        criticals = [f for f in ordered if f.severity is Severity.CRITICAL]
        warnings = [f for f in ordered if f.severity is Severity.WARNING]
        opportunities = [f for f in ordered if f.severity is Severity.OPPORTUNITY]

        if criticals or warnings:
            lines.append(f"\n## {section()}. Sinais de alerta")
            for f in criticals + warnings:
                mark = "🔴" if f.severity is Severity.CRITICAL else "🟡"
                lines.append(f"\n**{mark} {f.title}**")
                lines.append(f"- {f.detail}")
                lines.append(f"- Ação sugerida: {f.recommendation}")
                if f.impact_brl:
                    lines.append(f"- Impacto estimado: {brl(f.impact_brl)}/mês")
        if opportunities:
            lines.append(f"\n## {section()}. Oportunidades")
            for f in opportunities:
                lines.append(f"\n**🟢 {f.title}**")
                lines.append(f"- {f.detail}")
                lines.append(f"- Ação sugerida: {f.recommendation}")

    replies = state.get("email_replies") or []
    if replies:
        auto = [r for r in replies if r["auto_sendable"]]
        review = [r for r in replies if not r["auto_sendable"]]
        lines.append(f"\n## {section()}. Suporte")
        lines.append(
            f"- {len(replies)} e-mail(s) triados: {len(auto)} com resposta "
            f"automática, {len(review)} para revisão humana."
        )
        for r in review:
            lines.append(
                f"  - `{r['to']}` — {r['triage']['category']} "
                f"(confiança {pct(r['triage']['confidence'], 0)}): {r['reason']}"
            )

    account = state.get("ad_account")
    if account:
        lines.append(f"\n## {section()}. Meta Ads")
        available = account.get("available_brl")
        saldo = "sem teto definido" if available is None else f"{brl(available)}"
        lines.append(
            f"- Conta {'ativa' if account.get('is_active') else 'INATIVA'} — "
            f"saldo disponível: {saldo}"
        )
        for plan in state.get("campaign_plans") or []:
            lines.append(
                f"- Plano: **{plan['name']}** — {brl(plan['daily_budget_brl'])}/dia, "
                f"público: {plan['audience']}. {plan['rationale']}"
            )
        for note in state.get("plan_notes") or []:
            lines.append(f"  - _{note}_")

    actions = state.get("actions") or []
    if actions:
        lines.append(f"\n## {section()}. Ações")
        results = {r["action_id"]: r for r in (state.get("results") or [])}
        for a in actions[:15]:
            result = results.get(a["id"])
            status = result["status"] if result else a["mode"]
            why = result["detail"] if result and result["detail"] else a["mode_reason"]
            lines.append(f"- [{status}] {a['title']} — _{why}_")

    lines.append(f"\n## {section()}. Próximo passo")
    lines.append(state.get("next_step") or "Nenhuma ação pendente.")
    return "\n".join(lines)


def _fmt(value: Any, unit: str) -> str:
    if value is None:
        return "—"
    if unit == "BRL":
        return f"{brl(value)}"
    if unit == "%":
        return f"{pct(value, 2)}"
    if unit == "x":
        return f"{times(value)}"
    if unit == "un":
        return num(value, 0)
    return num(value)


def _fmt_delta(delta: float | None) -> str:
    if delta is None:
        return "—"
    return f"{signed_pct(delta, 1)}"
