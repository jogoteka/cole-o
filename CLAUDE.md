# CLAUDE.md — Guia para o Claude Code (Jogoteka)

Este arquivo é lido automaticamente pelo Claude Code ao abrir o projeto. Ele resume
arquitetura, convenções e armadilhas para que qualquer instância nova consiga dar
manutenção sem perder contexto.

## O que é

**Jogoteka** é o sistema de gestão de uma loja de jogos de tabuleiro: estoque,
vendas, locações (aluguel), cupons, conciliação bancária, contratos de locação com
assinatura digital, mensagens automáticas por WhatsApp e uma landing page pública
com catálogo. É um app Flask, hospedado no Render.

## Stack e execução

- **Backend:** Flask (Python). App exposto como `web:app`.
- **Servidor de produção:** gunicorn (ver `Procfile`): `gunicorn web:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120`.
- **Banco:** PostgreSQL em produção (quando `DATABASE_URL` está definido), SQLite local caso contrário (`jogos.db`, ignorado no git).
- **Frontend:** sem framework. Todo o HTML/CSS/JS está embutido em `web.py` como strings grandes renderizadas via `render_template_string` (Jinja2).
- **Deploy:** push para o GitHub (`jogoteka/cole-o`, branch `main`) → Render faz auto-deploy. Acompanhar status via `gh api repos/jogoteka/cole-o/deployments`.

### Rodar localmente
```bash
pip install -r requirements.txt
python3 web.py        # sobe em http://localhost:5001 (usa SQLite)
```
Sem `DATABASE_URL`, usa SQLite local. `init_db()` cria as tabelas no startup.

## Mapa dos arquivos

| Arquivo | Responsabilidade |
|---|---|
| `web.py` | **Tudo do app web**: rotas Flask (103 rotas), TODO o HTML/CSS/JS (templates inline), autenticação, scheduler de lembretes. É um arquivo grande (~8k linhas). |
| `contratos.py` | Geração de PDF de contrato (ReportLab/PyMuPDF/pypdf), integração ClicksZap (upload, assinatura, status), e **todas as mensagens de WhatsApp** (lembrete, devolução, avaliação) com seus templates editáveis. |
| `loja.py` | Regras de negócio de **vendas, locações, clientes e cupons** (registrar, listar, devolução, pendência). |
| `estoque.py` | Regras de **jogos, estoque, movimentações e compras**. |
| `database.py` | Conexão (PG/SQLite), schema (`_PG_SCHEMA`/`_SQLITE_SCHEMA`) e migrações em `init_db()`. |
| `conciliacao.py` | Conciliação bancária (importação/parse de extratos). |
| `relatorios.py` | Relatórios. |
| `main.py` | **Legado** — CLI de terminal antiga. NÃO é usada em produção. Ignorar para o app web. |

## Banco de dados — armadilha importante

O schema é definido em `database.py` na string `_PG_SCHEMA` (Postgres). O schema do
SQLite é derivado por substituição:
```python
_SQLITE_SCHEMA = _PG_SCHEMA.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
```

**Regras ao mexer no schema:**
- Tabela que precisa de id auto-incremental → usar **`SERIAL PRIMARY KEY`** (vira `INTEGER PRIMARY KEY AUTOINCREMENT` no SQLite). **Nunca** escrever `AUTOINCREMENT` direto (inválido no Postgres) nem `INTEGER PRIMARY KEY` esperando auto-incremento em PG.
  - Já houve um bug real: `lembretes_log` com `AUTOINCREMENT` derrubava a transação inteira do `executescript`, revertendo a criação de `mensagem_lembrete` (tabela seguinte). Sintoma: salvar a mensagem "não funcionava" em produção.
- Tabela de configuração de **linha única** (ex: `contrato_modelo`, `cron_status`): usam `id INTEGER PRIMARY KEY` e os INSERTs informam **`id` explícito = 1**. Seguir esse padrão para novas tabelas de config single-row.
- `init_db()` roda `executescript` (uma transação) + migrações idempotentes (`ADD COLUMN IF NOT EXISTS`). `CREATE TABLE IF NOT EXISTS` **não** altera tabelas existentes — mudanças de coluna precisam de migração.

Tabelas atuais: jogos, movimentacoes, compras, clientes, vendas, locacoes, cupons,
cupom_usos, jogo_imagens, nf_arquivos, usuarios, extratos, extrato_lancamentos,
contrato_modelo, mensagem_lembrete, lembretes_log, cron_status, mensagem_devolucao,
mensagem_avaliacao, landing_midia, landing_depoimentos, categorias, destaques_opcoes,
favoritos.

## Autenticação e perfis

- Login por sessão Flask. `@app.before_request` (`verificar_auth`) protege tudo, exceto
  `_ROTAS_PUBLICAS` e `_PREFIXOS_PUBLICOS` (landing, catálogo público, `/api/cron/`, etc.).
- Três perfis: **admin**, **gerente**, **vendedor**. Decoradores: `@requer_login`,
  `@requer_admin`, `@requer_perfil("admin","gerente",...)`.
- `vendedor` é redirecionado para `/loja` (operação); admin/gerente acessam `/painel`.
- **`SECRET_KEY` precisa ser uma env var fixa.** Se não for, cada restart do Render gera
  uma chave nova e derruba todas as sessões (usuários veem "não autenticado").

## Integrações externas

- **ClicksZap** (assinatura de contrato + envio de WhatsApp): self-hosted no Render, NÃO
  é o SaaS clickszap.com.br. URL real via env `CLICKSZAP_URL` (ex.: `https://clickzap-gd1r.onrender.com`).
  Endpoints usados: `POST /documents`, `POST /signature-requests`, `GET /signature-requests/{id}`,
  `GET /s/{token}`, `POST /messages`. Token via `CLICKSZAP_TOKEN` (Bearer).
- **UptimeRobot** (keep-alive): pinga `GET /health` a cada ~5 min para o Render não hibernar.
  É o que garante que o scheduler interno das 14h rode.
- **cron-job.org** (rede de segurança): chama `GET/POST /api/cron/lembretes?token=CRON_TOKEN`
  1x/dia às 14h SP (= 17:00 UTC) para disparar os lembretes mesmo se o scheduler interno falhar.
  O envio é idempotente (`lembretes_log` evita duplicidade). Último disparo é registrado em
  `cron_status` e exibido no painel (Admin → Lembretes).

## Variáveis de ambiente (Render)

| Variável | Uso |
|---|---|
| `DATABASE_URL` | Connection string do Postgres. Sem ela → SQLite local. |
| `SECRET_KEY` | Chave de sessão Flask (FIXA — ver acima). |
| `CLICKSZAP_URL` | URL base da instância ClicksZap. |
| `CLICKSZAP_TOKEN` | Bearer token da API ClicksZap. |
| `CRON_TOKEN` | Senha do endpoint `/api/cron/lembretes`. |
| `NOME_LOJA`, `ENDERECO_LOJA`, `CNPJ_LOJA` | Dados da loja usados nos contratos. |
| `DATA_DIR` | Diretório de dados/uploads (default no home). |
| `PORT` | Porta (Render injeta). |

## Funcionalidades principais

- **Catálogo/estoque** (`estoque.py`): jogos, capa/imagem, vídeo, faixas de preço de
  locação (loc1/loc2/loc3), multa/dia, cidades (CSV: ex. `florianopolis,porto-alegre`).
- **Vendas e locações** (`loja.py`): cliente é criado/atualizado por CPF. Locação pode ser
  de 1 jogo ou em **grupo** (vários jogos, mesmo cliente, mesma data → 1 contrato).
- **Devolução**: por jogo ou **em conjunto**. "Tudo certo" → jogo volta ao estoque + mensagem
  ao cliente. "Com avaria" → status `pendente`, jogo NÃO volta ao estoque, sem mensagem; depois
  o botão "Pendência Resolvida" encerra (volta ao estoque). Status de locação: `ativa`,
  `devolvido`, `pendente`.
- **Contratos** (`contratos.py`): 3 modos de modelo, com prioridade **DOCX > PDF > texto**:
  - **DOCX**: substitui `{{CAMPOS}}` e re-renderiza via ReportLab (perde fidelidade de layout/imagens — evitar para layouts ricos).
  - **PDF**: mantém o layout EXATO do usuário. Se o PDF tiver `{{CAMPOS}}` como **texto**, são substituídos in-place via **PyMuPDF** (`fitz`); se tiver campos de formulário (AcroForm), são preenchidos via pypdf. É o caminho recomendado para fidelidade total.
  - **texto**: editor simples → PDF via ReportLab.
  - Campos disponíveis: `{{NOME_CLIENTE}}`, `{{CPF_CLIENTE}}`, `{{ENDERECO_CLIENTE}}`, `{{JOGO_1..5}}`, `{{VALOR_LOCACAO_1..5}}`, `{{DATA_SAIDA_1..5}}`, `{{DATA_PREVISTA_1..5}}`, `{{OPCAO_DIAS_1..5}}`, `{{MULTA_DIA_1..5}}`, `{{VALOR_TOTAL}}`, `{{VALOR_VENDA}}`, `{{NUM_CONTRATO}}`, `{{DATA_GERACAO}}`, etc. (ver `_build_campos` e `CAMPOS_DISPONIVEIS`).
- **Mensagens de WhatsApp editáveis** (templates no banco, painel Admin → Lembretes):
  - **Lembrete de devolução** (`mensagem_lembrete`): automático às 14h para devoluções do dia seguinte. Variáveis `{nome}`, `{jogo}`, `{data}`.
  - **Devolução concluída** (`mensagem_devolucao`): ao confirmar devolução "tudo certo". `{nome}`, `{jogos}`.
  - **Avaliação no Google** (`mensagem_avaliacao`): botão ⭐ Avaliação. `{nome}`, `{jogo}`. O link do Google fica dentro do texto do template.
  - `{nome}` sempre resolve para o **primeiro nome** do cliente (`_primeiro_nome`).
- **Landing page pública** + catálogo, favoritos, depoimentos.

## Convenções e armadilhas (aprendidas na prática)

- **Cache do navegador:** após cada deploy, a página `/loja` ou `/painel` pode ficar em cache.
  Orientar o usuário a dar **Cmd/Ctrl + Shift + R**. Vários "não funcionou" foram só cache.
- **HTML/JS dentro de `web.py` é template Jinja:** os blocos são renderizados com
  `render_template_string`. Evitar `{{` e `{%` literais no JS (conflitam com Jinja). Usar
  template literals JS com `${...}` normalmente (não conflitam).
- **Envio de WhatsApp/contrato é síncrono e visível:** preferir disparar no fluxo e mostrar o
  resultado em `toast` na tela, em vez de threads em background (que falham silenciosamente no
  gunicorn). O envio do contrato é disparado pelo frontend após confirmar a locação
  (`dispararContratoAuto`); a devolução/avaliação também retornam o resultado para a tela.
- **Idempotência de lembretes:** `lembretes_log` registra envios do dia; reenviar não duplica.
- **Commits:** o usuário só quer commit/push quando ele pede ou no fluxo de deploy. Mensagens de
  commit em PT, prefixo `fix:`/`feat:`/`chore:`. Co-author do Claude no rodapé.

## Fluxo de trabalho de deploy

1. Editar código.
2. `git add ... && git commit -m "..." && git push` (branch `main`).
3. Render faz auto-deploy. Esperar `state: success` em
   `gh api repos/jogoteka/cole-o/deployments/<id>/statuses`.
4. Pedir ao usuário para recarregar com **Cmd+Shift+R** antes de testar.

## Pendências conhecidas / pontos abertos

- Mensagem que acompanha o contrato na assinatura é gerada pelo ClicksZap (não pelo Jogoteka).
  Personalizá-la depende do ClicksZap (verificar se `signature-requests` aceita texto custom ou
  se dá para desativar a mensagem padrão e enviar a nossa via `/messages`).
- Botão "Avaliação" das **Vendas** ainda usa link `wa.me` antigo (só o das Locações foi migrado
  para envio automático via ClicksZap).
