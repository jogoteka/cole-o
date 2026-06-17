# 🎲 Jogoteka — Sistema de Gestão

Sistema de gestão para loja de jogos de tabuleiro: estoque, vendas, locações
(aluguel), cupons, conciliação bancária, contratos de locação com assinatura
digital, mensagens automáticas por WhatsApp e uma página pública com catálogo.

---

## Visão geral

O Jogoteka cuida de toda a operação da loja:

- **Estoque/Catálogo** — cadastro de jogos, capa, vídeo, preços de locação e de venda, multa por atraso, e em quais cidades o jogo está disponível.
- **Vendas** — registro de vendas com cliente, pagamento, desconto e cupom.
- **Locações** — aluguel de um ou vários jogos, com contrato gerado e enviado para assinatura.
- **Devoluções** — conferência do jogo na volta, com aviso automático ao cliente.
- **Mensagens automáticas no WhatsApp** — lembrete de devolução, confirmação de devolução e pedido de avaliação no Google.
- **Cupons de desconto**.
- **Conciliação bancária** — importação de extrato para bater com vendas/locações.
- **Página pública** — landing com catálogo, favoritos e depoimentos.

## Quem usa (perfis)

| Perfil | O que acessa |
|---|---|
| **Admin** | Tudo — configurações, modelos de contrato, mensagens, relatórios. |
| **Gerente** | Operação + boa parte das gestões, exceto o que é exclusivo de admin. |
| **Vendedor** | Tela de loja (`/loja`): vendas, locações e devoluções do dia a dia. |

---

## Como roda (infraestrutura)

- **Hospedagem:** Render (deploy automático a cada atualização do código no GitHub).
- **Banco de dados:** PostgreSQL no Render (os dados ficam guardados com segurança).
- **WhatsApp e assinatura de contrato:** integração com o **ClicksZap** (hospedado no Render).
- **Sempre acordado:** o **UptimeRobot** "cutuca" o sistema a cada 5 minutos para o servidor
  não hibernar — é isso que garante que o lembrete automático das 14h aconteça.
- **Reforço do lembrete:** o **cron-job.org** chama o sistema 1x por dia às 14h como rede de
  segurança, caso o agendador interno falhe (sem risco de mandar mensagem repetida).

### Serviços externos e onde mexer

| Serviço | Para quê | Onde |
|---|---|---|
| **Render** | Hospeda o sistema e o banco | painel do Render |
| **GitHub** (`jogoteka/cole-o`) | Guarda o código; ao atualizar, dispara o deploy | github.com |
| **ClicksZap** | Envia WhatsApp e coleta assinatura do contrato | instância no Render |
| **UptimeRobot** | Mantém o servidor acordado (ping em `/health`) | uptimerobot.com |
| **cron-job.org** | Dispara os lembretes às 14h (reforço) | cron-job.org |

---

## Mensagens automáticas (WhatsApp)

Todas são **editáveis** no painel: **Admin → 💬 Lembretes WhatsApp**. Em todas, `{nome}`
vira só o **primeiro nome** do cliente.

| Mensagem | Quando dispara | Variáveis |
|---|---|---|
| **Lembrete de devolução** | Automático, todo dia às 14h, para devoluções do dia seguinte | `{nome}`, `{jogo}`, `{data}` |
| **Devolução concluída** | Ao confirmar a devolução com "✅ Tudo certo" | `{nome}`, `{jogos}` |
| **Avaliação no Google** | Ao clicar no botão "⭐ Avaliação" na tela de Locações | `{nome}`, `{jogo}` |

> A mensagem de avaliação deve conter o **link do Google** dentro do texto.

## Contratos de locação

Ao confirmar uma locação, o contrato é gerado e enviado para assinatura pelo WhatsApp
(via ClicksZap). O modelo é configurado em **Admin → Modelo de Contrato**, em um destes
formatos (prioridade nesta ordem):

1. **PDF** (recomendado) — mantém o layout exato (logo, assinatura). Coloque os campos como
   texto no PDF (ex.: `{{NOME_CLIENTE}}`, `{{JOGO_1}}`) e o sistema preenche automaticamente.
2. **Word (.docx)** — substitui os campos, mas re-desenha o layout (perde fidelidade visual).
3. **Texto** — editor simples dentro do painel.

Campos que o sistema preenche: nome/CPF/endereço do cliente, jogos (até 5), valores, datas,
multa, nº do contrato, data, etc.

## Devolução com avaria

- **✅ Tudo certo** → jogo volta ao estoque e o cliente recebe a mensagem de confirmação.
- **⚠️ Com avaria** → você descreve o problema; a locação fica **Pendente**, o jogo **não**
  volta ao estoque e **nenhuma mensagem** é enviada. Depois de resolver com o cliente, use o
  botão **"✔ Pendência Resolvida"** para encerrar (o jogo volta ao estoque).
- Quando vários jogos são devolvidos juntos, você marca a condição de cada um e o cliente
  recebe **uma única mensagem** listando os que voltaram OK.

---

## Para desenvolvedores

### Rodar localmente
```bash
pip install -r requirements.txt
python3 web.py     # http://localhost:5001 (usa banco SQLite local)
```

### Publicar (deploy)
```bash
git add .
git commit -m "feat: descrição da mudança"
git push           # Render faz o deploy automático
```
Depois do deploy, recarregue a página com **Ctrl/Cmd + Shift + R** (evita cache antigo).

### Estrutura do código
- `web.py` — app Flask: rotas + todo o HTML/CSS/JS (telas).
- `contratos.py` — contratos (PDF) e mensagens de WhatsApp (ClicksZap).
- `loja.py` — vendas, locações, clientes, cupons.
- `estoque.py` — jogos, estoque, compras.
- `database.py` — banco (PostgreSQL/SQLite) e criação das tabelas.
- `conciliacao.py` / `relatorios.py` — conciliação bancária e relatórios.
- `main.py` — ferramenta de terminal antiga (não usada no sistema web).

### Variáveis de ambiente (no Render)
`DATABASE_URL`, `SECRET_KEY`, `CLICKSZAP_URL`, `CLICKSZAP_TOKEN`, `CRON_TOKEN`,
`NOME_LOJA`, `ENDERECO_LOJA`, `CNPJ_LOJA`.

> ⚠️ `SECRET_KEY` precisa ser fixa no Render — senão, a cada reinício todos os
> usuários são deslogados.

Para detalhes técnicos e armadilhas de manutenção, veja **[CLAUDE.md](CLAUDE.md)**.
