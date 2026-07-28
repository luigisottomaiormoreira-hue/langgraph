"""Interface de linha de comando.

    python -m hotmart_agent "como estão as vendas essa semana?"
    python -m hotmart_agent --live --autonomy L3 "responde os e-mails do suporte"
    python -m hotmart_agent --json "sobe uma campanha nova se tiver saldo"

O comando é a única entrada obrigatória: o agente decide o resto.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from typing import Any

from langgraph.types import Command

from .config import Autonomy, Settings
from .formatting import brl
from .graph import build_graph
from .observability import setup_logging


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="hotmart-agent",
        description="Agente de operação para ebook na Hotmart.",
    )
    parser.add_argument("command", nargs="*", help="comando em linguagem natural")
    parser.add_argument(
        "--autonomy",
        choices=[a.value for a in Autonomy],
        help="L0 observar, L1 recomendar, L2 aprovar (padrão), L3 autônomo",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="executa de verdade (o padrão é dry-run, sem chamadas externas)",
    )
    parser.add_argument("--window", type=int, help="janela de análise em dias")
    parser.add_argument("--thread", help="id da conversa (retoma um estado anterior)")
    parser.add_argument("--json", action="store_true", help="saída em JSON")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="aprova automaticamente as ações pendentes (use com cuidado)",
    )
    parser.add_argument("--log-level", default="WARNING")
    return parser.parse_args(argv)


def _settings_from_args(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    if args.autonomy:
        settings = settings.with_(autonomy=Autonomy(args.autonomy))
    if args.live:
        settings = settings.with_(dry_run=False)
    if args.window:
        settings = settings.with_(analysis_window_days=args.window)
    return settings


def _prompt(payload: dict[str, Any], auto_yes: bool) -> Any:
    """Renderiza a interrupção e coleta a resposta do usuário."""
    kind = payload.get("type")
    if kind == "clarification":
        print(f"\n❓ {payload['question']}")
        for option in payload.get("options", []):
            print(f"   • {option}")
        if auto_yes:
            print("→ (--yes) assumindo: resumo geral")
            return "resumo geral"
        return input("\n> ").strip()

    print(f"\n⚠ {payload.get('message', 'Confirmação necessária')}")
    for action in payload.get("actions", []):
        custo = f" — {brl(action['cost_brl'])}/dia" if action.get("cost_brl") else ""
        print(f"   [{action['id']}] {action['title']}{custo}")
        print(f"        motivo: {action['rationale']}")
    if auto_yes:
        print("→ (--yes) aprovando tudo")
        return "aprovar tudo"
    print("\nResponda: 'aprovar tudo', 'rejeitar tudo' ou os ids separados por espaço")
    return input("> ").strip()


def run(
    command: str, settings: Settings, *, thread_id: str, auto_yes: bool
) -> dict[str, Any]:
    """Roda o grafo até o fim, resolvendo interrupções pelo terminal."""
    graph = build_graph(settings)
    config = {"configurable": {"thread_id": thread_id}}
    payload: Any = {"command": command}

    for _ in range(6):  # teto de rodadas de interação
        result = graph.invoke(payload, config=config)
        interrupts = result.get("__interrupt__")
        if not interrupts:
            return result
        answer = _prompt(interrupts[0].value, auto_yes)
        payload = Command(resume=answer)

    return graph.get_state(config).values


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    setup_logging(args.log_level)
    command = " ".join(args.command).strip()
    if not command:
        command = input("O que você precisa? > ").strip()

    settings = _settings_from_args(args)
    thread_id = args.thread or f"cli-{uuid.uuid4().hex[:8]}"

    try:
        state = run(command, settings, thread_id=thread_id, auto_yes=args.yes)
    except KeyboardInterrupt:
        print("\nInterrompido. O estado ficou salvo na thread", thread_id)
        return 130

    if args.json:
        print(
            json.dumps(
                {
                    "thread_id": thread_id,
                    "settings": settings.redacted(),
                    "routing": state.get("routing"),
                    "metrics": state.get("metrics"),
                    "findings": state.get("findings"),
                    "actions": state.get("actions"),
                    "results": state.get("results"),
                    "next_step": state.get("next_step"),
                    "degraded": state.get("degraded"),
                    "audit": state.get("audit"),
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
    else:
        print("\n" + (state.get("report") or "(sem relatório)"))
        if settings.dry_run:
            print(
                "\n[dry-run] Nenhuma chamada externa foi feita. Use --live para "
                "executar de verdade."
            )
        print(f"[thread: {thread_id}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
