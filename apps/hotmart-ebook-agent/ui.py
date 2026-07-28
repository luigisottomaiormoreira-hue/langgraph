"""
Interface web do Agente Hotmart — Salário no Controle
=====================================================

Coloque este arquivo em:  apps/hotmart-ebook-agent/ui.py
Rode com:                 uv run streamlit run ui.py

A interface conversa com o SEU agente (hotmart_agent), incluindo o fluxo
de aprovação humana: quando o grafo interrompe pedindo confirmação, os
botões de aprovar/rejeitar aparecem aqui.
"""

from __future__ import annotations

import uuid

import streamlit as st
from langgraph.types import Command

from hotmart_agent import Autonomy, Settings, build_graph

# ------------------------------------------------------------------
# Página
# ------------------------------------------------------------------
st.set_page_config(
    page_title="Agente · Salário no Controle",
    page_icon="📊",
    layout="wide",
)

st.markdown(
    """
    <style>
      .stApp { background-color: #F7F4EC; }
      h1, h2, h3 { color: #133A2E; }
      div[data-testid="stMetricValue"] { color: #133A2E; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ------------------------------------------------------------------
# Estado da sessão
# ------------------------------------------------------------------
if "thread_id" not in st.session_state:
    st.session_state.thread_id = f"ui-{uuid.uuid4().hex[:8]}"
if "pending_interrupt" not in st.session_state:
    st.session_state.pending_interrupt = None   # payload da interrupção atual
if "last_state" not in st.session_state:
    st.session_state.last_state = None


# ------------------------------------------------------------------
# Grafo (cacheado por combinação de configurações)
# ------------------------------------------------------------------
@st.cache_resource
def get_graph(autonomy: str, live: bool, window: int):
    settings = Settings.from_env()
    settings = settings.with_(
        autonomy=Autonomy(autonomy),
        dry_run=not live,
        analysis_window_days=window,
    )
    return build_graph(settings), settings


def executar(graph, payload):
    """Invoca o grafo e separa resultado de interrupção."""
    config = {"configurable": {"thread_id": st.session_state.thread_id}}
    result = graph.invoke(payload, config=config)
    interrupts = result.get("__interrupt__")
    if interrupts:
        st.session_state.pending_interrupt = interrupts[0].value
    else:
        st.session_state.pending_interrupt = None
        st.session_state.last_state = result
    st.rerun()


# ------------------------------------------------------------------
# Sidebar — operação
# ------------------------------------------------------------------
with st.sidebar:
    st.header("Operação")

    autonomy = st.select_slider(
        "Autonomia",
        options=["L0", "L1", "L2", "L3"],
        value="L2",
        help="L0 observa · L1 recomenda · L2 pede aprovação · L3 autônomo",
    )
    live = st.toggle(
        "Modo live (chamadas reais)",
        value=False,
        help="Desligado = dry-run: o agente mostra o que faria, sem tocar em nada.",
    )
    window = st.slider("Janela de análise (dias)", 3, 30, 7)

    if live and autonomy == "L3":
        st.warning("L3 + live: o agente executa sozinho dentro dos guardrails. "
                   "Recomendado só depois de semanas de L2 sem rejeições.")
    if not live:
        st.info("Dry-run ativo — nenhuma chamada externa será feita.", icon="🛡️")

    st.divider()
    st.caption(f"Thread: `{st.session_state.thread_id}`")
    if st.button("🔄 Nova conversa"):
        st.session_state.thread_id = f"ui-{uuid.uuid4().hex[:8]}"
        st.session_state.pending_interrupt = None
        st.session_state.last_state = None
        st.rerun()

graph, settings = get_graph(autonomy, live, window)

# ------------------------------------------------------------------
# Corpo
# ------------------------------------------------------------------
st.title("Agente · Salário no Controle")

# ---------- Interrupção pendente (aprovação / esclarecimento) ----------
pend = st.session_state.pending_interrupt
if pend:
    if pend.get("type") == "clarification":
        st.subheader("O agente precisa de um esclarecimento")
        st.write(pend.get("question", ""))
        opcoes = pend.get("options", [])
        if opcoes:
            escolha = st.radio("Opções", opcoes)
        else:
            escolha = st.text_input("Sua resposta")
        if st.button("Responder", type="primary"):
            executar(graph, Command(resume=escolha))
    else:
        st.subheader("⚠ Aprovação necessária")
        st.write(pend.get("message", "O agente propôs ações que precisam do seu OK."))
        acoes = pend.get("actions", [])
        for a in acoes:
            custo = f" — R$ {a['cost_brl']:.2f}/dia" if a.get("cost_brl") else ""
            with st.container(border=True):
                st.markdown(f"**[{a['id']}] {a['title']}**{custo}")
                st.caption(a.get("rationale", ""))
        c1, c2, c3 = st.columns(3)
        if c1.button("✅ Aprovar tudo", type="primary", use_container_width=True):
            executar(graph, Command(resume="aprovar tudo"))
        if c2.button("❌ Rejeitar tudo", use_container_width=True):
            executar(graph, Command(resume="rejeitar tudo"))
        ids = c3.text_input("ou IDs específicos", placeholder="ex.: a1 a3")
        if ids:
            if st.button("Aprovar selecionadas"):
                executar(graph, Command(resume=ids))
    st.divider()

# ---------- Entrada de comando ----------
st.subheader("O que você precisa?")

col_cmd, col_btn = st.columns([4, 1])
with col_cmd:
    comando = st.text_input(
        "Comando",
        placeholder='ex.: "como estão as vendas essa semana?"',
        label_visibility="collapsed",
    )
with col_btn:
    enviar = st.button("▶️ Executar", type="primary", use_container_width=True)

st.caption("Atalhos:")
a1, a2, a3, a4 = st.columns(4)
atalho = None
if a1.button("Resumo geral", use_container_width=True):
    atalho = "me dá um resumo geral do produto"
if a2.button("Analisar vendas", use_container_width=True):
    atalho = "analisa as vendas"
if a3.button("E-mails do suporte", use_container_width=True):
    atalho = "responde os e-mails do suporte"
if a4.button("Revisar anúncios", use_container_width=True):
    atalho = "revisa as campanhas de mídia paga"

if enviar and comando.strip():
    with st.spinner("O agente está trabalhando..."):
        executar(graph, {"command": comando.strip()})
elif atalho:
    with st.spinner("O agente está trabalhando..."):
        executar(graph, {"command": atalho})

# ---------- Resultado ----------
state = st.session_state.last_state
if state:
    st.divider()

    report = state.get("report")
    if report:
        st.subheader("Relatório")
        st.markdown(report)
        st.download_button("⬇️ Baixar relatório", report, file_name="relatorio.md")

    metrics = state.get("metrics") or {}
    if metrics:
        st.subheader("Métricas")
        nums = {k: v for k, v in metrics.items() if isinstance(v, (int, float))}
        if nums:
            cols = st.columns(min(4, len(nums)))
            for i, (k, v) in enumerate(list(nums.items())[:8]):
                cols[i % len(cols)].metric(k.replace("_", " "), f"{v:,.2f}" if isinstance(v, float) else v)
        with st.expander("Métricas completas"):
            st.json(metrics)

    findings = state.get("findings")
    if findings:
        st.subheader("Sinais encontrados")
        for f in findings:
            sev = str(f.get("severity", "")).lower() if isinstance(f, dict) else ""
            texto = f.get("summary") or f.get("title") or str(f) if isinstance(f, dict) else str(f)
            if "crit" in sev or "high" in sev:
                st.error(texto, icon="🔴")
            elif "warn" in sev or "med" in sev:
                st.warning(texto, icon="🟡")
            else:
                st.info(texto, icon="🔵")

    results = state.get("results")
    if results:
        st.subheader("Ações executadas")
        st.json(results)

    if state.get("degraded"):
        st.warning(
            f"Execução parcial — integrações degradadas: {state['degraded']}",
            icon="⚠️",
        )

    audit = state.get("audit")
    if audit:
        with st.expander(f"Trilha de auditoria ({len(audit)} eventos)"):
            for ev in audit:
                st.text(f"{ev.get('ts', '')}  {ev.get('event', '')}  {ev.get('detail', '')}")

st.divider()
st.caption(
    "O agente sugere, você aprova. Dry-run por padrão; suba a autonomia "
    "gradualmente conforme o runbook (docs/runbook.md)."
)
