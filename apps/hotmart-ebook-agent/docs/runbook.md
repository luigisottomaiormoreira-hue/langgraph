# Runbook operacional

Guia de quem opera o agente no dia a dia: como colocar em produção com
segurança, o que monitorar e o que fazer quando algo dá errado.

## Rampa de autonomia

Não comece em L3. A sequência que funciona:

| Semana | Modo | O que observar |
|---|---|---|
| 1 | `L1` + `dry_run` | Os sinais batem com o que você vê nos painéis? Os limiares estão calibrados para o seu ticket? |
| 2 | `L2` + `--live` | As ações propostas fazem sentido? Você aprovaria todas? |
| 3–4 | `L2` + `--live` | Quantas você rejeita? Menos de 1 em 10 é sinal de que dá para subir. |
| 5+ | `L3` com guardrails apertados | Suba `max_daily_budget_brl` aos poucos, não de uma vez. |

Um agente que erra em L1 custa cinco minutos de leitura. O mesmo erro em L3 custa
orçamento e reputação.

## Calibragem dos limiares

Os padrões em `config.py` valem para um ebook de R$ 47–197 com tráfego pago. Se o
seu produto foge disso:

- **Ticket acima de R$ 300**: reduza `cpa_target_ratio` (o CPA proporcional cai
  em ticket alto) e suba `refund_auto_approve_max_brl`.
- **Produto novo (< 100 vendas)**: suba `min_units_for_stats` para evitar que
  ruído vire alerta.
- **Sem tráfego pago**: os sinais de mídia simplesmente não disparam; rode com
  `"analisa as vendas"` em vez de `"resumo geral"` para economizar chamadas.
- **Muito afiliado**: baixe `affiliate_concentration_warn` se a dependência de um
  parceiro é o seu risco principal.

## Monitoramento

Cheque semanalmente:

1. **Taxa de rejeição de aprovações** — subiu? As regras derivaram do que você
   considera certo.
2. **E-mails que viraram rascunho** — se mais de 40% vira rascunho, alguma
   categoria recorrente está faltando no léxico de `support.py`.
3. **Reclamação de cliente sobre resposta automática** — sinal de que o limiar de
   confiança está baixo demais.
4. **`degraded` no relatório** — integração instável merece investigação antes de
   virar rotina invisível.

## Diagnóstico rápido

| Sintoma | Causa provável | O que fazer |
|---|---|---|
| "Hotmart indisponível" recorrente | token expirado ou credencial sem escopo | recriar credencial em Hotmart > Ferramentas > Credenciais de API |
| Nenhum plano de campanha | conta inativa, saldo baixo ou teto de campanhas | ler a seção Meta Ads do relatório; o motivo está explícito |
| Todos os e-mails viram rascunho | `auto_reply_min_confidence` alto ou léxico defasado | rodar `pytest tests/test_support.py -k classificacao` com os e-mails reais |
| Agente pergunta sempre | comandos genéricos demais | usar os verbos que o roteador reconhece ("analisa", "responde", "cria") |
| Relatório sem números | módulo de analytics degradou | conferir a seção "Execução parcial" |
| Ação `blocked` | guardrail financeiro | ajustar `max_daily_budget_brl` conscientemente, ou reduzir o orçamento pedido |

## Auditoria

Toda execução deixa trilha em `state["audit"]`, persistida junto ao checkpoint:

```python
from langgraph.checkpoint.postgres import PostgresSaver

with PostgresSaver.from_conn_string(DB_URI) as cp:
    graph = build_graph(settings, checkpointer=cp)
    estado = graph.get_state({"configurable": {"thread_id": "op-1"}})
    for evento in estado.values["audit"]:
        print(evento["ts"], evento["event"], evento["detail"])
```

Eventos: `command_received`, `plan`, `analytics_done`, `support_triaged`,
`ads_reviewed`, `decision`, `approval`, `action_executed`, `action_simulated`,
`action_blocked`, `action_failed`, `report`.

Segredos nunca aparecem: o payload passa por `redact()` antes de ser gravado.

## Agendamento

```bash
# Briefing diário às 8h, autônomo dentro dos guardrails
0 8 * * * cd /opt/hotmart-agent && HOTMART_AGENT_AUTONOMY=L3 \
    uv run python -m hotmart_agent --live "briefing diário do produto" \
    >> /var/log/hotmart-agent.log 2>&1

# Suporte a cada 2h em horário comercial (L2: só rascunha sozinho)
0 9-18/2 * * 1-5 cd /opt/hotmart-agent && \
    uv run python -m hotmart_agent --live "responde os e-mails do suporte" \
    >> /var/log/hotmart-agent.log 2>&1
```

Use `--thread` fixo para manter continuidade entre execuções, ou omita para que
cada rodada seja independente.

## Antes de dar `--live` pela primeira vez

- [ ] `make test` passa
- [ ] `.env` preenchido e **fora** do controle de versão
- [ ] Token da Meta com escopo mínimo (`ads_read` + `ads_management`)
- [ ] Senha de app no e-mail, nunca a senha da conta
- [ ] `max_daily_budget_brl` no valor que você aceita perder num dia ruim
- [ ] Rodou o mesmo comando em `dry_run` e leu o que ele pretendia fazer
