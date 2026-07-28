# Publicar a interface na internet

Guia para colocar o `ui.py` numa URL pública, de graça, no Streamlit Community
Cloud — e para fazer isso sem entregar o controle do seu produto a quem passar
pelo link.

## O risco, em uma frase

A interface não é um painel de leitura: com credenciais configuradas e o modo
live ligado, ela **cria campanhas que gastam dinheiro, envia e-mail para seus
clientes e pede reembolso na Hotmart**. Um endereço público sem trava significa
que qualquer visitante pode fazer isso — e ler seu faturamento e os e-mails dos
seus compradores, que são dado pessoal sob a LGPD.

## A trava: modo vitrine

Ligue a variável `HOTMART_AGENT_PUBLIC=1` (ou o secret `public_demo = "1"`) e a
instância fica presa em dry-run: o seletor de modo live some da tela e não há
caminho na interface para religá-lo.

Isso basta porque a escolha de cliente real × simulado depende do dry-run:

```python
# integrations/hotmart.py — mesma forma em meta_ads.py e mailbox.py
if settings.credentials.has_hotmart() and not settings.dry_run:
    return HotmartAPIClient(settings)
return FakeHotmartClient(settings)
```

Com dry-run travado, as três integrações caem no cliente simulado **mesmo que as
credenciais estejam presentes**. O visitante vê o agente raciocinando sobre
dados sintéticos coerentes; seu faturamento real não aparece e nenhuma chamada
externa acontece.

## Passo a passo

1. Acesse <https://share.streamlit.io> e entre com o GitHub.
2. Autorize o acesso ao repositório `luigisottomaiormoreira-hue/langgraph`
   (funciona também se ele for privado).
3. Em **Create app** → **Deploy a public app from GitHub**, preencha:

   | Campo | Valor |
   |---|---|
   | Repository | `luigisottomaiormoreira-hue/langgraph` |
   | Branch | `claude/hotmart-ebook-ai-agent-sufjvm` |
   | Main file path | `apps/hotmart-ebook-agent/ui.py` |

   Não é preciso apontar o `requirements.txt`: o Streamlit procura o arquivo no
   diretório do app.

4. Em **Advanced settings** → **Secrets**, cole:

   ```toml
   public_demo = "1"
   PRODUCT_NAME = "Salário no Controle"
   PRODUCT_PRICE_BRL = "97.00"
   ```

   **Não** coloque credencial de Hotmart, Meta ou e-mail numa instância pública.
   Em dry-run elas nem seriam usadas — só aumentariam a superfície de exposição.

5. **Deploy**. Em 2 a 3 minutos sai uma URL do tipo
   `https://<nome-que-você-escolher>.streamlit.app`, que você pode compartilhar
   com qualquer pessoa.

## Por que o `sys.path` funciona

O app fica num subdiretório de um monorepo, mas o import `from hotmart_agent
import ...` resolve porque o Streamlit insere o diretório do script principal no
`sys.path` antes de executá-lo:

```python
# streamlit/web/bootstrap.py:70
sys.path.insert(0, os.path.dirname(main_script_path))
```

Ou seja, apontar o Main file path para `apps/hotmart-ebook-agent/ui.py` já põe
`apps/hotmart-ebook-agent/` no caminho de import. Nenhum ajuste é necessário.

## Se você quiser operar de verdade pela web

Aí a exigência muda de figura, porque a instância passa a gastar dinheiro e
falar com clientes. O mínimo defensável:

- **Autenticação de verdade** na frente do app (`st.login` com OIDC, ou um proxy
  como Cloudflare Access). Senha em `st.secrets` compartilhada entre pessoas não
  serve para autorizar gasto.
- **Segredos fora do repositório**, em cofre gerenciado, com rotação.
- **Checkpointer durável** (Postgres) no lugar do `InMemorySaver`, senão a fila
  de aprovações morre a cada reinício da instância.
- **Trilha de auditoria persistida** fora do processo, para saber quem aprovou o
  quê.
- **Instância privada**, não um endereço público — o Community Cloud é feito
  para demonstração, não para painel de operação com acesso a dinheiro.

Regra prática: público e em vitrine, ou privado e operando. O meio-termo —
público operando com senha simples — junta o pior dos dois.
