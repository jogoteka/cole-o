import sys
import os
import re
import uuid
import secrets
import threading
sys.path.insert(0, os.path.dirname(__file__))

from flask import (Flask, jsonify, request, render_template_string,
                   send_from_directory, session, redirect)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime
from database import init_db, get_connection
import estoque as est
import loja as lj
import conciliacao as conc
import contratos as ct

DATA_DIR   = os.environ.get("DATA_DIR", os.path.join(os.path.expanduser("~"), "Desktop", "CLAUDE", "estoque_jogos"))
UPLOAD_DIR  = os.path.join(DATA_DIR, "notas")
IMAGENS_DIR = os.path.join(DATA_DIR, "imagens")
os.makedirs(UPLOAD_DIR,  exist_ok=True)
os.makedirs(IMAGENS_DIR, exist_ok=True)

app = Flask(__name__, instance_path=DATA_DIR)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

import logging as _logging
_logging.basicConfig(level=_logging.INFO)
_log = _logging.getLogger(__name__)

try:
    init_db()
    _log.info("[startup] init_db() concluído com sucesso")
except Exception as _e:
    _log.error("[startup] ERRO em init_db(): %s", _e, exc_info=True)

# ── Scheduler de lembretes ────────────────────────────────────────────────────
try:
    import pytz
    from apscheduler.schedulers.background import BackgroundScheduler
    _tz_sp = pytz.timezone("America/Sao_Paulo")
    _scheduler = BackgroundScheduler(timezone=_tz_sp)
    _scheduler.add_job(
        func=ct.enviar_lembretes_devolucao,
        trigger="cron",
        hour=14, minute=0,
        id="lembretes_devolucao",
        replace_existing=True,
    )
    _scheduler.start()
    _log.info("[scheduler] Lembrete de devolução agendado para 14h (horário SP)")
except Exception as _e_sched:
    _log.warning("[scheduler] Não foi possível iniciar o scheduler: %s", _e_sched)

# Garante colunas conteudo_b64/mime_type na landing_midia (bancos antigos)
try:
    from database import get_connection as _gc, DATABASE_URL as _DU, _get_cols as _gcols
    with _gc() as _conn:
        _add = "ADD COLUMN IF NOT EXISTS" if _DU else "ADD COLUMN"
        _cols = _gcols(_conn, "landing_midia")
        if "conteudo_b64" not in _cols:
            _conn.execute(f"ALTER TABLE landing_midia {_add} conteudo_b64 TEXT")
        if "mime_type" not in _cols:
            _conn.execute(f"ALTER TABLE landing_midia {_add} mime_type TEXT")
        # desativa registros órfãos (sem conteúdo no banco e sem arquivo no disco)
        _pasta = os.path.join(os.path.dirname(__file__), "static", "landing")
        _orfaos = _conn.execute(
            "SELECT id, nome_arquivo FROM landing_midia WHERE ativo=1 AND (conteudo_b64 IS NULL OR conteudo_b64='')"
        ).fetchall()
        for _r in _orfaos:
            if not os.path.exists(os.path.join(_pasta, _r["nome_arquivo"])):
                _conn.execute("UPDATE landing_midia SET ativo=0 WHERE id=?", (_r["id"],))
                _log.info("[startup] mídia órfã desativada: id=%s nome=%s", _r["id"], _r["nome_arquivo"])
except Exception as _e2:
    _log.warning("[startup] migração landing_midia: %s", _e2)

# ── Cidades ────────────────────────────────────────────────────────────────────
# Para adicionar uma cidade: inclua uma entrada neste dicionário.
# slug: identificador na URL (sem acentos, sem espaços)
# nome: nome exibido ao usuário
# whatsapp: número completo com DDI+DDD (só dígitos)
CIDADES = {
    "florianopolis": {"nome": "Floripa", "emoji": "🏖️", "whatsapp": "5548988072721"},
    "porto-alegre":  {"nome": "Porto Alegre",  "emoji": "🌉", "whatsapp": "5551981447898"},
}

# ── Auth helpers ───────────────────────────────────────────────────────────────

def _agora_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def requer_login(f):
    @wraps(f)
    def w(*a, **kw):
        if not session.get("uid"):
            # APIs (JSON ou fetch/multipart) recebem JSON; páginas recebem redirect
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"erro": "Não autenticado — faça login"}), 401
            return redirect("/login")
        return f(*a, **kw)
    return w

def requer_admin(f):
    @wraps(f)
    def w(*a, **kw):
        if not session.get("uid"):
            return redirect("/login")
        if session.get("perfil") != "admin":
            return redirect("/painel")
        return f(*a, **kw)
    return w

def requer_perfil(*perfis):
    def decorator(f):
        @wraps(f)
        def w(*a, **kw):
            if not session.get("uid"):
                if request.is_json:
                    return jsonify({"erro": "Não autenticado"}), 401
                return redirect("/login")
            if session.get("perfil") not in perfis:
                return jsonify({"erro": "Acesso negado"}), 403
            return f(*a, **kw)
        return w
    return decorator

FORMAS_PAGAMENTO = ["PIX", "Dinheiro", "Cartão de débito", "Cartão de crédito", "Boleto", "Transferência"]

HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Jogoteka — Gestão de Estoque</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Fredoka+One&family=Nunito:wght@400;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root{--red:#F10A0A;--green:#17C629;--purple:#7B20E1;--orange:#ED940E;
          --dark:#1a1a2e;--dark2:#16213e;--dark3:#0f3460;--border:rgba(255,255,255,.1);--text:#e0e0e0;--muted:#8892a4}
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Nunito',system-ui,sans-serif;background:var(--dark);color:var(--text);min-height:100vh}

    /* ── Header ── */
    header{background:linear-gradient(135deg,var(--dark2) 0%,#1a0533 100%);
      border-bottom:3px solid var(--orange);
      padding:.6rem 2rem;display:flex;align-items:center;gap:1.2rem;flex-wrap:wrap}
    .logo-img{height:48px;width:auto;object-fit:contain}
    .logo-fallback{font-family:'Fredoka One',cursive;font-size:1.8rem;letter-spacing:1px}
    .logo-fallback span.j{color:var(--red)}.logo-fallback span.o1{color:var(--orange)}
    .logo-fallback span.g{color:var(--green)}.logo-fallback span.o2{color:var(--purple)}
    .logo-fallback span.t{color:var(--red)}.logo-fallback span.e{color:var(--orange)}
    .logo-fallback span.k{color:var(--green)}.logo-fallback span.a{color:var(--purple)}
    .header-sub{font-size:.65rem;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-top:2px}
    .badge{background:var(--red);color:white;border-radius:999px;padding:2px 10px;font-size:.78rem;font-weight:700}
    nav{display:flex;gap:.4rem;margin-left:auto;flex-wrap:wrap}
    nav button{background:rgba(255,255,255,.08);color:var(--text);border:1px solid var(--border);
      border-radius:8px;padding:.4rem .9rem;cursor:pointer;font-size:.82rem;
      font-family:'Fredoka One',cursive;letter-spacing:.3px;transition:.2s}
    nav button.active,nav button:hover{background:var(--orange);color:white;border-color:var(--orange)}
    nav button.loja-btn{background:var(--purple);color:white;border-color:var(--purple);font-weight:600}
    nav button.loja-btn:hover{filter:brightness(1.15)}
    .subnav{background:rgba(255,255,255,.04);border-bottom:1px solid var(--border);
      padding:.4rem 2rem;display:none;gap:.4rem;flex-wrap:wrap}
    .subnav.visivel{display:flex}
    .subnav button{background:rgba(255,255,255,.06);color:var(--text);border:1px solid var(--border);
      border-radius:8px;padding:.35rem .8rem;cursor:pointer;font-size:.8rem;
      font-family:'Fredoka One',cursive;letter-spacing:.3px;transition:.2s}
    .subnav button.active,.subnav button:hover{background:var(--orange);color:white;border-color:var(--orange)}

    /* ── Layout ── */
    main{max-width:1200px;margin:1.5rem auto;padding:0 1rem}
    .page{display:none}.page.active{display:block}
    .toolbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem;flex-wrap:wrap;gap:.5rem}

    /* ── Cards ── */
    .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:1rem;margin-bottom:2rem}
    .card{background:var(--dark3);border:1px solid var(--border);border-radius:12px;overflow:hidden;
      box-shadow:0 2px 12px rgba(0,0,0,.3);display:flex;flex-direction:column;transition:border-color .2s}
    .card:hover{border-color:var(--orange)}
    .card-img{width:100%;height:160px;object-fit:contain;object-position:center;display:block;background:#0a1628;padding:6px}
    .card-img-placeholder{width:100%;height:160px;background:linear-gradient(135deg,#0a1628,#16213e);
      display:flex;align-items:center;justify-content:center;font-size:3rem;color:#3a4a6b}
    .card-body{padding:1.1rem;flex:1;display:flex;flex-direction:column}
    .card h2{font-family:'Fredoka One',cursive;font-size:1.05rem;margin-bottom:.3rem;color:white;letter-spacing:.3px}
    .card .meta{font-size:.78rem;color:var(--muted);margin-bottom:.6rem}
    .card .preco-row{display:flex;gap:1.2rem;margin-bottom:.6rem;align-items:center}
    .card .preco-item{font-size:.7rem;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;font-weight:700}
    .card .preco-item span{display:block;font-size:1.3rem;font-weight:800;color:var(--orange);letter-spacing:-.5px}
    .btn-video{display:inline-flex;align-items:center;gap:.3rem;background:rgba(241,10,10,.15);
      border:1px solid rgba(241,10,10,.4);border-radius:6px;padding:2px 8px;font-size:.78rem;
      color:var(--red);text-decoration:none;cursor:pointer}
    .btn-video:hover{background:rgba(241,10,10,.3)}
    .locacao-box{margin-top:.7rem}
    .locacao-box .loc-title{font-size:.7rem;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;font-weight:700;margin-bottom:.45rem}
    .locacao-opcoes{display:flex;flex-direction:column;gap:.3rem}
    .locacao-opcao{background:rgba(123,32,225,.1);border:1px solid rgba(123,32,225,.2);
      border-radius:8px;padding:.4rem .75rem;font-size:.82rem;color:#c4b5fd;
      display:flex;justify-content:space-between;align-items:center;white-space:nowrap}
    .locacao-opcao .loc-dias{font-weight:600;color:#e9d5ff}
    .locacao-opcao .loc-val{font-weight:800;color:#c4b5fd}
    .multa-tag{background:rgba(241,10,10,.1);border:1px solid rgba(241,10,10,.3);
      border-radius:6px;padding:2px 8px;font-size:.75rem;color:#fc8181;margin-top:.4rem;display:inline-block}
    .qty{font-size:1.8rem;font-weight:800}
    .qty.alert{color:var(--red)}.qty.ok{color:var(--green)}
    .label{font-size:.72rem;color:var(--muted)}
    .actions{display:flex;gap:.4rem;margin-top:.8rem;flex-wrap:wrap}
    button{border:none;border-radius:6px;padding:.4rem .85rem;cursor:pointer;font-size:.82rem;font-family:'Nunito',sans-serif;font-weight:700}
    .btn-in{background:rgba(23,198,41,.15);color:#6ee37a;border:1px solid rgba(23,198,41,.3)}
    .btn-in:hover{background:rgba(23,198,41,.28)}
    .btn-out{background:rgba(241,10,10,.12);color:#fc8181;border:1px solid rgba(241,10,10,.3)}
    .btn-out:hover{background:rgba(241,10,10,.25)}
    .btn-edit{background:rgba(237,148,14,.12);color:var(--orange);border:1px solid rgba(237,148,14,.3)}
    .btn-edit:hover{background:rgba(237,148,14,.25)}
    .btn-hist{background:rgba(123,32,225,.12);color:#c9a9ff;border:1px solid rgba(123,32,225,.3)}
    .btn-hist:hover{background:rgba(123,32,225,.25)}
    .btn-add{background:var(--orange);color:white;padding:.55rem 1.3rem;font-size:.92rem;
      border-radius:8px;font-family:'Fredoka One',cursive;letter-spacing:.5px;border:none}
    .btn-add:hover{filter:brightness(1.1)}

    /* ── Alertas ── */
    .alert-banner{background:rgba(241,10,10,.1);border:1px solid rgba(241,10,10,.3);
      border-radius:8px;padding:.75rem 1.1rem;margin-bottom:1.2rem;color:#fc8181;font-size:.88rem;font-weight:600}

    /* ── Tabelas ── */
    section{margin-bottom:2rem}
    section h2{font-family:'Fredoka One',cursive;margin-bottom:1rem;font-size:1.15rem;
      color:var(--orange);letter-spacing:.4px}
    table{width:100%;border-collapse:collapse;background:var(--dark3);border-radius:10px;
      overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.3);border:1px solid var(--border)}
    th{background:rgba(255,255,255,.05);text-align:left;padding:.7rem 1rem;
      font-size:.78rem;color:var(--muted);font-family:'Fredoka One',cursive;letter-spacing:.5px;font-weight:400}
    td{padding:.7rem 1rem;border-top:1px solid var(--border);font-size:.85rem}
    tr:hover td{background:rgba(255,255,255,.02)}
    .tipo-entrada{color:var(--green);font-weight:700}
    .tipo-saida{color:var(--red);font-weight:700}

    /* ── Search ── */
    input[type=search]{padding:.45rem .9rem;background:rgba(255,255,255,.07);
      border:1px solid var(--border);border-radius:20px;color:white;font-size:.88rem;
      width:240px;outline:none;font-family:'Nunito',sans-serif}
    input[type=search]:focus{border-color:var(--orange)}
    input[type=search]::placeholder{color:#555}

    /* ── Dashboard ── */
    .dash-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:1.2rem;margin-bottom:1.5rem}
    .dash-card{background:var(--dark3);border:1px solid var(--border);border-radius:14px;padding:1.2rem}
    .dash-card h3{font-family:'Fredoka One',cursive;font-size:1rem;margin-bottom:1rem;display:flex;align-items:center;gap:.5rem}
    .rank-item{display:flex;align-items:center;gap:.7rem;margin-bottom:.65rem}
    .rank-pos{font-family:'Fredoka One',cursive;font-size:1.1rem;width:24px;text-align:center;color:var(--muted)}
    .rank-pos.gold{color:#FFD700}.rank-pos.silver{color:#C0C0C0}.rank-pos.bronze{color:#CD7F32}
    .rank-bar-wrap{flex:1;min-width:0}
    .rank-label{font-size:.8rem;color:white;margin-bottom:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .rank-bar{height:6px;border-radius:999px;background:var(--border);overflow:hidden}
    .rank-bar-fill{height:100%;border-radius:999px;transition:width .6s ease}
    .rank-val{font-size:.82rem;font-weight:700;white-space:nowrap;min-width:60px;text-align:right}

    /* ── Modal ── */
    .modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:10;
      align-items:center;justify-content:center;padding:1rem}
    .modal-bg.open{display:flex}
    .modal{background:var(--dark2);border:1px solid var(--border);border-radius:14px;
      padding:1.8rem;width:100%;max-width:480px;max-height:90vh;overflow-y:auto;
      box-shadow:0 20px 60px rgba(0,0,0,.5)}
    .modal h3{font-family:'Fredoka One',cursive;margin-bottom:1rem;font-size:1.15rem;
      color:white;letter-spacing:.3px}
    .modal label{display:block;font-size:.8rem;margin:.6rem 0 .25rem;color:var(--muted);font-weight:600}
    .modal input,.modal select,.modal textarea{width:100%;padding:.5rem .75rem;
      background:rgba(255,255,255,.07);border:1px solid var(--border);border-radius:8px;
      color:white;font-size:.9rem;font-family:'Nunito',sans-serif;outline:none}
    .modal input:focus,.modal select:focus{border-color:var(--orange)}
    .modal input::placeholder{color:#555}
    .modal select option{background:var(--dark2);color:white}
    .modal textarea{resize:vertical;min-height:60px}
    .row2{display:grid;grid-template-columns:1fr 1fr;gap:.5rem}
    .row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:.5rem}
    .modal-actions{display:flex;gap:.5rem;margin-top:1.2rem;justify-content:flex-end}
    .btn-cancel{background:rgba(255,255,255,.07);color:var(--muted);border:1px solid var(--border)}
    .btn-cancel:hover{background:rgba(255,255,255,.12)}
    .btn-confirm{background:var(--orange);color:white;font-family:'Fredoka One',cursive;letter-spacing:.4px}
    .btn-confirm:hover{filter:brightness(1.1)}
    .btn-danger{background:rgba(241,10,10,.15);color:#fc8181;border:1px solid rgba(241,10,10,.3)}
    .section-divider{border:none;border-top:1px solid var(--border);margin:.8rem 0}
    .compra-section{background:rgba(255,255,255,.03);border:1px solid var(--border);
      border-radius:8px;padding:.8rem;margin-top:.5rem;display:none}
    .compra-section.visible{display:block}
    .tag{display:inline-block;background:rgba(255,255,255,.08);border-radius:4px;
      padding:1px 7px;font-size:.75rem;color:var(--muted);margin-right:4px}
    .highlight{background:rgba(237,148,14,.1);border-left:3px solid var(--orange);
      padding:.5rem .8rem;border-radius:0 6px 6px 0;font-size:.83rem;margin:.5rem 0;color:var(--orange)}

    /* ── Upload ── */
    .upload-area{border:2px dashed rgba(255,255,255,.15);border-radius:8px;padding:1rem;
      text-align:center;cursor:pointer;transition:.2s;margin-top:.3rem}
    .upload-area:hover,.upload-area.over{border-color:var(--orange);background:rgba(237,148,14,.05)}
    .upload-area input[type=file]{display:none}
    .upload-area .up-label{font-size:.83rem;color:var(--muted)}
    .upload-area .up-label strong{color:var(--orange)}
    .file-chosen{font-size:.8rem;color:var(--green);margin-top:.4rem;display:none}
    .nf-link{display:inline-flex;align-items:center;gap:.3rem;background:rgba(123,32,225,.15);
      border:1px solid rgba(123,32,225,.35);border-radius:6px;padding:2px 8px;
      font-size:.78rem;color:#c9a9ff;text-decoration:none}
    .nf-link:hover{background:rgba(123,32,225,.28)}
    .img-preview{width:100%;max-height:160px;object-fit:cover;border-radius:8px;margin-top:.5rem;display:none}

    /* ── Modal vídeo ── */
    .video-modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:20;
      align-items:center;justify-content:center;padding:1rem}
    .video-modal-bg.open{display:flex}
    .video-wrap{position:relative;width:100%;max-width:700px;background:#000;border-radius:10px;overflow:hidden}
    .video-wrap iframe,.video-wrap video{width:100%;aspect-ratio:16/9;display:block;border:none}
    .video-close{position:absolute;top:.5rem;right:.7rem;background:rgba(0,0,0,.7);color:white;
      border:none;border-radius:50%;width:2rem;height:2rem;font-size:1.1rem;cursor:pointer;
      display:flex;align-items:center;justify-content:center}
    .video-close:hover{background:rgba(0,0,0,.95)}

    /* ── Locação modal fields ── */
    .loc-op-modal{display:grid;grid-template-columns:80px 1fr;gap:.4rem .6rem;align-items:center}
    .loc-op-label{font-size:.78rem;color:var(--muted)}
  </style>
</head>
<body>
<header>
  <div id="header-logo">
    <img class="logo-img" src="/api/logo" alt="Jogoteka"
         onerror="this.style.display='none';document.getElementById('logo-fallback-admin').style.display='block'">
    <div class="logo-fallback" id="logo-fallback-admin" style="display:none">
      <span class="j">J</span><span class="o1">o</span><span class="g">g</span><span class="o2">o</span><span class="t">t</span><span class="e">e</span><span class="k">k</span><span class="a">a</span>
    </div>
    <div class="header-sub">Gestão de Estoque</div>
  </div>
  <span id="alert-badge" class="badge" style="display:none"></span>
  <nav>
    <button class="active" id="nav-fin" onclick="showSecao('financeiro',this)">💼 Financeiro</button>
    <button id="nav-dash" onclick="showSecao('dashboard',this)">📈 Dashboard</button>
    <button onclick="window.open('/loja','_blank')" class="loja-btn">🛍️ Loja</button>
    <button onclick="location.href='/admin'" style="background:rgba(123,32,225,.2);color:#c9a9ff;border:1px solid rgba(123,32,225,.4)">👤 Admin</button>
    <button onclick="location.href='/logout'" style="background:rgba(241,10,10,.12);color:#fc8181;border:1px solid rgba(241,10,10,.3)">Sair</button>
  </nav>
</header>
<div class="subnav visivel" id="subnav-financeiro">
  <button class="active" onclick="showPage('estoque',this)">📦 Estoque</button>
  <button onclick="showPage('movimentacoes',this)">🔄 Movimentações</button>
  <button onclick="showPage('compras',this)">🛒 Compras</button>
  <button onclick="showPage('conciliacao',this)">🏦 Conciliação</button>
  <button onclick="showPage('relatorios',this)">📊 Relatórios</button>
</div>

<main>
  <!-- ESTOQUE -->
  <div class="page active" id="page-estoque">
    <div id="alert-banner"></div>
    <div class="toolbar">
      <input type="search" id="busca" placeholder="Buscar jogo…" oninput="filtrarCards()">
      <button class="btn-add" onclick="openAddModal()">+ Adicionar Jogo</button>
    </div>
    <div id="cards" class="cards"></div>
  </div>

  <!-- MOVIMENTAÇÕES -->
  <div class="page" id="page-movimentacoes">
    <section>
      <h2>Histórico de Movimentações</h2>
      <table>
        <thead><tr><th>Data</th><th>Jogo</th><th>Tipo</th><th>Qtd</th><th>Motivo</th><th>Obs</th><th></th></tr></thead>
        <tbody id="historico"></tbody>
      </table>
    </section>
  </div>

  <!-- COMPRAS -->
  <div class="page" id="page-compras">
    <section>
      <h2>Histórico de Compras</h2>
      <table>
        <thead><tr><th>Data</th><th>Jogo</th><th>Fornecedor</th><th>Pedido</th><th>NF nº</th><th>Qtd</th><th>Valor pago</th><th>Pagamento</th><th>Parcelas</th><th>Nota Fiscal</th><th></th></tr></thead>
        <tbody id="tbl-compras"></tbody>
      </table>
    </section>
  </div>

  <!-- CUPONS -->
  <div class="page" id="page-cupons">
    <section>
      <div class="toolbar">
        <h2 style="margin:0">🎟️ Cupons de Desconto</h2>
        <button class="btn-add" onclick="openModalCupom()">+ Novo Cupom</button>
      </div>
      <table>
        <thead><tr><th>Código</th><th>Tipo</th><th>Valor</th><th>Descrição</th><th>Usos</th><th>Status</th><th>Criado em</th><th></th></tr></thead>
        <tbody id="tbl-cupons"></tbody>
      </table>
    </section>
    <section>
      <h2>Histórico de Uso</h2>
      <table>
        <thead><tr><th>Data</th><th>Cupom</th><th>Operação</th><th>Jogo</th><th>Cliente</th><th>Desconto</th></tr></thead>
        <tbody id="tbl-cupom-usos"></tbody>
      </table>
    </section>
  </div>

  <!-- CONCILIAÇÃO -->
  <div class="page" id="page-conciliacao">
    <section>
      <div class="toolbar">
        <h2 style="margin:0">🏦 Conciliação Bancária</h2>
      </div>
      <div class="conc-upload-box" id="conc-upload-box">
        <div style="font-size:2.5rem">📂</div>
        <p style="margin:.5rem 0;font-weight:700">Arraste o extrato bancário (CSV) ou clique para selecionar</p>
        <p style="font-size:.8rem;color:var(--muted)">Formatos suportados: CSV (Nubank, Inter, Bradesco, Itaú, Sicoob…)</p>
        <input type="file" id="conc-file-input" accept=".csv,.txt" style="display:none" onchange="uploadExtrato(this)">
        <button class="btn-add" style="margin-top:.8rem" onclick="document.getElementById('conc-file-input').click()">Selecionar arquivo</button>
      </div>

      <div id="conc-resultado" style="display:none">
        <div class="conc-result-bar">
          <div class="conc-stat">
            <span id="conc-total-val">0</span>
            <label>Créditos encontrados</label>
          </div>
          <div class="conc-stat" style="color:var(--green)">
            <span id="conc-conc-val">0</span>
            <label>Conciliados</label>
          </div>
          <div class="conc-stat" style="color:var(--orange)">
            <span id="conc-sem-val">0</span>
            <label>Sem match</label>
          </div>
          <button class="btn-confirm" id="btn-conciliar" onclick="conciliar()">⚡ Conciliar Agora</button>
        </div>
        <table>
          <thead><tr><th>Data</th><th>Descrição</th><th>Valor</th><th>Status</th><th>Vinculado</th></tr></thead>
          <tbody id="tbl-lancamentos"></tbody>
        </table>
      </div>

      <div id="conc-historico-box" style="margin-top:2rem">
        <h3 style="margin-bottom:.8rem">Extratos anteriores</h3>
        <table>
          <thead><tr><th>Arquivo</th><th>Data upload</th><th>Lançamentos</th><th></th></tr></thead>
          <tbody id="tbl-extratos"></tbody>
        </table>
      </div>
    </section>
  </div>

  <!-- DASHBOARD -->
  <div class="page" id="page-dashboard">
    <section>
      <div class="toolbar"><h2 style="margin:0">📈 Dashboard</h2>
        <button class="btn-add" onclick="loadDashboard()">↻ Atualizar</button>
      </div>
      <div class="dash-grid">
        <div class="dash-card">
          <h3>🏆 Jogos mais vendidos</h3>
          <div id="dash-mais-vendidos"><p style="color:var(--muted);font-size:.82rem">Carregando…</p></div>
        </div>
        <div class="dash-card">
          <h3>🔑 Jogos mais alugados</h3>
          <div id="dash-mais-alugados"><p style="color:var(--muted);font-size:.82rem">Carregando…</p></div>
        </div>
        <div class="dash-card">
          <h3>👥 Clientes que mais alugaram</h3>
          <div id="dash-top-clientes"><p style="color:var(--muted);font-size:.82rem">Carregando…</p></div>
        </div>
        <div class="dash-card">
          <h3>💰 Jogos com maior lucro</h3>
          <div id="dash-mais-lucro"><p style="color:var(--muted);font-size:.82rem">Carregando…</p></div>
        </div>
      </div>
    </section>
  </div>

  <!-- RELATÓRIOS -->
  <div class="page" id="page-relatorios">

    <!-- RELATÓRIO SEMANAL -->
    <section>
      <div class="toolbar" style="flex-wrap:wrap;gap:.8rem">
        <h2 style="margin:0">📊 Relatório Semanal</h2>
        <div style="display:flex;gap:.6rem;align-items:center;flex-wrap:wrap">
          <label style="font-size:.82rem;color:var(--muted)">De</label>
          <input type="date" id="rel-de" style="width:145px">
          <label style="font-size:.82rem;color:var(--muted)">até</label>
          <input type="date" id="rel-ate" style="width:145px">
          <button class="btn-add" onclick="loadRelatorio()">Gerar</button>
        </div>
      </div>

      <!-- Resumo -->
      <div id="rel-resumo" style="display:none;margin-bottom:1.5rem">
        <div class="conc-result-bar">
          <div class="conc-stat">
            <span id="rel-tot-vendas" style="color:var(--green)">0</span>
            <label>Total Vendas</label>
          </div>
          <div class="conc-stat">
            <span id="rel-tot-locacoes" style="color:var(--purple)">0</span>
            <label>Total Locações</label>
          </div>
          <div class="conc-stat">
            <span id="rel-lucro" style="color:var(--orange)">0</span>
            <label>Lucro Bruto</label>
          </div>
          <div class="conc-stat">
            <span id="rel-margem" style="color:var(--orange)">0%</span>
            <label>Margem Média</label>
          </div>
        </div>
      </div>

      <table id="tbl-relatorio" style="display:none">
        <thead>
          <tr>
            <th>Data</th>
            <th>Tipo</th>
            <th>Item</th>
            <th>Cliente</th>
            <th>Valor</th>
            <th>Custo</th>
            <th>Lucro Bruto</th>
            <th>Margem</th>
          </tr>
        </thead>
        <tbody id="body-relatorio"></tbody>
        <tfoot id="foot-relatorio"></tfoot>
      </table>
      <p id="rel-vazio" style="display:none;color:var(--muted);text-align:center;padding:2rem">Nenhum registro no período selecionado.</p>
    </section>

    <!-- RELATÓRIO DE FAVORITOS -->
    <section style="margin-top:2rem">
      <div class="toolbar">
        <h2 style="margin:0">❤️ Clientes com Favoritos</h2>
        <button class="btn-add" onclick="loadFavoritos()">Atualizar</button>
      </div>
      <p id="fav-vazio" style="display:none;color:var(--muted);text-align:center;padding:2rem">Nenhum cliente com jogos favoritados ainda.</p>
      <table id="tbl-favoritos" style="display:none;width:100%;border-collapse:collapse;margin-top:1rem">
        <thead>
          <tr>
            <th style="text-align:left;padding:.6rem;border-bottom:2px solid var(--border)">Cliente</th>
            <th style="text-align:left;padding:.6rem;border-bottom:2px solid var(--border)">Telefone</th>
            <th style="text-align:center;padding:.6rem;border-bottom:2px solid var(--border)">Favoritos</th>
            <th style="text-align:left;padding:.6rem;border-bottom:2px solid var(--border)">Jogos</th>
            <th style="text-align:left;padding:.6rem;border-bottom:2px solid var(--border)">Cadastro</th>
          </tr>
        </thead>
        <tbody id="body-favoritos"></tbody>
      </table>
    </section>
  </div>

</main>

<!-- Modal Novo Cupom -->
<div class="modal-bg" id="modal-edit-compra" onclick="if(event.target===this)closeModal('modal-edit-compra')">
  <div class="modal" style="max-width:480px">
    <h3>✏️ Editar Compra</h3>
    <label>Data</label>
    <input id="ec-data" type="date">
    <label>Fornecedor</label>
    <input id="ec-fornecedor" placeholder="Ex: Galápagos">
    <label>Pedido de Compra</label>
    <input id="ec-pedido" placeholder="Número do pedido">
    <label>Número NF</label>
    <input id="ec-nf" placeholder="Número da nota fiscal">
    <label>Valor Pago (R$)</label>
    <input id="ec-valor" type="number" min="0" step="0.01" placeholder="0,00">
    <label>Forma de Pagamento</label>
    <select id="ec-forma">
      <option value="">—</option>
      <option value="pix">PIX</option>
      <option value="dinheiro">Dinheiro</option>
      <option value="cartao_debito">Cartão Débito</option>
      <option value="cartao_credito">Cartão Crédito</option>
      <option value="boleto">Boleto</option>
      <option value="transferencia">Transferência</option>
    </select>
    <label>Parcelas</label>
    <input id="ec-parcelas" type="number" min="1" max="24" value="1">
    <label>Observação</label>
    <input id="ec-obs" placeholder="Observações">
    <label>Nota Fiscal (arquivo)</label>
    <div id="ec-nf-atual" style="font-size:.85rem;color:#a0aec0;margin-bottom:.4rem"></div>
    <input id="ec-nf-file" type="file" accept=".pdf,.jpg,.jpeg,.png">
    <div class="modal-actions">
      <button class="btn-cancel" onclick="closeModal('modal-edit-compra')">Cancelar</button>
      <button class="btn-confirm" id="btn-salvar-ec" onclick="salvarEditCompra()">Salvar</button>
    </div>
  </div>
</div>

<div class="modal-bg" id="modal-cupom" onclick="if(event.target===this)closeModal('modal-cupom')">
  <div class="modal" style="max-width:420px">
    <h3>🎟️ Novo Cupom</h3>
    <label>Código</label>
    <input id="cp-codigo" placeholder="Ex: JOGOTEKA10" style="text-transform:uppercase">
    <label>Tipo de desconto</label>
    <select id="cp-tipo">
      <option value="pct">Percentual (%)</option>
      <option value="reais">Valor fixo (R$)</option>
    </select>
    <label>Valor</label>
    <input id="cp-valor" type="number" min="0" step="0.01" placeholder="Ex: 10">
    <label>Descrição (opcional)</label>
    <input id="cp-desc" placeholder="Ex: Desconto de boas-vindas">
    <label>Limite de usos (deixe vazio para ilimitado)</label>
    <input id="cp-usos" type="number" min="1" placeholder="Ilimitado">
    <div class="modal-actions">
      <button class="btn-cancel" onclick="closeModal('modal-cupom')">Cancelar</button>
      <button class="btn-confirm" onclick="salvarCupom()">Criar Cupom</button>
    </div>
  </div>
</div>

<!-- Modal: Adicionar/Editar Jogo -->
<div class="modal-bg" id="modal-jogo">
  <div class="modal">
    <h3 id="modal-jogo-title">Novo Jogo</h3>
    <input type="hidden" id="jogo-id">
    <input type="hidden" id="f-imagem-atual">
    <label>Nome *</label><input id="f-nome" placeholder="Ex: Catan">
    <div class="row2">
      <div><label>Editora</label><input id="f-editora" placeholder="Ex: Devir"></div>
      <div><label>Categoria</label>
        <div style="display:flex;gap:.4rem">
          <select id="f-categoria" style="flex:1;border:1px solid var(--border);border-radius:6px;padding:.45rem .6rem;font-family:inherit;font-size:.9rem;background:#fff">
            <option value="">— selecione —</option>
          </select>
          <button type="button" title="Nova categoria" onclick="adicionarOpcao('categoria')"
            style="background:var(--orange);color:#fff;border:none;border-radius:6px;padding:0 .7rem;font-size:1.1rem;cursor:pointer;font-weight:700">+</button>
        </div>
      </div>
    </div>
    <div class="row3">
      <div><label>Mín. jogadores</label><input id="f-min" type="number" min="1" value="2"></div>
      <div><label>Máx. jogadores</label><input id="f-max" type="number" min="1" value="4"></div>
      <div><label>Tempo (min)</label><input id="f-tempo" type="number" min="1" value="60"></div>
    </div>
    <div class="row2">
      <div><label>Faixa etária</label><input id="f-faixa-etaria" placeholder="Ex: 10+"></div>
    </div>
    <label>Resumo do jogo</label>
    <textarea id="f-resumo" rows="3" placeholder="Breve descrição do jogo para o catálogo..." style="resize:vertical;min-height:70px"></textarea>
    <label>Badge de destaque <span style="font-size:.7rem;color:var(--orange)">(aparece como selo no catálogo)</span></label>
    <div style="display:flex;gap:.4rem">
      <select id="f-destaque" style="flex:1;border:1px solid var(--border);border-radius:6px;padding:.45rem .6rem;font-family:inherit;font-size:.9rem;background:#fff">
        <option value="">— nenhum —</option>
      </select>
      <button type="button" title="Novo badge" onclick="adicionarOpcao('destaque')"
        style="background:var(--orange);color:#fff;border:none;border-radius:6px;padding:0 .7rem;font-size:1.1rem;cursor:pointer;font-weight:700">+</button>
    </div>
    <label style="display:flex;align-items:center;gap:.6rem;cursor:pointer;margin-top:.3rem">
      <input id="f-em-destaque" type="checkbox" style="width:1.1rem;height:1.1rem;accent-color:var(--orange)">
      <span>🏆 Exibir como <strong>A Escolha da Comunidade</strong> no topo do catálogo</span>
    </label>
    <label style="margin-top:.5rem">🏙️ Disponível em <span style="font-size:.7rem;color:var(--orange)">(marque as cidades onde este jogo aparece no catálogo)</span></label>
    <div id="f-cidades-wrap" style="display:flex;flex-wrap:wrap;gap:.6rem .9rem;margin-top:.2rem">
      {% for slug, c in cidades.items() %}
      <label style="display:flex;align-items:center;gap:.4rem;cursor:pointer;font-weight:600;font-size:.9rem">
        <input type="checkbox" class="f-cidade-cb" value="{{ slug }}"
               style="width:1rem;height:1rem;accent-color:var(--purple)">
        {{ c.emoji }} {{ c.nome }}
      </label>
      {% endfor %}
    </div>
    <div class="row2">
      <div><label>Qtd inicial <span style="font-size:.7rem;color:var(--orange)">(lança entrada automática)</span></label><input id="f-qty" type="number" min="0" value="0"></div>
      <div><label>Qtd mínima p/ alerta</label><input id="f-min-qty" type="number" min="0" value="1"></div>
    </div>
    <hr class="section-divider">
    <div class="row2">
      <div><label>Preço de venda (R$)</label><input id="f-preco-venda" type="number" min="0" step="0.01" placeholder="0,00"></div>
    </div>
    <hr class="section-divider">
    <label>Imagem do jogo (capa)</label>
    <div class="upload-area" onclick="document.getElementById('f-imagem-file').click()"
         ondragover="event.preventDefault();this.classList.add('over')"
         ondragleave="this.classList.remove('over')"
         ondrop="handleDropImagem(event)">
      <input type="file" id="f-imagem-file" accept="image/jpeg,image/png,image/webp,image/gif" onchange="onImagemChosen(this)">
      <div class="up-label">Arraste a imagem aqui ou <strong>clique para selecionar</strong><br><small style="color:#a0aec0">JPG, PNG, WEBP ou GIF</small></div>
      <div class="file-chosen" id="imagem-chosen-label"></div>
    </div>
    <img id="f-imagem-preview" class="img-preview" alt="Prévia da capa">
    <label style="margin-top:.8rem">Link do vídeo sobre o jogo</label>
    <input id="f-video-url" placeholder="Ex: https://youtube.com/watch?v=... ou https://youtu.be/...">
    <hr class="section-divider">
    <div style="font-size:.83rem;font-weight:600;color:#4a5568;margin-bottom:.4rem">Opções de locação</div>
    <div style="display:grid;grid-template-columns:80px 1fr;gap:.4rem .6rem;align-items:center">
      <span style="font-size:.8rem;color:#718096">Opção 1</span>
      <div class="row2">
        <div><label>Dias</label><input id="f-loc1-dias" type="number" min="1" placeholder="Ex: 1"></div>
        <div><label>Valor (R$)</label><input id="f-loc1-valor" type="number" min="0" step="0.01" placeholder="0,00"></div>
      </div>
      <span style="font-size:.8rem;color:#718096">Opção 2</span>
      <div class="row2">
        <div><label>Dias</label><input id="f-loc2-dias" type="number" min="1" placeholder="Ex: 3"></div>
        <div><label>Valor (R$)</label><input id="f-loc2-valor" type="number" min="0" step="0.01" placeholder="0,00"></div>
      </div>
      <span style="font-size:.8rem;color:#718096">Opção 3</span>
      <div class="row2">
        <div><label>Dias</label><input id="f-loc3-dias" type="number" min="1" placeholder="Ex: 5"></div>
        <div><label>Valor (R$)</label><input id="f-loc3-valor" type="number" min="0" step="0.01" placeholder="0,00"></div>
      </div>
    </div>
    <label style="margin-top:.6rem">Multa por dia de atraso (R$)</label>
    <input id="f-multa-dia" type="number" min="0" step="0.01" placeholder="Ex: 5,00">
    <div class="modal-actions">
      <button class="btn-cancel" onclick="closeModal('modal-jogo')">Cancelar</button>
      <button class="btn-danger" id="btn-excluir-jogo" style="display:none" onclick="excluirJogo()">🗑️ Excluir</button>
      <button class="btn-confirm" id="btn-salvar-jogo" onclick="salvarJogo()">Salvar</button>
    </div>
  </div>
</div>

<!-- Modal: Player de vídeo -->
<div class="video-modal-bg" id="modal-video" onclick="fecharVideo(event)">
  <div class="video-wrap">
    <button class="video-close" onclick="fecharVideo()">✕</button>
    <div id="video-player"></div>
  </div>
</div>

<!-- Modal: Entrada/Saída -->
<div class="modal-bg" id="modal-mov">
  <div class="modal">
    <h3 id="mov-title">Movimentar</h3>
    <input type="hidden" id="mov-id">
    <input type="hidden" id="mov-tipo">
    <div class="row2">
      <div><label>Quantidade</label><input id="mov-qty" type="number" min="1" value="1"></div>
      <div><label>Motivo</label><select id="mov-motivo" onchange="toggleCompraSection()"></select></div>
    </div>
    <label>Observação</label><input id="mov-obs" placeholder="Opcional">

    <!-- Campos extras para compra -->
    <div class="compra-section" id="compra-section">
      <hr class="section-divider">
      <p class="highlight">Dados da compra</p>
      <div class="row2">
        <div><label>Fornecedor</label><input id="c-fornecedor" placeholder="Nome do fornecedor"></div>
        <div><label>Data da compra</label><input id="c-data" type="date"></div>
      </div>
      <div class="row2">
        <div><label>Pedido de compra nº</label><input id="c-pedido" placeholder="Ex: PC-2026-001"></div>
        <div><label>Nota fiscal nº</label><input id="c-nf" placeholder="Ex: NF-000123"></div>
      </div>
      <div class="row2">
        <div><label>Valor pago (R$)</label><input id="c-valor" type="number" min="0" step="0.01" placeholder="0,00"></div>
        <div><label>Forma de pagamento</label>
          <select id="c-forma">
            <option value="">Selecione…</option>
            <option>PIX</option>
            <option>Dinheiro</option>
            <option>Cartão de débito</option>
            <option>Cartão de crédito</option>
            <option>Boleto</option>
            <option>Transferência</option>
          </select>
        </div>
      </div>
      <div id="parcelas-row" style="display:none">
        <label>Parcelas</label><input id="c-parcelas" type="number" min="1" max="60" value="1" placeholder="Nº de parcelas">
      </div>
      <label>Nota fiscal</label>
      <div class="upload-area" id="upload-area" onclick="document.getElementById('c-nf-file').click()"
           ondragover="event.preventDefault();this.classList.add('over')"
           ondragleave="this.classList.remove('over')"
           ondrop="handleDrop(event)">
        <input type="file" id="c-nf-file" accept="application/pdf,image/*" onchange="onFileChosen(this)">
        <div class="up-label">Arraste o arquivo (PDF ou imagem) ou <strong>clique para selecionar</strong></div>
        <div class="file-chosen" id="file-chosen-label"></div>
      </div>
    </div>

    <div class="modal-actions">
      <button class="btn-cancel" onclick="closeModal('modal-mov')">Cancelar</button>
      <button class="btn-confirm" onclick="confirmarMov()">Confirmar</button>
    </div>
  </div>
</div>

<!-- Modal: Histórico de compras do jogo -->
<div class="modal-bg" id="modal-hist-compras">
  <div class="modal" style="max-width:640px">
    <h3 id="hist-title">Histórico de compras</h3>
    <div id="hist-content" style="margin-top:.8rem"></div>
    <div class="modal-actions">
      <button class="btn-cancel" onclick="closeModal('modal-hist-compras')">Fechar</button>
    </div>
  </div>
</div>

<script>
console.log('[DEBUG] Script iniciando...');
const MOTIVOS = { entrada: ['compra','devolução','ajuste','outro'], saida: ['venda','empréstimo','ajuste','perda','outro'] };
let todosJogos = [];

function fmt(v){ return v != null ? 'R$ ' + Number(v).toFixed(2).replace('.',',') : '—'; }
function fmtData(d){ return d ? d.slice(0,10).split('-').reverse().join('/') : '—'; }

function fmtLocacao(j){
  const ops = [[j.loc1_dias,j.loc1_valor],[j.loc2_dias,j.loc2_valor],[j.loc3_dias,j.loc3_valor]]
    .filter(([d,v])=>d&&v!=null);
  if(!ops.length && !j.multa_dia) return '';
  const badges = ops.map(([d,v])=>
    `<span class="locacao-opcao"><span class="loc-dias">${d} dia${d>1?'s':''}</span><span class="loc-val">${fmt(v)}</span></span>`).join('');
  const multa = j.multa_dia!=null
    ? `<div class="multa-tag">⚠️ Multa: ${fmt(j.multa_dia)}/dia de atraso</div>` : '';
  return `<div class="locacao-box">
    <div class="loc-title">🔑 Locação</div>
    <div class="locacao-opcoes">${badges}</div>
    ${multa}
  </div>`;
}

async function api(path, opts={}){
  const r = await fetch('/api'+path,{headers:{'Content-Type':'application/json'},...opts});
  return r.json();
}

function showSecao(secao, btn){
  document.querySelectorAll('nav button').forEach(b=>b.classList.remove('active'));
  if(btn) btn.classList.add('active');
  const subFin = document.getElementById('subnav-financeiro');
  if(secao==='financeiro'){
    subFin.classList.add('visivel');
    // Activa a sub-aba que estava ativa, ou Estoque por padrão
    const subAtivo = subFin.querySelector('button.active') || subFin.querySelector('button');
    showPage(subAtivo.getAttribute('onclick').match(/'(\w+)'/)[1], null);
  } else {
    subFin.classList.remove('visivel');
    document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
    document.getElementById('page-'+secao).classList.add('active');
    if(secao==='dashboard') loadDashboard();
  }
}

function showPage(name, btn){
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.subnav button').forEach(b=>b.classList.remove('active'));
  document.getElementById('page-'+name).classList.add('active');
  if(btn) btn.classList.add('active');
  if(name==='movimentacoes') loadMovimentacoes();
  if(name==='compras') loadCompras();
  if(name==='conciliacao'){ loadExtratos(); }
  if(name==='relatorios'){ initRelatorio(); }
  if(name==='estoque') load();
}

async function load(){
  try {
    todosJogos = await api('/jogos');
    if(!Array.isArray(todosJogos)) todosJogos = [];
    const alertas = todosJogos.filter(j=>j.quantidade<=j.quantidade_minima);
    document.getElementById('alert-badge').textContent = alertas.length ? alertas.length+' alerta(s)' : '';
    document.getElementById('alert-badge').style.display = alertas.length ? '' : 'none';
    document.getElementById('alert-banner').innerHTML = alertas.length
      ? '<div class="alert-banner">⚠️ Estoque baixo: '+alertas.map(j=>j.nome).join(', ')+'</div>' : '';
    renderCards(todosJogos);
  } catch(e){ console.error('[load]', e); }
  // opções do form carregadas lazily ao abrir o modal
}

async function carregarOpcoesForm(){
  const [cats, dests] = await Promise.all([
    api('/categorias'),
    api('/destaques-opcoes'),
  ]);
  _popularSelect('f-categoria', Array.isArray(cats)  ? cats  : [], '— selecione —');
  _popularSelect('f-destaque',  Array.isArray(dests) ? dests : [], '— nenhum —');
}

function _popularSelect(id, items, placeholder){
  const sel = document.getElementById(id);
  if(!sel || !Array.isArray(items)) return;
  const val = sel.value;
  sel.innerHTML = `<option value="">${placeholder}</option>` +
    items.map(i=>`<option value="${i.nome}">${i.nome}</option>`).join('');
  if(val) sel.value = val;
}

async function adicionarOpcao(tipo){
  const label = tipo === 'categoria' ? 'categoria' : 'badge de destaque';
  const nome = prompt(`Nome da nova ${label}:`);
  if(!nome || !nome.trim()) return;
  const endpoint = tipo === 'categoria' ? '/categorias' : '/destaques-opcoes';
  const res = await api(endpoint, {method:'POST', body:JSON.stringify({nome: nome.trim()})});
  if(res.erro){ alert(res.erro); return; }
  await carregarOpcoesForm();
  // seleciona a opção recém-criada
  document.getElementById(tipo === 'categoria' ? 'f-categoria' : 'f-destaque').value = res.nome;
}

function filtrarCards(){
  const q = document.getElementById('busca').value.toLowerCase();
  renderCards(todosJogos.filter(j=>
    j.nome.toLowerCase().includes(q) ||
    (j.editora||'').toLowerCase().includes(q) ||
    (j.categoria||'').toLowerCase().includes(q)
  ));
}

function renderCards(jogos){
  document.getElementById('cards').innerHTML = jogos.map(j=>`
    <div class="card">
      ${j.imagem
        ? `<img class="card-img" src="/api/imagens/${j.imagem}" alt="${j.nome}">`
        : `<div class="card-img-placeholder">🎲</div>`}
      <div class="card-body">
        <h2>${j.nome}</h2>
        <div class="meta">
          ${j.editora?'<span class="tag">'+j.editora+'</span>':''}
          ${j.categoria?'<span class="tag">'+j.categoria+'</span>':''}
          ${j.min_jogadores?'<span class="tag">'+j.min_jogadores+'-'+j.max_jogadores+' jog.</span>':''}
          ${j.tempo_jogo?'<span class="tag">'+j.tempo_jogo+'min</span>':''}
        </div>
        <div class="preco-row">
          <div class="preco-item">Valor de Venda<span>${fmt(j.preco_venda)}</span></div>
          ${j.video_url ? `<button class="btn-video" onclick="abrirVideo('${encodeURIComponent(j.video_url)}')">▶ Ver vídeo</button>` : ''}
        </div>
        ${fmtLocacao(j)}
        <div class="qty ${j.quantidade<=j.quantidade_minima?'alert':'ok'}">${j.quantidade}</div>
        <div class="label">unidades em estoque (mín: ${j.quantidade_minima})</div>
        <div class="actions">
          <button class="btn-in"   onclick="openMov(${j.id},'entrada')">+ Entrada</button>
          <button class="btn-out"  onclick="openMov(${j.id},'saida')">− Saída</button>
          <button class="btn-edit" onclick="openEditModal(${j.id})">Editar</button>
          <button class="btn-hist" onclick="verHistCompras(${j.id},'${j.nome.replace(/'/g,"\\'")}')">Compras</button>
        </div>
      </div>
    </div>`).join('');
}

// ── Jogo ──────────────────────────────────────────────────────────────────────
function _resetModalJogo(){
  document.getElementById('f-imagem-file').value='';
  document.getElementById('f-imagem-atual').value='';
  document.getElementById('imagem-chosen-label').style.display='none';
  document.getElementById('imagem-chosen-label').textContent='';
  document.getElementById('f-imagem-preview').style.display='none';
  document.getElementById('f-imagem-preview').src='';
  document.getElementById('f-video-url').value='';
  ['loc1-dias','loc1-valor','loc2-dias','loc2-valor','loc3-dias','loc3-valor','multa-dia']
    .forEach(id=>document.getElementById('f-'+id).value='');
}

function openAddModal(){
  document.getElementById('modal-jogo-title').textContent = 'Novo Jogo';
  document.getElementById('btn-excluir-jogo').style.display = 'none';
  document.getElementById('jogo-id').value = '';
  ['nome','editora','categoria'].forEach(id=>document.getElementById('f-'+id).value='');
  document.getElementById('f-min').value=2;
  document.getElementById('f-max').value=4;
  document.getElementById('f-tempo').value=60;
  document.getElementById('f-qty').value=0;
  document.getElementById('f-min-qty').value=1;
  document.getElementById('f-preco-venda').value='';
  document.getElementById('f-faixa-etaria').value='';
  document.getElementById('f-resumo').value='';
  document.getElementById('f-destaque').value='';
  document.getElementById('f-em-destaque').checked=false;
  document.querySelectorAll('.f-cidade-cb').forEach(cb => cb.checked = false);
  _resetModalJogo();
  carregarOpcoesForm().then(()=>{
    _verificarRascunho();
    _iniciarAutoSave();
    document.getElementById('modal-jogo').classList.add('open');
  }).catch(()=>{
    document.getElementById('modal-jogo').classList.add('open');
  });
}

function _iniciarAutoSave(){
  const modal = document.getElementById('modal-jogo');
  // remove listener anterior se existir
  if(modal._autoSaveHandler) modal.removeEventListener('input', modal._autoSaveHandler);
  if(modal._autoSaveHandler2) modal.removeEventListener('change', modal._autoSaveHandler2);
  const handler = () => {
    // só faz rascunho para novos jogos
    if(!document.getElementById('jogo-id').value) _salvarRascunho();
  };
  modal._autoSaveHandler = handler;
  modal._autoSaveHandler2 = handler;
  modal.addEventListener('input', handler);
  modal.addEventListener('change', handler);
}

function openEditModal(id){
  const j = todosJogos.find(x=>x.id===id);
  if(!j) return;
  document.getElementById('modal-jogo-title').textContent = 'Editar Jogo';
  document.getElementById('btn-excluir-jogo').style.display = '';
  document.getElementById('jogo-id').value = id;
  document.getElementById('f-nome').value = j.nome||'';
  document.getElementById('f-editora').value = j.editora||'';
  document.getElementById('f-categoria').value = j.categoria||'';
  document.getElementById('f-min').value = j.min_jogadores||2;
  document.getElementById('f-max').value = j.max_jogadores||4;
  document.getElementById('f-tempo').value = j.tempo_jogo||60;
  document.getElementById('f-qty').value = 0;
  document.getElementById('f-min-qty').value = j.quantidade_minima||1;
  document.getElementById('f-preco-venda').value = j.preco_venda||'';
  _resetModalJogo();
  document.getElementById('f-imagem-atual').value = j.imagem||'';
  document.getElementById('f-video-url').value = j.video_url||'';
  document.getElementById('f-loc1-dias').value  = j.loc1_dias||'';
  document.getElementById('f-loc1-valor').value = j.loc1_valor!=null?j.loc1_valor:'';
  document.getElementById('f-loc2-dias').value  = j.loc2_dias||'';
  document.getElementById('f-loc2-valor').value = j.loc2_valor!=null?j.loc2_valor:'';
  document.getElementById('f-loc3-dias').value  = j.loc3_dias||'';
  document.getElementById('f-loc3-valor').value = j.loc3_valor!=null?j.loc3_valor:'';
  document.getElementById('f-multa-dia').value  = j.multa_dia!=null?j.multa_dia:'';
  document.getElementById('f-faixa-etaria').value = j.faixa_etaria||'';
  document.getElementById('f-resumo').value = j.resumo||'';
  document.getElementById('f-destaque').value = j.destaque||'';
  document.getElementById('f-em-destaque').checked = !!j.em_destaque;
  const _cids = (j.cidades||'').split(',').map(s=>s.trim()).filter(Boolean);
  document.querySelectorAll('.f-cidade-cb').forEach(cb => cb.checked = _cids.includes(cb.value));
  if(j.imagem){
    const prev = document.getElementById('f-imagem-preview');
    prev.src = '/api/imagens/'+j.imagem;
    prev.style.display='block';
  }
  carregarOpcoesForm().catch(()=>{}).finally(()=>{
    document.getElementById('modal-jogo').classList.add('open');
  });
}

async function excluirJogo(){
  const id = parseInt(document.getElementById('jogo-id').value);
  const j = todosJogos.find(x=>x.id===id);
  if(!confirm(`Excluir "${j?.nome}"? Esta ação não pode ser desfeita.`)) return;
  const res = await api('/jogos/'+id, {method:'DELETE'});
  if(res.error){ alert(res.error); return; }
  closeModal('modal-jogo');
  loadEstoque();
}

function onImagemChosen(input){
  const label = document.getElementById('imagem-chosen-label');
  const prev  = document.getElementById('f-imagem-preview');
  if(input.files && input.files[0]){
    label.textContent = '🖼 ' + input.files[0].name;
    label.style.display = 'block';
    prev.src = URL.createObjectURL(input.files[0]);
    prev.style.display = 'block';
  }
}

function handleDropImagem(e){
  e.preventDefault();
  e.currentTarget.classList.remove('over');
  const file = e.dataTransfer.files[0];
  if(file && file.type.startsWith('image/')){
    const dt = new DataTransfer(); dt.items.add(file);
    const input = document.getElementById('f-imagem-file');
    input.files = dt.files;
    onImagemChosen(input);
  }
}

// ── Rascunho automático ────────────────────────────────────────────────────────
const _RASCUNHO_KEY = 'jgt_rascunho_jogo';

function _coletarFormJogo(){
  return {
    nome:         document.getElementById('f-nome').value,
    editora:      document.getElementById('f-editora').value,
    categoria:    document.getElementById('f-categoria').value,
    min:          document.getElementById('f-min').value,
    max:          document.getElementById('f-max').value,
    tempo:        document.getElementById('f-tempo').value,
    qty:          document.getElementById('f-qty').value,
    min_qty:      document.getElementById('f-min-qty').value,
    preco_venda:  document.getElementById('f-preco-venda').value,
    video_url:    document.getElementById('f-video-url').value,
    loc1_dias:    document.getElementById('f-loc1-dias').value,
    loc1_valor:   document.getElementById('f-loc1-valor').value,
    loc2_dias:    document.getElementById('f-loc2-dias').value,
    loc2_valor:   document.getElementById('f-loc2-valor').value,
    loc3_dias:    document.getElementById('f-loc3-dias').value,
    loc3_valor:   document.getElementById('f-loc3-valor').value,
    multa_dia:    document.getElementById('f-multa-dia').value,
    faixa_etaria: document.getElementById('f-faixa-etaria').value,
    resumo:       document.getElementById('f-resumo').value,
    destaque:     document.getElementById('f-destaque').value,
    em_destaque:  document.getElementById('f-em-destaque').checked,
    cidades:      [...document.querySelectorAll('.f-cidade-cb:checked')].map(cb=>cb.value),
    jogo_id:      document.getElementById('jogo-id').value,
    imagem_atual: document.getElementById('f-imagem-atual').value,
  };
}

function _salvarRascunho(){
  // só salva se for novo jogo (sem id) ou se tiver nome preenchido
  const dados = _coletarFormJogo();
  if(!dados.nome) return;
  localStorage.setItem(_RASCUNHO_KEY, JSON.stringify({...dados, _ts: Date.now()}));
}

function _limparRascunho(){ localStorage.removeItem(_RASCUNHO_KEY); }

function _verificarRascunho(){
  const raw = localStorage.getItem(_RASCUNHO_KEY);
  if(!raw) return;
  try {
    const d = JSON.parse(raw);
    if(d.jogo_id) return; // rascunho de edição — ignorar
    if(!d.nome) return;
    const mins = Math.round((Date.now() - d._ts) / 60000);
    const msg = `Encontramos um rascunho não salvo de "${d.nome}" (${mins < 1 ? 'agora mesmo' : mins + ' min atrás'}).\n\nDeseja restaurar o rascunho?`;
    if(confirm(msg)) _restaurarRascunho(d);
    else _limparRascunho();
  } catch(e){ _limparRascunho(); }
}

function _restaurarRascunho(d){
  document.getElementById('f-nome').value         = d.nome||'';
  document.getElementById('f-editora').value      = d.editora||'';
  document.getElementById('f-categoria').value    = d.categoria||'';
  document.getElementById('f-min').value          = d.min||2;
  document.getElementById('f-max').value          = d.max||4;
  document.getElementById('f-tempo').value        = d.tempo||60;
  document.getElementById('f-qty').value          = d.qty||0;
  document.getElementById('f-min-qty').value      = d.min_qty||1;
  document.getElementById('f-preco-venda').value  = d.preco_venda||'';
  document.getElementById('f-video-url').value    = d.video_url||'';
  document.getElementById('f-loc1-dias').value    = d.loc1_dias||'';
  document.getElementById('f-loc1-valor').value   = d.loc1_valor||'';
  document.getElementById('f-loc2-dias').value    = d.loc2_dias||'';
  document.getElementById('f-loc2-valor').value   = d.loc2_valor||'';
  document.getElementById('f-loc3-dias').value    = d.loc3_dias||'';
  document.getElementById('f-loc3-valor').value   = d.loc3_valor||'';
  document.getElementById('f-multa-dia').value    = d.multa_dia||'';
  document.getElementById('f-faixa-etaria').value = d.faixa_etaria||'';
  document.getElementById('f-resumo').value       = d.resumo||'';
  document.getElementById('f-destaque').value     = d.destaque||'';
  document.getElementById('f-em-destaque').checked= !!d.em_destaque;
  if(d.cidades && d.cidades.length){
    document.querySelectorAll('.f-cidade-cb').forEach(cb => {
      cb.checked = d.cidades.includes(cb.value);
    });
  }
}

async function salvarJogo(){
  const btn = document.getElementById('btn-salvar-jogo');
  const txtOriginal = btn ? btn.textContent : 'Salvar';
  if(btn){ btn.disabled = true; btn.textContent = '⏳ Salvando...'; }

  try {
    // Upload de imagem se escolhida
    let imagem = document.getElementById('f-imagem-atual').value || null;
    const fileInput = document.getElementById('f-imagem-file');
    if(fileInput.files && fileInput.files[0]){
      const fd = new FormData();
      fd.append('file', fileInput.files[0]);
      const up = await fetch('/api/upload/imagem', {method:'POST', body:fd});
      const upRes = await up.json();
      if(upRes.error){ alert('Erro no upload da imagem: '+upRes.error); return; }
      imagem = upRes.filename;
    }

    const id = document.getElementById('jogo-id').value;
    const dados = {
      nome:         document.getElementById('f-nome').value,
      editora:      document.getElementById('f-editora').value||null,
      categoria:    document.getElementById('f-categoria').value||null,
      min_jogadores: +document.getElementById('f-min').value||null,
      max_jogadores: +document.getElementById('f-max').value||null,
      tempo_jogo:   +document.getElementById('f-tempo').value||null,
      quantidade:   +document.getElementById('f-qty').value||0,
      quantidade_minima: +document.getElementById('f-min-qty').value||1,
      preco_venda:  parseFloat(document.getElementById('f-preco-venda').value)||null,
      imagem,
      video_url:    document.getElementById('f-video-url').value||null,
      loc1_dias:    parseInt(document.getElementById('f-loc1-dias').value)||null,
      loc1_valor:   parseFloat(document.getElementById('f-loc1-valor').value)||null,
      loc2_dias:    parseInt(document.getElementById('f-loc2-dias').value)||null,
      loc2_valor:   parseFloat(document.getElementById('f-loc2-valor').value)||null,
      loc3_dias:    parseInt(document.getElementById('f-loc3-dias').value)||null,
      loc3_valor:   parseFloat(document.getElementById('f-loc3-valor').value)||null,
      multa_dia:    parseFloat(document.getElementById('f-multa-dia').value)||null,
      faixa_etaria: document.getElementById('f-faixa-etaria').value||null,
      resumo:       document.getElementById('f-resumo').value||null,
      destaque:     document.getElementById('f-destaque').value||null,
      em_destaque:  document.getElementById('f-em-destaque').checked ? 1 : 0,
      cidades:      [...document.querySelectorAll('.f-cidade-cb:checked')].map(cb=>cb.value).join(',')||null,
    };

    if(!dados.nome){ alert('O nome do jogo é obrigatório.'); return; }

    const res = id
      ? await api('/jogos/'+id, {method:'PUT', body:JSON.stringify(dados)})
      : await api('/jogos',     {method:'POST', body:JSON.stringify(dados)});

    if(res && (res.erro || res.error)){
      alert('Erro ao salvar: ' + (res.erro || res.error));
      return;
    }

    _limparRascunho();
    closeModal('modal-jogo');
    load();

  } catch(e) {
    console.error('[salvarJogo]', e);
    alert('Erro ao salvar. Seus dados foram preservados no rascunho.');
    _salvarRascunho();
  } finally {
    if(btn){ btn.disabled = false; btn.textContent = txtOriginal; }
  }
}

// ── Vídeo ─────────────────────────────────────────────────────────────────────
function embedUrl(url){
  try {
    const u = new URL(url);
    // YouTube
    if(u.hostname.includes('youtube.com') || u.hostname.includes('youtu.be')){
      let vid = u.searchParams.get('v') || u.pathname.split('/').pop();
      return `https://www.youtube.com/embed/${vid}?autoplay=1`;
    }
    // Vimeo
    if(u.hostname.includes('vimeo.com')){
      const vid = u.pathname.split('/').filter(Boolean).pop();
      return `https://player.vimeo.com/video/${vid}?autoplay=1`;
    }
  } catch(e){}
  return null;
}

function abrirVideo(encoded){
  const url = decodeURIComponent(encoded);
  const embed = embedUrl(url);
  const player = document.getElementById('video-player');
  if(embed){
    player.innerHTML = `<iframe src="${embed}" allow="autoplay;fullscreen" allowfullscreen></iframe>`;
  } else {
    player.innerHTML = `<video src="${url}" controls autoplay></video>`;
  }
  document.getElementById('modal-video').classList.add('open');
}

function fecharVideo(e){
  if(e && e.target !== document.getElementById('modal-video') && !e.target.classList.contains('video-close')) return;
  document.getElementById('modal-video').classList.remove('open');
  document.getElementById('video-player').innerHTML='';
}

// ── Movimentação ──────────────────────────────────────────────────────────────
function openMov(id, tipo){
  document.getElementById('mov-id').value = id;
  document.getElementById('mov-tipo').value = tipo;
  document.getElementById('mov-title').textContent = tipo==='entrada'?'Registrar Entrada':'Registrar Saída';
  document.getElementById('mov-qty').value = 1;
  document.getElementById('mov-obs').value = '';
  const sel = document.getElementById('mov-motivo');
  sel.innerHTML = MOTIVOS[tipo].map(m=>`<option>${m}</option>`).join('');
  document.getElementById('c-data').value = new Date().toISOString().slice(0,10);
  document.getElementById('c-fornecedor').value='';
  document.getElementById('c-pedido').value='';
  document.getElementById('c-nf').value='';
  document.getElementById('c-valor').value='';
  document.getElementById('c-forma').value='';
  document.getElementById('c-parcelas').value=1;
  document.getElementById('c-nf-file').value='';
  document.getElementById('file-chosen-label').style.display='none';
  document.getElementById('file-chosen-label').textContent='';
  toggleCompraSection();
  document.getElementById('modal-mov').classList.add('open');
}

function toggleCompraSection(){
  const motivo = document.getElementById('mov-motivo').value;
  const tipo = document.getElementById('mov-tipo').value;
  const show = tipo==='entrada' && motivo==='compra';
  document.getElementById('compra-section').classList.toggle('visible', show);
  const forma = document.getElementById('c-forma').value;
  document.getElementById('parcelas-row').style.display =
    forma==='Cartão de crédito' ? 'block' : 'none';
}

document.getElementById('c-forma').addEventListener('change', toggleCompraSection);

function onFileChosen(input){
  const label = document.getElementById('file-chosen-label');
  if(input.files && input.files[0]){
    label.textContent = '📄 ' + input.files[0].name;
    label.style.display = 'block';
  }
}

function handleDrop(e){
  e.preventDefault();
  document.getElementById('upload-area').classList.remove('over');
  const file = e.dataTransfer.files[0];
  if(file && file.type==='application/pdf'){
    const dt = new DataTransfer();
    dt.items.add(file);
    const input = document.getElementById('c-nf-file');
    input.files = dt.files;
    onFileChosen(input);
  }
}

async function confirmarMov(){
  const btn = document.querySelector('#modal-mov .btn-confirm');
  if(btn.disabled) return;
  btn.disabled = true; btn.textContent = 'Salvando…';

  const id = +document.getElementById('mov-id').value;
  const tipo = document.getElementById('mov-tipo').value;
  const motivo = document.getElementById('mov-motivo').value;
  const isCompra = tipo==='entrada' && motivo==='compra';

  // Upload do PDF antes de salvar, se houver
  let arquivo_nf = null;
  if(isCompra){
    const fileInput = document.getElementById('c-nf-file');
    if(fileInput.files && fileInput.files[0]){
      const fd = new FormData();
      fd.append('file', fileInput.files[0]);
      const up = await fetch('/api/upload/nf', {method:'POST', body:fd});
      const upRes = await up.json();
      if(upRes.error){ alert('Erro no upload: '+upRes.error); return; }
      arquivo_nf = upRes.filename;
    }
  }

  const body = {
    tipo, quantidade: +document.getElementById('mov-qty').value,
    motivo, observacao: document.getElementById('mov-obs').value,
  };

  if(isCompra){
    body.compra = {
      fornecedor: document.getElementById('c-fornecedor').value,
      data_compra: document.getElementById('c-data').value,
      pedido_compra: document.getElementById('c-pedido').value||null,
      numero_nf: document.getElementById('c-nf').value||null,
      arquivo_nf,
      valor_pago: parseFloat(document.getElementById('c-valor').value)||null,
      forma_pagamento: document.getElementById('c-forma').value||null,
      parcelas: +document.getElementById('c-parcelas').value||1,
      observacao: document.getElementById('mov-obs').value,
    };
  }

  const res = await api('/jogos/'+id+'/movimentar',{method:'POST',body:JSON.stringify(body)});
  if(res.error){ alert('Erro: '+res.error); return; }
  closeModal('modal-mov'); load();
}

// ── Histórico compras por jogo ─────────────────────────────────────────────
async function verHistCompras(jogoId, jogoNome){
  document.getElementById('hist-title').textContent = 'Compras — '+jogoNome;
  const rows = await api('/compras?jogo_id='+jogoId);
  if(!rows.length){
    document.getElementById('hist-content').innerHTML='<p style="color:#718096">Nenhuma compra registrada.</p>';
  } else {
    document.getElementById('hist-content').innerHTML=`
      <table>
        <thead><tr><th>Data</th><th>Fornecedor</th><th>Pedido</th><th>NF</th><th>Qtd</th><th>Valor pago</th><th>Pagamento</th><th>Parcelas</th><th>Arquivo</th></tr></thead>
        <tbody>${rows.map(r=>`
          <tr>
            <td>${fmtData(r.data_compra)}</td>
            <td>${r.fornecedor||'—'}</td>
            <td>${r.pedido_compra||'—'}</td>
            <td>${r.numero_nf||'—'}</td>
            <td>${r.quantidade}</td>
            <td>${fmt(r.valor_pago)}</td>
            <td>${r.forma_pagamento||'—'}</td>
            <td>${r.parcelas&&r.parcelas>1?r.parcelas+'x':'—'}</td>
            <td>${r.arquivo_nf
              ? `<a class="nf-link" href="/api/notas/${r.arquivo_nf}" target="_blank">📄 Ver NF</a>`
              : '—'}</td>
          </tr>`).join('')}
        </tbody>
      </table>`;
  }
  document.getElementById('modal-hist-compras').classList.add('open');
}

// ── Tabelas das abas ──────────────────────────────────────────────────────────
async function loadMovimentacoes(){
  const rows = await api('/movimentacoes');
  document.getElementById('historico').innerHTML = rows.map(r=>`
    <tr>
      <td>${r.data}</td><td>${r.jogo}</td>
      <td class="tipo-${r.tipo}">${r.tipo}</td>
      <td>${r.quantidade}</td><td>${r.motivo||'—'}</td><td>${r.observacao||'—'}</td>
      <td><button class="btn-excluir-mov" onclick="excluirMovimentacao(${r.id},'${r.jogo}',${r.quantidade},'${r.tipo}')">🗑️</button></td>
    </tr>`).join('');
}

async function excluirMovimentacao(id, jogo, qty, tipo){
  const acao = tipo==='entrada' ? 'remover uma entrada' : 'remover uma saída';
  const efeito = tipo==='entrada' ? `reduzir ${qty} unidade(s) do estoque` : `devolver ${qty} unidade(s) ao estoque`;
  if(!confirm(`Excluir este lançamento de ${jogo}?\n\nIsso vai ${efeito}. Esta ação não pode ser desfeita.`)) return;
  const res = await api('/movimentacoes/'+id, {method:'DELETE'});
  if(res.error){ alert('Erro: '+res.error); return; }
  loadMovimentacoes();
  load();
}

async function loadCompras(){
  const rows = await api('/compras');
  window._comprasRows = rows;
  document.getElementById('tbl-compras').innerHTML = rows.length
    ? rows.map(r=>`
      <tr>
        <td>${fmtData(r.data_compra)}</td>
        <td>${r.jogo_nome}</td>
        <td>${r.fornecedor||'—'}</td>
        <td>${r.pedido_compra||'—'}</td>
        <td>${r.numero_nf||'—'}</td>
        <td>${r.quantidade}</td>
        <td>${fmt(r.valor_pago)}</td>
        <td>${r.forma_pagamento||'—'}</td>
        <td>${r.parcelas&&r.parcelas>1?r.parcelas+'x':'—'}</td>
        <td>${r.arquivo_nf
          ? `<a class="nf-link" href="/api/notas/${r.arquivo_nf}" target="_blank">📄 Ver NF</a>`
          : '—'}</td>
        <td style="white-space:nowrap">
          <button class="btn-edit" onclick="abrirEditCompra(${r.id})" title="Editar">✏️</button>
          <button class="btn-excluir-mov" onclick="excluirCompra(${r.id})" title="Excluir" style="margin-left:.3rem">🗑️</button>
        </td>
      </tr>`).join('')
    : '<tr><td colspan="11" style="color:#718096;text-align:center;padding:1.5rem">Nenhuma compra registrada ainda.</td></tr>';
}

let _compraEditId = null;
function abrirEditCompra(id){
  const rows = window._comprasRows;
  if(!rows) return;
  const r = rows.find(x=>x.id===id);
  if(!r) return;
  _compraEditId = id;
  document.getElementById('ec-data').value       = r.data_compra||'';
  document.getElementById('ec-fornecedor').value  = r.fornecedor||'';
  document.getElementById('ec-pedido').value      = r.pedido_compra||'';
  document.getElementById('ec-nf').value          = r.numero_nf||'';
  document.getElementById('ec-valor').value       = r.valor_pago||'';
  document.getElementById('ec-forma').value       = r.forma_pagamento||'';
  document.getElementById('ec-parcelas').value    = r.parcelas||1;
  document.getElementById('ec-obs').value         = r.observacao||'';
  document.getElementById('ec-nf-atual').textContent =
    r.arquivo_nf ? '📄 NF já anexada (envie novo arquivo para substituir)' : '';
  document.getElementById('ec-nf-file').value = '';
  document.getElementById('modal-edit-compra').classList.add('open');
}

async function salvarEditCompra(){
  const btn = document.getElementById('btn-salvar-ec');
  btn.disabled = true; btn.textContent = 'Salvando…';

  let arquivo_nf = null;
  const fileInput = document.getElementById('ec-nf-file');
  if(fileInput.files && fileInput.files[0]){
    const fd = new FormData();
    fd.append('file', fileInput.files[0]);
    const up = await fetch('/api/upload/nf', {method:'POST', body:fd});
    const upRes = await up.json();
    if(upRes.error){ alert('Erro no upload: '+upRes.error); btn.disabled=false; btn.textContent='Salvar'; return; }
    arquivo_nf = upRes.filename;
  }

  const body = {
    data_compra:    document.getElementById('ec-data').value,
    fornecedor:     document.getElementById('ec-fornecedor').value||null,
    pedido_compra:  document.getElementById('ec-pedido').value||null,
    numero_nf:      document.getElementById('ec-nf').value||null,
    valor_pago:     parseFloat(document.getElementById('ec-valor').value)||null,
    forma_pagamento:document.getElementById('ec-forma').value||null,
    parcelas:       parseInt(document.getElementById('ec-parcelas').value)||1,
    observacao:     document.getElementById('ec-obs').value||null,
  };
  if(arquivo_nf) body.arquivo_nf = arquivo_nf;

  const res = await fetch('/api/compras/'+_compraEditId, {
    method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)
  });
  const data = await res.json();
  btn.disabled=false; btn.textContent='Salvar';
  if(data.error){ alert('Erro: '+data.error); return; }
  document.getElementById('modal-edit-compra').classList.remove('open');
  loadCompras();
}

async function excluirCompra(id){
  if(!confirm('Excluir esta compra? Esta ação não pode ser desfeita.')) return;
  const res = await fetch('/api/compras/'+id, {method:'DELETE'});
  const data = await res.json();
  if(data.error){ alert('Erro: '+data.error); return; }
  loadCompras();
}

function closeModal(id){
  document.getElementById(id).classList.remove('open');
  if(id==='modal-mov'){
    const btn = document.querySelector('#modal-mov .btn-confirm');
    if(btn){ btn.disabled = false; btn.textContent = 'Confirmar'; }
  }
}

// ── Cupons ─────────────────────────────────────────────────────────────────
function openModalCupom(){
  ['cp-codigo','cp-valor','cp-desc','cp-usos'].forEach(id=>document.getElementById(id).value='');
  document.getElementById('cp-tipo').value='pct';
  document.getElementById('modal-cupom').classList.add('open');
}

async function salvarCupom(){
  const codigo = document.getElementById('cp-codigo').value.trim().toUpperCase();
  const valor  = parseFloat(document.getElementById('cp-valor').value);
  if(!codigo){ alert('Informe o código do cupom'); return; }
  if(!valor || valor<=0){ alert('Informe um valor válido'); return; }
  const body = {
    codigo,
    tipo: document.getElementById('cp-tipo').value,
    valor,
    descricao: document.getElementById('cp-desc').value||null,
    usos_maximos: parseInt(document.getElementById('cp-usos').value)||null
  };
  const res = await api('/cupons',{method:'POST',body:JSON.stringify(body)});
  if(res.error){ alert('Erro: '+res.error); return; }
  closeModal('modal-cupom');
  loadCupons(); loadUsosCupons();
}

async function desativarCupom(id){
  if(!confirm('Desativar este cupom?')) return;
  await api('/cupons/'+id+'/desativar',{method:'POST',body:'{}'});
  loadCupons();
}

async function loadCupons(){
  const rows = await api('/cupons');
  const tb = document.getElementById('tbl-cupons');
  if(!rows.length){ tb.innerHTML='<tr><td colspan="8" style="text-align:center;padding:2rem;color:#555">Nenhum cupom cadastrado</td></tr>'; return; }
  tb.innerHTML = rows.map(r=>{
    const tipoLabel = r.tipo==='pct' ? r.valor+'%' : 'R$ '+r.valor.toFixed(2).replace('.',',');
    const usos = r.usos_maximos ? r.usos_realizados+'/'+r.usos_maximos : r.usos_realizados+' (ilimitado)';
    const status = r.ativo
      ? '<span style="color:var(--green);font-weight:700">● Ativo</span>'
      : '<span style="color:#555">● Inativo</span>';
    const btnDes = r.ativo
      ? `<button class="btn-danger" onclick="desativarCupom(${r.id})">Desativar</button>` : '—';
    return `<tr>
      <td><strong style="color:white;font-family:\'Fredoka One\',cursive;letter-spacing:.5px">${r.codigo}</strong></td>
      <td>${r.tipo==='pct'?'%':'R$'}</td>
      <td style="color:var(--green);font-weight:700">${tipoLabel}</td>
      <td style="color:#888">${r.descricao||'—'}</td>
      <td>${usos}</td>
      <td>${status}</td>
      <td style="color:#666;font-size:.8rem">${(r.data_criacao||'').slice(0,10)}</td>
      <td>${btnDes}</td>
    </tr>`;
  }).join('');
}

async function loadUsosCupons(){
  const rows = await api('/cupons/usos');
  const tb = document.getElementById('tbl-cupom-usos');
  if(!rows.length){ tb.innerHTML='<tr><td colspan="6" style="text-align:center;padding:2rem;color:#555">Nenhum uso registrado</td></tr>'; return; }
  tb.innerHTML = rows.map(r=>`<tr>
    <td style="font-size:.82rem;color:#888">${(r.data_uso||'').slice(0,16).replace('T',' ')}</td>
    <td><strong style="color:white;font-family:\'Fredoka One\',cursive">${r.codigo}</strong></td>
    <td>${r.tipo_operacao==='venda'?'💰 Venda':'🔑 Locação'}</td>
    <td>${r.jogo_nome||'—'}</td>
    <td>${r.cliente_nome||'—'}</td>
    <td style="color:var(--red);font-weight:700">- R$ ${(r.valor_desconto||0).toFixed(2).replace('.',',')}</td>
  </tr>`).join('');
}

async function toggleRecebimento(tipo, id, recebido){
  await fetch('/api/recebimento/manual', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({tipo, ref_id:id, recebido})
  });
  loadVendas(); loadLocacoes();
}

// ── Dashboard ──────────────────────────────────────────────────────────────
function renderRanking(containerId, items, valFn, color, suffix=''){
  const el = document.getElementById(containerId);
  if(!items.length){ el.innerHTML='<p style="color:var(--muted);font-size:.82rem">Sem dados ainda.</p>'; return; }
  const max = valFn(items[0]) || 1;
  const medalhas = ['gold','silver','bronze'];
  el.innerHTML = items.map((r,i)=>`
    <div class="rank-item">
      <span class="rank-pos ${medalhas[i]||''}">${i+1}</span>
      <div class="rank-bar-wrap">
        <div class="rank-label" title="${r.nome}">${r.nome}</div>
        <div class="rank-bar"><div class="rank-bar-fill" style="width:${(valFn(r)/max*100).toFixed(1)}%;background:${color}"></div></div>
      </div>
      <span class="rank-val" style="color:${color}">${suffix}${typeof valFn(r)==='number'&&suffix?valFn(r).toFixed(2).replace('.',','):valFn(r)}</span>
    </div>`).join('');
}

async function loadDashboard(){
  const data = await api('/dashboard');
  renderRanking('dash-mais-vendidos', data.mais_vendidos,   r=>r.total,  'var(--green)', '');
  renderRanking('dash-mais-alugados', data.mais_alugados,   r=>r.total,  'var(--purple)', '');
  renderRanking('dash-top-clientes',  data.top_clientes,    r=>r.total,  'var(--orange)', '');
  renderRanking('dash-mais-lucro',    data.mais_lucro,      r=>r.lucro,  'var(--green)', 'R$ ');

  // Ajusta sufixo para unidades
  document.querySelectorAll('#dash-mais-vendidos .rank-val').forEach((el,i)=>{
    el.textContent = data.mais_vendidos[i]?.total + ' un.';
  });
  document.querySelectorAll('#dash-mais-alugados .rank-val').forEach((el,i)=>{
    el.textContent = data.mais_alugados[i]?.total + 'x';
  });
  document.querySelectorAll('#dash-top-clientes .rank-val').forEach((el,i)=>{
    el.textContent = data.top_clientes[i]?.total + 'x';
  });
  document.querySelectorAll('#dash-mais-lucro .rank-val').forEach((el,i)=>{
    const v = data.mais_lucro[i]?.lucro;
    el.textContent = v!=null ? 'R$ '+v.toFixed(2).replace('.',',') : '—';
    el.style.color = v>=0 ? 'var(--green)' : 'var(--red)';
  });
}

// ── Categorias & Badges ───────────────────────────────────────────────────────
// ── Relatórios ─────────────────────────────────────────────────────────────
function inicioSemana(){
  const d = new Date();
  d.setDate(d.getDate() - d.getDay() + (d.getDay()===0?-6:1)); // segunda-feira
  return d.toISOString().slice(0,10);
}
function fimSemana(){
  const d = new Date();
  d.setDate(d.getDate() - d.getDay() + (d.getDay()===0?0:7)); // domingo
  return d.toISOString().slice(0,10);
}

function initRelatorio(){
  if(!document.getElementById('rel-de').value){
    document.getElementById('rel-de').value  = inicioSemana();
    document.getElementById('rel-ate').value = fimSemana();
  }
  loadRelatorio();
  loadFavoritos();
}

async function loadFavoritos(){
  const rows = await api('/relatorio/favoritos');
  const tbl  = document.getElementById('tbl-favoritos');
  const vazio = document.getElementById('fav-vazio');
  if(!rows || !rows.length){
    tbl.style.display='none'; vazio.style.display='';
    return;
  }
  vazio.style.display='none'; tbl.style.display='';
  const tbody = document.getElementById('body-favoritos');
  tbody.innerHTML = rows.map(r => `
    <tr style="border-bottom:1px solid var(--border)">
      <td style="padding:.6rem;font-weight:700">${r.nome}</td>
      <td style="padding:.6rem">${r.telefone||'—'}</td>
      <td style="padding:.6rem;text-align:center">
        <span style="background:var(--purple);color:#fff;border-radius:20px;padding:2px 10px;font-weight:700">${r.total_favoritos}</span>
      </td>
      <td style="padding:.6rem;font-size:.85rem;color:var(--muted);max-width:300px">${r.jogos_favoritos||'—'}</td>
      <td style="padding:.6rem;font-size:.82rem;color:var(--muted)">${(r.data_cadastro||'').slice(0,10)}</td>
    </tr>
  `).join('');
}

async function loadRelatorio(){
  const de  = document.getElementById('rel-de').value;
  const ate = document.getElementById('rel-ate').value;
  if(!de || !ate) return;

  const rows = await api(`/relatorio/semanal?de=${de}&ate=${ate}`);

  if(!rows.length){
    document.getElementById('rel-resumo').style.display='none';
    document.getElementById('tbl-relatorio').style.display='none';
    document.getElementById('rel-vazio').style.display='';
    return;
  }
  document.getElementById('rel-vazio').style.display='none';
  document.getElementById('rel-resumo').style.display='';
  document.getElementById('tbl-relatorio').style.display='';

  let totVendas=0, totLocacoes=0, totLucro=0, totVendaValor=0;
  const tbody = document.getElementById('body-relatorio');
  tbody.innerHTML = rows.map(r=>{
    const isVenda = r.tipo==='venda';
    const valor   = r.valor || 0;
    const custo   = r.custo != null ? r.custo : null;
    const lucro   = custo != null ? valor - custo : null;
    const margem  = (lucro != null && valor > 0) ? (lucro/valor*100) : null;

    if(isVenda){ totVendas += valor; if(lucro!=null) totLucro += lucro; totVendaValor += valor; }
    else        { totLocacoes += valor; }

    const custoHtml   = custo!=null ? `<span style="color:#fc8181">R$ ${custo.toFixed(2).replace('.',',')}</span>` : '<span style="color:#555">—</span>';
    const lucroHtml   = lucro!=null ? `<span style="color:${lucro>=0?'var(--green)':'var(--red)'};font-weight:700">${lucro>=0?'+':''}R$ ${Math.abs(lucro).toFixed(2).replace('.',',')}</span>` : '<span style="color:#555">—</span>';
    const margemHtml  = margem!=null ? `<span style="color:${margem>=0?'var(--green)':'var(--red)'};font-weight:700">${margem.toFixed(1)}%</span>` : '<span style="color:#555">—</span>';
    const tipoBadge   = isVenda
      ? `<span style="background:rgba(23,198,41,.15);color:var(--green);border-radius:999px;padding:2px 8px;font-size:.72rem;font-weight:700">💰 Venda</span>`
      : `<span style="background:rgba(123,32,225,.15);color:var(--purple);border-radius:999px;padding:2px 8px;font-size:.72rem;font-weight:700">🔑 Locação</span>`;

    return `<tr>
      <td style="font-size:.82rem">${fmtData(r.data)}</td>
      <td>${tipoBadge}</td>
      <td><strong style="color:white">${r.item}</strong></td>
      <td style="font-size:.82rem">${r.cliente||'—'}</td>
      <td style="font-weight:700;color:${isVenda?'var(--green)':'var(--purple)'}">R$ ${valor.toFixed(2).replace('.',',')}</td>
      <td>${custoHtml}</td>
      <td>${lucroHtml}</td>
      <td>${margemHtml}</td>
    </tr>`;
  }).join('');

  const margemMedia = totVendaValor>0 ? (totLucro/totVendaValor*100) : 0;
  document.getElementById('rel-tot-vendas').textContent  = `R$ ${totVendas.toFixed(2).replace('.',',')}`;
  document.getElementById('rel-tot-locacoes').textContent= `R$ ${totLocacoes.toFixed(2).replace('.',',')}`;
  document.getElementById('rel-lucro').textContent       = `R$ ${totLucro.toFixed(2).replace('.',',')}`;
  document.getElementById('rel-margem').textContent      = `${margemMedia.toFixed(1)}%`;
}

// ── Conciliação ────────────────────────────────────────────────────────────
let concExtratId = null;

(function(){
  const box = document.getElementById('conc-upload-box');
  if(!box) return;
  box.addEventListener('dragover', e=>{ e.preventDefault(); box.classList.add('dragover'); });
  box.addEventListener('dragleave', ()=>box.classList.remove('dragover'));
  box.addEventListener('drop', e=>{
    e.preventDefault(); box.classList.remove('dragover');
    const f = e.dataTransfer.files[0];
    if(f) processarArquivoExtrato(f);
  });
})();

function uploadExtrato(input){
  const f = input.files[0];
  if(f) processarArquivoExtrato(f);
  input.value='';
}

async function processarArquivoExtrato(file){
  const fd = new FormData();
  fd.append('arquivo', file);
  const resp = await fetch('/api/extrato/upload', {method:'POST', body:fd});
  const data = await resp.json();
  if(!resp.ok){ alert(data.erro||'Erro ao processar arquivo'); return; }
  concExtratId = data.extrato_id;
  document.getElementById('conc-total-val').textContent = data.total;
  document.getElementById('conc-conc-val').textContent = '—';
  document.getElementById('conc-sem-val').textContent = '—';
  document.getElementById('btn-conciliar').textContent = '⚡ Conciliar Agora';
  renderLancamentos(data.lancamentos, false);
  document.getElementById('conc-resultado').style.display = '';
  loadExtratos();
}

function renderLancamentos(lancamentos, conciliado){
  const tb = document.getElementById('tbl-lancamentos');
  if(!lancamentos.length){
    tb.innerHTML='<tr><td colspan="5" style="text-align:center;padding:2rem;color:#555">Nenhum lançamento</td></tr>';
    return;
  }
  tb.innerHTML = lancamentos.map(l=>{
    const val = (l.valor||0).toFixed(2).replace('.',',');
    const cor = l.valor>0?'var(--green)':'var(--red)';
    let status = '';
    let vinc = '—';
    if(conciliado){
      if(l.conciliado){
        status = '<span class="badge-conciliado">✅ Conciliado</span>';
        vinc = l.venda_jogo ? `💰 ${l.venda_jogo}` : l.loc_jogo ? `🔑 ${l.loc_jogo}` : '—';
      } else {
        status = '<span class="badge-sem-match">⚠️ Sem match</span>';
      }
    } else {
      status = l.valor>0 ? '<span class="badge-recebido" style="background:rgba(123,32,225,.2);color:#a56aed;border-color:rgba(123,32,225,.4)">💰 Crédito</span>'
                         : '<span class="badge-pendente" style="color:#888">Débito</span>';
    }
    return `<tr>
      <td style="font-size:.82rem">${(l.data||'').slice(0,10).split('-').reverse().join('/')}</td>
      <td style="font-size:.8rem;max-width:200px">${l.descricao||'—'}</td>
      <td style="color:${cor};font-weight:700">R$ ${l.valor<0?'-':''}${Math.abs(l.valor||0).toFixed(2).replace('.',',')}</td>
      <td>${status}</td>
      <td style="font-size:.8rem">${vinc}</td>
    </tr>`;
  }).join('');
}

async function conciliar(){
  if(!concExtratId){ alert('Nenhum extrato carregado'); return; }
  const btn = document.getElementById('btn-conciliar');
  btn.textContent = 'Conciliando…'; btn.disabled = true;
  const resp = await fetch('/api/extrato/conciliar/'+concExtratId, {method:'POST'});
  const data = await resp.json();
  btn.textContent = '✅ Conciliado'; btn.disabled = false;
  document.getElementById('conc-total-val').textContent = data.total;
  document.getElementById('conc-conc-val').textContent = data.conciliados;
  document.getElementById('conc-sem-val').textContent = data.sem_match;
  const lanc = await api('/extrato/lancamentos/'+concExtratId);
  renderLancamentos(lanc, true);
  // Recarregar histórico de vendas e locações se estiver visível
  if(document.getElementById('page-loja') && document.getElementById('page-loja').classList.contains('active')){
    loadVendas(); loadLocacoes();
  }
}

async function verExtrato(id){
  concExtratId = id;
  const lanc = await api('/extrato/lancamentos/'+id);
  document.getElementById('conc-total-val').textContent = lanc.filter(l=>l.valor>0).length;
  document.getElementById('conc-conc-val').textContent = lanc.filter(l=>l.conciliado).length;
  document.getElementById('conc-sem-val').textContent = lanc.filter(l=>l.valor>0&&!l.conciliado).length;
  document.getElementById('btn-conciliar').textContent = '⚡ Reconciliar';
  renderLancamentos(lanc, true);
  document.getElementById('conc-resultado').style.display = '';
}

async function loadExtratos(){
  const rows = await api('/extrato/lista');
  const tb = document.getElementById('tbl-extratos');
  if(!rows.length){ tb.innerHTML='<tr><td colspan="4" style="text-align:center;padding:1.5rem;color:#555">Nenhum extrato enviado</td></tr>'; return; }
  tb.innerHTML = rows.map(r=>`<tr>
    <td>${r.nome_arquivo}</td>
    <td style="font-size:.82rem;color:#888">${(r.data_upload||'').slice(0,16)}</td>
    <td style="text-align:center">${r.total_lancamentos}</td>
    <td><button class="btn-devolver" onclick="verExtrato(${r.id})">Ver lançamentos</button></td>
  </tr>`).join('');
}

console.log('[DEBUG] Script carregado OK — chamando load()');
load();



</script>
</body>
</html>"""


LANDING_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Jogoteka — Locação e Venda de Jogos de Tabuleiro</title>
<meta name="description" content="Jogoteka: alugue e compre jogos de tabuleiro em Floripa e Porto Alegre. Catálogo online, atendimento todos os dias das 10h às 22h.">
<link href="https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800;900&display=swap" rel="stylesheet">
<style>
  :root{
    --red:#F10A0A;--green:#17C629;--purple:#7B20E1;--orange:#ED940E;
    --bg:#FFFFFF;--bg2:#F7F8FA;--text:#1a1a2e;--text2:#555;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Nunito',sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
  a{color:inherit;text-decoration:none}

  /* NAV */
  nav{display:flex;justify-content:space-between;align-items:center;padding:14px 40px;position:sticky;top:0;z-index:100;background:#fff;box-shadow:0 2px 12px rgba(0,0,0,.08)}
  .nav-logo img{height:52px;width:auto;object-fit:contain}
  /* HAMBURGER */
  .hamburger{background:none;border:none;cursor:pointer;padding:8px;display:flex;flex-direction:column;gap:5px;justify-content:center}
  .hamburger span{display:block;width:26px;height:3px;background:var(--text);border-radius:3px;transition:.3s}
  .hamburger.open span:nth-child(1){transform:translateY(8px) rotate(45deg)}
  .hamburger.open span:nth-child(2){opacity:0}
  .hamburger.open span:nth-child(3){transform:translateY(-8px) rotate(-45deg)}
  .nav-menu{
    display:none;position:absolute;top:100%;right:0;
    background:#fff;border-radius:0 0 16px 16px;
    box-shadow:0 8px 32px rgba(0,0,0,.12);
    min-width:220px;padding:8px 0;z-index:200;
  }
  .nav-menu.open{display:block}
  .nav-menu a{
    display:block;padding:14px 24px;font-size:.95rem;font-weight:700;
    color:var(--text);transition:background .15s;
  }
  .nav-menu a:hover{background:#f5f0ff;color:var(--purple)}
  .nav-menu .menu-divider{height:1px;background:#eee;margin:6px 0}
  .nav-menu .menu-restrito{color:var(--text2);font-weight:600;font-size:.88rem}

  /* HERO */
  .hero{text-align:center;padding:80px 24px 64px;background:linear-gradient(180deg,#fff 0%,#f7f0ff 100%);position:relative;overflow:hidden}
  .hero::before{content:'';position:absolute;top:-80px;left:-80px;width:320px;height:320px;background:radial-gradient(circle,rgba(237,148,14,.12) 0%,transparent 70%);pointer-events:none}
  .hero::after{content:'';position:absolute;bottom:-60px;right:-60px;width:280px;height:280px;background:radial-gradient(circle,rgba(123,32,225,.1) 0%,transparent 70%);pointer-events:none}
  .hero-logo{margin-bottom:32px}
  .hero-logo img{height:80px;width:auto}
  .hero h1{font-size:clamp(2rem,5vw,3.2rem);font-weight:900;line-height:1.2;margin-bottom:18px;color:var(--text)}
  .hero h1 .red{color:var(--red)}.hero h1 .orange{color:var(--orange)}.hero h1 .green{color:var(--green)}.hero h1 .purple{color:var(--purple)}
  .hero p{font-size:1.1rem;color:var(--text2);max-width:700px;margin:0 auto 40px;line-height:1.7;font-weight:600}
  .hero-ctas{display:flex;gap:14px;justify-content:center;flex-wrap:wrap}
  .cta-primary{background:var(--purple);color:#fff;padding:16px 36px;border-radius:30px;font-size:1.05rem;font-weight:800;transition:.2s;display:inline-flex;align-items:center;gap:8px;letter-spacing:.3px}
  .cta-primary:hover{background:#6a1bc7;transform:translateY(-2px);box-shadow:0 8px 24px rgba(123,32,225,.35)}
  .cta-wpp{background:#25d366;color:#fff;padding:16px 28px;border-radius:30px;font-size:1rem;font-weight:800;transition:.2s;display:inline-flex;align-items:center;gap:8px}
  .cta-wpp:hover{background:#20b558;transform:translateY(-2px);box-shadow:0 6px 20px rgba(37,211,102,.4)}

  /* FAIXAS COLORIDAS */
  .color-bar{display:flex;height:8px}
  .color-bar span{flex:1}
  .cb-red{background:var(--red)}.cb-orange{background:var(--orange)}.cb-green{background:var(--green)}.cb-purple{background:var(--purple)}

  /* GALERIA */
  .galeria-section{background:var(--bg2)}
  /* carrossel galeria */
  .gal-carousel{position:relative;max-width:900px;margin:0 auto;overflow:hidden;border-radius:20px}
  .gal-track{display:flex;transition:transform .5s cubic-bezier(.4,0,.2,1)}
  .gal-slide{min-width:100%;display:flex;gap:12px;padding:0 2px}
  .gal-slide.slide-foto .gal-cell{width:calc(50% - 6px);flex-shrink:0}
  .gal-slide.slide-video .gal-cell{width:100%}
  .gal-cell img,.gal-cell video{width:100%;height:340px;object-fit:cover;border-radius:16px;display:block}
  .gal-dots{display:flex;justify-content:center;gap:8px;margin-top:18px}
  .gal-dot{width:8px;height:8px;border-radius:50%;background:#ccc;border:none;cursor:pointer;padding:0;transition:.3s}
  .gal-dot.ativo{background:var(--purple);transform:scale(1.3)}
  .gal-arrows{position:absolute;top:50%;transform:translateY(-50%);width:100%;display:flex;justify-content:space-between;pointer-events:none;padding:0 10px}
  .gal-arrow{pointer-events:all;background:rgba(0,0,0,.45);border:2px solid rgba(255,255,255,.6);color:#fff;width:2.6rem;height:2.6rem;border-radius:50%;font-size:1.3rem;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:.2s;box-shadow:0 2px 8px rgba(0,0,0,.3)}
  .gal-arrow:hover{background:rgba(0,0,0,.7)}
  @media(max-width:600px){.gal-cell img,.gal-cell video{height:220px}}

  @media(max-width:640px){
    .galeria-grid{grid-template-columns:1fr;grid-template-rows:auto}
    .galeria-grid .foto-dest{grid-column:1;grid-row:auto}
    .galeria-grid .foto-dest .galeria-img{height:260px}
    .video-wrap{grid-column:1;grid-row:auto}
    .video-wrap video{height:220px}
    .galeria-grid .foto-sm .galeria-img{height:200px}
  }

  /* SECTIONS */
  section{padding:72px 24px}
  .section-inner{max-width:1020px;margin:0 auto}
  .section-title{text-align:center;margin-bottom:48px}
  .section-title h2{font-size:2rem;font-weight:900;margin-bottom:8px;color:var(--text)}
  .section-title p{color:var(--text2);font-size:1rem;font-weight:600}

  /* DIFERENCIAIS */
  .diferenciais{background:var(--bg2)}
  .diff-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:24px}
  .diff-card{background:#fff;border-radius:20px;padding:32px 24px;text-align:center;box-shadow:0 2px 16px rgba(0,0,0,.06);transition:.2s}
  .diff-card:hover{transform:translateY(-4px);box-shadow:0 8px 28px rgba(0,0,0,.1)}
  .diff-icon{font-size:2.8rem;margin-bottom:14px;display:block}
  .diff-card h3{font-size:1.05rem;font-weight:800;margin-bottom:6px;color:var(--text)}
  .diff-card p{font-size:.88rem;color:var(--text2);line-height:1.6;font-weight:600}
  .diff-card.red{border-top:4px solid var(--red)}
  .diff-card.orange{border-top:4px solid var(--orange)}
  .diff-card.green{border-top:4px solid var(--green)}
  .diff-card.purple{border-top:4px solid var(--purple)}

  /* LOJAS */
  .lojas-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:28px}
  .loja-card{background:#fff;border-radius:20px;padding:36px;box-shadow:0 2px 20px rgba(0,0,0,.08);transition:.2s;border-top:5px solid}
  .loja-card.floripa{border-color:var(--purple)}
  .loja-card.poa{border-color:var(--green)}
  .loja-card:hover{transform:translateY(-4px);box-shadow:0 10px 32px rgba(0,0,0,.12)}
  .loja-cidade{font-size:1.5rem;font-weight:900;margin-bottom:4px;color:var(--text)}
  .loja-estado-badge{display:inline-block;padding:3px 12px;border-radius:99px;font-size:.75rem;font-weight:800;letter-spacing:1px;text-transform:uppercase;margin-bottom:24px;color:#fff}
  .floripa .loja-estado-badge{background:var(--purple)}
  .poa .loja-estado-badge{background:var(--green)}
  .loja-info{display:flex;flex-direction:column;gap:14px;margin-bottom:28px}
  .loja-info-item{display:flex;gap:12px;align-items:flex-start;color:var(--text2);font-size:.95rem;line-height:1.5;font-weight:600}
  .loja-icon{font-size:1.2rem;flex-shrink:0}
  .btn-wpp{display:flex;align-items:center;justify-content:center;gap:8px;background:#25d366;color:#fff;padding:14px 20px;border-radius:30px;font-weight:800;font-size:.95rem;transition:.2s;width:100%}
  .btn-wpp:hover{background:#20b558;transform:translateY(-1px);box-shadow:0 4px 16px rgba(37,211,102,.4)}

  /* HORARIO */
  .horario-section{background:linear-gradient(135deg,var(--purple) 0%,#9b3de8 100%);color:#fff}
  .horario-section .section-title h2{color:#fff}
  .horario-section .section-title p{color:rgba(255,255,255,.8)}
  .horario-card{background:rgba(255,255,255,.15);backdrop-filter:blur(10px);border:1.5px solid rgba(255,255,255,.3);border-radius:24px;padding:48px;text-align:center;max-width:500px;margin:0 auto}
  .horario-horas{font-size:3rem;font-weight:900;margin:12px 0 8px;color:#FFE566}
  .horario-dias{font-size:1.05rem;color:rgba(255,255,255,.9);font-weight:700}
  .horario-label{font-size:.85rem;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:rgba(255,255,255,.7);margin-bottom:4px}

  /* DEPOIMENTOS */
  .depoi-section{background:#fff}
  .depoi-header{display:flex;align-items:center;justify-content:center;gap:16px;margin-bottom:48px;flex-wrap:wrap}
  .depoi-nota{text-align:center}
  .depoi-nota .nota-num{font-size:3.5rem;font-weight:900;color:var(--text);line-height:1}
  .depoi-nota .estrelas{color:#FBBC04;font-size:1.6rem;letter-spacing:2px}
  .depoi-nota .nota-sub{font-size:.85rem;color:var(--text2);font-weight:600;margin-top:4px}
  .depoi-google{display:flex;align-items:center;gap:6px;padding:8px 16px;border:1.5px solid #e0e0e0;border-radius:10px;font-size:.85rem;color:var(--text2);font-weight:700}
  .depoi-google svg{width:20px;height:20px;flex-shrink:0}
  /* carrossel */
  .depoi-carousel{position:relative;overflow:hidden;max-width:680px;margin:0 auto}
  .depoi-track{display:flex;transition:transform .5s cubic-bezier(.4,0,.2,1)}
  .depoi-slide{min-width:100%;padding:0 4px}
  .depoi-card{background:#fff;border:1.5px solid #f0f0f0;border-radius:16px;padding:28px;box-shadow:0 2px 12px rgba(0,0,0,.06)}
  .depoi-dots{display:flex;justify-content:center;gap:8px;margin-top:24px}
  .depoi-dot{width:8px;height:8px;border-radius:50%;background:#ddd;border:none;cursor:pointer;padding:0;transition:.3s}
  .depoi-dot.ativo{background:var(--purple);transform:scale(1.3)}
  .depoi-arrows{display:flex;justify-content:center;gap:12px;margin-top:16px}
  .depoi-arrow{background:none;border:1.5px solid #ddd;border-radius:50%;width:36px;height:36px;font-size:1rem;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:.2s;color:var(--text2)}
  .depoi-arrow:hover{border-color:var(--purple);color:var(--purple)}
  .depoi-top{display:flex;align-items:center;gap:12px;margin-bottom:14px}
  .depoi-avatar{width:44px;height:44px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:1.1rem;font-weight:900;color:#fff;flex-shrink:0}
  .av-red{background:var(--red)}.av-orange{background:var(--orange)}.av-green{background:var(--green)}.av-purple{background:var(--purple)}.av-blue{background:#4285F4}
  .depoi-nome{font-weight:800;font-size:.95rem;color:var(--text)}
  .depoi-tempo{font-size:.78rem;color:var(--text2);font-weight:600}
  .depoi-stars{color:#FBBC04;font-size:1rem;margin-bottom:10px;letter-spacing:1px}
  .depoi-texto{font-size:.9rem;color:#444;line-height:1.65;font-weight:600}

  /* CTA FINAL */
  .cta-section{background:var(--bg2);text-align:center}
  .cta-section h2{font-size:2rem;font-weight:900;margin-bottom:14px;color:var(--text)}
  .cta-section p{color:var(--text2);margin-bottom:36px;font-size:1.05rem;font-weight:600}

  /* FOOTER */
  footer{background:var(--text);color:rgba(255,255,255,.5);padding:32px 24px;text-align:center;font-size:.85rem;font-weight:600}
  footer a{color:rgba(255,255,255,.6);transition:.2s}
  footer a:hover{color:#fff}
  .footer-logo{margin-bottom:16px}
  .footer-logo img{height:44px;width:auto;object-fit:contain}

  @media(max-width:640px){
    nav{padding:12px 20px}
    .nav-logo img{height:40px}
    .hero{padding:56px 20px 48px}
    .hero-logo img{height:60px}
    .hero h1{font-size:1.8rem}
    .cta-primary,.cta-wpp{padding:14px 22px;font-size:.95rem}
    .horario-card{padding:32px 24px}
    .horario-horas{font-size:2.4rem}
    .lojas-grid{grid-template-columns:1fr}
    .diff-grid{grid-template-columns:1fr 1fr}
  }
</style>
</head>
<body>

<nav>
  <div class="nav-logo">
    <a href="/">
      <img src="/api/logo" alt="Jogoteka"
           onerror="this.outerHTML='<span style=&quot;font-family:Nunito,sans-serif;font-size:1.6rem;font-weight:900;color:#F10A0A&quot;>JOGO<span style=&quot;color:#7B20E1&quot;>TEKA</span></span>'">
    </a>
  </div>
  <div style="position:relative">
    <button class="hamburger" id="hambBtn" onclick="toggleMenu()" aria-label="Menu">
      <span></span><span></span><span></span>
    </button>
    <div class="nav-menu" id="navMenu">
      <a href="/catalogo">Ver Catálogo</a>
      <div class="menu-divider"></div>
      <a href="/login" class="menu-restrito">🔒 Área restrita</a>
    </div>
  </div>
</nav>
<script>
  function toggleMenu(){
    document.getElementById('hambBtn').classList.toggle('open');
    document.getElementById('navMenu').classList.toggle('open');
  }
  document.addEventListener('click',function(e){
    if(!e.target.closest('#hambBtn') && !e.target.closest('#navMenu')){
      document.getElementById('hambBtn').classList.remove('open');
      document.getElementById('navMenu').classList.remove('open');
    }
  });
</script>

<div class="color-bar">
  <span class="cb-red"></span><span class="cb-orange"></span><span class="cb-green"></span><span class="cb-purple"></span>
</div>

<section class="hero">
  <h1>Diversão offline para todos<br>em cada <span class="red">p</span><span class="orange">a</span><span class="green">r</span><span class="purple">t</span><span class="red">i</span><span class="orange">d</span><span class="green">a</span><span class="purple">!</span></h1>
  <p>Alugue ou compre jogos de tabuleiro em Floripa ou Porto Alegre.</p>
  <div class="hero-ctas">
    <a class="cta-primary" href="/catalogo">Ver Catálogo Completo</a>
  </div>
</section>

<div class="color-bar">
  <span class="cb-purple"></span><span class="cb-green"></span><span class="cb-orange"></span><span class="cb-red"></span>
</div>

<section class="galeria-section">
  <div class="section-inner">
    <div class="section-title">
      <h2>Conheça a Jogoteka</h2>
      <p>Nosso acervo, escolhido com muito carinho para momentos de muita diversão.</p>
    </div>
    <div id="galeriaContainer">
      <!-- carregado dinamicamente pelo JS abaixo -->
    </div>
  </div>
</section>

<section class="diferenciais">
  <div class="section-inner">
    <div class="section-title">
      <h2>Por que escolher a Jogoteka?</h2>
      <p>Mais do que jogos — uma experiência completa</p>
    </div>
    <div class="diff-grid">
      <div class="diff-card red">
        <span class="diff-icon">🎲</span>
        <h3>Catálogo Completo</h3>
        <p>Centenas de títulos para todos os gostos e idades</p>
      </div>
      <div class="diff-card orange">
        <span class="diff-icon">⚡</span>
        <h3>Fácil e Rápido</h3>
        <p>Escolha online, receba em casa ou retire conosco</p>
      </div>
      <div class="diff-card green">
        <span class="diff-icon">📅</span>
        <h3>Todos os Dias</h3>
        <p>Aberto de domingo a domingo das 10h às 22h</p>
      </div>
      <div class="diff-card purple">
        <span class="diff-icon">📍</span>
        <h3>Duas Lojas</h3>
        <p>Floripa e Porto Alegre</p>
      </div>
    </div>
  </div>
</section>

<section>
  <div class="section-inner">
    <div class="section-title">
      <h2>Nossas Lojas</h2>
      <p></p>
    </div>
    <div class="lojas-grid">

      <div class="loja-card floripa">
        <div class="loja-cidade">Floripa</div>
        <span class="loja-estado-badge">SC</span>
        <div class="loja-info">
          <div class="loja-info-item">
            <span class="loja-icon">🕐</span>
            <span>10h às 22h — Todos os dias</span>
          </div>
          <div class="loja-info-item">
            <span class="loja-icon">📞</span>
            <span>(48) 98807-2721</span>
          </div>
        </div>
        <a class="btn-wpp" href="https://wa.me/5548988072721" target="_blank">
          💬 WhatsApp Floripa
        </a>
      </div>

      <div class="loja-card poa">
        <div class="loja-cidade">Porto Alegre</div>
        <span class="loja-estado-badge">RS</span>
        <div class="loja-info">
          <div class="loja-info-item">
            <span class="loja-icon">🕐</span>
            <span>10h às 22h — Todos os dias</span>
          </div>
          <div class="loja-info-item">
            <span class="loja-icon">📞</span>
            <span>(51) 98144-7898</span>
          </div>
        </div>
        <a class="btn-wpp" href="https://wa.me/5551981447898" target="_blank">
          💬 WhatsApp Porto Alegre
        </a>
      </div>

    </div>
  </div>
</section>

<section class="horario-section">
  <div class="section-inner">
    <div class="section-title">
      <h2>Horário de Funcionamento</h2>
      <p>Estamos aqui quando você precisar</p>
    </div>
    <div class="horario-card">
      <div class="horario-label">Todos os dias</div>
      <div class="horario-horas">10h às 22h</div>
    </div>
  </div>
</section>

<section class="depoi-section">
  <div class="section-inner">
    <div class="section-title">
      <h2>O que nossos clientes dizem depois de experimentar a Jogoteka.</h2>
    </div>
    <div class="depoi-header">
      <div class="depoi-nota">
        <div class="nota-num">5,0</div>
        <div class="estrelas">★★★★★</div>
        <div class="nota-sub">Avaliação no Google</div>
      </div>
      <div class="depoi-google">
        <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
          <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" fill="#4285F4"/>
          <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/>
          <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05"/>
          <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/>
        </svg>
        Google Reviews
      </div>
    </div>
    <div class="depoi-carousel" id="depoiCarousel">
      <div class="depoi-track" id="depoiTrack">

        <div class="depoi-slide">
          <div class="depoi-card">
            <div class="depoi-top">
              <div class="depoi-avatar av-orange">A</div>
              <div>
                <div class="depoi-nome">Apoena Carla dos Santos</div>
                <div class="depoi-tempo">4 meses atrás</div>
              </div>
            </div>
            <div class="depoi-stars">★★★★★</div>
            <div class="depoi-texto">Locamos o jogo Azul por 3 dias. Foi uma experiência muito bacana, deu para se divertir bastante. O processo de locação também foi muito simples e ágil. Recomendo! 🎯😊</div>
          </div>
        </div>

        <div class="depoi-slide">
          <div class="depoi-card">
            <div class="depoi-top">
              <div class="depoi-avatar av-blue">L</div>
              <div>
                <div class="depoi-nome">Leonardo Freitas</div>
                <div class="depoi-tempo">6 meses atrás</div>
              </div>
            </div>
            <div class="depoi-stars">★★★★★</div>
            <div class="depoi-texto">Aluguei o jogo Monopoly na semana passada e foi uma experiência excelente! O quiosque tem uma variedade incrível de jogos para compra e aluguel, tudo muito organizado e de ótima qualidade. O jogo veio completinho e garantiu uma noite super divertida! Super recomendo! 🎯✨</div>
          </div>
        </div>

        <div class="depoi-slide">
          <div class="depoi-card">
            <div class="depoi-top">
              <div class="depoi-avatar av-purple">J</div>
              <div>
                <div class="depoi-nome">Jéssica Dihl</div>
                <div class="depoi-tempo">3 meses atrás</div>
              </div>
            </div>
            <div class="depoi-stars">★★★★★</div>
            <div class="depoi-texto">Jogos divertidos e variados. Vale muito a pena. Eu e minha filha adoramos e vamos locar mais jogos com certeza.</div>
          </div>
        </div>

        <div class="depoi-slide">
          <div class="depoi-card">
            <div class="depoi-top">
              <div class="depoi-avatar av-green">B</div>
              <div>
                <div class="depoi-nome">Bruna Faria Borges</div>
                <div class="depoi-tempo">3 meses atrás</div>
              </div>
            </div>
            <div class="depoi-stars">★★★★★</div>
            <div class="depoi-texto">Jogos para todas as idades. Para brincar no passeio ou alugar por alguns dias e levar para casa. Atendimento ótimo e curadoria dos brinquedos está de parabéns. Os meus filhos amam. Recomendamos muito!!</div>
          </div>
        </div>

        <div class="depoi-slide">
          <div class="depoi-card">
            <div class="depoi-top">
              <div class="depoi-avatar av-red">S</div>
              <div>
                <div class="depoi-nome">Silvana Palma</div>
                <div class="depoi-tempo">5 meses atrás</div>
              </div>
            </div>
            <div class="depoi-stars">★★★★★</div>
            <div class="depoi-texto">Muito interessante, gostamos muito de alugar o jogo, muito bem organizado e também da compra realizada. Gostamos da experiência de primeiro experimentar o jogo para saber se realmente gostaríamos de adquirir. Muito legal mesmo. O atendimento excelente! Voltaremos!</div>
          </div>
        </div>

      </div>
      <div class="depoi-dots" id="depoiDots"></div>
      <div class="depoi-arrows">
        <button class="depoi-arrow" onclick="depoiMover(-1)">&#8592;</button>
        <button class="depoi-arrow" onclick="depoiMover(1)">&#8594;</button>
      </div>
    </div>
    <script>
    (function(){
      const track = document.getElementById('depoiTrack');
      const dotsEl = document.getElementById('depoiDots');
      const total = track.children.length;
      let atual = 0, timer, pausado = false;
      // cria dots
      for(let i=0;i<total;i++){
        const d = document.createElement('button');
        d.className = 'depoi-dot' + (i===0?' ativo':'');
        d.addEventListener('click',()=>{ irPara(i); resetTimer(); });
        dotsEl.appendChild(d);
      }
      function irPara(n){
        atual = (n + total) % total;
        track.style.transform = `translateX(-${atual*100}%)`;
        dotsEl.querySelectorAll('.depoi-dot').forEach((d,i)=>d.classList.toggle('ativo',i===atual));
      }
      function resetTimer(){
        clearInterval(timer);
        if(!pausado) timer = setInterval(()=>irPara(atual+1), 5000);
      }
      window.depoiMover = function(dir){ irPara(atual+dir); resetTimer(); };
      // pausa ao passar o mouse
      const el = document.getElementById('depoiCarousel');
      el.addEventListener('mouseenter',()=>{ pausado=true; clearInterval(timer); });
      el.addEventListener('mouseleave',()=>{ pausado=false; resetTimer(); });
      // swipe no celular
      let tx=0;
      el.addEventListener('touchstart',e=>{ tx=e.touches[0].clientX; },{passive:true});
      el.addEventListener('touchend',e=>{
        const dx=e.changedTouches[0].clientX-tx;
        if(Math.abs(dx)>40){ irPara(atual+(dx<0?1:-1)); resetTimer(); }
      },{passive:true});
      resetTimer();
    })();
    </script>
  </div>
</section>

<section class="cta-section">
  <div class="section-inner">
    <h2>Pronto para jogar?</h2>
    <p>Escolha seu jogo favorito</p>
    <a class="cta-primary" href="/catalogo">Acessar Catálogo</a>
  </div>
</section>

<div class="color-bar">
  <span class="cb-red"></span><span class="cb-orange"></span><span class="cb-green"></span><span class="cb-purple"></span>
</div>

<footer>
  <div class="footer-logo">
    <img src="/api/logo" alt="Jogoteka"
         onerror="this.style.display='none'">
  </div>
  <p>© 2025 Jogoteka — Todos os direitos reservados &nbsp;·&nbsp;
     <a href="/catalogo">Catálogo</a> &nbsp;·&nbsp;
     <a href="https://wa.me/5548988072721" target="_blank">WhatsApp Floripa</a> &nbsp;·&nbsp;
     <a href="https://wa.me/5551981447898" target="_blank">WhatsApp Porto Alegre</a>
  </p>
</footer>

<script>
// Carrega depoimentos dinamicamente
fetch('/api/landing/publico/depoimentos')
  .then(r=>r.json())
  .then(lista=>{
    if(!lista||!lista.length) return;
    const COR={'av-orange':'#ED940E','av-red':'#F10A0A','av-green':'#17C629','av-purple':'#7B20E1','av-blue':'#4285F4'};
    const grid = document.getElementById('depGrid');
    if(!grid) return;
    grid.innerHTML = lista.map(d=>`
      <div class="depoi-card">
        <div class="depoi-top">
          <div class="depoi-avatar" style="background:${COR[d.cor]||'#ED940E'}">${d.nome[0].toUpperCase()}</div>
          <div><div class="depoi-nome">${d.nome}</div><div class="depoi-tempo">${d.tempo}</div></div>
        </div>
        <div class="depoi-stars">★★★★★</div>
        <div class="depoi-texto">${d.texto}</div>
      </div>`).join('');
  }).catch(()=>{});

// Carrega galeria — carrossel (2 fotos por slide, 1 vídeo por slide)
fetch('/api/landing/publico/midia')
  .then(r=>r.json())
  .then(lista=>{
    const cont = document.getElementById('galeriaContainer');
    if(!cont) return;

    // monta slides estáticos (fotos do git) se banco vazio
    let slides = [];
    if(!lista||!lista.length){
      slides = [
        {tipo:'foto', srcs:['/static/landing/foto1.jpg','/static/landing/foto4.jpg']},
        {tipo:'foto', srcs:['/static/landing/foto2.jpg','/static/landing/foto3.jpg']},
        {tipo:'video', srcs:['/static/landing/video.mp4']},
      ];
    } else {
      const fotos = lista.filter(m=>m.tipo==='foto');
      const videos = lista.filter(m=>m.tipo==='video');
      // agrupa fotos de 2 em 2
      for(let i=0;i<fotos.length;i+=2){
        const par = fotos.slice(i,i+2).map(f=>`/api/landing/midia/${f.id}/conteudo`);
        slides.push({tipo:'foto', srcs:par});
      }
      videos.forEach(v=>slides.push({tipo:'video', srcs:[`/api/landing/midia/${v.id}/conteudo`]}));
    }

    if(!slides.length){ cont.innerHTML=''; return; }

    const track = slides.map(s=>{
      const cells = s.srcs.map(src=>
        s.tipo==='video'
          ? `<div class="gal-cell"><video autoplay muted loop playsinline src="${src}"></video></div>`
          : `<div class="gal-cell"><img src="${src}" alt="Jogoteka" loading="lazy"></div>`
      ).join('');
      return `<div class="gal-slide slide-${s.tipo}">${cells}</div>`;
    }).join('');

    const dots = slides.map((_,i)=>`<button class="gal-dot${i===0?' ativo':''}" onclick="galIr(${i})"></button>`).join('');

    cont.innerHTML = `
      <div class="gal-carousel">
        <div class="gal-track" id="galTrack">${track}</div>
        <div class="gal-arrows">
          <button class="gal-arrow" onclick="galIr(_galIdx-1)">&#8249;</button>
          <button class="gal-arrow" onclick="galIr(_galIdx+1)">&#8250;</button>
        </div>
      </div>
      <div class="gal-dots" id="galDots">${dots}</div>`;

    // inicia carrossel
    window._galIdx = 0;
    window._galTotal = slides.length;
    window.galIr = function(n){
      _galIdx = (n + _galTotal) % _galTotal;
      document.getElementById('galTrack').style.transform = `translateX(-${_galIdx*100}%)`;
      document.querySelectorAll('.gal-dot').forEach((d,i)=>d.classList.toggle('ativo',i===_galIdx));
    };
    // auto-avança a cada 5s, pausa ao hover
    let _galTimer = setInterval(()=>galIr(_galIdx+1), 5000);
    cont.addEventListener('mouseenter',()=>clearInterval(_galTimer));
    cont.addEventListener('mouseleave',()=>{ _galTimer=setInterval(()=>galIr(_galIdx+1),5000); });
    // swipe
    let _gTx=0;
    cont.addEventListener('touchstart',e=>{ _gTx=e.touches[0].clientX; },{passive:true});
    cont.addEventListener('touchend',e=>{ const dx=e.changedTouches[0].clientX-_gTx; if(Math.abs(dx)>40) galIr(_galIdx+(dx<0?1:-1)); },{passive:true});
  }).catch(()=>{});
</script>
</body>
</html>"""

_ROTAS_PUBLICAS = {"/", "/login", "/setup", "/loja", "/health", "/api/jogos"}
_PREFIXOS_PUBLICOS = ("/catalogo", "/api/catalogo", "/api/imagens/", "/api/logo",
                      "/api/loja/", "/api/contrato-modelo", "/static/landing/",
                      "/api/landing/publico", "/api/recebimento/")

@app.before_request
def verificar_auth():
    path = request.path
    if path in _ROTAS_PUBLICAS or any(path.startswith(p) for p in _PREFIXOS_PUBLICOS):
        return None
    if not session.get("uid"):
        if request.is_json or path.startswith("/api/"):
            return jsonify({"erro": "Não autenticado"}), 401
        return redirect("/login")


@app.route("/health")
def health():
    return {"status": "ok"}


@app.route("/")
def index():
    return render_template_string(LANDING_HTML)


@app.route("/painel")
def painel():
    if not session.get("uid"):
        return redirect("/login")
    if session.get("perfil") == "vendedor":
        return redirect("/loja")
    return render_template_string(HTML, cidades=CIDADES)


@app.route("/api/jogos")
def listar():
    return jsonify([dict(j) for j in est.listar_jogos()])


# ── Categorias ────────────────────────────────────────────────────────────────
@app.route("/api/categorias")
def listar_categorias():
    with get_connection() as conn:
        rows = conn.execute("SELECT id, nome FROM categorias ORDER BY nome").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/categorias", methods=["POST"])
@requer_perfil("admin", "gerente")
def criar_categoria():
    nome = (request.get_json(force=True).get("nome") or "").strip()
    if not nome:
        return jsonify({"erro": "Nome obrigatório"}), 400
    with get_connection() as conn:
        try:
            cur = conn.execute("INSERT INTO categorias (nome) VALUES (?)", (nome,))
            return jsonify({"id": cur.lastrowid, "nome": nome}), 201
        except Exception:
            return jsonify({"erro": "Categoria já existe"}), 409

@app.route("/api/categorias/<int:cat_id>", methods=["DELETE"])
@requer_perfil("admin")
def excluir_categoria(cat_id):
    with get_connection() as conn:
        conn.execute("DELETE FROM categorias WHERE id = ?", (cat_id,))
    return jsonify({"ok": True})


# ── Destaques (opções de badge) ───────────────────────────────────────────────
@app.route("/api/destaques-opcoes")
def listar_destaques():
    with get_connection() as conn:
        rows = conn.execute("SELECT id, nome FROM destaques_opcoes ORDER BY nome").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/destaques-opcoes", methods=["POST"])
@requer_perfil("admin", "gerente")
def criar_destaque():
    nome = (request.get_json(force=True).get("nome") or "").strip()
    if not nome:
        return jsonify({"erro": "Nome obrigatório"}), 400
    with get_connection() as conn:
        try:
            cur = conn.execute("INSERT INTO destaques_opcoes (nome) VALUES (?)", (nome,))
            return jsonify({"id": cur.lastrowid, "nome": nome}), 201
        except Exception:
            return jsonify({"erro": "Badge já existe"}), 409

@app.route("/api/destaques-opcoes/<int:dest_id>", methods=["DELETE"])
@requer_perfil("admin")
def excluir_destaque(dest_id):
    with get_connection() as conn:
        conn.execute("DELETE FROM destaques_opcoes WHERE id = ?", (dest_id,))
    return jsonify({"ok": True})


@app.route("/api/jogos", methods=["POST"])
@requer_perfil("admin", "gerente")
def criar():
    dados = request.get_json()
    jogo_id = est.adicionar_jogo(dados)
    return jsonify({"id": jogo_id}), 201


@app.route("/api/jogos/<int:jogo_id>", methods=["PUT"])
def editar(jogo_id):
    est.editar_jogo(jogo_id, request.get_json())
    return jsonify({"ok": True})


@app.route("/api/jogos/<int:jogo_id>", methods=["DELETE"])
@requer_perfil("admin", "gerente")
def remover(jogo_id):
    est.remover_jogo(jogo_id)
    return jsonify({"ok": True})


@app.route("/api/jogos/<int:jogo_id>/movimentar", methods=["POST"])
def movimentar(jogo_id):
    d = request.get_json()
    try:
        if d.get("compra"):
            dados_compra = {**d["compra"], "quantidade": d["quantidade"]}
            est.registrar_compra(jogo_id, dados_compra)
        else:
            est.movimentar(jogo_id, d["tipo"], d["quantidade"], d.get("motivo"), d.get("observacao", ""))
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/movimentacoes/<int:mov_id>", methods=["DELETE"])
def excluir_movimentacao(mov_id):
    from database import get_connection
    with get_connection() as conn:
        mov = conn.execute("SELECT * FROM movimentacoes WHERE id=?", (mov_id,)).fetchone()
        if not mov:
            return jsonify({"error": "Movimentação não encontrada"}), 404
        # Reverte o efeito no estoque
        if mov["tipo"] == "entrada":
            jogo = conn.execute("SELECT quantidade FROM jogos WHERE id=?", (mov["jogo_id"],)).fetchone()
            if jogo["quantidade"] < mov["quantidade"]:
                return jsonify({"error": f"Não é possível excluir: o estoque atual ({jogo['quantidade']}) é menor que a quantidade desta entrada ({mov['quantidade']}). Faça ajustes antes."}), 400
            conn.execute("UPDATE jogos SET quantidade = quantidade - ? WHERE id=?", (mov["quantidade"], mov["jogo_id"]))
        else:
            conn.execute("UPDATE jogos SET quantidade = quantidade + ? WHERE id=?", (mov["quantidade"], mov["jogo_id"]))
        conn.execute("DELETE FROM movimentacoes WHERE id=?", (mov_id,))
    return jsonify({"ok": True})


@app.route("/api/movimentacoes")
def movimentacoes():
    from database import get_connection
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT m.id, j.nome AS jogo, m.tipo, m.quantidade, m.motivo, m.observacao, m.data
            FROM movimentacoes m JOIN jogos j ON j.id = m.jogo_id
            ORDER BY m.data DESC LIMIT 100
        """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/upload/imagem", methods=["POST"])
def upload_imagem():
    import base64
    if "file" not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado"}), 400
    f = request.files["file"]
    ext = os.path.splitext(f.filename)[1].lower()
    mime_map = {".jpg":"image/jpeg",".jpeg":"image/jpeg",".png":"image/png",
                ".webp":"image/webp",".gif":"image/gif"}
    if ext not in mime_map:
        return jsonify({"error": "Formato não suportado. Use JPG, PNG, WEBP ou GIF"}), 400
    conteudo_b64 = base64.b64encode(f.read()).decode("utf-8")
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO jogo_imagens (nome_original, conteudo_b64, mime_type, data_upload) VALUES (?,?,?,?)",
            (secure_filename(f.filename), conteudo_b64, mime_map[ext],
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        img_id = cur.lastrowid
    return jsonify({"filename": str(img_id)})


@app.route("/api/imagens/<path:filename>")
def servir_imagem(filename):
    import base64
    from flask import Response
    try:
        img_id = int(filename.split(".")[0])
    except (ValueError, IndexError):
        img_id = None
    if img_id is None:
        return "", 404
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM jogo_imagens WHERE id=?", (img_id,)).fetchone()
    if not row:
        return "", 404
    dados = base64.b64decode(row["conteudo_b64"])
    return Response(dados, mimetype=row["mime_type"],
                    headers={"Cache-Control": "public, max-age=31536000"})


@app.route("/api/upload/nf", methods=["POST"])
def upload_nf():
    import base64
    if "file" not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado"}), 400
    f = request.files["file"]
    nome_orig = secure_filename(f.filename)
    conteudo_b64 = base64.b64encode(f.read()).decode("utf-8")
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO nf_arquivos (nome_original, conteudo_b64, data_upload) VALUES (?,?,?)",
            (nome_orig, conteudo_b64, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        nf_id = cur.lastrowid
    return jsonify({"filename": str(nf_id)})


@app.route("/api/notas/<path:filename>")
def servir_nota(filename):
    import base64
    from flask import Response
    try:
        nf_id = int(filename.split("_")[0])
    except (ValueError, IndexError):
        nf_id = int(filename) if filename.isdigit() else None
    if nf_id is None:
        return "", 404
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM nf_arquivos WHERE id=?", (nf_id,)).fetchone()
    if not row:
        return "", 404
    dados = base64.b64decode(row["conteudo_b64"])
    nome = row["nome_original"] or "nota_fiscal"
    mime = "application/pdf" if nome.lower().endswith(".pdf") else "application/octet-stream"
    return Response(dados, mimetype=mime,
                    headers={"Content-Disposition": f'inline; filename="{nome}"'})


@app.route("/api/logo")
def servir_logo():
    logo_dir = os.path.dirname(__file__)
    for nome in ("logo_jogoteka.png", "logo_jogoteka.jpg", "logo_jogoteka.svg",
                 "logo_jogoteka.webp", "jogoteka_colorido.png", "jogoteka_colorido.jpg"):
        if os.path.exists(os.path.join(logo_dir, nome)):
            return send_from_directory(logo_dir, nome)
    return "", 404


@app.route("/api/landing/publico/midia")
def api_landing_pub_midia():
    pasta = os.path.join(os.path.dirname(__file__), "static", "landing")
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM landing_midia WHERE ativo=1 ORDER BY ordem, id"
        ).fetchall()
    # filtra registros sem conteúdo no banco e sem arquivo no disco
    validos = []
    for r in rows:
        d = dict(r)
        tem_b64 = d.get("conteudo_b64")
        tem_disco = os.path.exists(os.path.join(pasta, d.get("nome_arquivo", "")))
        if tem_b64 or tem_disco:
            validos.append(d)
    return jsonify(validos)


@app.route("/api/landing/publico/depoimentos")
def api_landing_pub_deps():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM landing_depoimentos WHERE ativo=1 ORDER BY ordem, id"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/static/landing/<path:filename>")
def servir_landing_static(filename):
    pasta = os.path.join(os.path.dirname(__file__), "static", "landing")
    return send_from_directory(pasta, filename)


@app.route("/api/compras")
def compras():
    jogo_id = request.args.get("jogo_id", type=int)
    rows = est.historico_compras(jogo_id=jogo_id)
    return jsonify([dict(r) for r in rows])


@app.route("/api/compras/<int:compra_id>", methods=["DELETE"])
def excluir_compra(compra_id):
    with get_connection() as conn:
        compra = conn.execute(
            "SELECT jogo_id, quantidade FROM compras WHERE id=?", (compra_id,)
        ).fetchone()
        if not compra:
            return jsonify({"erro": "Compra não encontrada"}), 404
        conn.execute("DELETE FROM compras WHERE id=?", (compra_id,))
    # Reverte o estoque que havia sido somado ao registrar a compra
    try:
        est.movimentar(compra["jogo_id"], "saida", compra["quantidade"],
                       "estorno compra", "registro de compra excluído")
    except ValueError:
        pass  # estoque já zerado por outro motivo — não bloqueia a exclusão
    return jsonify({"ok": True})


@app.route("/api/compras/<int:compra_id>", methods=["PATCH"])
def editar_compra(compra_id):
    d = request.get_json(force=True)
    with get_connection() as conn:
        compra = conn.execute(
            "SELECT jogo_id, quantidade FROM compras WHERE id=?", (compra_id,)
        ).fetchone()
        if not compra:
            return jsonify({"erro": "Compra não encontrada"}), 404
        campos = ["data_compra","fornecedor","pedido_compra","numero_nf",
                  "valor_pago","forma_pagamento","parcelas","observacao","arquivo_nf"]
        updates = {k: d[k] for k in campos if k in d}
        if not updates:
            return jsonify({"erro": "Nenhum campo enviado"}), 400
        set_clause = ", ".join(f"{k}=?" for k in updates)
        values = list(updates.values()) + [compra_id]
        conn.execute(f"UPDATE compras SET {set_clause} WHERE id=?", values)
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
#  LOJA JOGOTEKA
# ══════════════════════════════════════════════════════════════════════════════

LOJA_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Jogoteka — Loja</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Fredoka+One&family=Nunito:wght@400;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root{
      --red:#F10A0A; --green:#17C629; --purple:#7B20E1; --orange:#ED940E;
      --dark:#1a1a2e; --dark2:#16213e; --card:#0f3460; --text:#e0e0e0;
    }
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Nunito',system-ui,sans-serif;background:var(--dark);color:var(--text);min-height:100vh}

    /* ── Header ── */
    header{
      background:linear-gradient(135deg,var(--dark2) 0%,#1a0533 100%);
      border-bottom:3px solid var(--orange);
      padding:.6rem 2rem;display:flex;align-items:center;gap:1.5rem;flex-wrap:wrap
    }
    .logo-img{height:52px;width:auto;object-fit:contain;display:block}
    .logo-fallback{font-family:'Fredoka One',cursive;font-size:2rem;letter-spacing:1px;line-height:1}
    .logo-fallback span.j{color:var(--red)}
    .logo-fallback span.o1{color:var(--orange)}
    .logo-fallback span.g{color:var(--green)}
    .logo-fallback span.o2{color:var(--purple)}
    .logo-fallback span.t{color:var(--red)}
    .logo-fallback span.e{color:var(--orange)}
    .logo-fallback span.k{color:var(--green)}
    .logo-fallback span.a{color:var(--purple)}
    .filtros-bar{display:flex;gap:.6rem;padding:.5rem 1.2rem;flex-wrap:wrap;align-items:center}
    .filtro-sel{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.15);border-radius:20px;color:white;font-size:.85rem;padding:.4rem .9rem;outline:none;cursor:pointer;font-family:'Nunito',sans-serif}
    .filtro-sel:focus{border-color:var(--orange)}
    .filtro-sel option{background:#1a1a2e;color:white}
    .search-wrap{flex:1;max-width:500px;position:relative}
    .search-wrap input{
      width:100%;padding:.65rem 1rem .65rem 2.8rem;
      background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.15);
      border-radius:30px;color:white;font-size:1rem;outline:none
    }
    .search-wrap input:focus{border-color:var(--orange);background:rgba(255,255,255,.12)}
    .search-wrap input::placeholder{color:#888}
    .search-wrap .ico{position:absolute;left:.9rem;top:50%;transform:translateY(-50%);color:#888;font-size:1rem}
    .nav-tabs{display:flex;gap:.5rem;margin-left:auto}
    .tab-btn{background:rgba(255,255,255,.08);color:var(--text);border:1px solid rgba(255,255,255,.15);
      border-radius:8px;padding:.5rem 1.1rem;cursor:pointer;font-size:.88rem;transition:.2s;
      font-family:'Fredoka One',cursive;letter-spacing:.5px}
    .tab-btn:hover,.tab-btn.active{background:var(--orange);color:white;border-color:var(--orange)}
    .tab-btn.purple.active{background:var(--purple);border-color:var(--purple)}
    .tab-btn.green.active{background:var(--green);border-color:var(--green);color:#000}

    /* ── Catálogo ── */
    main{max-width:1400px;margin:0 auto;padding:1.5rem}
    .page{display:none}.page.active{display:block}
    .section-title{font-family:'Fredoka One',cursive;font-size:1.25rem;color:var(--orange);margin-bottom:1rem;
      display:flex;align-items:center;gap:.5rem;letter-spacing:.5px}
    .section-title::after{content:'';flex:1;height:1px;background:rgba(255,255,255,.1)}

    .catalogo{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:1.2rem}
    .jogo-card{
      background:var(--card);border-radius:12px;overflow:hidden;
      border:1px solid rgba(255,255,255,.08);cursor:pointer;
      transition:transform .2s,box-shadow .2s;display:flex;flex-direction:column
    }
    .jogo-card:hover{transform:translateY(-4px);box-shadow:0 8px 30px rgba(0,0,0,.5);border-color:var(--orange)}
    .jogo-card.sem-estoque{opacity:.5;cursor:not-allowed}
    .jogo-card.sem-estoque:hover{transform:none;box-shadow:none;border-color:rgba(255,255,255,.08)}
    .jogo-img{width:100%;height:180px;object-fit:contain;background:#0a1628;padding:8px}
    .jogo-placeholder{width:100%;height:180px;background:linear-gradient(135deg,#0a1628,#16213e);
      display:flex;align-items:center;justify-content:center;font-size:3.5rem}
    .jogo-info{padding:.9rem;flex:1;display:flex;flex-direction:column;gap:.4rem}
    .jogo-nome{font-family:'Fredoka One',cursive;font-size:1.05rem;color:white;line-height:1.2;letter-spacing:.3px}
    .jogo-meta{font-size:.75rem;color:#888;display:flex;gap:.4rem;flex-wrap:wrap}
    .jogo-tag{background:rgba(255,255,255,.06);border-radius:4px;padding:1px 6px}
    .jogo-precos{margin-top:auto;padding-top:.6rem;border-top:1px solid rgba(255,255,255,.06)}
    .preco-venda{font-size:1.1rem;font-weight:800;color:var(--green)}
    .preco-label{font-size:.7rem;color:#aaa;font-weight:700;letter-spacing:.05em;text-transform:uppercase}
    .loc-chips{display:flex;gap:.3rem;flex-wrap:wrap;margin-top:.4rem}
    .loc-chip{background:rgba(123,32,225,.2);border:1px solid rgba(123,32,225,.4);
      border-radius:20px;padding:2px 8px;font-size:.72rem;color:#c9a9ff}
    .estoque-badge{
      display:inline-flex;align-items:center;gap:.3rem;
      font-size:.72rem;font-weight:600;padding:2px 8px;border-radius:20px;margin-top:.4rem
    }
    .estoque-ok{background:rgba(23,198,41,.15);color:var(--green);border:1px solid rgba(23,198,41,.3)}
    .estoque-baixo{background:rgba(241,10,10,.15);color:var(--red);border:1px solid rgba(241,10,10,.3)}
    .estoque-zero{background:rgba(100,100,100,.15);color:#888;border:1px solid rgba(100,100,100,.3)}

    /* ── Histórico tabs ── */
    .hist-table{width:100%;border-collapse:collapse}
    .hist-table th{background:rgba(255,255,255,.05);padding:.7rem 1rem;text-align:left;
      font-size:.8rem;color:#aaa;border-bottom:1px solid rgba(255,255,255,.08)}
    .hist-table td{padding:.7rem 1rem;border-bottom:1px solid rgba(255,255,255,.05);font-size:.85rem}
    .hist-table tr:hover td{background:rgba(255,255,255,.03)}
    .status-ativa{color:var(--orange);font-weight:600}
    .status-devolvido{color:var(--green)}
    .status-atrasado{color:var(--red);font-weight:600}
    .condicao-opcoes{display:flex;gap:.6rem}
    .condicao-btn{flex:1;display:flex;align-items:center;justify-content:center;gap:.4rem;
      padding:.55rem;border-radius:8px;cursor:pointer;font-size:.88rem;font-weight:700;
      background:rgba(23,198,41,.08);border:2px solid rgba(23,198,41,.25);color:#6ee37a;transition:.15s}
    .condicao-btn.avaria{background:rgba(241,10,10,.08);border-color:rgba(241,10,10,.25);color:#fc8181}
    .condicao-btn:has(input:checked){border-width:2px}
    .condicao-btn:has(input:checked):not(.avaria){background:rgba(23,198,41,.2);border-color:var(--green)}
    .condicao-btn.avaria:has(input:checked){background:rgba(241,10,10,.2);border-color:var(--red)}
    .condicao-btn input[type=radio]{display:none}
    .btn-whatsapp{display:inline-flex;align-items:center;gap:.4rem;background:#25D366;color:white;
      border:none;border-radius:6px;padding:.3rem .7rem;font-size:.78rem;font-weight:700;
      cursor:pointer;text-decoration:none;font-family:'Nunito',sans-serif}
    .btn-whatsapp:hover{filter:brightness(1.1)}
    .btn-devolver{background:rgba(23,198,41,.15);border:1px solid var(--green);color:var(--green);
      border-radius:6px;padding:3px 10px;cursor:pointer;font-size:.78rem}
    .btn-devolver:hover{background:rgba(23,198,41,.3)}
    .btn-excluir-mov{background:rgba(241,10,10,.1);border:1px solid rgba(241,10,10,.3);color:var(--red);
      border-radius:6px;padding:2px 8px;cursor:pointer;font-size:.8rem}
    .btn-excluir-mov:hover{background:rgba(241,10,10,.25)}

    /* ── Dashboard ── */
    .dash-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:1.2rem;margin-bottom:1.5rem}
    .dash-card{background:var(--dark3);border:1px solid var(--border);border-radius:14px;padding:1.2rem}
    .dash-card h3{font-family:'Fredoka One',cursive;font-size:1rem;margin-bottom:1rem;display:flex;align-items:center;gap:.5rem}
    .rank-item{display:flex;align-items:center;gap:.7rem;margin-bottom:.65rem}
    .rank-pos{font-family:'Fredoka One',cursive;font-size:1.1rem;width:24px;text-align:center;color:var(--muted)}
    .rank-pos.gold{color:#FFD700}.rank-pos.silver{color:#C0C0C0}.rank-pos.bronze{color:#CD7F32}
    .rank-bar-wrap{flex:1}
    .rank-label{font-size:.8rem;color:white;margin-bottom:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .rank-bar{height:6px;border-radius:999px;background:var(--border);overflow:hidden}
    .rank-bar-fill{height:100%;border-radius:999px;transition:width .6s ease}
    .rank-val{font-size:.82rem;font-weight:700;white-space:nowrap;min-width:60px;text-align:right}

    /* ── Conciliação ── */
    .conc-upload-box{border:2px dashed var(--border);border-radius:14px;padding:2.5rem;
      text-align:center;cursor:pointer;transition:.2s;margin-bottom:1.5rem}
    .conc-upload-box:hover{border-color:var(--orange);background:rgba(237,148,14,.05)}
    .conc-upload-box.dragover{border-color:var(--green);background:rgba(23,198,41,.07)}
    .conc-result-bar{display:flex;gap:1.5rem;align-items:center;flex-wrap:wrap;
      background:var(--dark3);border-radius:12px;padding:1rem 1.5rem;margin-bottom:1.2rem}
    .conc-stat{display:flex;flex-direction:column;align-items:center;min-width:80px}
    .conc-stat span{font-size:2rem;font-weight:800;font-family:'Fredoka One',cursive}
    .conc-stat label{font-size:.72rem;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
    .badge-recebido{background:rgba(23,198,41,.2);color:var(--green);border:1px solid rgba(23,198,41,.4);
      border-radius:999px;padding:2px 8px;font-size:.72rem;font-weight:700;white-space:nowrap}
    .badge-pendente{background:rgba(237,148,14,.15);color:var(--orange);border:1px solid rgba(237,148,14,.4);
      border-radius:999px;padding:2px 8px;font-size:.72rem;font-weight:700;white-space:nowrap}
    .badge-conciliado{background:rgba(23,198,41,.2);color:var(--green);border:1px solid rgba(23,198,41,.4);
      border-radius:999px;padding:2px 8px;font-size:.72rem;font-weight:700}
    .badge-sem-match{background:rgba(237,148,14,.15);color:var(--orange);border:1px solid rgba(237,148,14,.4);
      border-radius:999px;padding:2px 8px;font-size:.72rem;font-weight:700}

    /* ── Modal ── */
    .modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:100;
      align-items:center;justify-content:center;padding:1rem}
    .modal-bg.open{display:flex}
    .modal{background:var(--dark2);border:1px solid rgba(255,255,255,.1);border-radius:16px;
      width:100%;max-width:560px;max-height:90vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,.5)}
    .modal-header{padding:1.2rem 1.5rem .8rem;border-bottom:1px solid rgba(255,255,255,.08);
      display:flex;align-items:center;gap:.8rem}
    .modal-header img,.modal-header .ph{width:60px;height:60px;border-radius:8px;
      object-fit:contain;background:#0a1628;padding:4px;flex-shrink:0}
    .modal-header .ph{display:flex;align-items:center;justify-content:center;font-size:1.8rem}
    .modal-header h3{font-size:1.05rem;color:white;font-weight:700}
    .modal-header p{font-size:.78rem;color:#888;margin-top:2px}
    .modal-body{padding:1.2rem 1.5rem}
    .modal-tabs{display:flex;gap:.5rem;margin-bottom:1.2rem}
    .modal-tab{flex:1;padding:.55rem;border-radius:8px;border:1px solid rgba(255,255,255,.1);
      background:transparent;color:#aaa;cursor:pointer;font-size:.92rem;font-family:'Fredoka One',cursive;
      letter-spacing:.5px;transition:.2s}
    .modal-tab.active-venda{background:var(--green);border-color:var(--green);color:#000}
    .modal-tab.active-locacao{background:var(--purple);border-color:var(--purple);color:white}
    label{display:block;font-size:.78rem;color:#aaa;margin:.65rem 0 .25rem;font-weight:500}
    input,select,textarea{
      width:100%;padding:.5rem .75rem;background:rgba(255,255,255,.07);
      border:1px solid rgba(255,255,255,.12);border-radius:8px;color:white;font-size:.9rem;outline:none
    }
    input:focus,select:focus{border-color:var(--orange)}
    input::placeholder{color:#555}
    select option{background:#1a1a2e;color:white}
    .row2{display:grid;grid-template-columns:1fr 1fr;gap:.6rem}
    .row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:.5rem}
    .divider{border:none;border-top:1px solid rgba(255,255,255,.08);margin:.9rem 0}
    .section-label{font-size:.72rem;font-weight:700;color:var(--orange);letter-spacing:1px;
      text-transform:uppercase;margin-bottom:.3rem}
    .preco-resumo{background:rgba(255,255,255,.04);border-radius:10px;padding:.9rem 1rem;margin-top:.6rem}
    .preco-resumo .linha{display:flex;justify-content:space-between;font-size:.85rem;margin:.25rem 0}
    .preco-resumo .total{font-size:1.1rem;font-weight:800;color:var(--green);margin-top:.5rem}
    .loc-opcoes{display:grid;grid-template-columns:repeat(3,1fr);gap:.5rem;margin-top:.3rem}
    .loc-op{border:1px solid rgba(123,32,225,.3);border-radius:8px;padding:.6rem;text-align:center;
      cursor:pointer;transition:.2s;background:rgba(123,32,225,.08)}
    .loc-op:hover,.loc-op.sel{background:rgba(123,32,225,.3);border-color:var(--purple)}
    .loc-op .dias{font-size:1.1rem;font-weight:800;color:var(--purple)}
    .loc-op .val{font-size:.82rem;color:#c9a9ff;margin-top:2px}
    .loc-op .dlab{font-size:.68rem;color:#888}
    .modal-footer{padding:.9rem 1.5rem 1.2rem;border-top:1px solid rgba(255,255,255,.08);
      display:flex;gap:.6rem;justify-content:flex-end}
    .btn-cancel{background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.12);
      color:#aaa;border-radius:8px;padding:.55rem 1.2rem;cursor:pointer;font-size:.88rem}
    .btn-cancel:hover{background:rgba(255,255,255,.12)}
    .btn-vender{background:var(--green);border:none;color:#000;font-family:'Fredoka One',cursive;
      border-radius:8px;padding:.55rem 1.4rem;cursor:pointer;font-size:.95rem;letter-spacing:.5px}
    .btn-vender:hover{filter:brightness(1.1)}
    .btn-locar{background:var(--purple);border:none;color:white;font-family:'Fredoka One',cursive;
      border-radius:8px;padding:.55rem 1.4rem;cursor:pointer;font-size:.95rem;letter-spacing:.5px}
    .btn-locar:hover{filter:brightness(1.2)}
    .toast{position:fixed;bottom:2rem;right:2rem;background:#222;color:white;padding:.8rem 1.4rem;
      border-radius:10px;font-size:.9rem;box-shadow:0 4px 20px rgba(0,0,0,.5);
      border-left:4px solid var(--green);z-index:999;opacity:0;transition:opacity .3s;pointer-events:none}
    .toast.show{opacity:1}
    .toast.err{border-color:var(--red)}

    /* Devolução modal */
    .loc-card{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);
      border-radius:10px;padding:.9rem 1rem;margin-bottom:.8rem}
    .loc-card h4{font-size:.92rem;color:white;margin-bottom:.3rem}
    .loc-card .meta{font-size:.78rem;color:#aaa;display:flex;gap:1rem;flex-wrap:wrap}
    .loc-card .atrasado{color:var(--red);font-weight:600}
    .multa-preview{background:rgba(241,10,10,.1);border:1px solid rgba(241,10,10,.3);
      border-radius:8px;padding:.6rem .9rem;margin-top:.5rem;font-size:.85rem;color:var(--red)}
    .desconto-box{margin-top:.6rem;border:1px solid rgba(255,255,255,.1);border-radius:8px;overflow:hidden}
    .desconto-tabs{display:flex;border-bottom:1px solid rgba(255,255,255,.08)}
    .dtab,.dtab-loc{flex:1;background:rgba(255,255,255,.04);border:none;border-radius:0;padding:.4rem;font-size:.82rem;color:var(--muted);cursor:pointer;font-family:'Nunito',sans-serif;font-weight:700;transition:.15s}
    .dtab:hover,.dtab-loc:hover{background:rgba(255,255,255,.09);color:white}
    .dtab.active{background:rgba(237,148,14,.18);color:var(--orange)}
    .dtab-loc.active{background:rgba(123,32,225,.2);color:#c9a9ff}
    .desc-campo{padding:.55rem .7rem}
    .desc-campo input{width:100%;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.1);border-radius:6px;color:white;padding:.4rem .65rem;font-size:.9rem;font-family:'Nunito',sans-serif;outline:none}
    .desc-campo input:focus{border-color:var(--orange)}
    .btn-aplicar-cupom{background:var(--orange);color:white;border:none;border-radius:6px;padding:.4rem .8rem;font-size:.82rem;font-family:'Fredoka One',cursive;cursor:pointer;white-space:nowrap}
    .btn-aplicar-cupom:hover{filter:brightness(1.1)}
    .cpf-busca-row{display:flex;gap:.5rem;align-items:flex-end}
    .btn-buscar-cpf{background:rgba(123,32,225,.2);color:#c9a9ff;border:1px solid rgba(123,32,225,.4);border-radius:6px;padding:.45rem .8rem;font-size:.82rem;cursor:pointer;white-space:nowrap;font-family:'Nunito',sans-serif;font-weight:700;flex-shrink:0}
    .btn-buscar-cpf:hover{background:rgba(123,32,225,.35)}
    .cliente-card{background:rgba(23,198,41,.07);border:1px solid rgba(23,198,41,.25);border-radius:8px;padding:.7rem 1rem;margin-bottom:.5rem;display:flex;align-items:center;gap:.8rem}
    .cliente-info{display:flex;flex-direction:column;gap:.15rem;flex:1}
    .cliente-nome{font-weight:700;color:white;font-size:.95rem}
    .cliente-detalhe{font-size:.78rem;color:var(--muted)}
    .btn-editar-cliente{background:rgba(237,148,14,.15);color:var(--orange);border:1px solid rgba(237,148,14,.3);border-radius:6px;padding:.25rem .7rem;font-size:.78rem;cursor:pointer;font-family:'Nunito',sans-serif;font-weight:700}
    .btn-editar-cliente:hover{background:rgba(237,148,14,.28)}

    .empty{text-align:center;padding:3rem;color:#555;font-size:.95rem}
    .empty .ico{font-size:3rem;margin-bottom:.8rem}
  </style>
</head>
<body>

<header>
  <div id="header-logo">
    <img class="logo-img" src="/api/logo" alt="Jogoteka"
         onerror="this.style.display='none';document.getElementById('logo-fallback').style.display='block'">
    <div class="logo-fallback" id="logo-fallback" style="display:none">
      <span class="j">J</span><span class="o1">O</span><span class="g">G</span><span class="o2">O</span><span class="t">T</span><span class="e">E</span><span class="k">K</span><span class="a">A</span>
    </div>
  </div>
  <div class="search-wrap">
    <span class="ico">🔍</span>
    <input type="search" id="busca" placeholder="Buscar jogo por nome, editora ou categoria…" oninput="filtrar()">
  </div>
  <div class="nav-tabs">
    <button class="tab-btn active" onclick="showTab('catalogo',this)">🎲 Catálogo</button>
    <button class="tab-btn green" onclick="showTab('vendas',this)">💰 Vendas</button>
    <button class="tab-btn purple" onclick="showTab('locacoes',this)">🔑 Locações</button>
    <button class="tab-btn" onclick="showTab('cupons',this)">🎟️ Cupons</button>
    <button class="tab-btn" onclick="showTab('contrato-modelo',this)">📝 Contrato</button>
  </div>
</header>

<main>
  <!-- CATÁLOGO -->
  <div class="page active" id="page-catalogo">
    <div class="section-title">🎮 Jogos disponíveis</div>
    <div class="filtros-bar">
      <select id="filtro-faixa" class="filtro-sel" onchange="filtrar()">
        <option value="">🎂 Faixa etária</option>
      </select>
      <select id="filtro-jogadores" class="filtro-sel" onchange="filtrar()">
        <option value="">👥 Nº de jogadores</option>
      </select>
    </div>
    <div class="catalogo" id="catalogo"></div>
  </div>

  <!-- VENDAS -->
  <div class="page" id="page-vendas">
    <div class="section-title">💰 Histórico de Vendas</div>
    <table class="hist-table" id="tbl-vendas">
      <thead><tr><th>Data</th><th>Jogo</th><th>Cliente</th><th>Atendente</th><th>Qtd</th><th>Preço unit.</th><th>Desconto</th><th>Total</th><th>Custo unit.</th><th>Margem (R$)</th><th>Margem (%)</th><th>Pagamento</th><th>Recebimento</th><th></th></tr></thead>
      <tbody id="body-vendas"></tbody>
    </table>
  </div>

  <!-- LOCAÇÕES -->
  <div class="page" id="page-locacoes">
    <div class="section-title">🔑 Locações Ativas & Histórico</div>
    <table class="hist-table">
      <thead><tr><th>Jogo</th><th>Cliente</th><th>Atendente</th><th>Saída</th><th>Devolução prevista</th><th>Status</th><th>Valor</th><th>Pagamento</th><th>Multa</th><th>Condição</th><th>Recebimento</th><th>Ações</th></tr></thead>
      <tbody id="body-locacoes"></tbody>
    </table>
  </div>

  <div class="page" id="page-cupons">
    <div class="section-title" style="display:flex;align-items:center;justify-content:space-between">
      <span>🎟️ Cupons de Desconto</span>
      <button class="btn-add" onclick="openModalCupom()">+ Novo Cupom</button>
    </div>
    <table class="hist-table">
      <thead><tr><th>Código</th><th>Tipo</th><th>Valor</th><th>Descrição</th><th>Usos</th><th>Status</th><th>Criado em</th><th></th></tr></thead>
      <tbody id="tbl-cupons"></tbody>
    </table>
    <div class="section-title" style="margin-top:1.5rem">Histórico de Uso</div>
    <table class="hist-table">
      <thead><tr><th>Data</th><th>Cupom</th><th>Operação</th><th>Jogo</th><th>Cliente</th><th>Desconto</th></tr></thead>
      <tbody id="tbl-cupom-usos"></tbody>
    </table>
  </div>

  <!-- MODELO DE CONTRATO -->
  <div class="page" id="page-contrato-modelo" style="max-width:800px;margin:0 auto;padding:1rem">
    <div class="section-title" style="display:flex;align-items:center;justify-content:space-between">
      <span>📝 Modelo de Contrato de Locação</span>
      <a href="/api/contrato-modelo/preview" target="_blank"
         style="background:rgba(123,32,225,.3);border:1px solid rgba(123,32,225,.6);color:#c9a9ff;
                border-radius:8px;padding:.4rem .9rem;text-decoration:none;font-size:.85rem">
        👁 Preview PDF
      </a>
    </div>

    <!-- Status do template atual -->
    <div id="loja-template-status" style="margin:.8rem 0;padding:.7rem 1rem;background:rgba(255,255,255,.05);
         border-radius:8px;font-size:.88rem;color:#aaa">Carregando...</div>

    <!-- Upload DOCX -->
    <div style="background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.1);border-radius:10px;padding:1rem;margin-bottom:1rem">
      <div style="font-weight:700;margin-bottom:.4rem;color:#ED940E">📄 Template Word (.docx) <span style="font-size:.75rem;font-weight:400;color:#aaa">— Recomendado</span></div>
      <p style="font-size:.82rem;color:#aaa;margin-bottom:.7rem">
        Crie seu contrato no Word com os <code style="background:rgba(237,148,14,.15);padding:1px 5px;border-radius:4px">{% raw %}{{CAMPOS}}{% endraw %}</code> onde quiser e faça o upload.
      </p>
      <div style="display:flex;gap:.6rem;align-items:center;flex-wrap:wrap">
        <input type="file" id="loja-input-docx" accept=".docx" style="display:none" onchange="lojaUploadDocx()">
        <button onclick="document.getElementById('loja-input-docx').click()"
                style="background:rgba(237,148,14,.2);border:1px solid rgba(237,148,14,.4);color:#ED940E;
                       border-radius:8px;padding:.45rem .9rem;cursor:pointer;font-size:.85rem">
          📤 Enviar DOCX
        </button>
        <button onclick="lojaRemoverDocx()"
                style="background:rgba(241,10,10,.12);border:1px solid rgba(241,10,10,.3);color:#fc8181;
                       border-radius:8px;padding:.45rem .9rem;cursor:pointer;font-size:.85rem">
          🗑 Remover DOCX
        </button>
      </div>
    </div>

    <!-- Editor de texto (fallback) -->
    <div style="background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.1);border-radius:10px;padding:1rem;margin-bottom:1rem">
      <div style="font-weight:700;margin-bottom:.6rem">✏️ Editor de Cláusulas (texto)</div>
      <div id="loja-campos-chips" style="display:flex;flex-wrap:wrap;gap:.4rem;margin-bottom:.7rem"></div>
      <textarea id="loja-textarea-modelo" rows="18"
        style="width:100%;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);
               border-radius:8px;color:#e0e0e0;padding:.8rem;font-size:.82rem;font-family:monospace;
               resize:vertical;line-height:1.5"></textarea>
      <div style="margin-top:.6rem;display:flex;gap:.6rem">
        <button onclick="lojaSalvarModelo()"
                style="background:rgba(23,198,41,.2);border:1px solid rgba(23,198,41,.4);color:#17C629;
                       border-radius:8px;padding:.45rem 1.2rem;cursor:pointer;font-size:.85rem;font-weight:700">
          💾 Salvar Modelo
        </button>
      </div>
    </div>
  </div>
</main>

<!-- Modal: Cupom -->
<div class="modal-bg" id="modal-cupom" onclick="if(event.target===this)fecharModal('modal-cupom')">
  <div class="modal" style="max-width:420px">
    <h3>🎟️ Novo Cupom</h3>
    <label>Código</label>
    <input id="cp-codigo" placeholder="Ex: JOGOTEKA10" style="text-transform:uppercase">
    <label>Tipo de desconto</label>
    <select id="cp-tipo">
      <option value="pct">Percentual (%)</option>
      <option value="reais">Valor fixo (R$)</option>
    </select>
    <label>Valor</label>
    <input id="cp-valor" type="number" min="0" step="0.01" placeholder="Ex: 10">
    <label>Descrição (opcional)</label>
    <input id="cp-desc" placeholder="Ex: Desconto de boas-vindas">
    <label>Limite de usos (deixe vazio para ilimitado)</label>
    <input id="cp-usos" type="number" min="1" placeholder="Ilimitado">
    <div class="modal-actions">
      <button class="btn-cancel" onclick="fecharModal('modal-cupom')">Cancelar</button>
      <button class="btn-confirm" onclick="salvarCupom()">Criar Cupom</button>
    </div>
  </div>
</div>

<!-- Modal: Venda / Locação -->
<div class="modal-bg" id="modal-op" onclick="fecharSeFora(event,'modal-op')">
  <div class="modal">
    <div class="modal-header">
      <img id="m-img" style="display:none">
      <div class="ph" id="m-ph">🎲</div>
      <div>
        <h3 id="m-nome">—</h3>
        <p id="m-meta">—</p>
      </div>
    </div>
    <div class="modal-body">
      <div class="modal-tabs">
        <button class="modal-tab active-venda" id="tab-venda" onclick="switchTab('venda')">💰 Vender</button>
        <button class="modal-tab" id="tab-locacao" onclick="switchTab('locacao')">🔑 Locar</button>
      </div>

      <!-- VENDA -->
      <div id="form-venda">
        <div class="section-label" style="display:flex;align-items:center;justify-content:space-between">
          Dados do cliente
          <button type="button" id="v-btn-editar" class="btn-editar-cliente" style="display:none" onclick="editarCliente('v')">✏️ Editar</button>
        </div>
        <div id="v-cliente-encontrado" class="cliente-card" style="display:none">
          <div class="cliente-info">
            <span class="cliente-nome" id="v-ce-nome"></span>
            <span class="cliente-detalhe" id="v-ce-detalhe"></span>
          </div>
        </div>
        <div id="v-cliente-form">
          <div class="cpf-busca-row">
            <div style="flex:1"><label>CPF</label><input id="v-cpf" placeholder="000.000.000-00" oninput="buscarClienteCPF('v')"></div>
            <button type="button" class="btn-buscar-cpf" onclick="buscarClienteCPF('v',true)">🔍 Buscar</button>
          </div>
          <input id="v-nome" placeholder="Nome completo" style="margin-top:.5rem">
          <div class="row2">
            <div><label>Data de nascimento</label><input id="v-nasc" type="text" placeholder="DD/MM" maxlength="5" oninput="mascDDMM(this)"></div>
            <div><label>Telefone</label><input id="v-tel" placeholder="(00) 00000-0000"></div>
          </div>
          <div><label>Instagram</label><input id="v-insta" placeholder="@usuario"></div>
          <div style="margin-top:.6rem;font-size:.78rem;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">Endereço</div>
          <div class="row2" style="margin-top:.3rem">
            <div style="flex:0 0 140px"><label>CEP</label><input id="v-cep" placeholder="00000-000" oninput="buscarCEP('v')"></div>
            <div style="flex:1"><label>Logradouro</label><input id="v-logradouro" placeholder="Rua / Av."></div>
          </div>
          <div class="row2">
            <div style="flex:0 0 100px"><label>Número</label><input id="v-numero" placeholder="Nº"></div>
            <div style="flex:1"><label>Complemento</label><input id="v-complemento" placeholder="Apto, bloco..."></div>
          </div>
          <div class="row2">
            <div style="flex:1"><label>Bairro</label><input id="v-bairro" placeholder="Bairro"></div>
            <div style="flex:1"><label>Cidade</label><input id="v-cidade" placeholder="Cidade"></div>
            <div style="flex:0 0 60px"><label>UF</label><input id="v-estado" placeholder="SP" maxlength="2"></div>
          </div>
        </div>
        <hr class="divider">
        <div class="section-label">Dados da venda</div>
        <div class="row2">
          <div><label>Quantidade</label><input id="v-qty" type="number" min="1" value="1" oninput="calcVenda()"></div>
          <div><label>Preço unitário (R$)</label><input id="v-preco" type="number" min="0" step="0.01" oninput="calcVenda()"></div>
        </div>
        <div class="desconto-box">
          <div class="desconto-tabs">
            <button type="button" class="dtab active" onclick="setDescTipo('reais',this)">R$</button>
            <button type="button" class="dtab" onclick="setDescTipo('pct',this)">%</button>
            <button type="button" class="dtab" onclick="setDescTipo('cupom',this)">Cupom</button>
          </div>
          <div id="desc-reais" class="desc-campo">
            <input id="v-desconto-r" type="number" min="0" step="0.01" value="0" placeholder="0,00" oninput="calcVenda()">
          </div>
          <div id="desc-pct" class="desc-campo" style="display:none">
            <input id="v-desconto-p" type="number" min="0" max="100" step="0.1" value="0" placeholder="0%" oninput="calcVenda()">
          </div>
          <div id="desc-cupom" class="desc-campo" style="display:none">
            <div style="display:flex;gap:.4rem">
              <input id="v-cupom" placeholder="Código do cupom" style="flex:1">
              <button type="button" class="btn-aplicar-cupom" onclick="aplicarCupom()">Aplicar</button>
            </div>
            <div id="cupom-status" style="font-size:.78rem;margin-top:.3rem"></div>
          </div>
        </div>
        <div class="row2">
          <div><label>Forma de pagamento</label>
            <select id="v-pagamento">
              <option value="">Selecione…</option>
              <option>PIX</option><option>Dinheiro</option>
              <option>Cartão de débito</option><option>Cartão de crédito</option>
              <option>Boleto</option><option>Transferência</option>
            </select>
          </div>
          <div><label>Data da venda</label><input id="v-data" type="date"></div>
        </div>
        <div class="row2">
          <div><label>Atendente</label><input id="v-atendente" placeholder="Nome do atendente"></div>
          <div><label>Observação</label><input id="v-obs" placeholder="Opcional"></div>
        </div>
        <div class="preco-resumo" id="resumo-venda">
          <div class="linha"><span>Subtotal</span><span id="r-subtotal">R$ 0,00</span></div>
          <div class="linha"><span>Desconto</span><span id="r-desconto" style="color:var(--red)">- R$ 0,00</span></div>
          <div class="linha total"><span>Total a pagar</span><span id="r-total">R$ 0,00</span></div>
        </div>
      </div>

      <!-- LOCAÇÃO -->
      <div id="form-locacao" style="display:none">
        <div class="section-label" style="display:flex;align-items:center;justify-content:space-between">
          Dados do cliente
          <button type="button" id="l-btn-editar" class="btn-editar-cliente" style="display:none" onclick="editarCliente('l')">✏️ Editar</button>
        </div>
        <div id="l-cliente-encontrado" class="cliente-card" style="display:none">
          <div class="cliente-info">
            <span class="cliente-nome" id="l-ce-nome"></span>
            <span class="cliente-detalhe" id="l-ce-detalhe"></span>
          </div>
        </div>
        <div id="l-cliente-form">
          <div class="cpf-busca-row">
            <div style="flex:1"><label>CPF</label><input id="l-cpf" placeholder="000.000.000-00" oninput="buscarClienteCPF('l')"></div>
            <button type="button" class="btn-buscar-cpf" onclick="buscarClienteCPF('l',true)">🔍 Buscar</button>
          </div>
          <input id="l-nome" placeholder="Nome completo" style="margin-top:.5rem">
          <div class="row2">
            <div><label>Telefone</label><input id="l-tel" placeholder="(00) 00000-0000"></div>
            <div><label>Data de nascimento</label><input id="l-nasc" type="text" placeholder="DD/MM" maxlength="5" oninput="mascDDMM(this)"></div>
          </div>
          <div><label>Instagram</label><input id="l-insta" placeholder="@usuario"></div>
          <div style="margin-top:.6rem;font-size:.78rem;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">Endereço</div>
          <div class="row2" style="margin-top:.3rem">
            <div style="flex:0 0 140px"><label>CEP</label><input id="l-cep" placeholder="00000-000" oninput="buscarCEP('l')"></div>
            <div style="flex:1"><label>Logradouro</label><input id="l-logradouro" placeholder="Rua / Av."></div>
          </div>
          <div class="row2">
            <div style="flex:0 0 100px"><label>Número</label><input id="l-numero" placeholder="Nº"></div>
            <div style="flex:1"><label>Complemento</label><input id="l-complemento" placeholder="Apto, bloco..."></div>
          </div>
          <div class="row2">
            <div style="flex:1"><label>Bairro</label><input id="l-bairro" placeholder="Bairro"></div>
            <div style="flex:1"><label>Cidade</label><input id="l-cidade" placeholder="Cidade"></div>
            <div style="flex:0 0 60px"><label>UF</label><input id="l-estado" placeholder="SP" maxlength="2"></div>
          </div>
        </div>
        <hr class="divider">
        <!-- ── Grupo multi-locação ──────────────────────────────────── -->
        <div id="loc-grupo-section" style="display:none">
          <div class="section-label" style="margin-bottom:.4rem">Jogos no grupo</div>
          <div id="loc-grupo-lista" style="display:flex;flex-direction:column;gap:.35rem;margin-bottom:.6rem"></div>
          <hr class="divider">
        </div>
        <!-- Picker para escolher próximo jogo (modo grupo) -->
        <div id="loc-picker-box" style="display:none;margin-bottom:.5rem">
          <div class="section-label" style="margin-bottom:.3rem">Adicionar jogo</div>
          <select id="loc-select-jogo" style="width:100%" onchange="trocarJogoLocacao(this.value)">
            <option value="">— Selecione o jogo —</option>
          </select>
        </div>
        <div class="section-label" id="loc-jogo-titulo">Opção de locação</div>
        <div class="loc-opcoes" id="loc-opcoes"></div>
        <button type="button" id="btn-add-loc-grupo" onclick="adicionarJogoAoGrupo()"
          style="display:none;margin-top:.5rem;width:100%;background:rgba(99,102,241,.12);
                 border:1.5px dashed rgba(99,102,241,.4);color:#a5b4fc;padding:.45rem;
                 border-radius:8px;cursor:pointer;font-size:.82rem;font-weight:700">
          ＋ Adicionar outro jogo
        </button>
        <div style="margin-top:.8rem" class="row2">
          <div><label>Data de saída</label><input id="l-saida" type="date" oninput="calcDevolucao()"></div>
          <div><label>Devolução prevista</label><input id="l-prevista" readonly style="color:var(--orange)"></div>
        </div>
        <div id="multa-info" style="display:none" class="multa-preview"></div>
        <hr class="divider">
        <div class="section-label">Desconto na locação</div>
        <div class="desconto-box">
          <div class="desconto-tabs">
            <button type="button" class="dtab-loc active" onclick="setDescTipoLoc('reais',this)">R$</button>
            <button type="button" class="dtab-loc" onclick="setDescTipoLoc('pct',this)">%</button>
            <button type="button" class="dtab-loc" onclick="setDescTipoLoc('cupom',this)">Cupom</button>
          </div>
          <div id="desc-loc-reais" class="desc-campo">
            <input id="l-desconto-r" type="number" min="0" step="0.01" value="0" placeholder="0,00" oninput="calcLocacao()">
          </div>
          <div id="desc-loc-pct" class="desc-campo" style="display:none">
            <input id="l-desconto-p" type="number" min="0" max="100" step="0.1" value="0" placeholder="0%" oninput="calcLocacao()">
          </div>
          <div id="desc-loc-cupom" class="desc-campo" style="display:none">
            <div style="display:flex;gap:.4rem">
              <input id="l-cupom" placeholder="Código do cupom" style="flex:1">
              <button type="button" class="btn-aplicar-cupom" onclick="aplicarCupomLoc()">Aplicar</button>
            </div>
            <div id="cupom-loc-status" style="font-size:.78rem;margin-top:.3rem"></div>
          </div>
        </div>
        <div class="preco-resumo" id="resumo-locacao" style="display:none">
          <div class="linha"><span>Valor da locação</span><span id="rl-valor">R$ 0,00</span></div>
          <div class="linha"><span>Desconto</span><span id="rl-desconto" style="color:var(--red)">—</span></div>
          <div class="linha total"><span>Total a pagar</span><span id="rl-total">R$ 0,00</span></div>
        </div>
        <div class="row2">
          <div><label>Forma de pagamento</label>
            <select id="l-pagamento">
              <option value="">Selecione…</option>
              <option>PIX</option><option>Dinheiro</option>
              <option>Cartão de débito</option><option>Cartão de crédito</option>
              <option>Boleto</option><option>Transferência</option>
            </select>
          </div>
          <div><label>Atendente</label><input id="l-atendente" placeholder="Nome do atendente"></div>
        </div>
        <div class="row2">
          <div><label>Observação</label><input id="l-obs" placeholder="Opcional"></div>
        </div>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn-cancel" onclick="closeModal('modal-op')">Cancelar</button>
      <button class="btn-vender" id="btn-confirmar-venda" onclick="confirmarVenda()">✓ Confirmar Venda</button>
      <button class="btn-locar" id="btn-confirmar-locacao" onclick="confirmarLocacao()" style="display:none">✓ Confirmar Locação</button>
    </div>
  </div>
</div>

<!-- Modal: Devolução -->
<div class="modal-bg" id="modal-dev" onclick="fecharSeFora(event,'modal-dev')">
  <div class="modal" style="max-width:480px">
    <div class="modal-header" style="border-color:rgba(23,198,41,.3)">
      <div class="ph" style="background:rgba(23,198,41,.1);font-size:1.5rem">📦</div>
      <div><h3>Registrar Devolução</h3><p id="dev-subtitle">—</p></div>
    </div>
    <div class="modal-body">
      <label>Data de devolução</label>
      <input id="dev-data" type="date" oninput="calcMultaPreview()">
      <div id="dev-multa-preview" style="margin-top:.8rem"></div>

      <div style="margin-top:1rem">
        <label style="display:block;margin-bottom:.4rem">Conferência do jogo</label>
        <div class="condicao-opcoes">
          <label class="condicao-btn" id="cond-ok-lbl">
            <input type="radio" name="condicao" id="cond-ok" value="ok" onchange="toggleAvaria()" checked>
            ✅ Tudo certo
          </label>
          <label class="condicao-btn avaria" id="cond-avaria-lbl">
            <input type="radio" name="condicao" id="cond-avaria" value="avaria" onchange="toggleAvaria()">
            ⚠️ Com avaria
          </label>
        </div>
        <div id="avaria-detalhe" style="display:none;margin-top:.6rem">
          <label>Descreva a avaria</label>
          <textarea id="dev-avaria" placeholder="Ex: caixa amassada, peças faltando, carta rasgada…" style="width:100%;background:rgba(255,255,255,.07);border:1px solid rgba(241,10,10,.4);border-radius:8px;color:white;padding:.5rem .75rem;font-family:'Nunito',sans-serif;font-size:.88rem;min-height:70px;resize:vertical;outline:none"></textarea>
        </div>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn-cancel" onclick="closeModal('modal-dev')">Cancelar</button>
      <button class="btn-vender" onclick="confirmarDevolucao()">✓ Confirmar Devolução</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let jogoAtual = null;
let tabAtual = 'venda';
let locOpcaoSel = null;
let _locGrupo   = []; // jogos acumulados no modo multi-locação
let locacaoDevId = null;
let locacaoDevDados = null;
let todosJogos = [];

const CZ_URL = "{{ cz_url }}";
const fmt = v => v!=null ? 'R$ '+Number(v).toFixed(2).replace('.',',') : '—';
const fmtData = d => d ? d.slice(0,10).split('-').reverse().join('/') : '—';
const hoje = () => new Date().toISOString().slice(0,10);

async function api(path, opts={}){
  try{
    const r = await fetch('/api'+path,{credentials:'same-origin',headers:{'Content-Type':'application/json'},...opts});
    if(!r.ok){
      try{
        const j = await r.json();
        return {error: j.error || j.erro || j.detail || ('HTTP '+r.status)};
      }catch(_){ return {error:'HTTP '+r.status}; }
    }
    return r.json();
  }catch(e){ return {error:String(e)}; }
}

function toast(msg, err=false){
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show' + (err?' err':'');
  setTimeout(()=>t.className='toast',3000);
}

function showTab(name, btn){
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('page-'+name).classList.add('active');
  if(btn){ btn.classList.add('active'); }
  if(name==='catalogo') loadCatalogo();
  if(name==='vendas') loadVendas();
  if(name==='locacoes') loadLocacoes();
  if(name==='cupons'){ loadCupons(); loadUsosCupons(); }
  if(name==='contrato-modelo') lojaLoadModeloContrato();
}

// ── Modelo de Contrato (Loja) ─────────────────────────────────────────────────
const LOJA_CAMPOS_CONTRATO = [
  // ── Cliente ──────────────────────────────────────────────────────
  ['NOME_CLIENTE','Nome do cliente'],['CPF_CLIENTE','CPF'],
  ['TELEFONE_CLIENTE','Telefone'],['ENDERECO_CLIENTE','Endereço do cliente'],
  // ── Loja ─────────────────────────────────────────────────────────
  ['NOME_LOJA','Nome da loja'],['CNPJ_LOJA','CNPJ'],
  ['ENDERECO_LOJA','Endereço da loja'],
  ['NUM_CONTRATO','Nº do contrato'],['DATA_GERACAO','Data de geração'],
  // ── 1 jogo (compatibilidade) ──────────────────────────────────────
  ['JOGO','Jogo (1º)'],['DATA_SAIDA','Data saída (1º)'],
  ['DATA_PREVISTA','Devolução (1º)'],['OPCAO_DIAS','Dias (1º)'],
  ['VALOR_LOCACAO','Valor (1º)'],['FORMA_PAGAMENTO','Pagamento (1º)'],['MULTA_DIA','Multa (1º)'],
  // ── Multi-jogo (numerados 1–5) ───────────────────────────────────
  ['JOGO_1','Jogo 1'],['VALOR_LOCACAO_1','Valor jogo 1'],['DATA_SAIDA_1','Saída jogo 1'],
  ['DATA_PREVISTA_1','Devolução jogo 1'],['OPCAO_DIAS_1','Dias jogo 1'],
  ['FORMA_PAGAMENTO_1','Pagamento jogo 1'],['MULTA_DIA_1','Multa jogo 1'],
  ['JOGO_2','Jogo 2'],['VALOR_LOCACAO_2','Valor jogo 2'],['DATA_SAIDA_2','Saída jogo 2'],
  ['DATA_PREVISTA_2','Devolução jogo 2'],['OPCAO_DIAS_2','Dias jogo 2'],
  ['FORMA_PAGAMENTO_2','Pagamento jogo 2'],['MULTA_DIA_2','Multa jogo 2'],
  ['JOGO_3','Jogo 3'],['VALOR_LOCACAO_3','Valor jogo 3'],
  ['JOGO_4','Jogo 4'],['VALOR_LOCACAO_4','Valor jogo 4'],
  ['JOGO_5','Jogo 5'],['VALOR_LOCACAO_5','Valor jogo 5'],
  // ── Totais ───────────────────────────────────────────────────────
  ['VALOR_TOTAL','Valor total (todos os jogos)'],['TOTAL_JOGOS','Quantidade de jogos'],
];

async function lojaLoadModeloContrato(){
  // Chips de campos
  const chips = document.getElementById('loja-campos-chips');
  if(chips && !chips.innerHTML){
    const ob = '{' + '{', cb = '}' + '}';
    chips.innerHTML = LOJA_CAMPOS_CONTRATO.map(([campo, label])=>
      `<button onclick="lojaInserirCampo('${ob}${campo}${cb}')"
        style="background:rgba(237,148,14,.15);border:1px solid rgba(237,148,14,.3);
               color:#ED940E;border-radius:6px;padding:3px 8px;font-size:.78rem;
               cursor:pointer;font-family:monospace"
        title="${label}">${ob}${campo}${cb}</button>`
    ).join('');
  }
  // Carrega texto do modelo
  const r = await api('/contrato-modelo');
  if(r && r.clausulas){
    document.getElementById('loja-textarea-modelo').value = r.clausulas;
  }
  // Verifica tipo de template ativo
  const rp = await api('/contrato-modelo/tem-pdf');
  lojaAtualizarStatusTemplate(rp || {tipo:'texto'});
}

function lojaAtualizarStatusTemplate(info){
  const el = document.getElementById('loja-template-status');
  if(!el) return;
  if(info.tipo==='docx')
    el.innerHTML = '✅ <strong>DOCX ativo</strong> — o sistema vai usar seu arquivo Word para gerar o contrato.';
  else if(info.tipo==='pdf')
    el.innerHTML = '📄 <strong>PDF ativo</strong> — o sistema vai usar seu PDF como base do contrato.';
  else
    el.innerHTML = '📝 <strong>Texto ativo</strong> — usando o modelo de texto do editor abaixo.';
}

async function lojaUploadDocx(){
  const inp = document.getElementById('loja-input-docx');
  if(!inp.files[0]) return;
  const fd = new FormData();
  fd.append('arquivo', inp.files[0]);
  try{
    const resp = await fetch('/api/contrato-modelo/upload-docx',{method:'POST',body:fd,credentials:'same-origin'});
    let r;
    try{ r = await resp.json(); }
    catch(_){ r = {error: 'Resposta inválida do servidor (HTTP '+resp.status+')'}; }
    if(r.ok){ toast('DOCX salvo com sucesso! ✅'); lojaAtualizarStatusTemplate({tipo:'docx'}); }
    else toast('Erro: '+(r.error||r.erro||'falha no upload'), true);
  }catch(e){ toast('Erro de rede: '+e, true); }
  inp.value = '';
}

async function lojaRemoverDocx(){
  const r = await api('/contrato-modelo/remover-docx',{method:'POST',body:'{}'});
  if(r.ok){ toast('DOCX removido'); lojaAtualizarStatusTemplate({tipo:'texto'}); }
  else toast('Erro: '+(r.error||'falha'), true);
}

async function lojaSalvarModelo(){
  const clausulas = document.getElementById('loja-textarea-modelo').value.trim();
  if(!clausulas){ alert('O modelo não pode ficar vazio.'); return; }
  const r = await api('/contrato-modelo',{method:'POST',body:JSON.stringify({clausulas})});
  if(r && r.ok) toast('Modelo salvo!');
  else toast('Erro ao salvar: '+(r&&r.error)||'falha', true);
}

function lojaInserirCampo(campo){
  const ta = document.getElementById('loja-textarea-modelo');
  const s = ta.selectionStart, e = ta.selectionEnd;
  ta.value = ta.value.slice(0,s) + campo + ta.value.slice(e);
  ta.selectionStart = ta.selectionEnd = s + campo.length;
  ta.focus();
}

function fecharModal(id){ document.getElementById(id).classList.remove('open'); }

function openModalCupom(){
  ['cp-codigo','cp-valor','cp-desc','cp-usos'].forEach(function(id){
    document.getElementById(id).value='';
  });
  document.getElementById('cp-tipo').value='pct';
  document.getElementById('modal-cupom').classList.add('open');
}

async function salvarCupom(){
  var codigo = document.getElementById('cp-codigo').value.trim().toUpperCase();
  var valor  = parseFloat(document.getElementById('cp-valor').value);
  if(!codigo){ alert('Informe o codigo do cupom'); return; }
  if(!valor || valor<=0){ alert('Informe um valor valido'); return; }
  var body = {
    codigo: codigo,
    tipo: document.getElementById('cp-tipo').value,
    valor: valor,
    descricao: document.getElementById('cp-desc').value || null,
    usos_maximos: parseInt(document.getElementById('cp-usos').value) || null
  };
  var res = await api('/cupons',{method:'POST',body:JSON.stringify(body)});
  if(res.error){ alert('Erro: '+res.error); return; }
  fecharModal('modal-cupom');
  loadCupons();
  loadUsosCupons();
}

async function desativarCupom(id){
  if(!confirm('Desativar este cupom?')) return;
  await api('/cupons/'+id+'/desativar',{method:'POST',body:'{}'});
  loadCupons();
}

async function loadCupons(){
  var rows = await api('/cupons');
  var tb = document.getElementById('tbl-cupons');
  if(!rows || !rows.length){
    tb.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:2rem;color:#555">Nenhum cupom cadastrado</td></tr>';
    return;
  }
  tb.innerHTML = rows.map(function(r){
    var tipoLabel = r.tipo==='pct' ? r.valor+'%' : 'R$ '+r.valor.toFixed(2).replace('.',',');
    var usos = r.usos_maximos ? r.usos_realizados+'/'+r.usos_maximos : r.usos_realizados+' (ilimitado)';
    var statusTxt = r.ativo ? 'Ativo' : 'Inativo';
    var statusCor = r.ativo ? 'var(--green)' : '#555';
    var btnDes = r.ativo ? '<button class="btn-danger" onclick="desativarCupom('+r.id+')">Desativar</button>' : '-';
    return '<tr>'+
      '<td><strong style="color:white;letter-spacing:.5px">'+r.codigo+'</strong></td>'+
      '<td>'+(r.tipo==='pct'?'%':'R$')+'</td>'+
      '<td style="color:var(--green);font-weight:700">'+tipoLabel+'</td>'+
      '<td style="color:#888">'+(r.descricao||'-')+'</td>'+
      '<td>'+usos+'</td>'+
      '<td><span style="color:'+statusCor+';font-weight:700">'+statusTxt+'</span></td>'+
      '<td style="color:#666;font-size:.8rem">'+(r.data_criacao||'').slice(0,10)+'</td>'+
      '<td>'+btnDes+'</td>'+
    '</tr>';
  }).join('');
}

async function loadUsosCupons(){
  var rows = await api('/cupons/usos');
  var tb = document.getElementById('tbl-cupom-usos');
  if(!rows || !rows.length){
    tb.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:2rem;color:#555">Nenhum uso registrado</td></tr>';
    return;
  }
  tb.innerHTML = rows.map(function(r){
    var tipoOp = r.tipo_operacao==='venda' ? 'Venda' : 'Locacao';
    return '<tr>'+
      '<td style="font-size:.82rem;color:#888">'+(r.data_uso||'').slice(0,16).replace('T',' ')+'</td>'+
      '<td><strong style="color:white">'+r.codigo+'</strong></td>'+
      '<td>'+tipoOp+'</td>'+
      '<td>'+(r.jogo_nome||'-')+'</td>'+
      '<td>'+(r.cliente_nome||'-')+'</td>'+
      '<td style="color:var(--red);font-weight:700">- R$ '+(r.valor_desconto||0).toFixed(2).replace('.',',')+'</td>'+
    '</tr>';
  }).join('');
}

// ── Catálogo ──────────────────────────────────────────────────────────────────
async function loadCatalogo(){
  const res = await api('/jogos');
  if(!Array.isArray(res)){
    document.getElementById('catalogo').innerHTML = '<div style="color:#fc8181;padding:1rem">Erro ao carregar jogos: '+(res&&res.error?res.error:'resposta inesperada')+'</div>';
    return;
  }
  todosJogos = res;
  popularFiltros();
  renderCatalogo(todosJogos);
}

function popularFiltros(){
  // Faixa etária
  const faixas = [...new Set(todosJogos.map(j=>j.faixa_etaria).filter(Boolean))].sort();
  const selFaixa = document.getElementById('filtro-faixa');
  selFaixa.innerHTML = '<option value="">🎂 Faixa etária</option>';
  faixas.forEach(f=> selFaixa.innerHTML += `<option value="${f}">${f}</option>`);

  // Número de jogadores
  const nums = new Set();
  todosJogos.forEach(j=>{ if(j.min_jogadores&&j.max_jogadores){ for(let n=j.min_jogadores;n<=j.max_jogadores;n++) nums.add(n); }});
  const selJog = document.getElementById('filtro-jogadores');
  selJog.innerHTML = '<option value="">👥 Nº de jogadores</option>';
  [...nums].sort((a,b)=>a-b).forEach(n=> selJog.innerHTML += `<option value="${n}">${n} jogadores</option>`);
}

function filtrar(){
  const q = document.getElementById('busca').value.toLowerCase();
  const faixa = document.getElementById('filtro-faixa').value;
  const jogadores = parseInt(document.getElementById('filtro-jogadores').value)||0;
  renderCatalogo(todosJogos.filter(j=>{
    const textoOk = j.nome.toLowerCase().includes(q)||
      (j.editora||'').toLowerCase().includes(q)||
      (j.categoria||'').toLowerCase().includes(q);
    const faixaOk = !faixa || j.faixa_etaria === faixa;
    const jogOk = !jogadores || (j.min_jogadores <= jogadores && j.max_jogadores >= jogadores);
    return textoOk && faixaOk && jogOk;
  }));
}

function renderCatalogo(jogos){
  const el = document.getElementById('catalogo');
  if(!jogos.length){
    el.innerHTML='<div class="empty"><div class="ico">🔍</div>Nenhum jogo encontrado</div>';
    return;
  }
  el.innerHTML = jogos.map(j=>{
    const semEstoque = j.quantidade <= 0;
    const baixo = j.quantidade > 0 && j.quantidade <= j.quantidade_minima;
    const badge = semEstoque
      ? `<span class="estoque-badge estoque-zero">✕ Sem estoque</span>`
      : baixo
      ? `<span class="estoque-badge estoque-baixo">⚠ ${j.quantidade} em estoque</span>`
      : `<span class="estoque-badge estoque-ok">✔ ${j.quantidade} em estoque</span>`;
    const locs = [[j.loc1_dias,j.loc1_valor],[j.loc2_dias,j.loc2_valor],[j.loc3_dias,j.loc3_valor]]
      .filter(([d,v])=>d&&v!=null)
      .map(([d,v])=>`<span class="loc-chip">${d}d — ${fmt(v)}</span>`).join('');
    const img = j.imagem
      ? `<img class="jogo-img" src="/api/imagens/${j.imagem}" alt="${j.nome}">`
      : `<div class="jogo-placeholder">🎲</div>`;
    return `
      <div class="jogo-card${semEstoque?' sem-estoque':''}" onclick="${semEstoque?'':'abrirModal('+j.id+')'}">
        ${img}
        <div class="jogo-info">
          <div class="jogo-nome">${j.nome}</div>
          <div class="jogo-meta">
            ${j.editora?`<span class="jogo-tag">${j.editora}</span>`:''}
            ${j.categoria?`<span class="jogo-tag">${j.categoria}</span>`:''}
          </div>
          <div class="jogo-precos">
            <div class="preco-label">VALOR DE VENDA</div>
            <div class="preco-venda">${fmt(j.preco_venda)}</div>
            ${locs?`<div class="loc-chips">${locs}</div>`:''}
            ${badge}
          </div>
        </div>
      </div>`;
  }).join('');
}

// ── Modal operação ─────────────────────────────────────────────────────────────
function abrirModal(id){
  jogoAtual = todosJogos.find(j=>j.id===id);
  if(!jogoAtual) return;

  const img = document.getElementById('m-img');
  const ph  = document.getElementById('m-ph');
  if(jogoAtual.imagem){ img.src='/api/imagens/'+jogoAtual.imagem; img.style.display='block'; ph.style.display='none'; }
  else { img.style.display='none'; ph.style.display='flex'; }
  document.getElementById('m-nome').textContent = jogoAtual.nome;
  document.getElementById('m-meta').textContent =
    [jogoAtual.editora, jogoAtual.categoria,
     jogoAtual.min_jogadores?jogoAtual.min_jogadores+'-'+jogoAtual.max_jogadores+' jogadores':null]
    .filter(Boolean).join(' · ');

  // Reset campos
  resetClienteForm('v');
  ['v-nome','v-cpf','v-insta','v-tel','v-obs','v-atendente',
   'v-cep','v-logradouro','v-numero','v-complemento','v-bairro','v-cidade','v-estado'
  ].forEach(id=>document.getElementById(id).value='');
  document.getElementById('v-nasc').value='';
  document.getElementById('v-qty').value=1;
  document.getElementById('v-preco').value=jogoAtual.preco_venda||'';
  document.getElementById('v-desconto-r').value=0;
  document.getElementById('v-desconto-p').value=0;
  document.getElementById('v-cupom').value='';
  document.getElementById('cupom-status').textContent='';
  cupomDesconto=0; cupomCodigo='';
  descTipo='reais';
  document.querySelectorAll('.dtab').forEach((b,i)=>b.classList.toggle('active',i===0));
  document.getElementById('desc-reais').style.display='';
  document.getElementById('desc-pct').style.display='none';
  document.getElementById('desc-cupom').style.display='none';
  document.getElementById('v-pagamento').value='';
  document.getElementById('v-data').value=hoje();
  resetClienteForm('l');
  ['l-nome','l-cpf','l-tel','l-insta','l-obs','l-atendente',
   'l-cep','l-logradouro','l-numero','l-complemento','l-bairro','l-cidade','l-estado'
  ].forEach(id=>document.getElementById(id).value='');
  document.getElementById('l-pagamento').value='';
  document.getElementById('l-nasc').value='';
  document.getElementById('l-saida').value=hoje();
  document.getElementById('l-prevista').value='';
  document.getElementById('multa-info').style.display='none';
  document.getElementById('l-desconto-r').value=0;
  document.getElementById('l-desconto-p').value=0;
  document.getElementById('l-cupom').value='';
  document.getElementById('cupom-loc-status').textContent='';
  document.getElementById('resumo-locacao').style.display='none';
  cupomDescontoLoc=0; cupomCodigoLoc='';
  descTipoLoc='reais';
  document.querySelectorAll('.dtab-loc').forEach((b,i)=>b.classList.toggle('active',i===0));
  document.getElementById('desc-loc-reais').style.display='';
  document.getElementById('desc-loc-pct').style.display='none';
  document.getElementById('desc-loc-cupom').style.display='none';
  locOpcaoSel=null;
  // Reset grupo de locação
  _locGrupo = [];
  document.getElementById('loc-grupo-section').style.display = 'none';
  document.getElementById('loc-picker-box').style.display    = 'none';
  document.getElementById('btn-add-loc-grupo').style.display = 'none';
  document.getElementById('loc-jogo-titulo').textContent     = jogoAtual.nome;

  // Opções de locação
  const ops = [[jogoAtual.loc1_dias,jogoAtual.loc1_valor],
               [jogoAtual.loc2_dias,jogoAtual.loc2_valor],
               [jogoAtual.loc3_dias,jogoAtual.loc3_valor]].filter(([d,v])=>d&&v!=null);
  const locHtml = ops.length
    ? ops.map(([d,v])=>`
        <div class="loc-op" data-dias="${d}" data-valor="${v}" onclick="selOpcao(this,${d},${v})">
          <div class="dias">${d}</div>
          <div class="dlab">dia${d>1?'s':''}</div>
          <div class="val">${fmt(v)}</div>
        </div>`).join('')
    : '<p style="color:#666;font-size:.85rem">Nenhuma opção de locação cadastrada para este jogo.</p>';
  document.getElementById('loc-opcoes').innerHTML = locHtml;

  switchTab('venda');
  calcVenda();
  document.getElementById('modal-op').classList.add('open');
}

function switchTab(tab){
  tabAtual = tab;
  document.getElementById('form-venda').style.display    = tab==='venda'?'block':'none';
  document.getElementById('form-locacao').style.display  = tab==='locacao'?'block':'none';
  document.getElementById('btn-confirmar-venda').style.display   = tab==='venda'?'':'none';
  document.getElementById('btn-confirmar-locacao').style.display = tab==='locacao'?'':'none';
  document.getElementById('tab-venda').className   = 'modal-tab'+(tab==='venda'?' active-venda':'');
  document.getElementById('tab-locacao').className = 'modal-tab'+(tab==='locacao'?' active-locacao':'');
}

let descTipo = 'reais';
let cupomDesconto = 0;
let cupomCodigo = '';
let descTipoLoc = 'reais';
let cupomDescontoLoc = 0;
let cupomCodigoLoc = '';

function setDescTipo(tipo, el){
  descTipo = tipo;
  document.querySelectorAll('.dtab').forEach(b=>b.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('desc-reais').style.display = tipo==='reais' ? '' : 'none';
  document.getElementById('desc-pct').style.display   = tipo==='pct'   ? '' : 'none';
  document.getElementById('desc-cupom').style.display = tipo==='cupom' ? '' : 'none';
  if(tipo!=='cupom'){ cupomDesconto=0; cupomCodigo=''; }
  calcVenda();
}

function calcDesconto(sub){
  if(descTipo==='reais')  return parseFloat(document.getElementById('v-desconto-r').value)||0;
  if(descTipo==='pct'){
    const p = parseFloat(document.getElementById('v-desconto-p').value)||0;
    return sub * p / 100;
  }
  if(descTipo==='cupom')  return cupomDesconto;
  return 0;
}

let cupomObj = null;
let cupomObjLoc = null;

async function aplicarCupom(){
  const codigo = document.getElementById('v-cupom').value.trim().toUpperCase();
  const status = document.getElementById('cupom-status');
  if(!codigo){ status.textContent=''; return; }
  const res = await fetch('/api/cupons/validar?codigo='+encodeURIComponent(codigo));
  if(!res.ok){
    const err = await res.json();
    cupomDesconto=0; cupomCodigo=''; cupomObj=null;
    status.innerHTML=`<span style="color:var(--red)">✘ ${err.error||'Cupom inválido'}</span>`;
    calcVenda(); return;
  }
  const c = await res.json();
  cupomObj = c; cupomCodigo = c.codigo;
  const sub = (parseFloat(document.getElementById('v-preco').value)||0)*(+document.getElementById('v-qty').value||1);
  cupomDesconto = c.tipo==='pct' ? sub*c.valor/100 : c.valor;
  const label = c.tipo==='pct' ? c.valor+'%' : 'R$ '+fmt(c.valor);
  status.innerHTML=`<span style="color:var(--green)">✔ ${c.descricao||c.codigo} — desconto de ${label}</span>`;
  calcVenda();
}

// ── Busca de cliente por CPF ────────────────────────────────────────────────
let clienteEncontradoV = null;
let clienteEncontradoL = null;

function preencherFormCliente(p, c){
  document.getElementById(p+'-nome').value        = c.nome||'';
  document.getElementById(p+'-cpf').value         = c.cpf||'';
  document.getElementById(p+'-tel').value         = c.telefone||'';
  document.getElementById(p+'-insta').value       = c.instagram||'';
  document.getElementById(p+'-nasc').value        = c.data_nascimento||'';
  document.getElementById(p+'-cep').value         = c.cep||'';
  document.getElementById(p+'-logradouro').value  = c.logradouro||'';
  document.getElementById(p+'-numero').value      = c.numero||'';
  document.getElementById(p+'-complemento').value = c.complemento||'';
  document.getElementById(p+'-bairro').value      = c.bairro||'';
  document.getElementById(p+'-cidade').value      = c.cidade||'';
  document.getElementById(p+'-estado').value      = c.estado||'';
}

function mascDDMM(el){
  let v = el.value.replace(/\D/g,'');
  if(v.length > 2) v = v.slice(0,2) + '/' + v.slice(2,4);
  el.value = v;
}

let _cepTimer = {};
async function buscarCEP(p){
  const cep = document.getElementById(p+'-cep').value.replace(/\D/g,'');
  clearTimeout(_cepTimer[p]);
  if(cep.length < 8) return;
  _cepTimer[p] = setTimeout(async ()=>{
    try{
      const r = await fetch('https://viacep.com.br/ws/'+cep+'/json/');
      const d = await r.json();
      if(d.erro) return;
      document.getElementById(p+'-logradouro').value = d.logradouro||'';
      document.getElementById(p+'-bairro').value     = d.bairro||'';
      document.getElementById(p+'-cidade').value     = d.localidade||'';
      document.getElementById(p+'-estado').value     = d.uf||'';
      document.getElementById(p+'-numero').focus();
    }catch(e){}
  }, 500);
}

function mostrarClienteEncontrado(p, c){
  const detalhes = [c.cpf, c.telefone, c.instagram].filter(Boolean).join(' · ');
  document.getElementById(p+'-ce-nome').textContent = c.nome;
  document.getElementById(p+'-ce-detalhe').textContent = detalhes;
  document.getElementById(p+'-cliente-encontrado').style.display = 'flex';
  document.getElementById(p+'-cliente-form').style.display = 'none';
  document.getElementById(p+'-btn-editar').style.display = '';
  preencherFormCliente(p, c);
  if(p==='v') clienteEncontradoV = c; else clienteEncontradoL = c;
}

function editarCliente(p){
  document.getElementById(p+'-cliente-encontrado').style.display = 'none';
  document.getElementById(p+'-cliente-form').style.display = '';
  document.getElementById(p+'-btn-editar').style.display = 'none';
}

function resetClienteForm(p){
  document.getElementById(p+'-cliente-encontrado').style.display = 'none';
  document.getElementById(p+'-cliente-form').style.display = '';
  document.getElementById(p+'-btn-editar').style.display = 'none';
  if(p==='v') clienteEncontradoV=null; else clienteEncontradoL=null;
}

let _cpfTimer = {};
async function buscarClienteCPF(p, forcar=false){
  const cpf = document.getElementById(p+'-cpf').value.replace(/\D/g,'');
  clearTimeout(_cpfTimer[p]);
  if(cpf.length < 11 && !forcar) return;
  _cpfTimer[p] = setTimeout(async ()=>{
    const c = await api('/loja/cliente?cpf='+cpf);
    if(c && c.id){ mostrarClienteEncontrado(p, c); toast(`✔ Cliente encontrado: ${c.nome}`); }
    else if(forcar){ toast('CPF não encontrado. Preencha os dados.',true); }
  }, forcar ? 0 : 400);
}

function setDescTipoLoc(tipo, el){
  descTipoLoc = tipo;
  document.querySelectorAll('.dtab-loc').forEach(b=>b.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('desc-loc-reais').style.display = tipo==='reais' ? '' : 'none';
  document.getElementById('desc-loc-pct').style.display   = tipo==='pct'   ? '' : 'none';
  document.getElementById('desc-loc-cupom').style.display = tipo==='cupom' ? '' : 'none';
  if(tipo!=='cupom'){ cupomDescontoLoc=0; cupomCodigoLoc=''; }
  calcLocacao();
}

function calcDescontoLoc(base){
  if(descTipoLoc==='reais') return parseFloat(document.getElementById('l-desconto-r').value)||0;
  if(descTipoLoc==='pct'){
    const p = parseFloat(document.getElementById('l-desconto-p').value)||0;
    return base * p / 100;
  }
  if(descTipoLoc==='cupom') return cupomDescontoLoc;
  return 0;
}

async function aplicarCupomLoc(){
  const codigo = document.getElementById('l-cupom').value.trim().toUpperCase();
  const status = document.getElementById('cupom-loc-status');
  if(!codigo){ status.textContent=''; return; }
  const res = await fetch('/api/cupons/validar?codigo='+encodeURIComponent(codigo));
  if(!res.ok){
    const err = await res.json();
    cupomDescontoLoc=0; cupomCodigoLoc=''; cupomObjLoc=null;
    status.innerHTML=`<span style="color:var(--red)">✘ ${err.error||'Cupom inválido'}</span>`;
    calcLocacao(); return;
  }
  const c = await res.json();
  cupomObjLoc = c; cupomCodigoLoc = c.codigo;
  const base = locOpcaoSel ? locOpcaoSel.valor : 0;
  cupomDescontoLoc = c.tipo==='pct' ? base*c.valor/100 : c.valor;
  const label = c.tipo==='pct' ? c.valor+'%' : 'R$ '+fmt(c.valor);
  status.innerHTML=`<span style="color:var(--green)">✔ ${c.descricao||c.codigo} — desconto de ${label}</span>`;
  calcLocacao();
}

function calcLocacao(){
  const grupoTotal = _locGrupo.reduce((s,i)=>s+i.valor, 0);
  const base = grupoTotal + (locOpcaoSel ? locOpcaoSel.valor : 0);
  if(!base) return;
  const desc = Math.min(calcDescontoLoc(base), base);
  const total = Math.max(0, base - desc);
  document.getElementById('resumo-locacao').style.display = 'block';
  document.getElementById('rl-valor').textContent = fmt(base);
  document.getElementById('rl-desconto').textContent = desc>0 ? '- '+fmt(desc) : '—';
  document.getElementById('rl-total').textContent = fmt(total);
}

function calcVenda(){
  const qty   = +document.getElementById('v-qty').value||1;
  const preco = parseFloat(document.getElementById('v-preco').value)||0;
  const sub   = preco*qty;
  const desc  = Math.min(calcDesconto(sub), sub);
  const total = Math.max(0, sub-desc);
  document.getElementById('r-subtotal').textContent = fmt(sub);
  document.getElementById('r-desconto').textContent = desc>0 ? '- '+fmt(desc) : '—';
  document.getElementById('r-total').textContent = fmt(total);
}

function selOpcao(el, dias, valor){
  document.querySelectorAll('.loc-op').forEach(o=>o.classList.remove('sel'));
  el.classList.add('sel');
  locOpcaoSel = {dias, valor};
  document.getElementById('btn-add-loc-grupo').style.display = 'block';
  calcDevolucao();
  calcLocacao();
  if(jogoAtual?.multa_dia){
    document.getElementById('multa-info').style.display='block';
    document.getElementById('multa-info').textContent =
      `⚠️ Multa por atraso: ${fmt(jogoAtual.multa_dia)}/dia`;
  }
}

function calcDevolucao(){
  if(!locOpcaoSel) return;
  const saida = document.getElementById('l-saida').value;
  if(!saida) return;
  const prev = new Date(saida+'T12:00:00');
  prev.setDate(prev.getDate()+locOpcaoSel.dias);
  const iso = prev.toISOString().slice(0,10);
  const [y,m,d] = iso.split('-');
  document.getElementById('l-prevista').value = `${d}/${m}/${y}`;
}

async function confirmarVenda(){
  if(!jogoAtual){ toast('Nenhum jogo selecionado',true); return; }
  const qty = +document.getElementById('v-qty').value||1;
  const preco = parseFloat(document.getElementById('v-preco').value)||0;
  const sub = preco*qty;
  const desconto = Math.min(calcDesconto(sub), sub);
  const obsBase = document.getElementById('v-obs').value||'';
  const obsDesc = cupomCodigo ? `Cupom: ${cupomCodigo}` : (descTipo==='pct'&&desconto>0?`Desconto ${document.getElementById('v-desconto-p').value}%`:'');
  const body = {
    jogo_id: jogoAtual.id,
    quantidade: qty,
    preco_unitario: preco||null,
    desconto,
    forma_pagamento: document.getElementById('v-pagamento').value||null,
    data_venda: document.getElementById('v-data').value,
    atendente: document.getElementById('v-atendente').value||null,
    observacao: [obsBase, obsDesc].filter(Boolean).join(' | ')||null,
    cliente:{
      nome: document.getElementById('v-nome').value,
      cpf: document.getElementById('v-cpf').value,
      data_nascimento: document.getElementById('v-nasc').value||null,
      instagram: document.getElementById('v-insta').value||null,
      telefone: document.getElementById('v-tel').value||null,
      cep: document.getElementById('v-cep').value||null,
      logradouro: document.getElementById('v-logradouro').value||null,
      numero: document.getElementById('v-numero').value||null,
      complemento: document.getElementById('v-complemento').value||null,
      bairro: document.getElementById('v-bairro').value||null,
      cidade: document.getElementById('v-cidade').value||null,
      estado: document.getElementById('v-estado').value||null,
    }
  };
  const res = await api('/loja/venda',{method:'POST',body:JSON.stringify(body)});
  if(res.error){ toast(res.error,true); return; }
  if(cupomObj && desconto>0){
    await api('/cupons/usar',{method:'POST',body:JSON.stringify({
      cupom_id: cupomObj.id, tipo_operacao:'venda', referencia_id: res.venda_id||null,
      valor_desconto: desconto,
      cliente_nome: document.getElementById('v-nome').value||null,
      jogo_nome: jogoAtual.nome
    })});
  }
  closeModal('modal-op');
  toast(`✔ Venda registrada! Total: ${fmt(res.valor_final)}`);
  loadCatalogo();
}

// ── Multi-locação ────────────────────────────────────────────────────────────
function renderLocGrupo(){
  const sec   = document.getElementById('loc-grupo-section');
  const lista = document.getElementById('loc-grupo-lista');
  if(!_locGrupo.length){ sec.style.display='none'; return; }
  sec.style.display = 'block';
  lista.innerHTML = _locGrupo.map((item,i)=>`
    <div style="background:#16213e;border:1px solid rgba(255,255,255,.08);border-radius:8px;
                padding:.4rem .75rem;display:flex;align-items:center;justify-content:space-between">
      <div>
        <span style="font-weight:700;font-size:.85rem">${item.jogo.nome}</span>
        <span style="color:var(--muted);font-size:.77rem;margin-left:.5rem">
          ${item.dias} dia${item.dias>1?'s':''} — ${fmt(item.valor)}</span>
      </div>
      <button onclick="removerDoGrupo(${i})"
        style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:.85rem;padding:.2rem .4rem">✕</button>
    </div>`).join('');
}

function adicionarJogoAoGrupo(){
  if(!locOpcaoSel){ toast('Selecione uma opção de locação primeiro',true); return; }
  _locGrupo.push({jogo:jogoAtual, dias:locOpcaoSel.dias, valor:locOpcaoSel.valor});
  jogoAtual=null; locOpcaoSel=null;
  document.getElementById('loc-opcoes').innerHTML    = '';
  document.getElementById('btn-add-loc-grupo').style.display = 'none';
  document.getElementById('resumo-locacao').style.display    = 'none';
  document.getElementById('multa-info').style.display        = 'none';
  renderLocGrupo();
  calcLocacao();
  // Abre picker para o próximo jogo
  const jaNoGrupo   = _locGrupo.map(i=>i.jogo.id);
  const disponiveis = todosJogos.filter(j=>j.quantidade>0 && !jaNoGrupo.includes(j.id));
  const sel = document.getElementById('loc-select-jogo');
  sel.innerHTML = '<option value="">— Selecione o jogo —</option>' +
    disponiveis.map(j=>`<option value="${j.id}">${j.nome}</option>`).join('');
  sel.value = '';
  document.getElementById('loc-picker-box').style.display  = 'block';
  document.getElementById('loc-jogo-titulo').textContent   = 'Opção de locação';
}

function removerDoGrupo(idx){
  _locGrupo.splice(idx,1);
  renderLocGrupo();
  calcLocacao();
}

function trocarJogoLocacao(id){
  if(!id) return;
  jogoAtual = todosJogos.find(j=>j.id==id);
  if(!jogoAtual) return;
  locOpcaoSel = null;
  document.getElementById('loc-jogo-titulo').textContent    = jogoAtual.nome;
  document.getElementById('loc-picker-box').style.display   = 'none';
  document.getElementById('btn-add-loc-grupo').style.display = 'none';
  const ops = [[jogoAtual.loc1_dias,jogoAtual.loc1_valor],
               [jogoAtual.loc2_dias,jogoAtual.loc2_valor],
               [jogoAtual.loc3_dias,jogoAtual.loc3_valor]].filter(([d,v])=>d&&v!=null);
  document.getElementById('loc-opcoes').innerHTML = ops.length
    ? ops.map(([d,v])=>`
        <div class="loc-op" data-dias="${d}" data-valor="${v}" onclick="selOpcao(this,${d},${v})">
          <div class="dias">${d}</div><div class="dlab">dia${d>1?'s':''}</div>
          <div class="val">${fmt(v)}</div></div>`).join('')
    : '<p style="color:#666;font-size:.85rem">Nenhuma opção de locação cadastrada.</p>';
}

async function confirmarLocacao(){
  // Coleta todos os jogos: grupo acumulado + jogo atual (se opção selecionada)
  const todos = [..._locGrupo];
  if(jogoAtual && locOpcaoSel) todos.push({jogo:jogoAtual, dias:locOpcaoSel.dias, valor:locOpcaoSel.valor});

  if(!todos.length){
    if(!jogoAtual){ toast('Nenhum jogo selecionado',true); return; }
    toast('Selecione uma opção de locação',true); return;
  }

  const clienteData = {
    nome:            document.getElementById('l-nome').value,
    cpf:             document.getElementById('l-cpf').value,
    data_nascimento: document.getElementById('l-nasc').value||null,
    instagram:       document.getElementById('l-insta').value||null,
    telefone:        document.getElementById('l-tel').value||null,
    cep:             document.getElementById('l-cep').value||null,
    logradouro:      document.getElementById('l-logradouro').value||null,
    numero:          document.getElementById('l-numero').value||null,
    complemento:     document.getElementById('l-complemento').value||null,
    bairro:          document.getElementById('l-bairro').value||null,
    cidade:          document.getElementById('l-cidade').value||null,
    estado:          document.getElementById('l-estado').value||null,
  };

  if(todos.length === 1 && _locGrupo.length === 0){
    // ── Fluxo single (original — com suporte a desconto/cupom) ─────────
    const base = locOpcaoSel.valor;
    const desc = Math.min(calcDescontoLoc(base), base);
    const valorFinal = Math.max(0, base - desc);
    const obsBase = document.getElementById('l-obs').value||'';
    const obsDesc = cupomCodigoLoc ? `Cupom: ${cupomCodigoLoc}` : (descTipoLoc==='pct'&&desc>0?`Desconto ${document.getElementById('l-desconto-p').value}%`:'');
    const body = {
      jogo_id: jogoAtual.id, opcao_dias: locOpcaoSel.dias, valor_locacao: valorFinal,
      data_saida: document.getElementById('l-saida').value,
      forma_pagamento: document.getElementById('l-pagamento').value||null,
      atendente: document.getElementById('l-atendente').value||null,
      observacao: [obsBase,obsDesc].filter(Boolean).join(' | ')||null,
      cliente: clienteData,
    };
    const res = await api('/loja/locacao',{method:'POST',body:JSON.stringify(body)});
    if(res.error){ toast(res.error,true); return; }
    if(cupomObjLoc && desc>0){
      await api('/cupons/usar',{method:'POST',body:JSON.stringify({
        cupom_id: cupomObjLoc.id, tipo_operacao:'locacao', referencia_id: res.locacao_id||null,
        valor_desconto: desc, cliente_nome: clienteData.nome, jogo_nome: jogoAtual.nome
      })});
    }
    closeModal('modal-op'); _locGrupo=[];
    toast(`✔ Locação registrada! Devolução em ${fmtData(res.data_prevista)}`);
    loadCatalogo();
    setTimeout(loadLocacoes, 2500); // aguarda o envio automático do contrato
  } else {
    // ── Fluxo batch (múltiplos jogos — um único contrato) ───────────────
    const body = {
      jogos: todos.map(item=>({
        jogo_id: item.jogo.id, opcao_dias: item.dias, valor_locacao: item.valor,
      })),
      data_saida:      document.getElementById('l-saida').value,
      forma_pagamento: document.getElementById('l-pagamento').value||null,
      atendente:       document.getElementById('l-atendente').value||null,
      observacao:      document.getElementById('l-obs').value||null,
      cliente:         clienteData,
    };
    const res = await api('/loja/locacao/grupo',{method:'POST',body:JSON.stringify(body)});
    if(res.error){ toast(res.error,true); return; }
    closeModal('modal-op'); _locGrupo=[];
    toast(`✔ ${todos.length} locações registradas! Devolução em ${fmtData(res.data_prevista)}`);
    loadCatalogo();
    setTimeout(loadLocacoes, 2500); // aguarda o envio automático do contrato
  }
}

// ── Devoluções ─────────────────────────────────────────────────────────────────
function abrirDevolucao(id, dados){
  locacaoDevId = id;
  locacaoDevDados = dados;
  document.getElementById('dev-subtitle').textContent =
    `${dados.jogo_nome} — ${dados.cliente_nome||'Cliente não identificado'}`;
  document.getElementById('dev-data').value = hoje();
  document.getElementById('cond-ok').checked = true;
  document.getElementById('avaria-detalhe').style.display = 'none';
  document.getElementById('dev-avaria').value = '';
  calcMultaPreview();
  document.getElementById('modal-dev').classList.add('open');
}

function toggleAvaria(){
  const avaria = document.getElementById('cond-avaria').checked;
  document.getElementById('avaria-detalhe').style.display = avaria ? 'block' : 'none';
}

function calcMultaPreview(){
  if(!locacaoDevDados) return;
  const devData = document.getElementById('dev-data').value;
  if(!devData) return;
  const prevista  = new Date(locacaoDevDados.data_prevista+'T12:00:00');
  const devolvida = new Date(devData+'T12:00:00');
  const atraso = Math.max(0, Math.round((devolvida-prevista)/(1000*60*60*24)));
  const multaDia = locacaoDevDados.multa_dia||0;
  const multa = atraso * multaDia;
  const el = document.getElementById('dev-multa-preview');
  if(atraso>0 && multaDia>0){
    el.innerHTML=`<div class="multa-preview">⚠️ ${atraso} dia(s) de atraso — Multa: <strong>${fmt(multa)}</strong></div>`;
  } else if(atraso>0){
    el.innerHTML=`<div class="multa-preview" style="background:rgba(237,148,14,.1);border-color:var(--orange);color:var(--orange)">⚠️ ${atraso} dia(s) de atraso (sem multa cadastrada)</div>`;
  } else {
    el.innerHTML=`<div style="color:var(--green);font-size:.85rem">✔ Devolução dentro do prazo</div>`;
  }
}

async function confirmarDevolucao(){
  const condicao = document.querySelector('input[name="condicao"]:checked')?.value || 'ok';
  const res = await api('/loja/devolucao',{method:'POST',body:JSON.stringify({
    locacao_id: locacaoDevId,
    data_devolucao: document.getElementById('dev-data').value,
    condicao_devolucao: condicao,
    avaria_descricao: condicao==='avaria' ? document.getElementById('dev-avaria').value||null : null
  })});
  if(res.error){ toast(res.error,true); return; }
  closeModal('modal-dev');
  const msg = res.valor_multa>0
    ? `✔ Devolução registrada. Multa: ${fmt(res.valor_multa)}`
    : '✔ Devolução registrada no prazo!';
  toast(msg);
  loadLocacoes(); loadCatalogo();
}

// ── Históricos ────────────────────────────────────────────────────────────────
async function loadVendas(){
  const rows = await api('/loja/vendas');
  const tbody = document.getElementById('body-vendas');
  if(!rows.length){
    tbody.innerHTML='<tr><td colspan="13" class="empty">Nenhuma venda registrada</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(r=>{
    const qty       = r.quantidade||1;
    const custo     = r.custo_unitario!=null ? r.custo_unitario*qty : null;
    const total     = r.valor_final||0;
    const margem    = custo!=null ? total - custo : null;
    const pct       = (margem!=null && total>0) ? (margem/total*100) : null;
    const custoHtml = custo!=null
      ? `<span style="color:#fc8181">${fmt(custo)}</span>`
      : '<span style="color:#555">—</span>';
    const margemHtml = margem!=null
      ? `<span style="color:${margem>=0?'var(--green)':'var(--red)'};font-weight:700">${margem>=0?'+':''}${fmt(margem)}</span>`
      : '<span style="color:#555">—</span>';
    const pctHtml = pct!=null
      ? `<span style="color:${pct>=0?'var(--green)':'var(--red)'};font-weight:700">${pct.toFixed(1)}%</span>`
      : '<span style="color:#555">—</span>';
    const btnAvaliacaoVenda = `<a class="btn-whatsapp" style="background:#4285F4;font-size:.75rem" href="${gerarLinkAvaliacaoCliente(r.cliente_tel,r.cliente_nome,'venda')}" target="_blank">⭐ Avaliação</a>`;
    const btnEditVenda = `<button class="btn-devolver" style="font-size:.75rem" onclick="abrirEdicao('venda',${r.id},'${r.forma_pagamento||''}','${(r.atendente||'').replace(/'/g,"\\'")}','${(r.observacao||'').replace(/'/g,"\\'")}')">✏️</button>`;
    const recVenda = r.status_pagamento==='recebido';
    const badgeRecVenda = recVenda
      ? `<span class="badge-recebido">✅ Recebido</span>`
      : `<span class="badge-pendente">💳 Pendente</span>`;
    const btnToggleVenda = recVenda
      ? `<button class="btn-devolver" style="font-size:.7rem;padding:2px 6px" onclick="toggleRecebimento('venda',${r.id},false)">Desfazer</button>`
      : `<button class="btn-devolver" style="font-size:.7rem;padding:2px 6px;color:var(--green);border-color:var(--green)" onclick="toggleRecebimento('venda',${r.id},true)">Marcar</button>`;
    return `
    <tr>
      <td>${fmtData(r.data_venda)}</td>
      <td><strong style="color:white">${r.jogo_nome}</strong></td>
      <td>${r.cliente_nome||'<span style="color:#555">—</span>'}</td>
      <td>${r.atendente||'<span style="color:#555">—</span>'}</td>
      <td>${r.quantidade}</td>
      <td>${fmt(r.preco_unitario)}</td>
      <td style="color:var(--red)">${r.desconto?'- '+fmt(r.desconto):'—'}</td>
      <td style="color:var(--green);font-weight:700">${fmt(r.valor_final)}</td>
      <td>${custoHtml}</td>
      <td>${margemHtml}</td>
      <td>${pctHtml}</td>
      <td>${r.forma_pagamento||'—'}</td>
      <td style="display:flex;gap:.3rem;align-items:center;flex-wrap:wrap">${badgeRecVenda}${btnToggleVenda}</td>
      <td style="display:flex;gap:.3rem;flex-wrap:wrap">${btnEditVenda}${btnAvaliacaoVenda}</td>
    </tr>`;
  }).join('');
}

async function _checarPendentes(rows){
  const pendentes = rows.filter(r => r.contrato_status === 'pending');
  if(!pendentes.length) return;
  for(const r of pendentes){
    const res = await api('/loja/locacoes/'+r.id+'/contrato-status');
    if(res && res.status === 'signed'){
      loadLocacoes(); // recarrega tudo ao encontrar o primeiro assinado
      return;
    }
  }
}

async function loadLocacoes(){
  const rows = await api('/loja/locacoes');
  const tbody = document.getElementById('body-locacoes');
  if(!rows.length){
    tbody.innerHTML='<tr><td colspan="10" class="empty">Nenhuma locação registrada</td></tr>';
    return;
  }
  const hoje = new Date().toISOString().slice(0,10);
  tbody.innerHTML = rows.map(r=>{
    const atrasado = r.status==='ativa' && r.data_prevista < hoje;
    const statusHtml = r.status==='devolvido'
      ? `<span class="status-devolvido">✔ Devolvido ${fmtData(r.data_devolucao)}</span>`
      : atrasado
      ? `<span class="status-atrasado">⚠ Atrasado</span>`
      : `<span class="status-ativa">🔑 Ativa</span>`;
    const dadosDev = JSON.stringify({
      jogo_nome:r.jogo_nome, cliente_nome:r.cliente_nome,
      data_prevista:r.data_prevista, multa_dia:r.multa_dia
    });
    const btnDev = r.status==='ativa'
      ? `<button class="btn-devolver" onclick='abrirDevolucao(${r.id},${dadosDev})'>Devolvido</button>` : '—';

    const telLimpo = (r.cliente_tel||'').replace(/\D/g,'');
    const btnAvaliacaoLoc = `<a class="btn-whatsapp" style="background:#4285F4" href="${gerarLinkAvaliacaoCliente(r.cliente_tel,r.cliente_nome,'locacao')}" target="_blank">⭐ Avaliação</a>`;
    const btnEditLoc = `<button class="btn-devolver" style="font-size:.75rem" onclick="abrirEdicao('locacao',${r.id},'${r.forma_pagamento||''}','${(r.atendente||'').replace(/'/g,"\\'")}','${(r.observacao||'').replace(/'/g,"\\'")}',${r.opcao_dias||0},'${r.data_saida||''}')">✏️</button>`;
    const btnExcluirLoc = `<button class="btn-devolver" style="font-size:.75rem;color:var(--red);border-color:var(--red)" onclick="excluirLocacao(${r.id},'${(r.jogo_nome||'').replace(/'/g,"\\'")}','${(r.cliente_nome||'').replace(/'/g,"\\'")}')">🗑️</button>`;

    // Condição do jogo na devolução
    const condicaoHtml = r.condicao_devolucao==='avaria'
      ? `<span style="color:var(--red);font-weight:700" title="${r.avaria_descricao||''}">⚠️ Avaria</span>`
      : r.condicao_devolucao==='ok'
      ? `<span style="color:var(--green)">✅ OK</span>`
      : '—';

    const recLoc = r.status_pagamento==='recebido';
    const badgeRecLoc = recLoc
      ? `<span class="badge-recebido">✅ Recebido</span>`
      : ``;
    const btnToggleLoc = recLoc
      ? `<button class="btn-devolver" style="font-size:.7rem;padding:2px 6px" onclick="toggleRecebimento('locacao',${r.id},false)">Desfazer</button>`
      : ``;

    // Botão contrato ClicksZap
    const btnContrato = renderBtnContrato(r);

    return `
      <tr>
        <td><strong style="color:white">${r.jogo_nome}</strong></td>
        <td>${r.cliente_nome||'—'}<br><span style="color:#666;font-size:.75rem">${r.cliente_tel||''}</span></td>
        <td>${r.atendente||'<span style="color:#555">—</span>'}</td>
        <td>${fmtData(r.data_saida)}</td>
        <td class="${atrasado?'status-atrasado':''}">${fmtData(r.data_prevista)}</td>
        <td>${statusHtml}</td>
        <td style="color:var(--purple)">${fmt(r.valor_locacao)}</td>
        <td>${r.forma_pagamento||'—'}</td>
        <td style="color:var(--red)">${r.valor_multa?fmt(r.valor_multa):'—'}</td>
        <td>${condicaoHtml}</td>
        <td style="display:flex;gap:.3rem;align-items:center;flex-wrap:wrap">${badgeRecLoc}${btnToggleLoc}</td>
        <td style="display:flex;gap:.4rem;flex-wrap:wrap;align-items:center">${btnEditLoc}${btnExcluirLoc}${btnDev}${btnContrato}${btnAvaliacaoLoc}</td>
      </tr>`;
  }).join('');
  // Checa silenciosamente se algum contrato pendente já foi assinado
  setTimeout(() => _checarPendentes(rows), 1500);
}

async function excluirLocacao(id, jogoNome, clienteNome){
  const msg = `Excluir a locação de "${jogoNome}" para ${clienteNome||'cliente'}?\n\nSe a locação estiver ativa, o jogo volta ao estoque.`;
  if(!confirm(msg)) return;
  const res = await api('/loja/locacao/'+id, {method:'DELETE'});
  if(res && res.ok){ toast('Locação excluída'); loadLocacoes(); }
  else toast((res&&res.error)||'Erro ao excluir', true);
}

async function toggleRecebimento(tipo, id, recebido){
  const res = await api('/recebimento/manual', {
    method:'POST',
    body: JSON.stringify({tipo, ref_id:id, recebido})
  });
  if(res && res.ok){ loadVendas(); loadLocacoes(); }
  else toast((res&&res.error)||'Erro ao atualizar recebimento', true);
}

function renderBtnContrato(r){
  var cs = r.contrato_status;
  if(cs === 'signed'){
    return '<a class="btn-whatsapp" style="background:#17C629;font-size:.75rem" href="'+CZ_URL+'/s/'+r.contrato_token+'/download" target="_blank">📄 Assinado</a>';
  }
  if(cs === 'pending'){
    return '<button class="btn-devolver" style="font-size:.75rem;color:#ED940E;border-color:#ED940E" onclick="verStatusContrato('+r.id+')">⏳ Aguardando</button>';
  }
  // Não enviado ainda
  return '<button class="btn-devolver" style="font-size:.75rem;color:#7B20E1;border-color:#7B20E1" onclick="enviarContrato('+r.id+')">📝 Contrato</button>';
}

async function enviarContrato(locacaoId){
  if(!confirm('Gerar e enviar o contrato via WhatsApp para o cliente?')) return;
  toast('Gerando contrato...');
  var res = await api('/loja/locacoes/'+locacaoId+'/enviar-contrato',{method:'POST',body:'{}'});
  if(res.error){ toast(res.error, true); return; }
  toast('Contrato enviado pelo WhatsApp!');
  loadLocacoes();
}

async function verStatusContrato(locacaoId){
  var res = await api('/loja/locacoes/'+locacaoId+'/contrato-status');
  if(res.error){ toast(res.error, true); return; }
  if(res.status === 'signed'){
    toast('Contrato assinado!');
    loadLocacoes();
  } else if(res.status === 'pending'){
    if(confirm('Contrato aguardando assinatura. Reenviar link pelo WhatsApp?')){
      window.open(res.signing_link, '_blank');
    }
  } else {
    toast('Status: '+(res.status||'desconhecido'), true);
  }
}

// ── Configuração — altere para a URL da sua página de avaliações do Google ──
const GOOGLE_REVIEW_URL = 'https://g.page/r/CSq-394orJrWECA/review';

function msgAvaliacao(nome){
  return `Olá, ${nome||'cliente'}! 😊

Aproveitamos para pedir que nos avalie no Google pelo link abaixo. Sua avaliação é bem importante pra nós. Obrigada desde já!

${GOOGLE_REVIEW_URL}`;
}

function gerarLinkAvaliacao(nomeCliente){
  return `https://wa.me/?text=${encodeURIComponent(msgAvaliacao(nomeCliente))}`;
}

function gerarLinkAvaliacaoCliente(tel, nomeCliente){
  const telLimpo = (tel||'').replace(/\D/g,'');
  const ddi = telLimpo.startsWith('55') ? telLimpo : '55'+telLimpo;
  const base = telLimpo ? `https://wa.me/${ddi}` : `https://wa.me/`;
  return `${base}?text=${encodeURIComponent(msgAvaliacao(nomeCliente))}`;
}


function closeModal(id){ document.getElementById(id).classList.remove('open'); }
function fecharSeFora(e,id){ if(e.target===document.getElementById(id)) closeModal(id); }

// ── Editar venda / locação ──────────────────────────────────────────────────
let editTipo = null;
let editId   = null;

function abrirEdicao(tipo, id, forma_pagamento, atendente, observacao, opcao_dias, data_saida){
  editTipo = tipo;
  editId   = id;
  document.getElementById('edit-titulo').textContent = tipo === 'venda' ? '✏️ Editar Venda' : '✏️ Editar Locação';
  document.getElementById('edit-pagamento').value   = forma_pagamento || '';
  document.getElementById('edit-atendente').value   = atendente || '';
  document.getElementById('edit-obs').value         = observacao || '';
  // Campo de dias só aparece para locação
  const diasRow = document.getElementById('edit-dias-row');
  diasRow.style.display = tipo === 'locacao' ? '' : 'none';
  if(tipo === 'locacao'){
    document.getElementById('edit-dias').value      = opcao_dias || '';
    document.getElementById('edit-data-saida').value = data_saida || '';
  }
  document.getElementById('modal-edit').classList.add('open');
}

async function salvarEdicao(){
  const btn = document.getElementById('btn-salvar-edit');
  btn.disabled = true; btn.textContent = 'Salvando…';
  const body = {
    forma_pagamento: document.getElementById('edit-pagamento').value || null,
    atendente:       document.getElementById('edit-atendente').value || null,
    observacao:      document.getElementById('edit-obs').value || null,
  };
  if(editTipo === 'locacao'){
    body.opcao_dias  = parseInt(document.getElementById('edit-dias').value) || null;
    body.data_saida  = document.getElementById('edit-data-saida').value || null;
  }
  const res = await fetch(`/api/loja/${editTipo}/${editId}`, {
    method: 'PATCH',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(body)
  });
  const data = await res.json();
  btn.disabled = false; btn.textContent = 'Salvar';
  closeModal('modal-edit');
  if(editTipo==='venda') loadVendas(); else loadLocacoes();
}

loadCatalogo();
</script>

<!-- Modal Editar Venda/Locação -->
<div class="modal-bg" id="modal-edit" onclick="fecharSeFora(event,'modal-edit')">
  <div class="modal" style="max-width:420px">
    <div class="modal-header">
      <div class="ph" style="font-size:1.4rem">✏️</div>
      <div><h3 id="edit-titulo">Editar</h3></div>
    </div>
    <div class="modal-body">
      <div id="edit-dias-row" style="display:none">
        <div class="row2" style="margin-bottom:.8rem">
          <div><label>Dias de locação</label>
            <input id="edit-dias" type="number" min="1" placeholder="Ex: 5">
          </div>
          <div><label>Data de saída</label>
            <input id="edit-data-saida" type="date">
          </div>
        </div>
        <p style="font-size:.75rem;color:var(--orange);margin-bottom:.8rem">⚠️ A devolução prevista será recalculada automaticamente.</p>
      </div>
      <label>Forma de pagamento</label>
      <select id="edit-pagamento">
        <option value="">Selecione…</option>
        <option>PIX</option><option>Dinheiro</option>
        <option>Cartão de débito</option><option>Cartão de crédito</option>
        <option>Boleto</option><option>Transferência</option>
      </select>
      <label>Atendente</label>
      <input id="edit-atendente" placeholder="Nome do atendente">
      <label>Observação</label>
      <input id="edit-obs" placeholder="Opcional">
    </div>
    <div class="modal-footer">
      <button class="btn-cancel" onclick="closeModal('modal-edit')">Cancelar</button>
      <button class="btn-confirm" id="btn-salvar-edit" onclick="salvarEdicao()">Salvar</button>
    </div>
  </div>
</div>

</body>
</html>"""


@app.route("/loja")
def loja():
    return render_template_string(LOJA_HTML, cz_url=ct.CLICKSZAP_URL)


@app.route("/api/loja/venda/<int:venda_id>", methods=["PATCH"])
def api_editar_venda(venda_id):
    from database import get_connection
    d = request.get_json()
    with get_connection() as conn:
        conn.execute("""UPDATE vendas SET forma_pagamento=?, atendente=?, observacao=?
                        WHERE id=?""",
                     (d.get("forma_pagamento"), d.get("atendente"), d.get("observacao"), venda_id))
    return jsonify({"ok": True})


@app.route("/api/loja/locacao/<int:locacao_id>", methods=["PATCH"])
def api_editar_locacao(locacao_id):
    from database import get_connection
    from datetime import date, timedelta
    d = request.get_json()
    with get_connection() as conn:
        loc = conn.execute("SELECT data_saida, opcao_dias FROM locacoes WHERE id=?", (locacao_id,)).fetchone()
        data_saida  = d.get("data_saida") or loc["data_saida"]
        opcao_dias  = d.get("opcao_dias") or loc["opcao_dias"]
        data_prevista = (date.fromisoformat(data_saida) + timedelta(days=int(opcao_dias))).isoformat()
        conn.execute("""UPDATE locacoes
                        SET forma_pagamento=?, atendente=?, observacao=?,
                            opcao_dias=?, data_saida=?, data_prevista=?
                        WHERE id=?""",
                     (d.get("forma_pagamento"), d.get("atendente"), d.get("observacao"),
                      opcao_dias, data_saida, data_prevista, locacao_id))
    return jsonify({"ok": True, "data_prevista": data_prevista})


@app.route("/api/loja/locacao/<int:locacao_id>", methods=["DELETE"])
@requer_perfil("admin", "gerente")
def api_excluir_locacao(locacao_id):
    try:
        lj.excluir_locacao(locacao_id)
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404

@app.route("/api/loja/venda", methods=["POST"])
@requer_perfil("admin", "gerente", "vendedor")
def api_venda():
    try:
        res = lj.registrar_venda(request.get_json())
        return jsonify(res)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/loja/locacao", methods=["POST"])
@requer_perfil("admin", "gerente", "vendedor")
def api_locacao():
    try:
        res = lj.registrar_locacao(request.get_json())
        # Disparar envio de contrato automaticamente em background
        if ct._get_token():
            locacao_id = res["locacao_id"]
            def _enviar_contrato_bg():
                try:
                    result = ct.enviar_contrato(locacao_id)
                    if "error" in result:
                        _log.warning("[contrato-auto] Locação #%d: %s", locacao_id, result["error"])
                    else:
                        _log.info("[contrato-auto] Locação #%d: contrato enviado OK", locacao_id)
                except Exception as e:
                    _log.error("[contrato-auto] Locação #%d erro inesperado: %s", locacao_id, e)
            threading.Thread(target=_enviar_contrato_bg, daemon=True).start()
        return jsonify(res)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/loja/locacao/grupo", methods=["POST"])
@requer_perfil("admin", "gerente", "vendedor")
def api_locacao_grupo():
    """Registra múltiplos jogos em locação para o mesmo cliente/data, enviando UM único contrato."""
    try:
        data       = request.get_json()
        jogos      = data.get("jogos", [])
        if not jogos:
            return jsonify({"error": "Informe ao menos um jogo"}), 400
        cliente    = data.get("cliente", {})
        data_saida = data.get("data_saida")
        pagamento  = data.get("forma_pagamento")
        atendente  = data.get("atendente")
        observacao = data.get("observacao")

        locacao_ids, ultima_res = [], None
        for jogo in jogos:
            res = lj.registrar_locacao({
                "jogo_id":         jogo["jogo_id"],
                "opcao_dias":      jogo["opcao_dias"],
                "valor_locacao":   jogo["valor_locacao"],
                "data_saida":      data_saida,
                "forma_pagamento": pagamento,
                "atendente":       atendente,
                "observacao":      observacao,
                "cliente":         cliente,
            })
            locacao_ids.append(res["locacao_id"])
            ultima_res = res

        # Um único contrato cobrindo todos os jogos do grupo
        if ct._get_token() and locacao_ids:
            first_id = locacao_ids[0]
            def _bg():
                try:
                    r = ct.enviar_contrato(first_id)
                    if "error" in r:
                        _log.warning("[contrato-grupo] %s", r["error"])
                    else:
                        _log.info("[contrato-grupo] contrato enviado — grupo %s", locacao_ids)
                except Exception as exc:
                    _log.error("[contrato-grupo] erro: %s", exc)
            threading.Thread(target=_bg, daemon=True).start()

        return jsonify({
            "ok": True,
            "locacao_ids": locacao_ids,
            "data_prevista": ultima_res["data_prevista"] if ultima_res else None,
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/loja/devolucao", methods=["POST"])
@requer_perfil("admin", "gerente", "vendedor")
def api_devolucao():
    try:
        d = request.get_json()
        res = lj.registrar_devolucao(d["locacao_id"], d.get("data_devolucao"), dados=d)
        return jsonify(res)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/loja/cliente")
def api_buscar_cliente():
    cpf = request.args.get("cpf", "").strip()
    if not cpf:
        return jsonify(None)
    cpf_digits = cpf.replace(".", "").replace("-", "")
    # CPF é salvo formatado (XXX.XXX.XXX-XX); o LIKE precisa da versão formatada
    if len(cpf_digits) == 11:
        cpf_fmt = f"{cpf_digits[:3]}.{cpf_digits[3:6]}.{cpf_digits[6:9]}-{cpf_digits[9:]}"
    else:
        cpf_fmt = cpf
    clientes = lj.listar_clientes(busca=cpf_fmt)
    for c in clientes:
        if (c["cpf"] or "").replace(".", "").replace("-", "") == cpf_digits:
            return jsonify(dict(c))
    return jsonify(None)


@app.route("/api/loja/vendas")
def api_vendas():
    return jsonify([dict(r) for r in lj.listar_vendas()])


@app.route("/api/loja/locacoes")
def api_locacoes():
    rows = lj.listar_locacoes()
    result = []
    for r in rows:
        d = dict(r)
        result.append(d)
    return jsonify(result)


@app.route("/api/contrato-modelo", methods=["GET"])
def api_get_modelo():
    return jsonify({"clausulas": ct.carregar_modelo()})

@app.route("/api/contrato-modelo", methods=["POST"])
@requer_login
def api_salvar_modelo():
    d = request.get_json()
    if not d or not d.get("clausulas"):
        return jsonify({"error": "Campo 'clausulas' obrigatório"}), 400
    ct.salvar_modelo(d["clausulas"])
    return jsonify({"ok": True})

@app.route("/api/contrato-modelo/upload-pdf", methods=["POST"])
@requer_login
def api_upload_template_pdf():
    f = request.files.get("arquivo")
    if not f:
        return jsonify({"error": "Nenhum arquivo enviado"}), 400
    if f.content_type != "application/pdf" and not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Apenas arquivos PDF são aceitos"}), 400
    conteudo = f.read()
    if len(conteudo) > 20 * 1024 * 1024:
        return jsonify({"error": "Arquivo muito grande (máximo 20 MB)"}), 400
    ct.salvar_template_pdf(conteudo)
    return jsonify({"ok": True, "nome": f.filename, "tamanho": len(conteudo)})

@app.route("/api/contrato-modelo/remover-pdf", methods=["POST"])
@requer_login
def api_remover_template_pdf():
    ct.remover_template_pdf()
    return jsonify({"ok": True})

@app.route("/api/contrato-modelo/upload-docx", methods=["POST"])
@requer_login
def api_upload_template_docx():
    try:
        f = request.files.get("arquivo")
        if not f:
            return jsonify({"error": "Nenhum arquivo enviado"}), 400
        nome = f.filename.lower()
        if not nome.endswith(".docx"):
            return jsonify({"error": "Apenas arquivos .docx são aceitos"}), 400
        conteudo = f.read()
        if len(conteudo) > 20 * 1024 * 1024:
            return jsonify({"error": "Arquivo muito grande (máximo 20 MB)"}), 400
        ct.salvar_template_docx(conteudo)
        return jsonify({"ok": True, "nome": f.filename, "tamanho": len(conteudo)})
    except Exception as e:
        import traceback
        _log.error("upload-docx erro: %s", traceback.format_exc())
        return jsonify({"error": str(e)}), 500

@app.route("/api/contrato-modelo/remover-docx", methods=["POST"])
@requer_login
def api_remover_template_docx():
    ct.remover_template_docx()
    return jsonify({"ok": True})

@app.route("/api/contrato-modelo/tem-pdf")
def api_tem_template_pdf():
    info = ct.info_template()
    return jsonify(info)

@app.route("/api/contrato-modelo/preview")
def api_preview_modelo():
    """Gera um PDF de preview do modelo (sem dados reais de locação)."""
    try:
        # Usa um conjunto de dados fictícios para o preview
        from database import get_connection as _gc
        with _gc() as conn:
            loc_real = conn.execute(
                "SELECT id FROM locacoes ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if loc_real:
            pdf_bytes = ct.gerar_pdf_contrato(loc_real["id"])
        else:
            # Nenhuma locação ainda — gera com campos não substituídos (mostra os {{CAMPOS}})
            modelo = ct.carregar_modelo()
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.units import cm
            from reportlab.lib.colors import HexColor
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
            from reportlab.lib.styles import getSampleStyleSheet
            import io as _io
            buf = _io.BytesIO()
            doc = SimpleDocTemplate(buf, pagesize=A4,
                leftMargin=2*cm, rightMargin=2*cm,
                topMargin=2.5*cm, bottomMargin=2.5*cm)
            styles = getSampleStyleSheet()
            story = [Paragraph(linha, styles["Normal"]) for linha in modelo.split("\n")]
            doc.build(story)
            pdf_bytes = buf.getvalue()

        from flask import Response
        return Response(pdf_bytes, mimetype="application/pdf",
            headers={"Content-Disposition": "inline; filename=preview_contrato.pdf"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/loja/locacoes/<int:locacao_id>/enviar-contrato", methods=["POST"])
def api_enviar_contrato(locacao_id):
    result = ct.enviar_contrato(locacao_id)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)


@app.route("/api/loja/locacoes/<int:locacao_id>/contrato-status")
def api_contrato_status(locacao_id):
    result = ct.status_contrato(locacao_id)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)


@app.route("/api/loja/locacoes/<int:locacao_id>/contrato-pdf")
def api_contrato_pdf(locacao_id):
    """Baixa o PDF do contrato (preview local, sem assinatura)."""
    try:
        pdf_bytes = ct.gerar_pdf_contrato(locacao_id)
        from flask import Response
        return Response(
            pdf_bytes,
            mimetype="application/pdf",
            headers={"Content-Disposition": f"inline; filename=Contrato_{locacao_id:05d}.pdf"}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/cupons", methods=["GET"])
def api_listar_cupons():
    return jsonify([dict(r) for r in lj.listar_cupons()])

@app.route("/api/cupons", methods=["POST"])
def api_criar_cupom():
    try:
        lj.criar_cupom(request.get_json())
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/cupons/<int:cupom_id>/desativar", methods=["POST"])
def api_desativar_cupom(cupom_id):
    lj.desativar_cupom(cupom_id)
    return jsonify({"ok": True})

@app.route("/api/cupons/validar")
def api_validar_cupom():
    codigo = request.args.get("codigo", "")
    cupom, erro = lj.validar_cupom(codigo)
    if erro:
        return jsonify({"error": erro}), 404
    return jsonify(cupom)

@app.route("/api/cupons/usar", methods=["POST"])
def api_usar_cupom():
    d = request.get_json()
    lj.registrar_uso_cupom(
        d["cupom_id"], d["tipo_operacao"], d.get("referencia_id"),
        d["valor_desconto"], d.get("cliente_nome"), d.get("jogo_nome")
    )
    return jsonify({"ok": True})

@app.route("/api/cupons/usos")
def api_usos_cupons():
    cupom_id = request.args.get("cupom_id", type=int)
    return jsonify([dict(r) for r in lj.listar_usos_cupom(cupom_id)])


@app.route("/api/extrato/upload", methods=["POST"])
def api_extrato_upload():
    f = request.files.get("arquivo")
    if not f:
        return jsonify({"erro": "Nenhum arquivo enviado"}), 400
    try:
        lancamentos = conc.parse_csv(f.read())
    except ValueError as e:
        return jsonify({"erro": str(e)}), 422
    extrato_id = conc.salvar_extrato(f.filename, lancamentos)
    return jsonify({
        "extrato_id": extrato_id,
        "total": len([l for l in lancamentos if l["valor"] > 0]),
        "lancamentos": lancamentos
    })


@app.route("/api/extrato/conciliar/<int:extrato_id>", methods=["POST"])
def api_extrato_conciliar(extrato_id):
    resultado = conc.conciliar(extrato_id)
    return jsonify(resultado)


@app.route("/api/extrato/lancamentos/<int:extrato_id>")
def api_extrato_lancamentos(extrato_id):
    return jsonify([dict(r) for r in conc.listar_lancamentos(extrato_id)])


@app.route("/api/extrato/lista")
def api_extrato_lista():
    return jsonify([dict(r) for r in conc.listar_extratos()])


@app.route("/api/recebimento/manual", methods=["POST"])
def api_recebimento_manual():
    d = request.get_json()
    conc.marcar_recebido(d["tipo"], d["ref_id"], d.get("recebido", True))
    return jsonify({"ok": True})


@app.route("/api/dashboard")
def api_dashboard():
    from database import get_connection
    with get_connection() as conn:
        mais_vendidos = conn.execute("""
            SELECT j.nome, SUM(v.quantidade) AS total
            FROM vendas v JOIN jogos j ON j.id = v.jogo_id
            GROUP BY v.jogo_id ORDER BY total DESC LIMIT 5
        """).fetchall()

        mais_alugados = conn.execute("""
            SELECT j.nome, COUNT(*) AS total
            FROM locacoes l JOIN jogos j ON j.id = l.jogo_id
            GROUP BY l.jogo_id ORDER BY total DESC LIMIT 5
        """).fetchall()

        top_clientes = conn.execute("""
            SELECT c.nome, COUNT(*) AS total
            FROM locacoes l JOIN clientes c ON c.id = l.cliente_id
            WHERE l.cliente_id IS NOT NULL
            GROUP BY l.cliente_id ORDER BY total DESC LIMIT 5
        """).fetchall()

        mais_lucro = conn.execute("""
            SELECT j.nome,
                   ROUND(SUM(v.valor_final) - SUM(
                       COALESCE((SELECT ROUND(cp.valor_pago*1.0/cp.quantidade,2)
                                 FROM compras cp
                                 WHERE cp.jogo_id = v.jogo_id AND cp.valor_pago IS NOT NULL
                                 ORDER BY cp.data_compra DESC LIMIT 1), 0) * v.quantidade
                   ), 2) AS lucro
            FROM vendas v JOIN jogos j ON j.id = v.jogo_id
            GROUP BY v.jogo_id ORDER BY lucro DESC LIMIT 5
        """).fetchall()

    return jsonify({
        "mais_vendidos": [{"nome": r["nome"], "total": r["total"]} for r in mais_vendidos],
        "mais_alugados": [{"nome": r["nome"], "total": r["total"]} for r in mais_alugados],
        "top_clientes":  [{"nome": r["nome"], "total": r["total"]} for r in top_clientes],
        "mais_lucro":    [{"nome": r["nome"], "lucro": r["lucro"]} for r in mais_lucro],
    })


@app.route("/api/relatorio/clientes")
@requer_login
def api_relatorio_clientes():
    from database import get_connection
    tipo   = request.args.get("tipo", "")
    estado = request.args.get("estado", "")
    with get_connection() as conn:
        ult_venda = {r["cliente_id"]: r for r in conn.execute("""
            SELECT cliente_id, MAX(data_venda) AS ultima_data, 'venda' AS ultimo_tipo
            FROM vendas WHERE cliente_id IS NOT NULL GROUP BY cliente_id
        """).fetchall()}
        ult_loc = {r["cliente_id"]: r for r in conn.execute("""
            SELECT cliente_id, MAX(data_saida) AS ultima_data, 'locacao' AS ultimo_tipo
            FROM locacoes WHERE cliente_id IS NOT NULL GROUP BY cliente_id
        """).fetchall()}
        clientes = conn.execute("SELECT * FROM clientes ORDER BY nome").fetchall()
    resultado = []
    for c in clientes:
        cid = c["id"]
        tem_venda = cid in ult_venda
        tem_loc   = cid in ult_loc
        if tipo == "venda"   and not tem_venda: continue
        if tipo == "locacao" and not tem_loc:   continue
        if not tipo and not tem_venda and not tem_loc: continue
        est_uf = (c["estado"] or "").strip().upper()
        if estado and estado != "outros" and est_uf != estado.upper(): continue
        if estado == "outros" and est_uf in ("SC","RS","PR","SP","RJ","MG",""): continue
        dv = ult_venda.get(cid, {}).get("ultima_data") or ""
        dl = ult_loc.get(cid, {}).get("ultima_data") or ""
        ultima_data, ultimo_tipo = (dv, "venda") if dv >= dl else (dl, "locacao")
        resultado.append({
            "nome": c["nome"], "cpf": c["cpf"] or "",
            "telefone": c["telefone"] or "",
            "logradouro": c["logradouro"] or "", "numero": c["numero"] or "",
            "complemento": c["complemento"] or "", "bairro": c["bairro"] or "",
            "cidade": c["cidade"] or "", "estado": est_uf,
            "ultima_data": ultima_data, "ultimo_tipo": ultimo_tipo,
        })
    resultado.sort(key=lambda x: x["nome"].lower())
    return jsonify(resultado)


@app.route("/api/relatorio/semanal")
def api_relatorio_semanal():
    from database import get_connection
    de  = request.args.get("de")
    ate = request.args.get("ate")
    if not de or not ate:
        return jsonify([])
    with get_connection() as conn:
        vendas = conn.execute("""
            SELECT v.data_venda AS data, j.nome AS item,
                   c.nome AS cliente, v.valor_final AS valor,
                   (SELECT ROUND(cp.valor_pago * 1.0 / cp.quantidade, 2)
                    FROM compras cp
                    WHERE cp.jogo_id = v.jogo_id AND cp.valor_pago IS NOT NULL
                    ORDER BY cp.data_compra DESC LIMIT 1) AS custo_unit,
                   v.quantidade
            FROM vendas v
            JOIN jogos j ON j.id = v.jogo_id
            LEFT JOIN clientes c ON c.id = v.cliente_id
            WHERE v.data_venda BETWEEN ? AND ?
            ORDER BY v.data_venda
        """, (de, ate)).fetchall()

        locacoes = conn.execute("""
            SELECT l.data_saida AS data, j.nome AS item,
                   c.nome AS cliente, l.valor_locacao AS valor
            FROM locacoes l
            JOIN jogos j ON j.id = l.jogo_id
            LEFT JOIN clientes c ON c.id = l.cliente_id
            WHERE l.data_saida BETWEEN ? AND ?
            ORDER BY l.data_saida
        """, (de, ate)).fetchall()

    resultado = []
    for v in vendas:
        custo = (v["custo_unit"] * v["quantidade"]) if v["custo_unit"] else None
        resultado.append({
            "tipo": "venda", "data": v["data"], "item": v["item"],
            "cliente": v["cliente"], "valor": v["valor"], "custo": custo
        })
    for l in locacoes:
        resultado.append({
            "tipo": "locacao", "data": l["data"], "item": l["item"],
            "cliente": l["cliente"], "valor": l["valor"], "custo": None
        })

    resultado.sort(key=lambda x: x["data"])
    return jsonify(resultado)


CIDADES_LANDING_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Jogoteka — Escolha sua cidade</title>
  <link href="https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800;900&display=swap" rel="stylesheet">
  <style>
    :root{--red:#F10A0A;--green:#17C629;--purple:#7B20E1;--orange:#ED940E}
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Nunito',sans-serif;background:#fff;color:#1a1a2e;min-height:100vh;display:flex;flex-direction:column}

    /* NAV */
    nav{display:flex;justify-content:space-between;align-items:center;padding:14px 40px;
      background:#fff;box-shadow:0 2px 12px rgba(0,0,0,.08);position:sticky;top:0;z-index:10}
    .nav-logo img{height:52px;width:auto;object-fit:contain}
    .nav-logo-txt{font-size:1.6rem;font-weight:900;color:var(--red)}
    .nav-logo-txt span{color:var(--purple)}
    .btn-voltar{color:#666;font-weight:700;font-size:.9rem;text-decoration:none;
      padding:9px 18px;border-radius:8px;background:#f4f4f4;transition:.2s}
    .btn-voltar:hover{background:#e8e8e8;color:#333}

    /* FAIXA */
    .color-bar{display:flex;height:6px}
    .color-bar span{flex:1}
    .cb-r{background:var(--red)}.cb-o{background:var(--orange)}
    .cb-g{background:var(--green)}.cb-p{background:var(--purple)}

    /* CONTEÚDO */
    .content{flex:1;display:flex;flex-direction:column;align-items:center;
      justify-content:center;padding:60px 24px;text-align:center}
    .titulo{font-size:clamp(1.6rem,4vw,2.4rem);font-weight:900;margin-bottom:10px;color:#1a1a2e}
    .subtitulo{color:#666;font-size:1rem;font-weight:600;margin-bottom:48px}

    /* CARDS */
    .cidades-grid{display:flex;flex-wrap:wrap;gap:24px;justify-content:center;max-width:700px;width:100%}
    .cidade-card{
      background:#fff;border:2.5px solid #eee;border-radius:24px;
      padding:36px 40px;text-align:center;text-decoration:none;color:#1a1a2e;
      transition:.25s cubic-bezier(.34,1.56,.64,1);min-width:220px;flex:1;max-width:280px;
      box-shadow:0 4px 16px rgba(0,0,0,.07)
    }
    .cidade-card.floripa{border-top:5px solid var(--purple)}
    .cidade-card.poa{border-top:5px solid var(--green)}
    .cidade-card:hover{transform:translateY(-6px);box-shadow:0 12px 32px rgba(0,0,0,.13)}
    .cidade-card.floripa:hover{border-color:var(--purple)}
    .cidade-card.poa:hover{border-color:var(--green)}
    .cidade-emoji{font-size:3.5rem;display:block;margin-bottom:14px}
    .cidade-nome{font-size:1.5rem;font-weight:900;margin-bottom:6px}
    .cidade-estado{display:inline-block;padding:3px 12px;border-radius:99px;
      font-size:.75rem;font-weight:800;letter-spacing:1px;text-transform:uppercase;
      color:#fff;margin-bottom:16px}
    .floripa .cidade-estado{background:var(--purple)}
    .poa .cidade-estado{background:var(--green)}
    .cidade-cta{font-size:.9rem;font-weight:700;color:#888;margin-top:4px}
    .cidade-cta strong{color:var(--orange)}

    /* FOOTER */
    footer{background:#f7f8fa;border-top:1px solid #eee;padding:20px;
      text-align:center;font-size:.82rem;color:#999;font-weight:600}

    @media(max-width:520px){
      nav{padding:12px 20px}
      .nav-logo img{height:40px}
      .cidade-card{min-width:160px;padding:28px 24px}
      .content{padding:40px 20px}
    }
  </style>
</head>
<body>

<nav>
  <div class="nav-logo">
    <a href="/">
      <img src="/api/logo" alt="Jogoteka"
           onerror="this.outerHTML='<span class=nav-logo-txt>JOGO<span>TEKA</span></span>'">
    </a>
  </div>
  <a class="btn-voltar" href="/">← Voltar</a>
</nav>

<div class="color-bar">
  <span class="cb-r"></span><span class="cb-o"></span><span class="cb-g"></span><span class="cb-p"></span>
</div>

<div class="content">
  <div class="titulo">Escolha sua cidade 📍</div>
  <p class="subtitulo">Veja os jogos disponíveis na loja mais perto de você</p>

  <div class="cidades-grid">
    {% for slug, c in cidades.items() %}
    <a class="cidade-card {{ 'floripa' if slug == 'florianopolis' else 'poa' }}"
       href="/catalogo?cidade={{ slug }}">
      <span class="cidade-emoji">{{ c.emoji }}</span>
      <div class="cidade-nome">{{ c.nome }}</div>
      <div class="cidade-estado">{{ 'SC' if slug == 'florianopolis' else 'RS' }}</div>
      <div class="cidade-cta">Ver catálogo <strong>→</strong></div>
    </a>
    {% endfor %}
  </div>
</div>

<div class="color-bar">
  <span class="cb-p"></span><span class="cb-g"></span><span class="cb-o"></span><span class="cb-r"></span>
</div>

<footer>© 2025 Jogoteka — Aluguel e venda de jogos de tabuleiro</footer>

</body>
</html>"""

CATALOGO_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Jogoteka — Catálogo de Jogos</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Fredoka+One&family=Nunito:wght@400;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root{
      --red:#F10A0A;--green:#17C629;--purple:#7B20E1;--orange:#ED940E;
      --bg:#f7f8fc;--bg2:#ffffff;--card:#ffffff;--border:#e2e8f0;
      --text:#1a202c;--muted:#718096;--wa:#25D366;--accent:#ED940E
    }
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Nunito',system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}

    /* ── Header / Nav ── */
    header{
      background:#ffffff;
      border-bottom:1px solid #eee;
      padding:.85rem 2rem;
      display:flex;justify-content:space-between;align-items:center;
      position:sticky;top:0;z-index:20;
      box-shadow:0 2px 12px rgba(0,0,0,.08)
    }
    .logo-wrap{display:inline-flex;align-items:center;gap:.6rem;text-decoration:none}
    .logo-img-cat{height:48px;width:auto;object-fit:contain}
    .logo-text{font-family:'Fredoka One',cursive;font-size:2rem;letter-spacing:1px;line-height:1}
    .logo-text .j{color:var(--red)}.logo-text .o1{color:var(--orange)}
    .logo-text .g{color:var(--green)}.logo-text .o2{color:var(--purple)}
    .logo-text .t{color:var(--red)}.logo-text .e{color:var(--orange)}
    .logo-text .k{color:var(--green)}.logo-text .a{color:var(--purple)}
    .btn-voltar-cat{color:#666;font-weight:700;font-size:.88rem;text-decoration:none;
      padding:8px 16px;border-radius:8px;background:#f4f4f4;transition:.2s;white-space:nowrap}
    .btn-voltar-cat:hover{background:#e8e8e8;color:#333}
    .color-bar-cat{display:flex;height:5px}
    .color-bar-cat span{flex:1}
    .cb-r{background:var(--red)}.cb-o{background:var(--orange)}
    .cb-g{background:var(--green)}.cb-p{background:var(--purple)}

    /* ── Barra de busca ── */
    .busca-bar{
      background:#ffffff;border-bottom:1px solid var(--border);
      padding:.9rem 2rem;display:flex;gap:.8rem;align-items:center;flex-wrap:wrap;
      position:sticky;top:0;z-index:10;box-shadow:0 1px 4px rgba(0,0,0,.06)
    }
    .busca-bar input{
      background:#f7f8fc;border:1px solid var(--border);color:var(--text);
      border-radius:8px;padding:.5rem .9rem;font-size:.9rem;font-family:inherit;
      flex:1;min-width:200px
    }
    .busca-bar input:focus{outline:none;border-color:var(--orange)}
    .filtro-disp{
      background:#f7f8fc;border:1px solid var(--border);color:var(--text);
      border-radius:8px;padding:.5rem .9rem;font-size:.9rem;font-family:inherit;min-width:155px
    }
    .filtro-disp:focus{outline:none;border-color:var(--orange)}
    .filtro-jog-wrap{display:inline-flex;align-items:center;gap:0;
      background:#f7f8fc;border:1px solid var(--border);border-radius:8px;overflow:hidden}
    .filtro-jog-wrap:focus-within{border-color:var(--orange)}
    .filtro-jog-btn{background:none;border:none;color:var(--purple);font-size:1.1rem;font-weight:700;
      padding:.4rem .7rem;cursor:pointer;font-family:inherit;line-height:1;transition:background .15s}
    .filtro-jog-btn:hover{background:rgba(107,70,193,.1)}
    .filtro-jog-label{font-size:.88rem;color:var(--text);padding:0 .2rem;white-space:nowrap;
      min-width:6.5rem;text-align:center;user-select:none}
    .filtros-count{color:var(--muted);font-size:.85rem;white-space:nowrap}

    /* ── Filtro de categorias ── */
    .cats-bar{
      background:#ffffff;border-bottom:1px solid var(--border);
      padding:.75rem 1.5rem;overflow-x:auto;
      display:flex;gap:.7rem;align-items:center;
    }
    .cats-bar::-webkit-scrollbar{height:4px}
    .cats-bar::-webkit-scrollbar-thumb{background:#cbd5e0;border-radius:4px}
    .cat-btn{
      display:flex;flex-direction:column;align-items:center;gap:.3rem;
      background:#f7f8fc;border:2px solid transparent;
      border-radius:14px;padding:.55rem .9rem;cursor:pointer;
      font-family:inherit;color:var(--muted);font-size:.72rem;font-weight:700;
      white-space:nowrap;transition:all .18s;min-width:72px;flex-shrink:0
    }
    .cat-btn .icon{font-size:1.6rem;line-height:1;display:flex;align-items:center;justify-content:center}
    .cat-btn .icon svg{width:32px;height:32px;display:block;transition:stroke .18s}
    .cat-btn:hover .icon svg{stroke:#2d3748}
    .cat-btn.ativo .icon svg{stroke:var(--orange)}
    .cat-btn:hover{background:#edf2f7;color:var(--text)}
    .cat-btn.ativo{
      background:#fff8f0;border-color:var(--orange);
      color:var(--orange)
    }

    /* ── Grid ── */
    .catalogo{
      max-width:1300px;margin:0 auto;padding:2rem 1.5rem;
      display:grid;
      grid-template-columns:repeat(auto-fill,minmax(260px,1fr));
      gap:1.5rem
    }

    /* ── Card ── */
    .card{
      background:#ffffff;border-radius:16px;overflow:hidden;
      border:2px solid transparent;display:flex;flex-direction:column;
      box-shadow:0 2px 8px rgba(0,0,0,.06);
      transition:transform .25s cubic-bezier(.34,1.56,.64,1),
                 box-shadow .25s ease,
                 border-color .2s ease;
      cursor:pointer;will-change:transform
    }
    .card:hover{
      transform:translateY(-6px) scale(1.015);
      box-shadow:0 20px 48px rgba(107,70,193,.18),
                 0 4px 12px rgba(0,0,0,.08);
      border-color:var(--purple)
    }
    .card-img{
      width:100%;height:220px;background:#f7f8fc;
      display:flex;align-items:center;justify-content:center;
      font-size:3.5rem;color:#cbd5e0;overflow:hidden
    }
    .card-img img{width:100%;height:100%;object-fit:contain;padding:8px}
    .card-body{padding:1rem 1.1rem;flex:1;display:flex;flex-direction:column;gap:.5rem}
    .card-nome{font-weight:800;font-size:1.05rem;line-height:1.3;color:var(--text)}
    .card-editora{color:var(--muted);font-size:.8rem}
    .card-meta{display:flex;gap:.5rem;flex-wrap:wrap;margin:.2rem 0}
    .tag{
      background:#edf2f7;border-radius:6px;
      padding:2px 8px;font-size:.72rem;color:#4a5568
    }
    .tag.cat{background:#faf5ff;color:#6b46c1;border:1px solid #e9d8fd;font-style:italic}
    .tag.tagline{background:#faf5ff;color:#553c9a;border:1px solid #e9d8fd;
      font-size:.73rem;font-style:italic;letter-spacing:.1px}
    .card-status{
      display:inline-flex;align-items:center;gap:5px;
      font-size:.78rem;font-weight:700;border-radius:20px;padding:3px 10px;
      width:fit-content
    }
    .status-disp{background:#f0fff4;color:#276749;border:1px solid #c6f6d5}
    .status-indisp{background:#fff5f5;color:#c53030;border:1px solid #fed7d7}
    .resumo-wrap{position:relative;margin:.1rem 0}
    .card-resumo{font-size:.8rem;color:var(--muted);line-height:1.45;
      overflow:hidden;max-height:4.64rem}
    .card-resumo.expandido{max-height:none;padding-bottom:1.5rem}
    .btn-resumo{position:absolute;bottom:0;right:0;
      border:none;background:linear-gradient(to right,transparent 0%,#fff 30%);
      padding:0 0 0 3rem;color:var(--orange);font-size:.78rem;font-weight:700;
      cursor:pointer;font-family:inherit;white-space:nowrap;line-height:1.16rem}
    .btn-video-cat{display:inline-flex;align-items:center;gap:.35rem;flex:none;
      background:rgba(241,10,10,.12);border:1px solid rgba(241,10,10,.35);border-radius:6px;
      padding:4px 10px;font-size:.8rem;color:#fc8181;cursor:pointer;font-family:inherit}
    .btn-video-cat:hover{background:rgba(241,10,10,.25)}
    .video-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.88);z-index:100;
      align-items:center;justify-content:center;padding:1rem;flex-direction:column;gap:.8rem}
    .video-overlay.open{display:flex}
    .video-titulo{color:white;font-family:'Fredoka One',cursive;font-size:1.1rem;letter-spacing:.3px}
    .video-wrap-cat{position:relative;width:100%;max-width:720px;background:#000;border-radius:10px;overflow:hidden}
    .video-wrap-cat iframe,.video-wrap-cat video{width:100%;aspect-ratio:16/9;display:block;border:none}
    .video-fechar{position:absolute;top:.5rem;right:.7rem;background:rgba(0,0,0,.7);color:white;
      border:none;border-radius:50%;width:2rem;height:2rem;font-size:1.1rem;cursor:pointer}
    .card-img-wrap{position:relative;width:100%;height:260px;background:#f7f8fc;overflow:hidden;display:flex;align-items:center;justify-content:center}
    .card-img-wrap img{width:100%;height:100%;object-fit:contain;padding:8px;transition:transform .35s cubic-bezier(.34,1.56,.64,1)}
    .card:hover .card-img-wrap img{transform:scale(1.06)}
    .card-img-wrap .no-img{font-size:4rem;color:#cbd5e0}
    .badges{position:absolute;top:.55rem;left:.55rem;display:flex;flex-direction:column;gap:.35rem;z-index:2}
    .badge-item{
      display:inline-flex;align-items:center;gap:.3rem;
      padding:3px 9px;border-radius:999px;font-size:.72rem;font-weight:800;
      letter-spacing:.2px;box-shadow:0 2px 6px rgba(0,0,0,.18);
      backdrop-filter:blur(4px);white-space:nowrap
    }
    .badge-destaque{background:linear-gradient(135deg,#f6ad55,#ed8936);color:#fff}
    .badge-rapido{background:linear-gradient(135deg,#68d391,#38a169);color:#fff}
    .badge-kids{background:linear-gradient(135deg,#76e4f7,#0bc5ea);color:#fff}
    .badge-coop{background:linear-gradient(135deg,#b794f4,#6b46c1);color:#fff}
    .badge-party{background:linear-gradient(135deg,#fc8181,#e53e3e);color:#fff}
    .badge-expert{background:linear-gradient(135deg,#667eea,#4c51bf);color:#fff}
    .badge-longo{background:linear-gradient(135deg,#718096,#4a5568);color:#fff}
    .curtir-btn{
      display:inline-flex;align-items:center;gap:.3rem;
      background:none;border:1.5px solid #bee3f8;border-radius:999px;
      padding:4px 8px;font-size:.75rem;font-weight:700;color:#2b6cb0;
      cursor:pointer;font-family:inherit;transition:all .18s;white-space:nowrap
    }
    .curtir-btn:hover{background:#ebf8ff;border-color:#63b3ed}
    .curtir-btn.curtido{background:#ebf8ff;border-color:#63b3ed;color:#2c5282}
    .curtir-btn .heart{font-size:1rem;transition:transform .2s;line-height:1}
    .curtir-btn.curtido .heart{transform:scale(1.25)}
    .curtir-count{font-size:.75rem;font-weight:700}
    .card-acoes{display:flex;flex-direction:column;gap:.35rem;margin:.4rem 0}
    .card-acoes-row1{display:flex;align-items:center;justify-content:space-between}
    .card-acoes .btn-video-cat{align-self:flex-start;margin:0}
    .curtir-wrap{display:flex;align-items:center;justify-content:flex-end;margin-bottom:.3rem}
    /* ── Favoritos ── */
    .fav-btn{display:inline-flex;align-items:center;
      background:none;border:1.5px solid #fed7d7;border-radius:999px;
      padding:4px 8px;cursor:pointer;transition:all .18s}
    .fav-btn:hover{background:#fff5f5;border-color:#fc8181;transform:scale(1.1)}
    .fav-btn.favoritado{background:#fff5f5;border-color:#fc8181}
    .fav-btn .fav-icon{font-size:1rem;transition:transform .2s;line-height:1}
    .fav-btn.favoritado .fav-icon{transform:scale(1.25)}
    /* ── Botão flutuante de cadastro ── */
    .fab-cadastro{position:fixed;bottom:1.5rem;right:1.5rem;z-index:50;
      background:var(--purple);color:#fff;border:none;border-radius:999px;
      padding:.75rem 1.3rem;font-size:.9rem;font-weight:700;font-family:inherit;
      cursor:pointer;box-shadow:0 4px 20px rgba(107,70,193,.4);
      display:flex;align-items:center;gap:.5rem;
      transition:transform .2s cubic-bezier(.34,1.56,.64,1),box-shadow .2s}
    .fab-cadastro:hover{transform:translateY(-3px) scale(1.04);box-shadow:0 8px 28px rgba(107,70,193,.55)}
    .fab-cadastro.logado{background:var(--green)}
    .fab-cadastro.mostrando-fav{background:#e53e3e;animation:pulse-fav 1.5s infinite}
    @keyframes pulse-fav{0%,100%{box-shadow:0 4px 20px rgba(229,62,62,.4)}50%{box-shadow:0 4px 28px rgba(229,62,62,.7)}}
    .fav-banner{background:linear-gradient(135deg,#e53e3e,#c53030);color:#fff;
      text-align:center;padding:.6rem 1rem;font-size:.9rem;font-weight:700;
      position:sticky;top:0;z-index:40;display:none}
    .fav-banner.ativo{display:block}
    /* ── Modal cadastro ── */
    .modal-cad-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);
      z-index:200;align-items:center;justify-content:center;padding:1rem}
    .modal-cad-overlay.open{display:flex}
    .modal-cad{background:#fff;border-radius:20px;padding:2rem;width:100%;max-width:380px;
      box-shadow:0 24px 64px rgba(0,0,0,.2)}
    .modal-cad h3{font-family:'Fredoka One',cursive;font-size:1.5rem;color:var(--purple);margin-bottom:.3rem}
    .modal-cad p{font-size:.85rem;color:var(--muted);margin-bottom:1.2rem}
    .modal-cad input{width:100%;border:1.5px solid var(--border);border-radius:8px;
      padding:.6rem .9rem;font-size:.95rem;font-family:inherit;margin-bottom:.8rem;outline:none}
    .modal-cad input:focus{border-color:var(--purple)}
    .modal-cad .btn-entrar{width:100%;background:var(--purple);color:#fff;border:none;
      border-radius:10px;padding:.75rem;font-size:1rem;font-weight:700;font-family:inherit;
      cursor:pointer;transition:background .2s}
    .modal-cad .btn-entrar:hover{background:#553c9a}
    .modal-cad .btn-fechar{background:none;border:none;color:var(--muted);font-size:.85rem;
      cursor:pointer;margin-top:.8rem;display:block;width:100%;text-align:center}
    .hero-curtir-wrap{display:flex;align-items:center;gap:.5rem;margin-bottom:.3rem}
    .prova-social{font-size:.75rem;color:var(--muted)}
    .ancora-valor{
      display:block;margin-top:.4rem;
      background:linear-gradient(135deg,#fffbeb,#fef3c7);
      border:1px solid #fcd34d;border-radius:8px;
      padding:6px 12px;font-size:.78rem;font-weight:700;color:#92400e;
      white-space:nowrap;overflow:hidden;text-overflow:ellipsis
    }
    .ancora-valor .ancora-num{color:#d97706;font-size:.88rem}
    .card-precos{margin-top:auto;padding-top:.6rem;border-top:1px solid var(--border)}
    .preco-venda{font-size:1.15rem;font-weight:800;color:var(--orange);margin-bottom:.4rem}
    .preco-loc{font-size:.82rem;display:flex;flex-direction:column;gap:.25rem;margin-top:.3rem}
    .preco-loc-titulo{font-size:.75rem;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:.1rem}
    .preco-loc-item{display:flex;justify-content:space-between;align-items:center;
      background:#faf5ff;border-radius:6px;padding:3px 8px;border:1px solid #e9d8fd}
    .preco-loc-item .dias{color:#4a5568}
    .preco-loc-item .val{color:#6b46c1;font-weight:700}

    /* ── Etiquetas de preço (não clicáveis) ── */
    .card-btns{padding:.8rem 1.1rem;display:flex;gap:.5rem;flex-direction:column}
    .preco-tag{
      display:flex;align-items:center;gap:.5rem;
      border-radius:10px;padding:.55rem .9rem;
      font-family:inherit;font-size:.85rem;font-weight:700;user-select:none
    }
    .preco-tag-comprar{background:var(--orange);color:#fff}
    .preco-tag-alugar{background:var(--wa);color:#fff}
    /* ── Adicionar ao carrinho ── */
    .btn-add-cart{
      display:flex;align-items:center;justify-content:center;gap:.4rem;
      border:2px solid var(--purple);border-radius:10px;padding:.5rem;
      background:#fff;color:var(--purple);font-family:inherit;font-size:.82rem;
      font-weight:800;cursor:pointer;transition:.2s;width:100%;margin-top:.2rem
    }
    .btn-add-cart:hover{background:#f5f0ff}
    .btn-add-cart.no-carrinho{background:var(--green);color:#fff;border-color:var(--green)}
    .btn-add-cart.no-carrinho:hover{background:#13a821}
    /* Picker de opção */
    .picker-overlay{display:none;position:fixed;inset:0;z-index:300;background:rgba(0,0,0,.35)}
    .picker-overlay.open{display:flex;align-items:flex-end;justify-content:center}
    .picker-box{background:#fff;border-radius:20px 20px 0 0;padding:24px 20px;width:100%;max-width:480px;
      box-shadow:0 -8px 32px rgba(0,0,0,.2);animation:slideUp .25s ease}
    @keyframes slideUp{from{transform:translateY(100%)}to{transform:translateY(0)}}
    .picker-title{font-size:1.05rem;font-weight:900;color:#1a1a2e;margin-bottom:16px;text-align:center;
      padding-bottom:12px;border-bottom:1px solid #eee}
    .picker-opt{display:flex;align-items:center;justify-content:space-between;
      padding:14px 16px;border-radius:12px;border:2px solid #eee;margin-bottom:8px;
      cursor:pointer;transition:.15s;font-weight:700;font-size:.95rem}
    .picker-opt:hover{border-color:var(--purple);background:#f5f0ff}
    .picker-opt.opt-comprar{color:var(--orange)}.picker-opt.opt-alugar{color:#2e7d32}
    .picker-opt .opt-val{font-size:.9rem;font-weight:900}
    .picker-cancel{width:100%;padding:12px;border:none;background:#f4f5f7;border-radius:10px;
      font-family:inherit;font-size:.9rem;font-weight:700;color:#888;cursor:pointer;margin-top:4px}
    .picker-cancel:hover{background:#e0e0e0}
    /* Bubble flutuante */
    .cart-bubble{position:fixed;bottom:24px;right:24px;z-index:200;
      background:var(--green);color:#fff;border:none;border-radius:99px;
      padding:13px 22px;font-family:inherit;font-size:.95rem;font-weight:900;
      box-shadow:0 4px 20px rgba(0,0,0,.28);cursor:pointer;
      display:none;align-items:center;gap:8px;transition:.2s}
    .cart-bubble.visible{display:flex}
    .cart-bubble:hover{transform:translateY(-3px);box-shadow:0 8px 28px rgba(0,0,0,.3)}
    .cart-badge{background:#fff;color:var(--green);border-radius:99px;
      padding:1px 8px;font-size:.85rem;font-weight:900;min-width:22px;text-align:center}
    /* Drawer do carrinho */
    .cart-drawer-bg{display:none;position:fixed;inset:0;z-index:400;background:rgba(0,0,0,.4)}
    .cart-drawer-bg.open{display:flex;justify-content:flex-end}
    .cart-drawer{background:#fff;width:100%;max-width:420px;height:100%;
      display:flex;flex-direction:column;box-shadow:-8px 0 32px rgba(0,0,0,.15);
      animation:slideRight .25s ease}
    @keyframes slideRight{from{transform:translateX(100%)}to{transform:translateX(0)}}
    .drawer-header{padding:20px 24px;border-bottom:1px solid #eee;
      display:flex;justify-content:space-between;align-items:center}
    .drawer-header h2{font-size:1.15rem;font-weight:900;color:#1a1a2e}
    .drawer-close{background:none;border:none;font-size:1.4rem;cursor:pointer;color:#888;
      width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;transition:.15s}
    .drawer-close:hover{background:#f4f5f7}
    .drawer-items{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px}
    .drawer-item{background:#f9f9f9;border-radius:14px;padding:14px 16px;
      display:flex;justify-content:space-between;align-items:flex-start;gap:10px;border:1.5px solid #eee}
    .drawer-item-info{flex:1}
    .drawer-item-nome{font-weight:800;font-size:.92rem;color:#1a1a2e;margin-bottom:5px}
    .drawer-item-opc{display:inline-block;font-size:.78rem;font-weight:700;padding:2px 9px;border-radius:99px}
    .opc-alugar{background:#e8f5e9;color:#2e7d32}.opc-comprar{background:#fff3e0;color:#e65100}
    .drawer-item-val{font-size:.98rem;font-weight:900;color:var(--purple);margin-top:6px}
    .drawer-item-rm{background:none;border:none;font-size:1.1rem;cursor:pointer;
      color:#ccc;padding:4px 6px;border-radius:50%;transition:.15s;line-height:1}
    .drawer-item-rm:hover{background:#fee;color:var(--red)}
    .drawer-footer{padding:18px 16px;border-top:1px solid #eee}
    .drawer-total{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}
    .drawer-total-label{font-size:.9rem;color:#888;font-weight:700}
    .drawer-total-val{font-size:1.25rem;font-weight:900;color:var(--purple)}
    .drawer-empty{text-align:center;color:#bbb;font-weight:700;font-size:.92rem;padding:48px 0}
    .btn-wa-cart{display:flex;align-items:center;justify-content:center;gap:.5rem;
      width:100%;padding:15px;border:none;border-radius:14px;background:#25D366;color:#fff;
      font-family:inherit;font-size:1rem;font-weight:900;cursor:pointer;transition:.2s;text-decoration:none}
    .btn-wa-cart:hover{filter:brightness(1.08)}
    .btn-limpar-cart{width:100%;padding:10px;border:none;background:none;
      color:#bbb;font-family:inherit;font-size:.82rem;font-weight:700;cursor:pointer;margin-top:6px}
    .btn-limpar-cart:hover{color:var(--red)}

    /* ── Jogo da Semana ── */
    /* ── Banner "A Escolha da Comunidade" ── */
    .banner-section{max-width:1300px;margin:1.8rem auto .5rem;padding:0 1.5rem}
    .carrossel-wrap{position:relative}
    #semana-grid .banner-card{display:none;animation:fadeSlide .5s ease}
    #semana-grid .banner-card.ativo{display:flex}
    @keyframes fadeSlide{from{opacity:0;transform:translateX(18px)}to{opacity:1;transform:translateX(0)}}
    .carr-arrow{position:absolute;top:50%;transform:translateY(-50%);
      background:rgba(0,0,0,.45);border:2px solid rgba(255,255,255,.6);color:#fff;
      font-size:2rem;line-height:1;width:2.8rem;height:2.8rem;border-radius:50%;
      cursor:pointer;z-index:10;transition:.2s;display:flex;align-items:center;justify-content:center;
      box-shadow:0 2px 12px rgba(0,0,0,.4);backdrop-filter:blur(4px)}
    .carr-arrow:hover{background:rgba(0,0,0,.7);border-color:#fff;transform:translateY(-50%) scale(1.1)}
    .carr-prev{left:.7rem}.carr-next{right:.7rem}
    .carr-dots{display:flex;justify-content:center;gap:.5rem;margin-top:.8rem}
    .carr-dot{width:.55rem;height:.55rem;border-radius:50%;background:rgba(107,70,193,.3);
      border:none;cursor:pointer;transition:all .25s;padding:0}
    .carr-dot.ativo{background:var(--purple);transform:scale(1.3)}
    .banner-card{
      border-radius:22px;overflow:hidden;display:flex;min-height:300px;
      position:relative;box-shadow:0 24px 64px rgba(0,0,0,.35);
    }
    .banner-card.bc-green {background:linear-gradient(135deg,#0a4d12 0%,#138a20 50%,#0d6b18 100%)}
    .banner-card.bc-red   {background:linear-gradient(135deg,#6b0303 0%,#b80707 50%,#7a0404 100%)}
    .banner-card.bc-purple{background:linear-gradient(135deg,#2e0a5e 0%,#5a1ab8 50%,#3b0d7a 100%)}
    .banner-card.bc-orange{background:linear-gradient(135deg,#6b3a02 0%,#b86205 50%,#7a4403 100%)}
    .banner-content{
      flex:1;padding:2.2rem 2rem 2rem;display:flex;flex-direction:column;
      gap:.7rem;z-index:2;position:relative;min-width:0
    }
    .banner-badge{
      display:inline-flex;align-items:center;gap:.4rem;width:fit-content;
      background:rgba(255,215,0,.12);border:1px solid rgba(255,215,0,.35);
      border-radius:999px;padding:4px 14px;
      font-size:.72rem;font-weight:800;color:#fbbf24;
      text-transform:uppercase;letter-spacing:.6px
    }
    .banner-nome{
      font-family:'Fredoka One',cursive;font-size:2.2rem;
      color:#fff;line-height:1.1;text-shadow:0 2px 12px rgba(0,0,0,.4)
    }
    .banner-tagline{
      font-size:.95rem;color:rgba(255,255,255,.7);
      line-height:1.5;max-width:480px;
      overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical
    }
    .banner-meta{display:flex;gap:.45rem;flex-wrap:wrap;align-items:center}
    .banner-tag{
      background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.18);
      border-radius:6px;padding:3px 10px;font-size:.76rem;color:rgba(255,255,255,.85)
    }
    .banner-tag.disp{background:rgba(72,187,120,.15);border-color:rgba(72,187,120,.35);color:#68d391}
    .banner-tag.indisp{background:rgba(252,129,129,.12);border-color:rgba(252,129,129,.3);color:#fc8181}
    .banner-social{font-size:.78rem;color:rgba(255,255,255,.5);margin-top:-.2rem}
    .banner-precos{display:flex;flex-direction:column;gap:.25rem;margin-top:auto;padding-top:.6rem;border-top:1px solid rgba(255,255,255,.1)}
    .banner-preco-venda{font-size:1.5rem;font-weight:800;color:#f6ad55;letter-spacing:-.5px}
    .banner-loc{display:flex;flex-wrap:wrap;gap:.4rem;margin-top:.2rem}
    .banner-loc-item{
      background:rgba(167,139,250,.15);border:1px solid rgba(167,139,250,.3);
      border-radius:8px;padding:4px 12px;font-size:.8rem;
      display:flex;gap:.5rem;align-items:center
    }
    .banner-loc-item .b-dias{color:#c4b5fd;font-weight:600}
    .banner-loc-item .b-val{color:#a78bfa;font-weight:800}
    .banner-ancora{
      font-size:.76rem;color:#fbbf24;font-weight:700;margin-top:.15rem
    }
    .banner-btns{display:flex;gap:.7rem;margin-top:.8rem;flex-wrap:wrap}
    .banner-btn-cta{
      display:inline-flex;align-items:center;gap:.5rem;
      background:linear-gradient(135deg,#f6ad55,#dd6b20);color:#fff;
      border:none;border-radius:12px;padding:.75rem 1.8rem;
      font-family:inherit;font-size:1rem;font-weight:800;
      text-decoration:none;cursor:pointer;
      box-shadow:0 4px 20px rgba(221,107,32,.55);
      transition:transform .15s,box-shadow .15s
    }
    .banner-btn-cta:hover{transform:translateY(-2px);box-shadow:0 8px 30px rgba(221,107,32,.7)}
    .banner-btn-sec{
      display:inline-flex;align-items:center;gap:.45rem;
      background:rgba(255,255,255,.1);color:#fff;
      border:1.5px solid rgba(255,255,255,.25);border-radius:12px;
      padding:.75rem 1.4rem;font-family:inherit;font-size:.95rem;font-weight:700;
      text-decoration:none;cursor:pointer;transition:background .15s
    }
    .banner-btn-sec:hover{background:rgba(255,255,255,.18)}
    .banner-btn-video{
      display:inline-flex;align-items:center;gap:.4rem;
      background:rgba(252,129,129,.15);border:1px solid rgba(252,129,129,.3);
      border-radius:10px;padding:.55rem 1rem;color:#fc8181;
      font-family:inherit;font-size:.85rem;font-weight:700;cursor:pointer;
      transition:background .15s
    }
    .banner-btn-video:hover{background:rgba(252,129,129,.28)}
    .banner-img-side{
      width:300px;min-width:300px;flex-shrink:0;position:relative;overflow:hidden
    }
    .banner-img-side img{
      width:100%;height:100%;object-fit:contain;padding:1.5rem;
      position:relative;z-index:1;transition:transform .4s
    }
    .banner-card:hover .banner-img-side img{transform:scale(1.04)}
    .banner-img-side::before{
      content:'';position:absolute;inset:0;z-index:2;
      background:linear-gradient(90deg,#302b63 0%,transparent 45%)
    }
    .banner-img-side .no-img{font-size:5rem;position:absolute;top:50%;left:50%;transform:translate(-50%,-50%)}
    @media(max-width:700px){
      .banner-img-side{display:none}
      .banner-nome{font-size:1.6rem}
      .banner-btn-cta{width:100%;justify-content:center}
    }

    /* ── Vazio ── */
    .vazio{grid-column:1/-1;text-align:center;padding:4rem;color:var(--muted)}
    .vazio .icon{font-size:3rem;margin-bottom:.8rem}

    /* ── Footer ── */
    footer{
      text-align:center;padding:2rem;color:var(--muted);font-size:.8rem;
      border-top:1px solid var(--border);margin-top:2rem;background:#fff
    }
    footer a{color:var(--orange);text-decoration:none}

    @media(max-width:480px){
      .catalogo{grid-template-columns:1fr;padding:1rem}
      .card-img{height:180px}
    }
  </style>
</head>
<body>
  <header>
    <a href="/catalogo" class="logo-wrap">
      <span id="cat-logo-img"></span>
    </a>
    <a class="btn-voltar-cat" href="/">← Início</a>
  </header>
  <div class="color-bar-cat">
    <span class="cb-r"></span><span class="cb-o"></span><span class="cb-g"></span><span class="cb-p"></span>
  </div>

  <div class="fav-banner" id="fav-banner">
    ❤️ Mostrando seus jogos favoritos &nbsp;—&nbsp;

    <button onclick="sairModoFav()" style="background:rgba(255,255,255,.25);border:none;color:#fff;
      border-radius:20px;padding:2px 12px;cursor:pointer;font-weight:700;font-family:inherit">
      Ver todos ✕
    </button>
  </div>

  <div class="busca-bar">
    <input id="busca" type="search" placeholder="🔍 Buscar jogo..." oninput="filtrar()">
    <select id="filtro-disp" class="filtro-disp" onchange="filtrar()">
      <option value="">Todos</option>
      <option value="1">✅ Disponíveis</option>
      <option value="0">❌ Indisponíveis</option>
    </select>
    <select id="filtro-faixa" class="filtro-disp" onchange="filtrar()">
      <option value="">🎂 Faixa etária</option>
    </select>
    <div class="filtro-jog-wrap">
      <button class="filtro-jog-btn" onclick="jogStep(-1)">−</button>
      <span class="filtro-jog-label" id="jog-label">👥 Jogadores</span>
      <button class="filtro-jog-btn" onclick="jogStep(1)">+</button>
    </div>
    <span class="filtros-count" id="contagem"></span>
  </div>
  <div class="cats-bar" id="cats-bar"></div>

  <div class="banner-section" id="semana-section" style="display:none">
    <div class="carrossel-wrap" id="carrossel-wrap"
         onmouseenter="carrosselPause()" onmouseleave="carrosselPlay()">
      <div id="semana-grid"></div>
      <button class="carr-arrow carr-prev" onclick="carrosselNav(-1)">&#8249;</button>
      <button class="carr-arrow carr-next" onclick="carrosselNav(1)">&#8250;</button>
      <div class="carr-dots" id="carr-dots"></div>
    </div>
  </div>

  <div class="catalogo" id="grid"></div>

  <footer>
    Jogoteka — {{ cidade_nome }} &mdash;
    <a href="https://wa.me/{{ cidade_wa }}" target="_blank">📞 WhatsApp</a>
    &nbsp;|&nbsp; Entre em contato pelo WhatsApp para comprar ou alugar
    {% if cidades|length > 1 %}
    <br><br><a href="/catalogo" style="color:var(--muted);font-size:.85rem">🏙️ Trocar cidade</a>
    {% endif %}
  </footer>

  <!-- Botão flutuante -->
  <button class="fab-cadastro" id="fab-cadastro" onclick="clicouFab()">
    ❤️ <span id="fab-label">Salvar favoritos</span>
  </button>

  <!-- Modal de cadastro -->
  <div class="modal-cad-overlay" id="modal-cad-overlay" onclick="fecharModalCad(event)">
    <div class="modal-cad">
      <h3>❤️ Seus Favoritos</h3>
      <p id="modal-cad-desc">Cadastre-se para salvar seus jogos favoritos e acessá-los sempre!</p>
      <div id="modal-cad-form">
        <input id="cad-nome" type="text" placeholder="Ex: Maria Silva" autocomplete="name">
        <input id="cad-tel" type="tel" placeholder="Ex: (48) 99999-9999" autocomplete="tel">
        <button class="btn-entrar" onclick="cadastrarCliente()">Entrar →</button>
      </div>
      <div id="modal-cad-logado" style="display:none;text-align:center">
        <p style="font-size:1.1rem;margin:.5rem 0">👋 Olá, <strong id="cad-nome-logado"></strong>!</p>
        <p style="font-size:.85rem;color:var(--muted)">Seus favoritos estão sendo salvos. ❤️</p>
        <button class="btn-entrar" style="margin-top:.8rem;background:#e53e3e" onclick="sairCad()">Sair</button>
      </div>
      <button class="btn-fechar" onclick="fecharModalCad()">Fechar</button>
    </div>
  </div>

  <script>
    const WA     = "{{ cidade_wa }}";
    const CIDADE = "{{ cidade_slug }}";
    let todos = [];
    let catAtiva = "";

    /* ── Ícones de categoria: estilo linha fina 24×24 ── */
    const SVG_TODOS      = `<svg viewBox="0 0 24 24" fill="none" stroke="#4a5568" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>`;
    const SVG_ADULTO     = `<svg viewBox="0 0 24 24" fill="none" stroke="#4a5568" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 3h10Q20 8 17 13Q15 16 12 16Q9 16 7 13Q4 8 7 3Z"/><line x1="12" y1="16" x2="12" y2="21"/><line x1="8" y1="21" x2="16" y2="21"/></svg>`;
    const SVG_EDUCATIVO  = `<svg viewBox="0 0 24 24" fill="none" stroke="#4a5568" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="9" r="6"/><path d="M9 15v3h6v-3"/><line x1="9" y1="18" x2="15" y2="18"/><line x1="10" y1="22" x2="14" y2="22"/></svg>`;
    const SVG_FAMILIA    = `<svg viewBox="0 0 24 24" fill="none" stroke="#4a5568" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 11L12 3l9 8v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M9 22v-7h6v7"/></svg>`;
    const SVG_INFANTIL   = `<svg viewBox="0 0 24 24" fill="none" stroke="#4a5568" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12,2 15.09,8.26 22,9.27 17,14.14 18.18,21.02 12,17.77 5.82,21.02 7,14.14 2,9.27 8.91,8.26"/></svg>`;
    const SVG_COOPERATIVO= `<svg viewBox="0 0 24 24" fill="none" stroke="#4a5568" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="7" r="4"/><path d="M3 21v-2a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v2"/><path d="M19 7a4 4 0 0 1 0 7.75"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/></svg>`;
    const SVG_PARTYGAME  = `<svg viewBox="0 0 24 24" fill="none" stroke="#4a5568" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 20L13 4l6 16H7z"/><line x1="5" y1="20" x2="21" y2="20"/><circle cx="13" cy="4" r="1.5" fill="#4a5568" stroke="none"/><circle cx="4" cy="10" r="1" fill="#4a5568" stroke="none"/><circle cx="3" cy="16" r="1" fill="#4a5568" stroke="none"/><circle cx="21" cy="9" r="1" fill="#4a5568" stroke="none"/><circle cx="22" cy="15" r="1" fill="#4a5568" stroke="none"/></svg>`;
    const SVG_ESTRATEGICO= `<svg viewBox="0 0 24 24" fill="none" stroke="#4a5568" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 17h18v3H3z"/><polyline points="3,17 6,8 12,13 18,6 21,17"/><circle cx="6" cy="8" r="1.5" fill="#4a5568" stroke="none"/><circle cx="12" cy="13" r="1.5" fill="#4a5568" stroke="none"/><circle cx="18" cy="6" r="1.5" fill="#4a5568" stroke="none"/></svg>`;

    // Renomeia categorias para exibição nos botões de filtro
    const CAT_LABELS = {
      "estratégico": "expert", "estrategico": "expert",
      "estratégia":  "expert", "estrategia":  "expert",
    };

    // Enquadramento emocional por categoria (substitui rótulo taxonômico no card)
    const CAT_TAGLINES = {
      "família":      "✨ Diversão para toda a família",
      "familia":      "✨ Diversão para toda a família",
      "infantil":     "🌟 Alegria garantida às crianças",
      "cooperativo":  "🤝 Todos contra o jogo — ninguém fica de fora",
      "cooperativos": "🤝 Todos contra o jogo — ninguém fica de fora",
      "party game":   "🎉 A festa começa aqui",
      "estratégico":  "🧠 Desafie sua mente",
      "estrategico":  "🧠 Desafie sua mente",
      "estratégia":   "🧠 Desafie sua mente",
      "estrategia":   "🧠 Desafie sua mente",
      "expert":       "🧠 Desafie sua mente",
      "rpg":          "🐉 Sua próxima jornada épica",
      "abstrato":     "🔷 Puro desafio mental",
      "abstratos":    "🔷 Puro desafio mental",
      "temático":     "🧙 Entre em outro mundo",
      "tematico":     "🧙 Entre em outro mundo",
      "temáticos":    "🧙 Entre em outro mundo",
      "tematicos":    "🧙 Entre em outro mundo",
      "memória":      "💡 Treine seu cérebro",
      "memoria":      "💡 Treine seu cérebro",
      "memory":       "💡 Treine seu cérebro",
      "dedução":      "🔍 O detetive está em você",
      "deducao":      "🔍 O detetive está em você",
      "guerra":       "⚔️ Conquiste o mundo",
      "aventura":     "🗺️ Viva uma aventura",
      "econômico":    "💰 Construa seu império",
      "economico":    "💰 Construa seu império",
      "dados":        "🎲 A sorte está lançada",
      "trivia":       "🏆 Quem sabe mais?",
      "quiz":         "🏆 Quem sabe mais?",
      "blefe":        "🃏 Quem vai cair na sua?",
      "palavra":      "📝 Jogue com as palavras",
      "palavras":     "📝 Jogue com as palavras",
    };

    const CAT_ICONS = {
      "estratégia": SVG_ESTRATEGICO,"estrategia": SVG_ESTRATEGICO,"estratégico": SVG_ESTRATEGICO,"estrategico": SVG_ESTRATEGICO,
      "expert": SVG_ESTRATEGICO,
      "adulto": SVG_ADULTO,"adultos": SVG_ADULTO,
      "educativo": SVG_EDUCATIVO,"educativos": SVG_EDUCATIVO,
      "família": SVG_FAMILIA,"familia": SVG_FAMILIA,
      "cooperativo": SVG_COOPERATIVO,"cooperativos": SVG_COOPERATIVO,
      "infantil": SVG_INFANTIL,"infantis": SVG_INFANTIL,
      "party game": SVG_PARTYGAME,"party games": SVG_PARTYGAME,"festa":SVG_PARTYGAME,"jogo de festa":SVG_PARTYGAME,
      "temático":"🧙","tematico":"🧙","temáticos":"🧙","tematicos":"🧙",
      "abstrato":"🔷","abstratos":"🔷",
      "rpg":"🐉",
      "quiz":"❓",
      "memory":"🃏","memória":"🃏","memoria":"🃏",
      "dedução":"🔍","deducao":"🔍","dedução lógica":"🔍",
      "blefe":"🃏",
      "dados":"🎲",
      "palavra":"📝","palavras":"📝",
      "trivia":"🏆",
      "econômico":"💰","economico":"💰",
      "guerra":"⚔️",
      "aventura":"🗺️",
      "construção":"🏗️","construcao":"🏗️",
    };

    function iconCat(cat){
      if(!cat) return "🎮";
      return CAT_ICONS[cat.toLowerCase()] || "🎮";
    }

    function renderCats(){
      const bar = document.getElementById("cats-bar");

      // Deduplica por label de exibição (evita "expert" + "Expert" duplicados)
      const labelMap = {}; // label_normalizado → valor_original_primeiro_encontrado
      todos.forEach(j => {
        if(!j.categoria) return;
        const label = (CAT_LABELS[j.categoria.toLowerCase()] || j.categoria).toLowerCase();
        if(!labelMap[label]) labelMap[label] = j.categoria;
      });
      const cats = Object.values(labelMap).sort((a,b) =>
        (CAT_LABELS[a.toLowerCase()]||a).localeCompare(CAT_LABELS[b.toLowerCase()]||b, 'pt')
      );

      let html = `<button class="cat-btn${catAtiva===""?" ativo":""}" onclick="selecionarCat('')">
        <span class="icon">${SVG_TODOS}</span>Todos
      </button>`;
      cats.forEach(c => {
        const ic = iconCat(c);
        const label = CAT_LABELS[c.toLowerCase()] || c;
        html += `<button class="cat-btn${catAtiva===c?" ativo":""}" onclick="selecionarCat('${c.replace(/'/g,"\\'")}')">
          <span class="icon">${ic}</span>${label}
        </button>`;
      });
      bar.innerHTML = html;
    }

    function selecionarCat(cat){
      catAtiva = cat;
      renderCats();
      filtrar();
    }

    function fmtVal(v){
      if(v==null||v===undefined) return null;
      return "R$ " + Number(v).toLocaleString("pt-BR",{minimumFractionDigits:2,maximumFractionDigits:2});
    }

    // ── Curtidas ──────────────────────────────────────────────────────────────
    function _likedSet(){
      try{ return new Set(JSON.parse(localStorage.getItem("jgt_liked")||"[]")); }
      catch(e){ return new Set(); }
    }
    function _saveLiked(set){
      localStorage.setItem("jgt_liked", JSON.stringify([...set]));
    }

    async function curtir(jogoId, el){
      const liked = _likedSet();
      const jaLikei = liked.has(jogoId);
      // atualiza localStorage
      if(jaLikei){ liked.delete(jogoId); } else { liked.add(jogoId); }
      _saveLiked(liked);
      // chama API e usa o count REAL retornado
      const url = jaLikei
        ? `/api/catalogo/curtir/${jogoId}?undo=1`
        : `/api/catalogo/curtir/${jogoId}`;
      const res = await fetch(url, {method:"POST"});
      const d   = await res.json();
      // atualiza o array em memória com o valor real do banco
      const jogo = todos.find(j=>j.id===jogoId);
      if(jogo) jogo.curtidas = d.curtidas;
      // atualiza todos os botões deste jogo na página
      document.querySelectorAll(`[data-curtir="${jogoId}"]`).forEach(btn=>{
        _renderCurtirBtn(btn, jogo||{curtidas:d.curtidas,id:jogoId}, liked.has(jogoId));
      });
    }

    function _renderCurtirBtn(btn, j, jaLikei){
      const c = j.curtidas||0;
      btn.className = "curtir-btn" + (jaLikei?" curtido":"");
      btn.title = jaLikei ? "Remover curtida" : "Curtir";
      btn.innerHTML = `<span class="heart">${jaLikei?"👍":"👍🏻"}</span>${c>0?` <span class="curtir-count">${c}</span>`:""}`;
    }

    function buildCurtirBtn(j){
      const liked = _likedSet();
      const jaLikei = liked.has(j.id);
      const c = j.curtidas||0;
      return `<button class="curtir-btn${jaLikei?" curtido":""}" data-curtir="${j.id}"
        title="${jaLikei?"Remover curtida":"Curtir"}" onclick="curtir(${j.id},this)">
        <span class="heart">${jaLikei?"👍":"👍🏻"}</span>${c>0?`<span class="curtir-count">${c}</span>`:""}
      </button>`;
    }

    // ── Favoritos ──────────────────────────────────────────────────────────────
    let _favSet = new Set();
    let _cadToken = localStorage.getItem("jgt_token") || "";
    let _cadNome  = localStorage.getItem("jgt_nome")  || "";

    function buildFavBtn(j){
      const fav = _cadToken && _favSet.has(j.id);
      return `<button class="fav-btn${fav?" favoritado":""}" title="${fav?"Remover dos favoritos":"Favoritar"}"
        onclick="toggleFav(${j.id},this)">
        <span class="fav-icon">${fav?"❤️":"🤍"}</span>
      </button>`;
    }

    async function toggleFav(jogoId, btn){
      if(!_cadToken){ abrirModalCad(); return; }
      const r = await fetch(`/api/catalogo/favoritos/${jogoId}?token=${_cadToken}`, {method:"POST"});
      const d = await r.json();
      const icon = btn.querySelector(".fav-icon");
      if(d.favoritado){
        _favSet.add(jogoId);
        btn.classList.add("favoritado");
        btn.title = "Remover dos favoritos";
        if(icon) icon.textContent = "❤️";
      } else {
        _favSet.delete(jogoId);
        btn.classList.remove("favoritado");
        btn.title = "Favoritar";
        if(icon) icon.textContent = "🤍";
      }
    }

    async function carregarFavoritos(){
      if(!_cadToken) return;
      const r = await fetch(`/api/catalogo/favoritos?token=${_cadToken}`);
      const ids = await r.json();
      _favSet = new Set(ids);
    }

    function abrirModalCad(){
      const overlay = document.getElementById("modal-cad-overlay");
      overlay.classList.add("open");
      if(_cadToken){
        document.getElementById("modal-cad-form").style.display="none";
        document.getElementById("modal-cad-logado").style.display="block";
        document.getElementById("cad-nome-logado").textContent = _cadNome;
      } else {
        document.getElementById("modal-cad-form").style.display="block";
        document.getElementById("modal-cad-logado").style.display="none";
      }
    }

    function fecharModalCad(e){
      if(!e || e.target === document.getElementById("modal-cad-overlay"))
        document.getElementById("modal-cad-overlay").classList.remove("open");
    }

    async function cadastrarCliente(){
      const nome = document.getElementById("cad-nome").value.trim();
      const tel  = document.getElementById("cad-tel").value.trim();
      if(!nome || !tel){ alert("Preencha nome e telefone."); return; }
      const r = await fetch("/api/catalogo/cadastro", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({nome, telefone: tel})
      });
      const d = await r.json();
      if(d.erro){ alert(d.erro); return; }
      _cadToken = d.token; _cadNome = d.nome;
      localStorage.setItem("jgt_token", _cadToken);
      localStorage.setItem("jgt_nome",  _cadNome);
      await carregarFavoritos();
      _atualizarFab();
      fecharModalCad();
      // re-renderiza cards para mostrar botão de favorito
      filtrar();
    }

    function sairCad(){
      _cadToken=""; _cadNome=""; _favSet=new Set();
      localStorage.removeItem("jgt_token");
      localStorage.removeItem("jgt_nome");
      _atualizarFab();
      fecharModalCad();
      filtrar();
    }

    let _modoFav = false;

    function clicouFab(){
      if(!_cadToken){ abrirModalCad(); return; }
      if(_modoFav){ sairModoFav(); return; }
      // entra no modo favoritos
      _modoFav = true;
      document.getElementById("fav-banner").classList.add("ativo");
      document.getElementById("fab-cadastro").classList.remove("logado");
      document.getElementById("fab-cadastro").classList.add("mostrando-fav");
      document.getElementById("fab-label").textContent = "Ver todos ✕";
      filtrar();
    }

    function sairModoFav(){
      _modoFav = false;
      document.getElementById("fav-banner").classList.remove("ativo");
      const fab = document.getElementById("fab-cadastro");
      fab.classList.remove("mostrando-fav");
      fab.classList.add("logado");
      document.getElementById("fab-label").textContent = "Meus favoritos";
      filtrar();
    }

    function _atualizarFab(){
      const fab = document.getElementById("fab-cadastro");
      const lbl = document.getElementById("fab-label");
      if(_cadToken){
        fab.classList.add("logado");
        lbl.textContent = "Meus favoritos";
      } else {
        fab.classList.remove("logado");
        lbl.textContent = "Salvar favoritos";
      }
    }

    function calcAncora(j){
      // âncora: menor valor de locação ÷ max_jogadores = "a partir de R$X/pessoa"
      const ops = [[j.loc1_valor],[j.loc2_valor],[j.loc3_valor]].map(([v])=>v).filter(v=>v!=null);
      const maxJog = j.max_jogadores || 0;
      if(!ops.length || maxJog < 2) return "";
      const menor = Math.min(...ops);
      const pp = (menor / maxJog).toFixed(2).replace(".", ",");
      return `<div class="ancora-valor">👥 <span class="ancora-num">R$ ${pp}/pessoa</span> para até ${maxJog} jogadores</div>`;
    }

    const _BANNER_CORES = ["bc-red","bc-orange","bc-green","bc-purple"];
    function buildHeroCard(j, idx=0){
      const _corBanner = _BANNER_CORES[idx % _BANNER_CORES.length];
      const disp = j.quantidade > 0;
      const temVenda = j.preco_venda != null;
      const temLoc   = j.loc1_valor != null || j.loc2_valor != null || j.loc3_valor != null;
      const opcLoc   = [[j.loc1_dias,j.loc1_valor],[j.loc2_dias,j.loc2_valor],[j.loc3_dias,j.loc3_valor]].filter(([d,v])=>d&&v!=null);

      const txtCompra = encodeURIComponent(`Olá! Tenho interesse em *comprar* o jogo *${j.nome}*. Poderia me dar mais informações?`);

      const btnComprar = temVenda
        ? `<a class="hero-btn hero-btn-comprar" href="https://wa.me/${WA}?text=${txtCompra}" target="_blank">🛒 Comprar</a>`
        : `<span class="hero-btn hero-btn-comprar" style="opacity:.45;cursor:not-allowed">🛒 Consultar</span>`;
      const btnAlugar = temLoc && opcLoc.length
        ? opcLoc.map(([dias,val])=>{
            const txt = encodeURIComponent(`Olá! Gostaria de *alugar* o jogo *${j.nome}* por *${dias} dia${dias>1?"s":""}*. O valor seria *${fmtVal(val)}*. Poderia confirmar a disponibilidade?`);
            return `<a class="hero-btn hero-btn-alugar" href="https://wa.me/${WA}?text=${txt}" target="_blank">🔑 Alugar ${dias} dia${dias>1?"s":""}\u00a0—\u00a0${fmtVal(val)}</a>`;
          }).join("") : "";

      const locHtml = opcLoc.length
        ? `<div class="banner-loc">${opcLoc.map(([d,v])=>`<span class="banner-loc-item"><span class="b-dias">${d} dia${d>1?"s":""}</span><span class="b-val">${fmtVal(v)}</span></span>`).join("")}</div>` : "";

      const metaTags = [
        disp ? `<span class="banner-tag disp">✅ Disponível</span>`
             : `<span class="banner-tag indisp">❌ Indisponível</span>`,
        j.min_jogadores ? `<span class="banner-tag">👥 ${j.min_jogadores}–${j.max_jogadores}</span>` : "",
        j.tempo_jogo    ? `<span class="banner-tag">⏱ ${j.tempo_jogo} min</span>` : "",
        j.faixa_etaria  ? `<span class="banner-tag">👶 ${j.faixa_etaria}</span>` : "",
      ].filter(Boolean).join("");

      const ancora = calcAncora(j);
      let ancoraTexto = "";
      if(opcLoc.length && j.max_jogadores){
        const _m = Math.min(...opcLoc.map(([,v])=>v));
        const _pp = (_m/(j.max_jogadores||1)).toFixed(2).replace(".",",");
        ancoraTexto = '<div class="banner-ancora">👥 A partir de R$ ' + _pp + '/pessoa para até ' + j.max_jogadores + ' jogadores</div>';
      }

      const curtidas = j.curtidas||0;
      const socialTexto = curtidas>=5
        ? `<div class="banner-social">🔥 ${curtidas} pessoas curtiram este jogo esta semana</div>` : "";

      return `<div class="banner-card ${_corBanner}">
        <div class="banner-content">
          <div class="banner-badge">🏆 A Escolha da Comunidade</div>
          <div class="banner-nome">${j.nome}</div>
          ${j.resumo ? `<div class="banner-tagline">${j.resumo}</div>` : ""}
          <div class="banner-meta">${metaTags}</div>
          ${socialTexto}
          <div class="banner-btns">
            ${j.video_url ? `<button class="banner-btn-video" onclick="abrirVideoCat('${encodeURIComponent(j.video_url)}','${j.nome.replace(/'/g,'')}')">▶ Ver vídeo</button>` : ""}
          </div>
        </div>
        <div class="banner-img-side">
          ${j.imagem ? `<img src="/api/imagens/${j.imagem}" alt="${j.nome}" onerror="this.style.display='none'">` : `<span class="no-img">🎲</span>`}
        </div>
      </div>`;
    }

    let _carrIdx = 0;
    let _carrTimer = null;
    let _carrTotal = 0;

    function renderSemana(){
      const destaques = todos.filter(j => j.em_destaque);
      const sec = document.getElementById("semana-section");
      if(!destaques.length){ sec.style.display="none"; return; }
      sec.style.display="";
      _carrTotal = destaques.length;
      document.getElementById("semana-grid").innerHTML = destaques.map((j,i)=>buildHeroCard(j,i)).join("");

      // gera dots
      const dotsEl = document.getElementById("carr-dots");
      dotsEl.innerHTML = _carrTotal > 1
        ? destaques.map((_,i) => `<button class="carr-dot${i===0?" ativo":""}" onclick="carrosselIr(${i})"></button>`).join("")
        : "";

      // setas só aparecem se tiver mais de 1
      document.querySelector(".carr-prev").style.display = _carrTotal > 1 ? "" : "none";
      document.querySelector(".carr-next").style.display = _carrTotal > 1 ? "" : "none";

      carrosselIr(0);
      if(_carrTotal > 1) carrosselPlay();
    }

    function carrosselIr(idx){
      _carrIdx = (idx + _carrTotal) % _carrTotal;
      const cards = document.querySelectorAll("#semana-grid .banner-card");
      const dots  = document.querySelectorAll(".carr-dot");
      cards.forEach((c,i) => c.classList.toggle("ativo", i === _carrIdx));
      dots.forEach((d,i)  => d.classList.toggle("ativo",  i === _carrIdx));
    }

    function carrosselNav(dir){ carrosselIr(_carrIdx + dir); }

    function carrosselPlay(){
      carrosselPause();
      if(_carrTotal > 1) _carrTimer = setInterval(() => carrosselNav(1), 5000);
    }

    function carrosselPause(){
      if(_carrTimer){ clearInterval(_carrTimer); _carrTimer = null; }
    }

    function buildBadges(j){
      const cat = (j.categoria||"").toLowerCase();
      const bs = [];
      if(j.destaque) bs.push(`<span class="badge-item badge-destaque">⭐ ${j.destaque}</span>`);
      if(j.tempo_jogo && j.tempo_jogo <= 30) bs.push(`<span class="badge-item badge-rapido">⚡ Rápido</span>`);
      if(j.tempo_jogo && j.tempo_jogo >= 120) bs.push(`<span class="badge-item badge-longo">⏳ Longo</span>`);
      if(cat.includes("infantil")) bs.push(`<span class="badge-item badge-kids">👶 Kids</span>`);
      if(cat.includes("cooperat")) bs.push(`<span class="badge-item badge-coop">🤝 Coop</span>`);
      if(cat.includes("party") || cat.includes("festa")) bs.push(`<span class="badge-item badge-party">🎉 Party</span>`);
      if(cat.includes("estrateg") || cat.includes("expert")) bs.push(`<span class="badge-item badge-expert">🧠 Expert</span>`);
      return bs.length ? `<div class="badges">${bs.join("")}</div>` : "";
    }

    function buildCard(j){
      const disp  = j.quantidade > 0;
      const temVenda = j.preco_venda != null;
      const temLoc   = j.loc1_valor != null || j.loc2_valor != null || j.loc3_valor != null;

      const opcLoc = [];
      if(j.loc1_dias && j.loc1_valor) opcLoc.push([j.loc1_dias, j.loc1_valor]);
      if(j.loc2_dias && j.loc2_valor) opcLoc.push([j.loc2_dias, j.loc2_valor]);
      if(j.loc3_dias && j.loc3_valor) opcLoc.push([j.loc3_dias, j.loc3_valor]);

      const jogadores = (j.min_jogadores || j.max_jogadores)
        ? `👥 ${j.min_jogadores||"?"}–${j.max_jogadores||"?"}` : "";
      const tempo = j.tempo_jogo ? `⏱ ${j.tempo_jogo} min` : "";


      const btnComprar = temVenda
        ? `<span class="preco-tag preco-tag-comprar">🛒 Comprar — ${fmtVal(j.preco_venda)}</span>`
        : "";

      const btnAlugar = temLoc && opcLoc.length
        ? opcLoc.map(([dias,val])=>
            `<span class="preco-tag preco-tag-alugar">🔑 Alugar ${dias} dia${dias>1?"s":""} — ${fmtVal(val)}</span>`
          ).join("") : "";

      const dispBadge = disp
        ? `<span class="card-status status-disp">✅ Disponível</span>`
        : `<span class="card-status status-indisp">❌ Indisponível</span>`;

      const locHtml = opcLoc.length
        ? `<div class="preco-loc">
             <div class="preco-loc-titulo">🔑 Locação</div>
             ${opcLoc.map(([dias,val])=>`<div class="preco-loc-item"><span class="dias">${dias} dia${dias>1?"s":""}</span><span class="val">${fmtVal(val)}</span></div>`).join("")}
           </div>`
        : "";

      const catTagline = j.categoria ? (CAT_TAGLINES[j.categoria.toLowerCase()]||null) : null;
      const metaTags = [
        catTagline
          ? `<span class="tag tagline">${catTagline}</span>`
          : (j.categoria ? `<span class="tag cat">${j.categoria}</span>` : ""),
        jogadores ? `<span class="tag">${jogadores}</span>` : "",
        tempo ? `<span class="tag">${tempo}</span>` : "",
        j.faixa_etaria ? `<span class="tag">👶 ${j.faixa_etaria}</span>` : "",
      ].filter(Boolean).join("");

      return `
        <div class="card" data-nome="${j.nome.toLowerCase()}" data-cat="${(j.categoria||"").toLowerCase()}" data-disp="${disp?1:0}">
          <div class="card-img-wrap">
            ${j.imagem ? `<img src="/api/imagens/${j.imagem}" alt="${j.nome}" onerror="this.parentElement.innerHTML='<span class=no-img>🎲</span>'">` : `<span class="no-img">🎲</span>`}
            ${buildBadges(j)}
          </div>
          <div class="card-body">
            <div class="card-nome">${j.nome}</div>
            ${j.editora ? `<div class="card-editora">${j.editora}</div>` : ""}
            <div class="card-meta">${metaTags}</div>
            ${j.resumo ? `<div class="resumo-wrap"><div class="card-resumo" id="resumo-${j.id}">${j.resumo}</div><button class="btn-resumo" id="btn-resumo-${j.id}" onclick="toggleResumo(${j.id})">ver mais ▾</button></div>` : ""}
            <div class="card-acoes">
              <div class="card-acoes-row1">
                ${dispBadge}
                <div style="display:flex;align-items:center;gap:.4rem">
                  ${buildCurtirBtn(j)}
                  ${buildFavBtn(j)}
                </div>
              </div>
              ${j.video_url ? `<button class="btn-video-cat" onclick="abrirVideoCat('${encodeURIComponent(j.video_url)}','${j.nome.replace(/'/g,'')}')">▶ Ver vídeo</button>` : ""}
            </div>
            <div class="card-precos">
              ${calcAncora(j)}
            </div>
          </div>
          <div class="card-btns">
            ${btnComprar}
            ${btnAlugar}
            ${(temVenda || (temLoc && opcLoc.length)) ? `<button class="btn-add-cart" data-id="${j.id}" onclick="abrirPickerById(${j.id})">+ Adicionar ao carrinho</button>` : ""}
          </div>
        </div>`;
    }

    let _jogVal = 0;  // 0 = sem filtro

    function jogStep(dir){
      _jogVal = Math.max(0, Math.min(10, _jogVal + dir));
      const label = document.getElementById("jog-label");
      if(_jogVal === 0)       label.textContent = "👥 Jogadores";
      else if(_jogVal === 10) label.textContent = "👥 10+ jogadores";
      else                    label.textContent = `👥 ${_jogVal} jogador${_jogVal>1?"es":""}`;
      filtrar();
    }

    function popularFiltrosCatalogo(){
      const faixas = [...new Set(todos.map(j=>j.faixa_etaria).filter(Boolean))].sort();
      const selFaixa = document.getElementById("filtro-faixa");
      selFaixa.innerHTML = '<option value="">🎂 Faixa etária</option>';
      faixas.forEach(f => selFaixa.innerHTML += `<option value="${f}">${f}</option>`);
    }

    function filtrar(){
      const busca    = document.getElementById("busca").value.toLowerCase().trim();
      const disp     = document.getElementById("filtro-disp").value;
      const faixa    = document.getElementById("filtro-faixa").value;
      const jogadores = _jogVal;
      const grid     = document.getElementById("grid");

      // esconde banner quando qualquer filtro estiver ativo
      const temFiltro = busca || disp !== "" || faixa || jogadores || catAtiva || _modoFav;
      const sec = document.getElementById("semana-section");
      if(sec) sec.style.display = temFiltro ? "none" : "";

      const filtrados = todos.filter(j => {
        const matchBusca = !busca || j.nome.toLowerCase().includes(busca) ||
                           (j.editora||"").toLowerCase().includes(busca) ||
                           (j.categoria||"").toLowerCase().includes(busca);
        const matchCat   = !catAtiva || (j.categoria||"").toLowerCase() === catAtiva.toLowerCase();
        const matchDisp  = disp==="" || (disp==="1" ? j.quantidade>0 : j.quantidade===0);
        const matchFaixa = !faixa || j.faixa_etaria === faixa;
        const matchFav   = !_modoFav || _favSet.has(j.id);
        const matchJog   = !jogadores || (
          jogadores >= 10
            ? (j.max_jogadores >= 10)
            : (j.min_jogadores <= jogadores && j.max_jogadores >= jogadores)
        );
        return matchBusca && matchCat && matchDisp && matchFaixa && matchJog && matchFav;
      });

      document.getElementById("contagem").textContent =
        `${filtrados.length} jogo${filtrados.length!==1?"s":""} encontrado${filtrados.length!==1?"s":""}`;

      if(filtrados.length === 0){
        const msg = _modoFav
          ? `<div class="vazio"><div class="icon">❤️</div><div>Você ainda não favoritou nenhum jogo.<br><small>Explore o catálogo e clique em ❤️ Favoritar para salvar!</small></div></div>`
          : `<div class="vazio"><div class="icon">🎲</div><div>Nenhum jogo encontrado</div></div>`;
        grid.innerHTML = msg;
        return;
      }
      grid.innerHTML = filtrados.map(buildCard).join("");
    }

    function toggleResumo(id){
      const el  = document.getElementById("resumo-"+id);
      const btn = document.getElementById("btn-resumo-"+id);
      const expandido = el.classList.toggle("expandido");
      btn.textContent = expandido ? "ver menos ▴" : "ver mais ▾";
      // expandido: sai do absolute e fica como bloco normal abaixo do texto
      if(expandido){
        btn.style.position   = "static";
        btn.style.background = "none";
        btn.style.padding    = "0";
        btn.style.lineHeight = "normal";
        btn.style.display    = "block";
        btn.style.marginTop  = ".2rem";
      } else {
        btn.style.cssText = "";   // restaura CSS da classe
      }
    }

    async function init(){
      // Carrega logo
      try{
        const lr = await fetch("/api/logo");
        if(lr.ok){
          const b = await lr.blob();
          const u = URL.createObjectURL(b);
          document.getElementById("cat-logo-img").innerHTML =
            `<img class="logo-img-cat" src="${u}" alt="Jogoteka">`;
        } else {
          document.getElementById("cat-logo-img").innerHTML =
            `<span class="logo-text"><span class="j">J</span><span class="o1">o</span><span class="g">g</span><span class="o2">o</span><span class="t">t</span><span class="e">e</span><span class="k">k</span><span class="a">a</span></span>`;
        }
      } catch(e){
        document.getElementById("cat-logo-img").innerHTML =
          `<span class="logo-text"><span class="j">J</span><span class="o1">o</span><span class="g">g</span><span class="o2">o</span><span class="t">t</span><span class="e">e</span><span class="k">k</span><span class="a">a</span></span>`;
      }

      const url = CIDADE ? `/api/catalogo?cidade=${CIDADE}` : "/api/catalogo";
      const r = await fetch(url);
      todos = await r.json();

      // carrega favoritos se tiver sessão
      await carregarFavoritos();
      _atualizarFab();

      renderCats();
      popularFiltrosCatalogo();
      renderSemana();
      filtrar();
    }

    init();

    function embedUrl(url){
      url = decodeURIComponent(url);
      const yt = url.match(/(?:youtu\.be\/|v=|\/shorts\/)([A-Za-z0-9_-]{11})/);
      if(yt) return `https://www.youtube.com/embed/${yt[1]}?autoplay=1&rel=0`;
      const vm = url.match(/vimeo\.com\/(\d+)/);
      if(vm) return `https://player.vimeo.com/video/${vm[1]}?autoplay=1`;
      return url;
    }

    function abrirVideoCat(encodedUrl, nome){
      const embed = embedUrl(encodedUrl);
      const isFile = !embed.includes('youtube') && !embed.includes('vimeo');
      document.getElementById('video-overlay-titulo').textContent = nome;
      const player = document.getElementById('video-overlay-player');
      player.innerHTML = isFile
        ? `<video src="${embed}" controls autoplay style="width:100%;aspect-ratio:16/9"></video>`
        : `<iframe src="${embed}" allow="autoplay;fullscreen" allowfullscreen></iframe>`;
      document.getElementById('video-overlay').classList.add('open');
    }

    function fecharVideoCat(e){
      if(e && e.target !== document.getElementById('video-overlay') && !e.target.classList.contains('video-fechar')) return;
      document.getElementById('video-overlay').classList.remove('open');
      document.getElementById('video-overlay-player').innerHTML = '';
    }

    // ── Carrinho WhatsApp ──────────────────────────────────────────
    let _cart = []; // [{id,nome,tipo,dias,valor}]

    function abrirPickerById(id){
      const j = todos.find(x=>x.id===id);
      if(j) abrirPicker(j);
    }

    function abrirPicker(j){
      const opts = [];
      if(j.preco_venda != null)
        opts.push({tipo:'comprar', dias:null, valor:j.preco_venda,
          label:'🛒 Comprar', val:fmtVal(j.preco_venda)});
      const opcLoc = [];
      if(j.loc1_dias && j.loc1_valor) opcLoc.push([j.loc1_dias, j.loc1_valor]);
      if(j.loc2_dias && j.loc2_valor) opcLoc.push([j.loc2_dias, j.loc2_valor]);
      if(j.loc3_dias && j.loc3_valor) opcLoc.push([j.loc3_dias, j.loc3_valor]);
      opcLoc.forEach(([dias,val])=>opts.push({tipo:'alugar', dias, valor:val,
        label:`🔑 Alugar ${dias} dia${dias>1?"s":""}`, val:fmtVal(val)}));
      if(!opts.length) return;
      if(opts.length===1){ addToCart(j, opts[0]); return; }
      document.getElementById('pickerTitle').textContent = j.nome;
      document.getElementById('pickerOpts').innerHTML = opts.map((o,i)=>`
        <div class="picker-opt opt-${o.tipo}" onclick="pickerEscolheu(${i})">
          <span>${o.label}</span><span class="opt-val">${o.val}</span>
        </div>`).join('');
      window._pickerJogo = j;
      window._pickerOpts = opts;
      document.getElementById('pickerOverlay').classList.add('open');
    }

    function pickerEscolheu(i){
      addToCart(window._pickerJogo, window._pickerOpts[i]);
      fecharPicker();
    }

    function fecharPicker(e){
      if(e && e.target !== document.getElementById('pickerOverlay')) return;
      document.getElementById('pickerOverlay').classList.remove('open');
    }

    function addToCart(j, opc){
      _cart = _cart.filter(c=>c.id !== j.id);
      _cart.push({id:j.id, nome:j.nome, tipo:opc.tipo, dias:opc.dias, valor:opc.valor});
      atualizarCartUI();
      document.querySelectorAll(`.btn-add-cart[data-id="${j.id}"]`).forEach(btn=>{
        btn.classList.add('no-carrinho');
        btn.textContent = '✅ No carrinho';
      });
    }

    function removeFromCart(id){
      _cart = _cart.filter(c=>c.id !== id);
      atualizarCartUI();
      renderDrawer();
      document.querySelectorAll(`.btn-add-cart[data-id="${id}"]`).forEach(btn=>{
        btn.classList.remove('no-carrinho');
        btn.textContent = '+ Adicionar ao carrinho';
      });
    }

    function atualizarCartUI(){
      const n = _cart.length;
      document.getElementById('cartBadge').textContent = n;
      document.getElementById('cartBubble').classList.toggle('visible', n>0);
    }

    function abrirCart(){
      renderDrawer();
      document.getElementById('cartDrawerBg').classList.add('open');
    }

    function fecharCartDrawer(e){
      if(e && e.target !== document.getElementById('cartDrawerBg')) return;
      document.getElementById('cartDrawerBg').classList.remove('open');
    }

    function renderDrawer(){
      const cont = document.getElementById('drawerItems');
      const foot = document.getElementById('drawerFooter');
      if(!_cart.length){
        cont.innerHTML = '<div class="drawer-empty">Nenhum jogo adicionado ainda 🎲</div>';
        foot.innerHTML = '';
        return;
      }
      cont.innerHTML = _cart.map(c=>`
        <div class="drawer-item">
          <div class="drawer-item-info">
            <div class="drawer-item-nome">${c.nome}</div>
            <span class="drawer-item-opc opc-${c.tipo}">
              ${c.tipo==='alugar'?`🔑 Alugar ${c.dias} dia${c.dias>1?"s":""}` :'🛒 Comprar'}
            </span>
            <div class="drawer-item-val">${fmtVal(c.valor)}</div>
          </div>
          <button class="drawer-item-rm" onclick="removeFromCart(${c.id})" title="Remover">✕</button>
        </div>`).join('');
      const total = _cart.reduce((s,c)=>s+(c.valor||0), 0);
      foot.innerHTML = `
        <div class="drawer-total">
          <span class="drawer-total-label">Total estimado</span>
          <span class="drawer-total-val">${fmtVal(total)}</span>
        </div>
        <a class="btn-wa-cart" href="${montarLinkWA()}" target="_blank">
          💬 Enviar pedido pelo WhatsApp
        </a>
        <button class="btn-limpar-cart" onclick="limparCart()">🗑 Limpar carrinho</button>`;
    }

    function montarLinkWA(){
      const linhas = _cart.map(c=>c.tipo==='comprar'
        ? `${c.nome} — Comprar`
        : `${c.nome} — Alugar ${c.dias} dia${c.dias>1?"s":""}`);
      const msg = `Olá! Gostaria de fazer um pedido:\\n\\n${linhas.join('\\n')}`;
      return `https://wa.me/${WA}?text=${encodeURIComponent(msg)}`;
    }

    function limparCart(){
      _cart.forEach(c=>{
        document.querySelectorAll(`.btn-add-cart[data-id="${c.id}"]`).forEach(btn=>{
          btn.classList.remove('no-carrinho');
          btn.textContent = '+ Adicionar ao carrinho';
        });
      });
      _cart = [];
      atualizarCartUI();
      renderDrawer();
    }
  </script>

<!-- Modal vídeo catálogo -->
<div class="video-overlay" id="video-overlay" onclick="fecharVideoCat(event)">
  <div class="video-titulo" id="video-overlay-titulo"></div>
  <div class="video-wrap-cat">
    <button class="video-fechar" onclick="fecharVideoCat(event)">✕</button>
    <div id="video-overlay-player"></div>
  </div>
</div>

<!-- Picker de opção do carrinho -->
<div class="picker-overlay" id="pickerOverlay" onclick="fecharPicker(event)">
  <div class="picker-box">
    <div class="picker-title" id="pickerTitle"></div>
    <div id="pickerOpts"></div>
    <button class="picker-cancel" onclick="fecharPicker()">Cancelar</button>
  </div>
</div>

<!-- Bubble flutuante do carrinho -->
<button class="cart-bubble" id="cartBubble" onclick="abrirCart()">
  🛒 Carrinho <span class="cart-badge" id="cartBadge">0</span>
</button>

<!-- Drawer do carrinho -->
<div class="cart-drawer-bg" id="cartDrawerBg" onclick="fecharCartDrawer(event)">
  <div class="cart-drawer">
    <div class="drawer-header">
      <h2>🛒 Meu Carrinho</h2>
      <button class="drawer-close" onclick="fecharCartDrawer()">✕</button>
    </div>
    <div class="drawer-items" id="drawerItems"></div>
    <div class="drawer-footer" id="drawerFooter"></div>
  </div>
</div>

</body>
</html>
"""


@app.route("/catalogo")
def catalogo():
    cidade_slug = request.args.get("cidade", "").strip()
    # se não há cidade na URL e há mais de uma cidade cadastrada → landing
    if not cidade_slug and len(CIDADES) > 1:
        return render_template_string(CIDADES_LANDING_HTML, cidades=CIDADES)
    # cidade inválida → landing
    if cidade_slug and cidade_slug not in CIDADES:
        return render_template_string(CIDADES_LANDING_HTML, cidades=CIDADES)
    # cidade única ou slug válido → catálogo
    cidade_info = CIDADES.get(cidade_slug) or (list(CIDADES.values())[0] if CIDADES else None)
    cidade_slug_final = cidade_slug or (list(CIDADES.keys())[0] if CIDADES else "")
    return render_template_string(CATALOGO_HTML,
        cidade_slug=cidade_slug_final,
        cidade_nome=cidade_info["nome"] if cidade_info else "",
        cidade_wa=cidade_info["whatsapp"] if cidade_info else "",
        cidades=CIDADES)


@app.route("/api/catalogo")
def api_catalogo():
    cidade = request.args.get("cidade", "").strip()
    jogos = est.listar_jogos()
    campos = [
        "id","nome","editora","categoria","min_jogadores","max_jogadores","tempo_jogo",
        "quantidade","preco_venda","imagem","faixa_etaria","resumo","destaque","em_destaque",
        "curtidas","video_url","loc1_dias","loc1_valor","loc2_dias","loc2_valor",
        "loc3_dias","loc3_valor","cidades"
    ]
    resultado = [{c: j[c] for c in campos if c in j.keys()} for j in jogos]
    # filtra por cidade se informada
    # jogos sem cidade definida aparecem em todas as cidades
    if cidade:
        resultado = [j for j in resultado
                     if not (j.get("cidades") or "").strip()   # sem cidade = aparece em todas
                     or cidade in (j.get("cidades") or "").replace(" ","").split(",")]
    return jsonify(resultado)


@app.route("/api/catalogo/curtir/<int:jogo_id>", methods=["POST"])
def curtir_jogo(jogo_id):
    undo = request.args.get("undo") == "1"
    with get_connection() as conn:
        if undo:
            conn.execute(
                "UPDATE jogos SET curtidas = MAX(0, curtidas - 1) WHERE id = ?", (jogo_id,)
            )
        else:
            conn.execute(
                "UPDATE jogos SET curtidas = curtidas + 1 WHERE id = ?", (jogo_id,)
            )
        row = conn.execute("SELECT curtidas FROM jogos WHERE id = ?", (jogo_id,)).fetchone()
    return jsonify({"curtidas": row["curtidas"] if row else 0})


@app.route("/api/catalogo/cadastro", methods=["POST"])
def catalogo_cadastro():
    """Cadastra ou recupera cliente pelo telefone. Retorna token único."""
    import secrets as _sec
    d = request.get_json(force=True)
    nome = (d.get("nome") or "").strip()
    telefone = (d.get("telefone") or "").strip()
    if not nome or not telefone:
        return jsonify({"erro": "Nome e telefone são obrigatórios"}), 400

    agora = datetime.now().isoformat(sep=" ", timespec="seconds")
    with get_connection() as conn:
        # busca por telefone
        cli = conn.execute(
            "SELECT id, nome, catalogo_token FROM clientes WHERE telefone = ?", (telefone,)
        ).fetchone()

        if cli:
            token = cli["catalogo_token"]
            # garante que tem token (clientes antigos podem não ter)
            if not token:
                token = _sec.token_urlsafe(24)
                conn.execute(
                    "UPDATE clientes SET catalogo_token = ? WHERE id = ?",
                    (token, cli["id"])
                )
            return jsonify({"token": token, "nome": cli["nome"], "novo": False})
        else:
            token = _sec.token_urlsafe(24)
            cur = conn.execute(
                "INSERT INTO clientes (nome, telefone, catalogo_token, data_cadastro) VALUES (?,?,?,?)",
                (nome, telefone, token, agora)
            )
            return jsonify({"token": token, "nome": nome, "novo": True}), 201


@app.route("/api/catalogo/favoritos", methods=["GET"])
def catalogo_favoritos_get():
    """Retorna lista de jogo_ids favoritados pelo token."""
    token = request.args.get("token", "")
    if not token:
        return jsonify([])
    with get_connection() as conn:
        cli = conn.execute(
            "SELECT id FROM clientes WHERE catalogo_token = ?", (token,)
        ).fetchone()
        if not cli:
            return jsonify([])
        rows = conn.execute(
            "SELECT jogo_id FROM favoritos WHERE cliente_id = ?", (cli["id"],)
        ).fetchall()
    return jsonify([r["jogo_id"] for r in rows])


@app.route("/api/catalogo/favoritos/<int:jogo_id>", methods=["POST"])
def catalogo_favorito_toggle(jogo_id):
    """Marca ou desmarca favorito. Retorna {favoritado: bool}."""
    from datetime import datetime as _dt
    token = request.args.get("token", "")
    if not token:
        return jsonify({"erro": "Token inválido"}), 401
    agora = _dt.now().isoformat(sep=" ", timespec="seconds")
    with get_connection() as conn:
        cli = conn.execute(
            "SELECT id FROM clientes WHERE catalogo_token = ?", (token,)
        ).fetchone()
        if not cli:
            return jsonify({"erro": "Cliente não encontrado"}), 404
        existe = conn.execute(
            "SELECT id FROM favoritos WHERE cliente_id = ? AND jogo_id = ?",
            (cli["id"], jogo_id)
        ).fetchone()
        if existe:
            conn.execute(
                "DELETE FROM favoritos WHERE cliente_id = ? AND jogo_id = ?",
                (cli["id"], jogo_id)
            )
            return jsonify({"favoritado": False})
        else:
            conn.execute(
                "INSERT INTO favoritos (cliente_id, jogo_id, data) VALUES (?,?,?)",
                (cli["id"], jogo_id, agora)
            )
            return jsonify({"favoritado": True})


@app.route("/api/relatorio/favoritos")
@requer_perfil("admin", "gerente")
def relatorio_favoritos():
    """Clientes com favoritos para o relatório admin."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT c.id, c.nome, c.telefone, c.data_cadastro,
                   COUNT(f.jogo_id) as total_favoritos,
                   GROUP_CONCAT(j.nome, ' | ') as jogos_favoritos
            FROM clientes c
            JOIN favoritos f ON f.cliente_id = c.id
            JOIN jogos j ON j.id = f.jogo_id
            GROUP BY c.id
            ORDER BY total_favoritos DESC, c.nome
        """).fetchall()
    return jsonify([dict(r) for r in rows])


# ── Login / Logout / Setup ────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Jogoteka — Login</title>
  <link href="https://fonts.googleapis.com/css2?family=Fredoka+One&family=Nunito:wght@400;600;700;800&display=swap" rel="stylesheet">
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Nunito',sans-serif;background:#1a1a2e;color:#e0e0e0;
      min-height:100vh;display:flex;align-items:center;justify-content:center}
    .box{background:#16213e;border:1px solid rgba(255,255,255,.1);border-radius:16px;
      padding:2.5rem 2rem;width:100%;max-width:380px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
    .logo{font-family:'Fredoka One',cursive;font-size:2rem;text-align:center;margin-bottom:.3rem}
    .logo .j{color:#F10A0A}.logo .o1{color:#ED940E}.logo .g{color:#17C629}
    .logo .o2{color:#7B20E1}.logo .t{color:#F10A0A}.logo .e{color:#ED940E}
    .logo .k{color:#17C629}.logo .a{color:#7B20E1}
    .sub{text-align:center;font-size:.78rem;color:#8892a4;letter-spacing:2px;
      text-transform:uppercase;margin-bottom:2rem}
    label{display:block;font-size:.8rem;color:#8892a4;font-weight:600;margin:.8rem 0 .3rem}
    input{width:100%;padding:.6rem .9rem;background:rgba(255,255,255,.07);
      border:1px solid rgba(255,255,255,.1);border-radius:8px;color:white;
      font-size:.95rem;font-family:'Nunito',sans-serif;outline:none}
    input:focus{border-color:#ED940E}
    .btn{width:100%;margin-top:1.5rem;padding:.7rem;background:#ED940E;color:white;
      border:none;border-radius:8px;font-family:'Fredoka One',cursive;font-size:1.1rem;
      letter-spacing:.5px;cursor:pointer}
    .btn:hover{filter:brightness(1.1)}
    .erro{background:rgba(241,10,10,.12);border:1px solid rgba(241,10,10,.3);
      border-radius:8px;padding:.6rem .9rem;margin-bottom:1rem;color:#fc8181;font-size:.85rem}
  </style>
</head>
<body>
  <div class="box">
    <div style="text-align:center;margin-bottom:.3rem">
      <img src="/api/logo" alt="Jogoteka" style="max-height:72px;max-width:220px;object-fit:contain"
           onerror="this.style.display='none';document.getElementById('logo-txt-login').style.display='block'">
      <div class="logo" id="logo-txt-login" style="display:none">
        <span class="j">J</span><span class="o1">o</span><span class="g">g</span>
        <span class="o2">o</span><span class="t">t</span><span class="e">e</span>
        <span class="k">k</span><span class="a">a</span>
      </div>
    </div>
    <div class="sub">Sistema de Gestão</div>
    {% if erro %}<div class="erro">{{ erro }}</div>{% endif %}
    <form method="POST">
      <label>E-mail</label>
      <input type="email" name="email" placeholder="seu@email.com" required autofocus>
      <label>Senha</label>
      <input type="password" name="senha" placeholder="••••••••" required>
      <button class="btn" type="submit">Entrar</button>
    </form>
  </div>
</body>
</html>"""

SETUP_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Jogoteka — Configuração inicial</title>
  <link href="https://fonts.googleapis.com/css2?family=Fredoka+One&family=Nunito:wght@400;600;700;800&display=swap" rel="stylesheet">
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Nunito',sans-serif;background:#1a1a2e;color:#e0e0e0;
      min-height:100vh;display:flex;align-items:center;justify-content:center}
    .box{background:#16213e;border:1px solid rgba(255,255,255,.1);border-radius:16px;
      padding:2.5rem 2rem;width:100%;max-width:400px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
    h2{font-family:'Fredoka One',cursive;font-size:1.4rem;margin-bottom:.4rem;color:#ED940E}
    p{font-size:.85rem;color:#8892a4;margin-bottom:1.5rem}
    label{display:block;font-size:.8rem;color:#8892a4;font-weight:600;margin:.8rem 0 .3rem}
    input{width:100%;padding:.6rem .9rem;background:rgba(255,255,255,.07);
      border:1px solid rgba(255,255,255,.1);border-radius:8px;color:white;
      font-size:.95rem;font-family:'Nunito',sans-serif;outline:none}
    input:focus{border-color:#ED940E}
    .btn{width:100%;margin-top:1.5rem;padding:.7rem;background:#ED940E;color:white;
      border:none;border-radius:8px;font-family:'Fredoka One',cursive;font-size:1.1rem;
      letter-spacing:.5px;cursor:pointer}
    .btn:hover{filter:brightness(1.1)}
    .erro{background:rgba(241,10,10,.12);border:1px solid rgba(241,10,10,.3);
      border-radius:8px;padding:.6rem .9rem;margin-bottom:1rem;color:#fc8181;font-size:.85rem}
  </style>
</head>
<body>
  <div class="box">
    <div style="text-align:center;margin-bottom:1rem">
      <img src="/api/logo" alt="Jogoteka" style="max-height:64px;max-width:200px;object-fit:contain"
           onerror="this.style.display='none'">
    </div>
    <h2>🔧 Configuração inicial</h2>
    <p>Nenhum usuário cadastrado. Crie o primeiro administrador do sistema.</p>
    {% if erro %}<div class="erro">{{ erro }}</div>{% endif %}
    <form method="POST">
      <label>Nome</label>
      <input type="text" name="nome" placeholder="Seu nome" required autofocus>
      <label>E-mail</label>
      <input type="email" name="email" placeholder="seu@email.com" required>
      <label>Senha</label>
      <input type="password" name="senha" placeholder="Mínimo 6 caracteres" required minlength="6">
      <button class="btn" type="submit">Criar Administrador</button>
    </form>
  </div>
</body>
</html>"""

ADMIN_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Jogoteka — Administração</title>
  <link href="https://fonts.googleapis.com/css2?family=Fredoka+One&family=Nunito:wght@400;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root{--red:#F10A0A;--green:#17C629;--purple:#7B20E1;--orange:#ED940E;
          --dark:#1a1a2e;--dark2:#16213e;--dark3:#0f3460;--border:rgba(255,255,255,.1);
          --text:#e0e0e0;--muted:#8892a4}
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Nunito',sans-serif;background:var(--dark);color:var(--text);min-height:100vh}
    header{background:linear-gradient(135deg,var(--dark2) 0%,#1a0533 100%);
      border-bottom:3px solid var(--orange);padding:.6rem 2rem;
      display:flex;align-items:center;gap:1.2rem;flex-wrap:wrap}
    .logo{font-family:'Fredoka One',cursive;font-size:1.6rem}
    .logo .j{color:var(--red)}.logo .o1{color:var(--orange)}.logo .g{color:var(--green)}
    .logo .o2{color:var(--purple)}.logo .t{color:var(--red)}.logo .e{color:var(--orange)}
    .logo .k{color:var(--green)}.logo .a{color:var(--purple)}
    .header-sub{font-size:.65rem;color:var(--muted);letter-spacing:2px;text-transform:uppercase}
    nav{display:flex;gap:.5rem;margin-left:auto;align-items:center;flex-wrap:wrap}
    .nav-info{font-size:.82rem;color:var(--muted)}
    .btn-nav{background:rgba(255,255,255,.08);color:var(--text);border:1px solid var(--border);
      border-radius:8px;padding:.4rem .9rem;cursor:pointer;font-size:.82rem;
      font-family:'Fredoka One',cursive;letter-spacing:.3px;text-decoration:none;display:inline-block}
    .btn-nav:hover{background:var(--orange);color:white;border-color:var(--orange)}
    .btn-logout{background:rgba(241,10,10,.12);color:#fc8181;border:1px solid rgba(241,10,10,.3)}
    .btn-logout:hover{background:rgba(241,10,10,.25);color:#fc8181}
    .subnav{background:var(--dark2);border-bottom:1px solid var(--border);padding:.4rem 2rem;display:flex;gap:.3rem;flex-wrap:wrap}
    .subnav button{background:none;border:none;color:var(--muted);padding:.4rem .8rem;cursor:pointer;border-radius:6px;font-size:.82rem;font-family:'Nunito',sans-serif;font-weight:700;letter-spacing:.2px}
    .subnav button:hover{color:var(--text);background:rgba(255,255,255,.05)}
    .subnav button.active{color:var(--orange);background:rgba(237,148,14,.1)}
    .admin-page{display:none}.admin-page.active{display:block}
    .toast{position:fixed;bottom:1.5rem;left:50%;transform:translateX(-50%);
      background:#333;color:#fff;padding:.6rem 1.4rem;border-radius:30px;
      font-size:.85rem;opacity:0;pointer-events:none;transition:opacity .3s;z-index:9999}
    .toast.show{opacity:1}.toast.err{background:#c0392b}
    main{max-width:1100px;margin:2rem auto;padding:0 1rem}
    h2{font-family:'Fredoka One',cursive;font-size:1.2rem;color:var(--orange);margin-bottom:1rem}
    .toolbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem;flex-wrap:wrap;gap:.5rem}
    table{width:100%;border-collapse:collapse;background:var(--dark3);border-radius:10px;
      overflow:hidden;border:1px solid var(--border)}
    th{background:rgba(255,255,255,.05);text-align:left;padding:.7rem 1rem;
      font-size:.78rem;color:var(--muted);font-family:'Fredoka One',cursive;letter-spacing:.5px}
    td{padding:.7rem 1rem;border-top:1px solid var(--border);font-size:.85rem}
    tr:hover td{background:rgba(255,255,255,.02)}
    .badge-perfil{display:inline-block;border-radius:999px;padding:2px 10px;font-size:.75rem;font-weight:700}
    .p-admin{background:rgba(241,10,10,.15);color:#fc8181;border:1px solid rgba(241,10,10,.3)}
    .p-gerente{background:rgba(237,148,14,.15);color:var(--orange);border:1px solid rgba(237,148,14,.3)}
    .p-financeiro{background:rgba(123,32,225,.15);color:#c9a9ff;border:1px solid rgba(123,32,225,.3)}
    .p-vendedor{background:rgba(23,198,41,.15);color:#6ee37a;border:1px solid rgba(23,198,41,.3)}
    .badge-ativo{display:inline-block;border-radius:999px;padding:2px 10px;font-size:.75rem;font-weight:700}
    .ativo-sim{background:rgba(23,198,41,.12);color:#6ee37a;border:1px solid rgba(23,198,41,.25)}
    .ativo-nao{background:rgba(100,100,100,.15);color:#666;border:1px solid rgba(100,100,100,.25)}
    button{border:none;border-radius:6px;padding:.4rem .85rem;cursor:pointer;
      font-size:.82rem;font-family:'Nunito',sans-serif;font-weight:700}
    .btn-add{background:var(--orange);color:white;padding:.55rem 1.3rem;font-size:.92rem;
      border-radius:8px;font-family:'Fredoka One',cursive;letter-spacing:.5px;border:none}
    .btn-add:hover{filter:brightness(1.1)}
    .btn-edit{background:rgba(237,148,14,.12);color:var(--orange);border:1px solid rgba(237,148,14,.3)}
    .btn-edit:hover{background:rgba(237,148,14,.25)}
    .modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:10;
      align-items:center;justify-content:center;padding:1rem}
    .modal-bg.open{display:flex}
    .modal{background:var(--dark2);border:1px solid var(--border);border-radius:14px;
      width:100%;max-width:440px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
    .modal-header{padding:1.4rem 1.8rem 0}
    .modal-header h3{font-family:'Fredoka One',cursive;font-size:1.1rem;color:white}
    .modal-body{padding:1.2rem 1.8rem}
    .modal-footer{padding:0 1.8rem 1.4rem;display:flex;gap:.5rem;justify-content:flex-end}
    .modal label{display:block;font-size:.8rem;margin:.7rem 0 .25rem;color:var(--muted);font-weight:600}
    .modal input,.modal select{width:100%;padding:.5rem .75rem;
      background:rgba(255,255,255,.07);border:1px solid var(--border);border-radius:8px;
      color:white;font-size:.9rem;font-family:'Nunito',sans-serif;outline:none}
    .modal input:focus,.modal select:focus{border-color:var(--orange)}
    .modal select option{background:var(--dark2)}
    .btn-cancel{background:rgba(255,255,255,.07);color:var(--muted);border:1px solid var(--border)}
    .btn-cancel:hover{background:rgba(255,255,255,.12)}
    .btn-confirm{background:var(--orange);color:white;font-family:'Fredoka One',cursive}
    .btn-confirm:hover{filter:brightness(1.1)}
    .hint{font-size:.75rem;color:var(--muted);margin-top:.25rem}
    .empty{text-align:center;padding:2rem;color:var(--muted);font-size:.9rem}
  </style>
</head>
<body>
<header>
  <div style="display:flex;align-items:center;gap:.8rem">
    <img src="/api/logo" alt="Jogoteka" style="height:44px;object-fit:contain"
         onerror="this.style.display='none';document.getElementById('logo-txt-adm').style.display='block'">
    <div class="logo" id="logo-txt-adm" style="display:none">
      <span class="j">J</span><span class="o1">o</span><span class="g">g</span>
      <span class="o2">o</span><span class="t">t</span><span class="e">e</span>
      <span class="k">k</span><span class="a">a</span>
    </div>
    <div class="header-sub">Administração</div>
  </div>
  <nav>
    <span class="nav-info" id="nav-usuario"></span>
    <a class="btn-nav" href="/painel">Estoque</a>
    <a class="btn-nav" href="/loja">Loja</a>
    <a class="btn-nav" href="/landing-admin">🌐 Landing</a>
    <a class="btn-nav btn-logout" href="/logout">Sair</a>
  </nav>
</header>

<div class="subnav">
  <button class="active" onclick="showAdminPage('usuarios',this)">👤 Usuários</button>
  <button onclick="showAdminPage('contrato-modelo',this)">📝 Modelo de Contrato</button>
  <button onclick="showAdminPage('opcoes-catalogo',this)">🏷️ Categorias & Badges</button>
  <button onclick="showAdminPage('lembretes',this)">💬 Lembretes WhatsApp</button>
</div>

<main>

  <!-- USUÁRIOS -->
  <div class="admin-page active" id="apage-usuarios">
    <div class="toolbar">
      <h2>👤 Usuários do Sistema</h2>
      <button class="btn-add" onclick="abrirNovo()">+ Novo Usuário</button>
    </div>
    <table>
      <thead>
        <tr>
          <th>Nome</th><th>E-mail</th><th>Perfil</th><th>Status</th><th></th>
        </tr>
      </thead>
      <tbody id="tbl-usuarios">
        <tr><td colspan="5" class="empty">Carregando…</td></tr>
      </tbody>
    </table>
  </div>

  <!-- MODELO DE CONTRATO -->
  <div class="admin-page" id="apage-contrato-modelo" style="max-width:900px">
    <div class="toolbar">
      <h2>📝 Modelo de Contrato de Locação</h2>
      <div style="display:flex;gap:.6rem">
        <a href="/api/contrato-modelo/preview" target="_blank" class="btn-add" style="background:rgba(123,32,225,.3);border-color:rgba(123,32,225,.6);text-decoration:none">👁 Preview PDF</a>
        <button class="btn-add" onclick="salvarModelo(this)">💾 Salvar Modelo de Texto</button>
      </div>
    </div>
    <div style="background:#16213e;border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:1rem;margin-bottom:1rem">
      <div style="font-size:.85rem;font-weight:700;color:var(--text);margin-bottom:.4rem">📄 Seu Contrato Padrão</div>
      <div id="status-pdf-template" style="margin-bottom:.8rem;font-size:.82rem"></div>
      <div style="background:rgba(23,198,41,.07);border:1px solid rgba(23,198,41,.2);border-radius:8px;padding:.75rem;margin-bottom:.6rem">
        <div style="font-size:.8rem;font-weight:700;color:#17C629;margin-bottom:.3rem">✅ Recomendado — Word (.docx)</div>
        <div style="font-size:.78rem;color:var(--muted);margin-bottom:.6rem">
          Crie seu contrato no Word com os <code>{% raw %}{{CAMPOS}}{% endraw %}</code> onde quiser. O sistema substitui os campos e gera o PDF automaticamente.
        </div>
        <div style="display:flex;gap:.5rem;align-items:center;flex-wrap:wrap">
          <label style="background:#17C629;color:#0d1117;padding:.4rem .9rem;border-radius:8px;font-size:.82rem;font-weight:700;cursor:pointer;display:inline-block">
            📤 Subir .docx
            <input type="file" accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document" style="display:none" onchange="uploadTemplateDocx(this)">
          </label>
          <button id="btn-remover-docx" onclick="removerTemplateDocx()"
            style="display:none;background:rgba(241,10,10,.12);border:1px solid rgba(241,10,10,.3);color:#fc8181;padding:.4rem .9rem;border-radius:8px;font-size:.82rem;cursor:pointer">
            🗑 Remover .docx
          </button>
        </div>
      </div>
      <div style="background:rgba(237,148,14,.07);border:1px solid rgba(237,148,14,.2);border-radius:8px;padding:.75rem">
        <div style="font-size:.8rem;font-weight:700;color:var(--orange);margin-bottom:.3rem">📋 Alternativa — PDF</div>
        <div style="font-size:.78rem;color:var(--muted);margin-bottom:.6rem">
          Sobe seu contrato já pronto em PDF. O sistema adiciona uma ficha de dados na primeira página e envia junto.
          Os campos <strong>não são substituídos</strong> no PDF (use o Word para isso).
        </div>
        <div style="display:flex;gap:.5rem;align-items:center;flex-wrap:wrap">
          <label style="background:var(--orange);color:white;padding:.4rem .9rem;border-radius:8px;font-size:.82rem;font-weight:700;cursor:pointer;display:inline-block">
            📤 Subir PDF
            <input type="file" accept=".pdf,application/pdf" style="display:none" onchange="uploadTemplatePDF(this)">
          </label>
          <button id="btn-remover-pdf" onclick="removerTemplatePDF()"
            style="display:none;background:rgba(241,10,10,.12);border:1px solid rgba(241,10,10,.3);color:#fc8181;padding:.4rem .9rem;border-radius:8px;font-size:.82rem;cursor:pointer">
            🗑 Remover PDF
          </button>
        </div>
      </div>
    </div>
    <div id="secao-editor-texto">
      <div style="background:#16213e;border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:1rem;margin-bottom:1rem">
        <div style="font-size:.78rem;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:.6px;margin-bottom:.4rem">Campos disponíveis — clique para inserir no texto</div>
        <div style="font-size:.75rem;color:var(--muted);margin-bottom:.6rem">(Usado apenas quando <strong>não há PDF cadastrado</strong> acima)</div>
        <div style="display:flex;flex-wrap:wrap;gap:.4rem" id="campos-chips"></div>
      </div>
      <div style="background:#16213e;border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:1rem">
        <div style="font-size:.8rem;color:var(--muted);margin-bottom:.7rem">
          Modelo de texto do contrato. Cada linha vira um parágrafo no PDF.
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:.4rem;margin-bottom:.7rem;font-size:.78rem">
          <span style="background:#16213e;border:1px solid rgba(255,255,255,.12);border-radius:6px;padding:.25rem .6rem;color:#e0e0e0">
            <code style="color:#ED940E"># Título</code> → centralizado grande
          </span>
          <span style="background:#16213e;border:1px solid rgba(255,255,255,.12);border-radius:6px;padding:.25rem .6rem;color:#e0e0e0">
            <code style="color:#ED940E">## Subtítulo</code> → centralizado médio
          </span>
          <span style="background:#16213e;border:1px solid rgba(255,255,255,.12);border-radius:6px;padding:.25rem .6rem;color:#e0e0e0">
            <code style="color:#ED940E">1. SEÇÃO</code> → título de seção
          </span>
          <span style="background:#16213e;border:1px solid rgba(255,255,255,.12);border-radius:6px;padding:.25rem .6rem;color:#e0e0e0">
            <code style="color:#ED940E">linha em branco</code> → espaçamento
          </span>
        </div>
        <textarea id="textarea-modelo"
          style="width:100%;min-height:480px;background:#0d1117;border:1px solid rgba(255,255,255,.12);
                 border-radius:8px;color:#e0e0e0;font-family:monospace;font-size:.85rem;
                 padding:.8rem;line-height:1.6;resize:vertical;outline:none"
          placeholder="Carregando modelo..."></textarea>
      </div>
    </div>
  </div>

  <!-- CATEGORIAS & BADGES -->
  <div class="admin-page" id="apage-opcoes-catalogo">
    <h2>🏷️ Categorias & Badges</h2>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:1.5rem;max-width:800px">
      <section>
        <div class="toolbar" style="margin-bottom:.8rem">
          <h2 style="margin:0;font-size:1.1rem">🏷️ Categorias</h2>
          <button class="btn-add" onclick="novaOpcaoCatalogo('categoria')">+ Nova</button>
        </div>
        <div id="lista-categorias" style="display:flex;flex-direction:column;gap:.4rem"></div>
      </section>
      <section>
        <div class="toolbar" style="margin-bottom:.8rem">
          <h2 style="margin:0;font-size:1.1rem">⭐ Badges de Destaque</h2>
          <button class="btn-add" onclick="novaOpcaoCatalogo('destaque')">+ Novo</button>
        </div>
        <div id="lista-destaques" style="display:flex;flex-direction:column;gap:.4rem"></div>
      </section>
    </div>
  </div>

  <!-- LEMBRETES WHATSAPP -->
  <div class="admin-page" id="apage-lembretes">
    <div style="max-width:780px">

      <!-- ── Diagnóstico ClicksZap ──────────────────────────────────────── -->
      <div style="background:#16213e;border:1px solid var(--border);border-radius:10px;padding:1rem;margin-bottom:1.4rem">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:.7rem">
          <span style="font-weight:700;font-size:.9rem">🔌 Integração ClicksZap</span>
          <button class="btn-add" style="font-size:.78rem;padding:.3rem .8rem" onclick="verificarClicksZap()">🔍 Verificar</button>
        </div>
        <div id="cz-status" style="font-size:.82rem;color:var(--muted)">Clique em Verificar para checar a conexão.</div>
        <div id="cz-teste-box" style="display:none;margin-top:.8rem;display:flex;gap:.5rem;flex-wrap:wrap">
          <input id="cz-tel-teste" placeholder="Telefone para teste (ex: 51999998888)"
            style="flex:1;min-width:200px;background:rgba(255,255,255,.06);border:1px solid var(--border);
                   border-radius:6px;color:var(--text);padding:.4rem .7rem;font-size:.82rem">
          <button class="btn-add" style="font-size:.78rem;padding:.4rem .8rem;background:rgba(37,211,102,.15);
                  border-color:rgba(37,211,102,.4);color:#25D366" onclick="enviarTeste()">
            💬 Enviar mensagem de teste
          </button>
        </div>
      </div>

      <div class="toolbar" style="margin-bottom:1.2rem">
        <h2 style="margin:0">💬 Lembretes de Devolução — WhatsApp</h2>
        <div style="display:flex;gap:.6rem">
          <button class="btn-add" style="background:rgba(34,197,94,.15);border-color:rgba(34,197,94,.5);color:#4ade80" onclick="dispararLembretes()">▶ Disparar Agora</button>
          <button class="btn-add" onclick="salvarLembrete()">💾 Salvar Template</button>
        </div>
      </div>
      <p style="color:var(--muted);font-size:.85rem;margin-bottom:1rem">
        Toda vez que o servidor estiver rodando às <strong>14h</strong>, o sistema busca todas as locações
        ativas com devolução prevista para o dia seguinte e envia automaticamente uma mensagem via
        WhatsApp pelo ClicksZap. Use <code>{nome}</code>, <code>{jogo}</code> e <code>{data}</code> no texto.
      </p>
      <label style="font-size:.8rem;color:var(--muted);font-weight:700;display:block;margin-bottom:.4rem">MENSAGEM</label>
      <textarea id="lembrete-template" rows="8"
        style="width:100%;background:rgba(255,255,255,.05);border:1px solid var(--border);border-radius:8px;
               color:var(--text);font-family:inherit;font-size:.9rem;padding:.8rem;resize:vertical;line-height:1.6"
        placeholder="Ex: Olá {nome}! Seu jogo {jogo} deve ser devolvido amanhã, dia {data}. Obrigado!"></textarea>
      <div style="display:flex;gap:1rem;margin-top:.5rem;flex-wrap:wrap">
        <span style="font-size:.8rem;color:var(--muted)">Variáveis: <code>{nome}</code> · <code>{jogo}</code> · <code>{data}</code></span>
        <button style="background:none;border:none;color:var(--muted);font-size:.8rem;cursor:pointer;padding:0" onclick="restaurarPadrao()">↩ Restaurar padrão</button>
      </div>
      <div style="margin-top:1.8rem">
        <div class="toolbar" style="margin-bottom:.6rem">
          <h3 style="margin:0;font-size:1rem">📋 Histórico de Envios</h3>
          <button class="btn-add" style="font-size:.78rem;padding:.3rem .8rem" onclick="carregarLogLembretes()">🔄 Atualizar</button>
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:.82rem">
          <thead>
            <tr style="border-bottom:2px solid var(--border)">
              <th style="text-align:left;padding:.5rem;color:var(--muted)">Data/Hora</th>
              <th style="text-align:left;padding:.5rem;color:var(--muted)">Cliente</th>
              <th style="text-align:left;padding:.5rem;color:var(--muted)">Jogo</th>
              <th style="text-align:left;padding:.5rem;color:var(--muted)">Devolução</th>
              <th style="text-align:left;padding:.5rem;color:var(--muted)">Status</th>
            </tr>
          </thead>
          <tbody id="log-lembretes-body">
            <tr><td colspan="5" style="padding:.8rem;color:var(--muted);text-align:center">Clique em Atualizar para carregar</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

</main>

<div id="toast" class="toast"></div>

<!-- Modal usuário -->
<div class="modal-bg" id="modal-usuario" onclick="fecharSeFora(event,'modal-usuario')">
  <div class="modal">
    <div class="modal-header">
      <h3 id="modal-titulo">Novo Usuário</h3>
    </div>
    <div class="modal-body">
      <input type="hidden" id="u-id">
      <label>Nome completo</label>
      <input id="u-nome" placeholder="Nome do usuário">
      <label>E-mail</label>
      <input id="u-email" type="email" placeholder="email@exemplo.com">
      <label>Senha <span id="senha-hint" class="hint">(deixe em branco para manter a atual)</span></label>
      <input id="u-senha" type="password" placeholder="••••••••" minlength="6">
      <label>Perfil</label>
      <select id="u-perfil">
        <option value="vendedor">Vendedor — só vendas e locações</option>
        <option value="financeiro">Financeiro — relatórios, conciliação e dashboard</option>
        <option value="gerente">Gerente — estoque, vendas, locações, compras, relatórios</option>
        <option value="admin">Admin — acesso total + gerenciar usuários</option>
      </select>
      <label>Status</label>
      <select id="u-ativo">
        <option value="1">Ativo</option>
        <option value="0">Inativo</option>
      </select>
    </div>
    <div class="modal-footer">
      <button class="btn-cancel" onclick="fechar()">Cancelar</button>
      <button class="btn-confirm" onclick="salvar()">Salvar</button>
    </div>
  </div>
</div>

<script>
// ── Utilitários ───────────────────────────────────────────────────────────────
async function api(path, opts={}){
  const r = await fetch('/api'+path,{headers:{'Content-Type':'application/json'},...opts});
  return r.json();
}

function toast(msg, err=false){
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show' + (err?' err':'');
  setTimeout(()=>t.className='toast',3000);
}

function showAdminPage(name, btn){
  document.querySelectorAll('.admin-page').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.subnav button').forEach(b=>b.classList.remove('active'));
  document.getElementById('apage-'+name).classList.add('active');
  if(btn) btn.classList.add('active');
  if(name==='contrato-modelo') loadModeloContrato();
  if(name==='opcoes-catalogo') initOpcoesCatalogo();
  if(name==='lembretes') carregarLembrete();
}

// ── Categorias & Badges ───────────────────────────────────────────────────────
async function initOpcoesCatalogo(){
  await renderListaOpcoes('categoria');
  await renderListaOpcoes('destaque');
}

async function renderListaOpcoes(tipo){
  const endpoint = tipo === 'categoria' ? '/categorias' : '/destaques-opcoes';
  const containerId = tipo === 'categoria' ? 'lista-categorias' : 'lista-destaques';
  const items = await api(endpoint);
  const el = document.getElementById(containerId);
  if(!el) return;
  el.innerHTML = (Array.isArray(items)?items:[]).map(i => `
    <div style="display:flex;align-items:center;justify-content:space-between;
      background:rgba(255,255,255,.06);border-radius:8px;padding:.5rem .8rem;border:1px solid var(--border)">
      <span style="font-weight:600">${i.nome}</span>
      <button onclick="excluirOpcaoCatalogo('${tipo}',${i.id},'${i.nome.replace(/'/g,"\\'")}',this)"
        style="background:none;border:none;color:var(--red);cursor:pointer;font-size:1rem;padding:0 .3rem"
        title="Excluir">🗑️</button>
    </div>
  `).join('') || '<p style="color:var(--muted);font-size:.85rem">Nenhum item cadastrado.</p>';
}

async function novaOpcaoCatalogo(tipo){
  const label = tipo === 'categoria' ? 'categoria' : 'badge de destaque';
  const nome = prompt(`Nome da nova ${label}:`);
  if(!nome || !nome.trim()) return;
  const endpoint = tipo === 'categoria' ? '/categorias' : '/destaques-opcoes';
  const res = await api(endpoint, {method:'POST', body:JSON.stringify({nome: nome.trim()})});
  if(res.erro){ alert(res.erro); return; }
  await renderListaOpcoes(tipo);
}

async function excluirOpcaoCatalogo(tipo, id, nome){
  if(!confirm(`Excluir "${nome}"?`)) return;
  const endpoint = tipo === 'categoria' ? `/categorias/${id}` : `/destaques-opcoes/${id}`;
  await api(endpoint, {method:'DELETE'});
  await renderListaOpcoes(tipo);
}

// ── Modelo de Contrato ────────────────────────────────────────────────────────
const CAMPOS_CONTRATO = [
  ['NOME_LOJA','Nome da loja'],['CNPJ_LOJA','CNPJ da loja'],['ENDERECO_LOJA','Endereço da loja'],
  ['NUM_CONTRATO','Nº do contrato'],['DATA_GERACAO','Data de geração'],
  ['NOME_CLIENTE','Nome do cliente'],['CPF_CLIENTE','CPF do cliente'],
  ['TELEFONE_CLIENTE','Telefone'],['ENDERECO_CLIENTE','Endereço do cliente'],
  ['JOGO','Nome do jogo'],
  ['DATA_SAIDA','Data de saída'],['DATA_PREVISTA','Data prevista devol.'],
  ['OPCAO_DIAS','Nº de dias'],['VALOR_LOCACAO','Valor da locação'],
  ['FORMA_PAGAMENTO','Forma de pagamento'],['MULTA_DIA','Multa/dia atraso'],
];

async function loadModeloContrato(){
  const chips = document.getElementById('campos-chips');
  if(chips && !chips.innerHTML){
    const ob = '{' + '{', cb = '}' + '}';
    chips.innerHTML = CAMPOS_CONTRATO.map(([campo, label])=>
      `<button onclick="inserirCampo('${ob}${campo}${cb}')"
        style="background:rgba(237,148,14,.15);border:1px solid rgba(237,148,14,.3);
               color:#ED940E;border-radius:6px;padding:3px 8px;font-size:.78rem;
               cursor:pointer;font-family:monospace"
        title="${label}">${ob}${campo}${cb}</button>`
    ).join('');
  }
  const r = await api('/contrato-modelo');
  if(r && r.clausulas) document.getElementById('textarea-modelo').value = r.clausulas;
  const rp = await api('/contrato-modelo/tem-pdf');
  atualizarStatusTemplate(rp || {tipo:'texto'});
}

function atualizarStatusTemplate(info){
  const el = document.getElementById('status-pdf-template');
  const btnRemDocx = document.getElementById('btn-remover-docx');
  const btnRemPdf  = document.getElementById('btn-remover-pdf');
  btnRemDocx.style.display = 'none';
  btnRemPdf.style.display  = 'none';
  if(info.tipo === 'docx'){
    el.innerHTML = '<span style="color:#17C629;font-weight:700">✅ Word (.docx) cadastrado — campos substituídos automaticamente</span>';
    btnRemDocx.style.display = '';
  } else if(info.tipo === 'pdf'){
    el.innerHTML = '<span style="color:var(--orange);font-weight:700">📋 PDF cadastrado — ficha de dados adicionada na página 1</span>';
    btnRemPdf.style.display = '';
  } else {
    el.innerHTML = '<span style="color:#555">Nenhum arquivo cadastrado — usando o modelo de texto abaixo</span>';
  }
}

async function uploadTemplateDocx(input){
  const file = input.files[0];
  if(!file) return;
  if(file.size > 20*1024*1024){ alert('Arquivo muito grande (máximo 20 MB)'); return; }
  const fd = new FormData();
  fd.append('arquivo', file);
  const resp = await fetch('/api/contrato-modelo/upload-docx',{method:'POST',body:fd,credentials:'same-origin'});
  const r = await resp.json().catch(()=>({}));
  if(r.ok){
    atualizarStatusTemplate({tipo:'docx'});
    const label = input.closest('label');
    label.innerHTML = '✅ .docx salvo!';
    setTimeout(()=>label.innerHTML = '📤 Subir .docx<input type="file" accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document" style="display:none" onchange="uploadTemplateDocx(this)">', 2500);
  } else { alert('Erro: '+(r.error||'tente novamente')); }
  input.value = '';
}

async function removerTemplateDocx(){
  if(!confirm('Remover o arquivo Word? O sistema voltará a usar o modelo de texto.')) return;
  const r = await api('/contrato-modelo/remover-docx',{method:'POST',body:'{}'});
  if(r.ok) atualizarStatusTemplate({tipo:'texto'});
}

async function uploadTemplatePDF(input){
  const file = input.files[0];
  if(!file) return;
  if(file.size > 20*1024*1024){ alert('Arquivo muito grande (máximo 20 MB)'); return; }
  const fd = new FormData();
  fd.append('arquivo', file);
  const resp = await fetch('/api/contrato-modelo/upload-pdf',{method:'POST',body:fd,credentials:'same-origin'});
  const r = await resp.json().catch(()=>({}));
  if(r.ok){
    atualizarStatusTemplate({tipo:'pdf'});
    const label = input.closest('label');
    label.innerHTML = '✅ PDF salvo!';
    setTimeout(()=>label.innerHTML = '📤 Subir PDF<input type="file" accept=".pdf,application/pdf" style="display:none" onchange="uploadTemplatePDF(this)">', 2500);
  } else { alert('Erro: '+(r.error||'tente novamente')); }
  input.value = '';
}

async function removerTemplatePDF(){
  if(!confirm('Remover o PDF de contrato? O sistema voltará a usar o modelo de texto.')) return;
  const r = await api('/contrato-modelo/remover-pdf',{method:'POST',body:'{}'});
  if(r.ok) atualizarStatusTemplate({tipo:'texto'});
}

function inserirCampo(campo){
  const ta = document.getElementById('textarea-modelo');
  const s = ta.selectionStart, e = ta.selectionEnd;
  ta.value = ta.value.slice(0,s) + campo + ta.value.slice(e);
  ta.selectionStart = ta.selectionEnd = s + campo.length;
  ta.focus();
  const btn = event.target;
  btn.style.background = 'rgba(23,198,41,.3)';
  setTimeout(()=>btn.style.background='rgba(237,148,14,.15)', 600);
}

async function salvarModelo(btn){
  const clausulas = document.getElementById('textarea-modelo').value.trim();
  if(!clausulas){ toast('O modelo não pode ficar vazio.', true); return; }
  if(btn){ const orig = btn.textContent; btn.disabled = true; btn.textContent = '⏳ Salvando...';
    var _restore = ()=>{ btn.disabled=false; btn.textContent=orig; }; }
  const r = await api('/contrato-modelo',{method:'POST',body:JSON.stringify({clausulas})});
  if(btn) _restore();
  if(r && r.ok){
    toast('✅ Modelo salvo com sucesso!');
  } else {
    toast('Erro ao salvar: '+(r&&r.error ? r.error : 'tente novamente'), true);
  }
}

// ── Diagnóstico ClicksZap ─────────────────────────────────────────────────────
async function verificarClicksZap(){
  const el = document.getElementById('cz-status');
  el.textContent = '⏳ Verificando...';
  const r = await api('/admin/clickszap/status');
  if(r.error){ el.innerHTML=`<span style="color:var(--red)">❌ Erro: ${r.error}</span>`; return; }

  let html = '';

  if(!r.token_configurado){
    html = `<span style="color:var(--red)">❌ CLICKSZAP_TOKEN não configurado.</span>
      <br><span style="color:var(--muted);font-size:.79rem">Railway → Variables → adicione <strong>CLICKSZAP_TOKEN</strong> com o token da sua conta ClicksZap.</span>`;
    document.getElementById('cz-teste-box').style.display = 'none';
    el.innerHTML = html; return;
  }

  // URL em uso
  const urlLabel = r.url_env
    ? `<code style="font-size:.78rem">${r.url}</code>`
    : `<code style="font-size:.78rem">${r.url}</code> <span style="color:var(--orange)">(padrão — verifique se é a URL correta da sua instância)</span>`;
  html += `<div style="margin-bottom:.4rem">🔗 URL em uso: ${urlLabel}</div>`;

  // Token
  html += `<span style="color:#4ade80">✅ Token configurado</span> <span style="color:var(--muted);font-size:.78rem">(${r.token_preview})</span> &nbsp;·&nbsp; `;

  // Conectividade
  if(r.api_ok && r.api_token_valido){
    html += `<span style="color:#4ade80">✅ API acessível e token válido</span>
      <br><span style="color:var(--muted);font-size:.79rem">Tudo configurado! Contratos e lembretes serão enviados via WhatsApp.</span>`;
    document.getElementById('cz-teste-box').style.display = 'flex';
  } else if(!r.api_token_valido){
    html += `<span style="color:var(--red)">❌ Token inválido ou expirado (HTTP ${r.api_status})</span>
      <br><span style="color:var(--muted);font-size:.79rem">Acesse <strong>${r.url}/panel/api-token</strong> e atualize o <strong>CLICKSZAP_TOKEN</strong> no Render.</span>`;
    document.getElementById('cz-teste-box').style.display = 'none';
  } else if(r.api_erro && r.api_erro.includes('timed out')){
    html += `<span style="color:var(--red)">❌ Timeout — URL provavelmente incorreta</span>
      <br><span style="color:var(--muted);font-size:.79rem">
      A URL <strong>${r.url}</strong> não respondeu.<br>
      Verifique a URL do seu serviço ClicksZap no Render e atualize a variável <strong>CLICKSZAP_URL</strong>.</span>`;
    document.getElementById('cz-teste-box').style.display = 'none';
  } else if(r.api_erro){
    html += `<span style="color:var(--red)">❌ Erro: ${r.api_erro}</span>`;
    document.getElementById('cz-teste-box').style.display = 'none';
  } else if(r.api_status === 404){
    html += `<span style="color:var(--orange)">⚠️ Servidor acessível, mas endpoint não encontrado (404)</span>
      <br><span style="color:var(--muted);font-size:.79rem">
      Verifique se a URL <strong>${r.url}</strong> aponta para o serviço ClicksZap correto no Render.</span>`;
    document.getElementById('cz-teste-box').style.display = 'none';
  } else {
    html += `<span style="color:var(--orange)">⚠️ HTTP ${r.api_status}</span>`;
    document.getElementById('cz-teste-box').style.display = 'none';
  }

  el.innerHTML = html;
}

async function enviarTeste(){
  const tel = document.getElementById('cz-tel-teste').value.trim();
  if(!tel){ toast('Informe um número de telefone', true); return; }
  const r = await api('/admin/clickszap/teste-mensagem',{method:'POST',body:JSON.stringify({telefone:tel})});
  if(r.error){ toast('Erro: '+r.error, true); return; }
  toast('✅ Mensagem de teste enviada! Verifique o WhatsApp.');
}

// ── Lembretes WhatsApp ────────────────────────────────────────────────────────
async function carregarLembrete(){
  const d = await api('/admin/lembrete');
  if(d && d.template) document.getElementById('lembrete-template').value = d.template;
  carregarLogLembretes();
}

async function salvarLembrete(){
  const template = document.getElementById('lembrete-template').value.trim();
  if(!template){ toast('Template não pode ser vazio', true); return; }
  const res = await api('/admin/lembrete', {method:'POST', body:JSON.stringify({template})});
  if(res && res.ok) toast('Template salvo!');
  else toast((res&&res.erro)||'Erro ao salvar', true);
}

async function dispararLembretes(){
  if(!confirm('Disparar lembretes agora para todas as locações com devolução amanhã?')) return;
  toast('Disparando...');
  const res = await api('/admin/lembrete/disparar', {method:'POST', body:'{}'});
  if(res) toast(`Enviados: ${res.enviados} | Erros: ${res.erros} | Total: ${res.total}`);
  carregarLogLembretes();
}

async function carregarLogLembretes(){
  const rows = await api('/admin/lembrete/log');
  const tbody = document.getElementById('log-lembretes-body');
  if(!tbody) return;
  if(!Array.isArray(rows)||!rows.length){
    tbody.innerHTML='<tr><td colspan="5" style="padding:.8rem;color:var(--muted);text-align:center">Nenhum envio registrado ainda</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(r=>`<tr>
    <td style="padding:.4rem .5rem">${(r.enviado_em||'').slice(0,16).replace('T',' ')}</td>
    <td style="padding:.4rem .5rem">${r.cliente_nome||'—'}</td>
    <td style="padding:.4rem .5rem">${r.jogo_nome||'—'}</td>
    <td style="padding:.4rem .5rem">${(r.data_prevista||'').slice(0,10)}</td>
    <td style="padding:.4rem .5rem">${r.status==='ok'
      ? '<span style="color:#4ade80">✔ Enviado</span>'
      : '<span style="color:var(--red)">✘ Erro: '+(r.erro||'')+'</span>'}</td>
  </tr>`).join('');
}

function restaurarPadrao(){
  document.getElementById('lembrete-template').value =
    'Olá *{nome}*! 👋\\n\\nPassando para lembrar que o jogo *{jogo}* deve ser devolvido *amanhã, dia {data}*. 🎲\\n\\nQualquer dúvida é só chamar. Obrigado! 😊\\n\\n— Jogoteka 🎲';
}

// ── Usuários ──────────────────────────────────────────────────────────────────
let usuarioAtual = null;

async function carregarUsuarios(){
  const r = await fetch('/api/admin/usuarios');
  const lista = await r.json();
  const tbody = document.getElementById('tbl-usuarios');
  if(!lista.length){
    tbody.innerHTML = '<tr><td colspan="5" class="empty">Nenhum usuário cadastrado.</td></tr>';
    return;
  }
  tbody.innerHTML = lista.map(u => `
    <tr>
      <td><strong>${u.nome}</strong></td>
      <td style="color:#8892a4">${u.email}</td>
      <td><span class="badge-perfil p-${u.perfil}">${u.perfil}</span></td>
      <td><span class="badge-ativo ${u.ativo ? 'ativo-sim':'ativo-nao'}">${u.ativo ? 'Ativo':'Inativo'}</span></td>
      <td><button class="btn-edit" onclick="abrirEditar(${u.id})">✏️ Editar</button></td>
    </tr>`).join('');
  window._usuarios = lista;
  document.getElementById('nav-usuario').textContent = '{{ nome }} ({{ perfil }})';
}

function abrirNovo(){
  usuarioAtual = null;
  document.getElementById('modal-titulo').textContent = 'Novo Usuário';
  document.getElementById('u-id').value = '';
  document.getElementById('u-nome').value = '';
  document.getElementById('u-email').value = '';
  document.getElementById('u-senha').value = '';
  document.getElementById('u-perfil').value = 'vendedor';
  document.getElementById('u-ativo').value = '1';
  document.getElementById('senha-hint').style.display = 'none';
  document.getElementById('modal-usuario').classList.add('open');
}

function abrirEditar(id){
  const u = window._usuarios.find(x=>x.id===id);
  if(!u) return;
  usuarioAtual = u;
  document.getElementById('modal-titulo').textContent = 'Editar Usuário';
  document.getElementById('u-id').value = u.id;
  document.getElementById('u-nome').value = u.nome;
  document.getElementById('u-email').value = u.email;
  document.getElementById('u-senha').value = '';
  document.getElementById('u-perfil').value = u.perfil;
  document.getElementById('u-ativo').value = u.ativo ? '1':'0';
  document.getElementById('senha-hint').style.display = '';
  document.getElementById('modal-usuario').classList.add('open');
}

async function salvar(){
  const id = document.getElementById('u-id').value;
  const body = {
    nome:   document.getElementById('u-nome').value.trim(),
    email:  document.getElementById('u-email').value.trim(),
    senha:  document.getElementById('u-senha').value,
    perfil: document.getElementById('u-perfil').value,
    ativo:  parseInt(document.getElementById('u-ativo').value),
  };
  if(!body.nome || !body.email){ alert('Nome e e-mail são obrigatórios.'); return; }
  if(!id && !body.senha){ alert('Informe uma senha para o novo usuário.'); return; }

  const url = id ? '/api/admin/usuarios/'+id : '/api/admin/usuarios';
  const method = id ? 'PUT' : 'POST';
  const r = await fetch(url, {method, headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const data = await r.json();
  if(data.erro || data.error){ alert(data.erro || data.error); return; }
  fechar();
  carregarUsuarios();
}

function fechar(){ document.getElementById('modal-usuario').classList.remove('open'); }
function fecharSeFora(e,id){ if(e.target===document.getElementById(id)) fechar(); }

carregarUsuarios();
</script>
</body>
</html>"""


@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email","").strip().lower()
        senha = request.form.get("senha","")
        with get_connection() as conn:
            u = conn.execute("SELECT * FROM usuarios WHERE email=? AND ativo=1", (email,)).fetchone()
        if not u or not check_password_hash(u["senha_hash"], senha):
            return render_template_string(LOGIN_HTML, erro="E-mail ou senha incorretos.")
        session["uid"]   = u["id"]
        session["nome"]  = u["nome"]
        session["perfil"] = u["perfil"]
        if u["perfil"] == "vendedor":
            return redirect("/loja")
        if u["perfil"] == "financeiro":
            return redirect("/painel?tab=conciliacao")
        return redirect("/painel")
    if session.get("uid"):
        return redirect("/painel")
    return render_template_string(LOGIN_HTML, erro=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/setup", methods=["GET","POST"])
def setup():
    with get_connection() as conn:
        existe = conn.execute("SELECT id FROM usuarios LIMIT 1").fetchone()
    if existe:
        return redirect("/login")
    if request.method == "POST":
        nome  = request.form.get("nome","").strip()
        email = request.form.get("email","").strip().lower()
        senha = request.form.get("senha","")
        if not nome or not email or len(senha) < 6:
            return render_template_string(SETUP_HTML, erro="Preencha todos os campos (senha mínimo 6 caracteres).")
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO usuarios (nome, email, senha_hash, perfil, ativo, data_cadastro) VALUES (?,?,?,?,1,?)",
                (nome, email, generate_password_hash(senha), "admin", _agora_str())
            )
        return redirect("/login")
    return render_template_string(SETUP_HTML, erro=None)


@app.route("/admin")
@requer_admin
def admin():
    return render_template_string(ADMIN_HTML,
                                  nome=session["nome"], perfil=session["perfil"])


# ── API Admin — Usuários ───────────────────────────────────────────────────────

@app.route("/api/admin/usuarios")
@requer_admin
def api_listar_usuarios():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, nome, email, perfil, ativo FROM usuarios ORDER BY nome"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/admin/usuarios", methods=["POST"])
@requer_admin
def api_criar_usuario():
    d = request.get_json()
    if not d.get("senha"):
        return jsonify({"erro": "Senha obrigatória"}), 400
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO usuarios (nome, email, senha_hash, perfil, ativo, data_cadastro) VALUES (?,?,?,?,?,?)",
                (d["nome"], d["email"].lower(), generate_password_hash(d["senha"]),
                 d.get("perfil","vendedor"), d.get("ativo",1), _agora_str())
            )
        return jsonify({"ok": True}), 201
    except Exception as e:
        return jsonify({"erro": "E-mail já cadastrado" if "unique" in str(e).lower() else str(e)}), 400


@app.route("/api/admin/usuarios/<int:uid>", methods=["PUT"])
@requer_admin
def api_editar_usuario(uid):
    d = request.get_json()
    try:
        with get_connection() as conn:
            if d.get("senha"):
                conn.execute(
                    "UPDATE usuarios SET nome=?, email=?, senha_hash=?, perfil=?, ativo=? WHERE id=?",
                    (d["nome"], d["email"].lower(), generate_password_hash(d["senha"]),
                     d.get("perfil","vendedor"), d.get("ativo",1), uid)
                )
            else:
                conn.execute(
                    "UPDATE usuarios SET nome=?, email=?, perfil=?, ativo=? WHERE id=?",
                    (d["nome"], d["email"].lower(), d.get("perfil","vendedor"), d.get("ativo",1), uid)
                )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"erro": "E-mail já cadastrado" if "unique" in str(e).lower() else str(e)}), 400


# ── API Lembretes de Devolução ────────────────────────────────────────────────

@app.route("/api/admin/lembrete", methods=["GET"])
@requer_admin
def api_get_lembrete():
    return jsonify({
        "template": ct.carregar_template_lembrete(),
        "padrao": ct.TEMPLATE_LEMBRETE_PADRAO
    })

@app.route("/api/admin/lembrete", methods=["POST"])
@requer_admin
def api_salvar_lembrete():
    d = request.get_json()
    template = (d.get("template") or "").strip()
    if not template:
        return jsonify({"erro": "Template não pode ser vazio"}), 400
    ct.salvar_template_lembrete(template, d.get("nome", "Lembrete de Devolução"))
    return jsonify({"ok": True})

@app.route("/api/admin/lembrete/disparar", methods=["POST"])
@requer_admin
def api_disparar_lembretes():
    """Disparo manual para testes — ignora horário."""
    resultado = ct.enviar_lembretes_devolucao()
    return jsonify(resultado)

@app.route("/api/admin/lembrete/log", methods=["GET"])
@requer_admin
def api_log_lembretes():
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT id, locacao_id, cliente_nome, jogo_nome, data_prevista,
                      status, erro, enviado_em
               FROM lembretes_log ORDER BY id DESC LIMIT 100"""
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/admin/clickszap/status", methods=["GET"])
@requer_login
def api_clickszap_status():
    """Retorna status da integração ClicksZap (token, URL, conectividade)."""
    token = ct._get_token()
    info = {
        "token_configurado": bool(token),
        "token_preview": (token[:4] + "..." + token[-4:]) if len(token) > 8 else ("***" if token else ""),
        "url": ct.CLICKSZAP_URL,
        "url_env": os.environ.get("CLICKSZAP_URL", ""),  # vazio = usando padrão
    }
    if token:
        try:
            import httpx as _hx
            # Testa GET /documents — lista documentos, retorna 200 com token válido
            # 401/403 = token inválido | 200 = tudo ok
            resp = _hx.get(
                f"{ct.CLICKSZAP_URL}/documents",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
                follow_redirects=True,
            )
            info["api_status"] = resp.status_code
            info["api_ok"] = resp.status_code in (200, 201)
            info["api_token_valido"] = resp.status_code not in (401, 403)
            try:
                info["api_resp"] = resp.json()
            except Exception:
                info["api_resp"] = resp.text[:200]
        except Exception as e:
            info["api_status"] = None
            info["api_ok"] = False
            info["api_erro"] = str(e)
    return jsonify(info)


@app.route("/api/admin/clickszap/teste-mensagem", methods=["POST"])
@requer_login
def api_clickszap_teste():
    """Envia uma mensagem de teste via ClicksZap para validar a integração."""
    d = request.get_json()
    telefone = re.sub(r'\D', '', d.get("telefone") or "")
    if not telefone:
        return jsonify({"error": "Informe um número de telefone"}), 400
    if not telefone.startswith("55"):
        telefone = "55" + telefone
    resultado = ct._enviar_mensagem_whatsapp(telefone, "✅ Teste de integração Jogoteka — ClicksZap funcionando!")
    if resultado.get("ok"):
        return jsonify({"ok": True, "mensagem": "Mensagem de teste enviada!"})
    return jsonify({"error": resultado.get("error", "Erro desconhecido")}), 400


# ── Gestão da Landing Page ─────────────────────────────────────────────────────

LANDING_ADMIN_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gestão da Landing Page — Jogoteka</title>
<link href="https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800;900&display=swap" rel="stylesheet">
<style>
  :root{--red:#F10A0A;--green:#17C629;--purple:#7B20E1;--orange:#ED940E}
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Nunito',sans-serif;background:#f4f5f7;color:#1a1a2e;min-height:100vh}
  nav{background:#fff;padding:14px 32px;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;z-index:10;box-shadow:0 2px 12px rgba(0,0,0,.08)}
  .nav-logo img{height:48px;width:auto;object-fit:contain}
  .nav-logo-txt{font-size:1.5rem;font-weight:900;color:var(--red)}
  .nav-logo-txt span{color:var(--purple)}
  .color-bar{display:flex;height:6px}
  .color-bar span{flex:1}
  .cb-red{background:var(--red)}.cb-orange{background:var(--orange)}.cb-green{background:var(--green)}.cb-purple{background:var(--purple)}
  .h-links{display:flex;gap:10px}
  @media(max-width:520px){nav{padding:12px 16px}.nav-logo img{height:38px}}
  .btn{padding:9px 20px;border-radius:8px;font-weight:700;font-size:.9rem;cursor:pointer;border:none;transition:.2s;font-family:inherit}
  .btn-primary{background:var(--purple);color:#fff}.btn-primary:hover{background:#6a1bc7}
  .btn-orange{background:var(--orange);color:#fff}.btn-orange:hover{background:#d4840c}
  .btn-red{background:var(--red);color:#fff}.btn-red:hover{background:#c00}
  .btn-green{background:var(--green);color:#fff}.btn-green:hover{background:#13a821}
  .btn-ghost{background:#f0f0f0;color:#555}.btn-ghost:hover{background:#e0e0e0}
  .btn-sm{padding:6px 14px;font-size:.82rem}
  main{max-width:1000px;margin:32px auto;padding:0 20px;display:flex;flex-direction:column;gap:32px}
  .card{background:#fff;border-radius:16px;padding:28px;box-shadow:0 2px 12px rgba(0,0,0,.07)}
  .card h2{font-size:1.2rem;font-weight:900;margin-bottom:6px;display:flex;align-items:center;gap:8px}
  .card .sub{font-size:.88rem;color:#888;margin-bottom:20px;font-weight:600}

  /* UPLOAD AREA */
  .drop-area{border:2.5px dashed #ccc;border-radius:12px;padding:36px;text-align:center;cursor:pointer;transition:.2s;background:#fafafa}
  .drop-area:hover,.drop-area.dragover{border-color:var(--purple);background:#f5f0ff}
  .drop-area input{display:none}
  .drop-icon{font-size:2.5rem;margin-bottom:10px}
  .drop-text{font-size:.95rem;color:#666;font-weight:700}
  .drop-sub{font-size:.82rem;color:#aaa;margin-top:4px;font-weight:600}

  /* GRID DE MÍDIAS */
  .midia-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:14px;margin-top:16px}
  .midia-item{background:#f7f7f7;border-radius:12px;overflow:hidden;position:relative;border:2px solid transparent;transition:.2s;cursor:grab}
  .midia-item:hover{border-color:var(--purple)}
  .midia-item.drag-over{border-color:var(--orange);background:#fff8ee}
  .midia-item.dragging{opacity:.4}
  .midia-item img,.midia-item video{width:100%;height:120px;object-fit:cover;display:block}
  .midia-info{padding:8px 10px}
  .midia-tipo{font-size:.72rem;font-weight:800;text-transform:uppercase;color:var(--purple)}
  .midia-nome{font-size:.78rem;color:#555;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600}
  .midia-ordem{font-size:.75rem;font-weight:800;color:#aaa;margin-top:2px}
  .midia-actions{display:flex;gap:5px;padding:0 10px 10px;flex-wrap:wrap}
  .btn-mover{background:#f0f0f0;border:none;border-radius:6px;padding:5px 9px;font-size:.85rem;cursor:pointer;font-weight:800;transition:.2s}
  .btn-mover:hover{background:#ddd}
  .btn-mover:disabled{opacity:.3;cursor:default}
  .loading{text-align:center;color:#888;padding:20px;font-weight:700}

  /* DEPOIMENTOS */
  .dep-list{display:flex;flex-direction:column;gap:12px;margin-top:4px}
  .dep-item{background:#f9f9f9;border-radius:12px;padding:16px;display:flex;gap:14px;align-items:flex-start;border:1.5px solid #eee}
  .dep-avatar{width:40px;height:40px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:900;color:#fff;font-size:1rem;flex-shrink:0}
  .dep-body{flex:1}
  .dep-nome{font-weight:800;font-size:.95rem}
  .dep-tempo{font-size:.78rem;color:#888;font-weight:600}
  .dep-texto{font-size:.88rem;color:#555;margin-top:6px;line-height:1.55;font-weight:600}
  .dep-actions{display:flex;gap:6px;margin-top:10px}
  .dep-form{background:#f0f0ff;border-radius:12px;padding:20px;display:none;flex-direction:column;gap:12px}
  .dep-form.visible{display:flex}
  label{font-size:.85rem;font-weight:800;color:#555;margin-bottom:4px;display:block}
  input[type=text],textarea,select{width:100%;padding:10px 14px;border:1.5px solid #ddd;border-radius:8px;font-size:.9rem;font-family:inherit;font-weight:600;transition:.2s}
  input[type=text]:focus,textarea:focus,select:focus{border-color:var(--purple);outline:none}
  textarea{resize:vertical;min-height:80px}
  .form-row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .msg{padding:10px 16px;border-radius:8px;font-weight:700;font-size:.88rem;margin-top:8px}
  .msg.ok{background:#e8fbe8;color:#166534}.msg.err{background:#fee;color:#b91c1c}
  .cor-preview{display:inline-block;width:22px;height:22px;border-radius:50%;margin-left:8px;vertical-align:middle}
  @media(max-width:600px){.form-row{grid-template-columns:1fr}}
</style>
</head>
<body>
<nav>
  <div class="nav-logo">
    <a href="/"><img src="/api/logo" alt="Jogoteka"
       onerror="this.outerHTML='<span class=nav-logo-txt>JOGO<span>TEKA</span></span>'"></a>
  </div>
  <div class="h-links">
    <a href="/" target="_blank" class="btn btn-ghost">Ver Site</a>
    <a href="/painel" class="btn btn-ghost">Painel Admin</a>
  </div>
</nav>
<div class="color-bar">
  <span class="cb-red"></span><span class="cb-orange"></span><span class="cb-green"></span><span class="cb-purple"></span>
</div>

<main>

  <!-- MÍDIAS -->
  <div class="card">
    <h2>🖼️ Fotos e Vídeos</h2>
    <p class="sub">Faça upload de fotos (JPG/PNG) e vídeos (MP4) para exibir na landing page. Arraste para reordenar.</p>

    <button class="btn btn-ghost btn-sm" style="margin-bottom:12px" onclick="limparOrfaos()">🧹 Limpar mídias com erro</button>

    <div class="drop-area" id="dropArea" onclick="document.getElementById('fileInput').click()">
      <div class="drop-icon">📁</div>
      <div class="drop-text">Clique ou arraste arquivos aqui</div>
      <div class="drop-sub">JPG, PNG ou MP4 · Máx. 50MB por arquivo</div>
      <input type="file" id="fileInput" multiple accept="image/*,video/mp4" onchange="uploadMidias(this.files)">
    </div>

    <div id="midiaGrid" class="midia-grid"><div class="loading">Carregando...</div></div>
  </div>

  <!-- DEPOIMENTOS -->
  <div class="card">
    <h2>💬 Depoimentos de Clientes</h2>
    <p class="sub">Adicione, edite ou remova depoimentos exibidos na landing page.</p>

    <button class="btn btn-primary" onclick="abrirFormDep()">+ Adicionar Depoimento</button>

    <div class="dep-form" id="depForm">
      <input type="hidden" id="depId">
      <div class="form-row">
        <div>
          <label>Nome do cliente</label>
          <input type="text" id="depNome" placeholder="Ex: Maria Silva">
        </div>
        <div>
          <label>Tempo</label>
          <input type="text" id="depTempo" placeholder="Ex: 2 meses atrás">
        </div>
      </div>
      <div>
        <label>Depoimento</label>
        <textarea id="depTexto" placeholder="Texto do depoimento..."></textarea>
      </div>
      <div>
        <label>Cor do avatar
          <span class="cor-preview" id="corPreview" style="background:#ED940E"></span>
        </label>
        <select id="depCor" onchange="document.getElementById('corPreview').style.background=this.options[this.selectedIndex].dataset.hex">
          <option value="av-orange" data-hex="#ED940E" selected>🟠 Laranja</option>
          <option value="av-red"    data-hex="#F10A0A">🔴 Vermelho</option>
          <option value="av-green"  data-hex="#17C629">🟢 Verde</option>
          <option value="av-purple" data-hex="#7B20E1">🟣 Roxo</option>
          <option value="av-blue"   data-hex="#4285F4">🔵 Azul</option>
        </select>
      </div>
      <div style="display:flex;gap:10px">
        <button class="btn btn-green" onclick="salvarDep()">💾 Salvar</button>
        <button class="btn btn-ghost" onclick="fecharFormDep()">Cancelar</button>
      </div>
      <div id="depMsg"></div>
    </div>

    <div id="depList" class="dep-list" style="margin-top:16px"><div class="loading">Carregando...</div></div>
  </div>

</main>

<script>
const COR_HEX = {
  'av-orange':'#ED940E','av-red':'#F10A0A','av-green':'#17C629',
  'av-purple':'#7B20E1','av-blue':'#4285F4'
};

// ── MÍDIAS ──────────────────────────────────────────────────────────────────
let _midiaLista = [];

async function carregarMidias(){
  const r = await fetch('/api/landing/midia');
  _midiaLista = await r.json();
  renderMidias();
}

function renderMidias(){
  const grid = document.getElementById('midiaGrid');
  if(!_midiaLista.length){ grid.innerHTML='<p style="color:#aaa;font-weight:700;padding:12px">Nenhuma mídia ainda. Faça upload acima!</p>'; return; }
  grid.innerHTML = _midiaLista.map((m,i) => `
    <div class="midia-item" id="mi${m.id}" draggable="true" data-id="${m.id}" data-idx="${i}"
         ondragstart="dragStart(event)" ondragover="dragOver(event)" ondragleave="dragLeave(event)" ondrop="dragDrop(event)">
      ${m.tipo==='video'
        ? `<video src="/api/landing/midia/${m.id}/conteudo" muted></video>`
        : `<img src="/api/landing/midia/${m.id}/conteudo" alt="${m.legenda||''}">`}
      <div class="midia-info">
        <div class="midia-tipo">${m.tipo==='video'?'📹 Vídeo':'🖼️ Foto'}</div>
        <div class="midia-ordem">Posição ${i+1} de ${_midiaLista.length}</div>
      </div>
      <div class="midia-actions">
        <button class="btn-mover" onclick="moverMidia(${i},-1)" ${i===0?'disabled':''}>↑</button>
        <button class="btn-mover" onclick="moverMidia(${i},1)" ${i===_midiaLista.length-1?'disabled':''}>↓</button>
        <button class="btn btn-red btn-sm" onclick="removerMidia(${m.id})">🗑️</button>
      </div>
    </div>`).join('');
}

async function moverMidia(idx, dir){
  const novo = idx + dir;
  if(novo < 0 || novo >= _midiaLista.length) return;
  [_midiaLista[idx], _midiaLista[novo]] = [_midiaLista[novo], _midiaLista[idx]];
  renderMidias();
  await salvarOrdemMidia();
}

async function salvarOrdemMidia(){
  const ids = _midiaLista.map(m => m.id);
  await fetch('/api/landing/midia/ordem', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ids})
  });
}

// Drag & drop reordenação
let _dragIdx = null;
function dragStart(e){ _dragIdx = parseInt(e.currentTarget.dataset.idx); e.currentTarget.classList.add('dragging'); }
function dragOver(e){ e.preventDefault(); e.currentTarget.classList.add('drag-over'); }
function dragLeave(e){ e.currentTarget.classList.remove('drag-over'); }
async function dragDrop(e){
  e.preventDefault();
  const toIdx = parseInt(e.currentTarget.dataset.idx);
  e.currentTarget.classList.remove('drag-over');
  document.querySelectorAll('.midia-item').forEach(el=>el.classList.remove('dragging'));
  if(_dragIdx === null || _dragIdx === toIdx) return;
  const item = _midiaLista.splice(_dragIdx, 1)[0];
  _midiaLista.splice(toIdx, 0, item);
  _dragIdx = null;
  renderMidias();
  await salvarOrdemMidia();
}

async function uploadMidias(files){
  for(const f of files){
    const fd = new FormData();
    fd.append('arquivo', f);
    const r = await fetch('/api/landing/midia', {method:'POST', body:fd});
    const j = await r.json();
    if(j.erro) alert('Erro: '+j.erro);
  }
  carregarMidias();
}

async function removerMidia(id){
  if(!confirm('Remover esta mídia da landing page?')) return;
  await fetch('/api/landing/midia/'+id, {method:'DELETE'});
  carregarMidias();
}

async function limparOrfaos(){
  if(!confirm('Isso vai remover todas as mídias que estão com erro (sem conteúdo). Continuar?')) return;
  const r = await fetch('/api/landing/midia/limpar-orfaos', {method:'POST'});
  const j = await r.json();
  alert(`${j.removidos} mídia(s) com erro removida(s).`);
  carregarMidias();
}

// Drag & drop upload
const drop = document.getElementById('dropArea');
drop.addEventListener('dragover', e=>{e.preventDefault();drop.classList.add('dragover')});
drop.addEventListener('dragleave', ()=>drop.classList.remove('dragover'));
drop.addEventListener('drop', e=>{e.preventDefault();drop.classList.remove('dragover');uploadMidias(e.dataTransfer.files)});

// ── DEPOIMENTOS ──────────────────────────────────────────────────────────────
async function carregarDeps(){
  const r = await fetch('/api/landing/depoimentos');
  const lista = await r.json();
  const el = document.getElementById('depList');
  if(!lista.length){ el.innerHTML='<p style="color:#aaa;font-weight:700;padding:12px">Nenhum depoimento ainda.</p>'; return; }
  el.innerHTML = lista.map(d=>`
    <div class="dep-item">
      <div class="dep-avatar" style="background:${COR_HEX[d.cor]||'#ED940E'}">${d.nome[0].toUpperCase()}</div>
      <div class="dep-body">
        <div class="dep-nome">${d.nome}</div>
        <div class="dep-tempo">⭐⭐⭐⭐⭐ · ${d.tempo}</div>
        <div class="dep-texto">${d.texto}</div>
        <div class="dep-actions">
          <button class="btn btn-ghost btn-sm" onclick='editarDep(${JSON.stringify(d)})'>✏️ Editar</button>
          <button class="btn btn-red btn-sm" onclick="removerDep(${d.id})">🗑️ Remover</button>
        </div>
      </div>
    </div>`).join('');
}

function abrirFormDep(){
  document.getElementById('depId').value='';
  document.getElementById('depNome').value='';
  document.getElementById('depTexto').value='';
  document.getElementById('depTempo').value='1 mês atrás';
  document.getElementById('depCor').value='av-orange';
  document.getElementById('corPreview').style.background='#ED940E';
  document.getElementById('depMsg').innerHTML='';
  document.getElementById('depForm').classList.add('visible');
  document.getElementById('depForm').scrollIntoView({behavior:'smooth'});
}

function fecharFormDep(){ document.getElementById('depForm').classList.remove('visible'); }

function editarDep(d){
  document.getElementById('depId').value=d.id;
  document.getElementById('depNome').value=d.nome;
  document.getElementById('depTexto').value=d.texto;
  document.getElementById('depTempo').value=d.tempo;
  document.getElementById('depCor').value=d.cor;
  document.getElementById('corPreview').style.background=COR_HEX[d.cor]||'#ED940E';
  document.getElementById('depMsg').innerHTML='';
  document.getElementById('depForm').classList.add('visible');
  document.getElementById('depForm').scrollIntoView({behavior:'smooth'});
}

async function salvarDep(){
  const id = document.getElementById('depId').value;
  const body = {
    nome: document.getElementById('depNome').value.trim(),
    texto: document.getElementById('depTexto').value.trim(),
    tempo: document.getElementById('depTempo').value.trim(),
    cor: document.getElementById('depCor').value,
  };
  if(!body.nome||!body.texto){ mostrarMsg('depMsg','Preencha nome e depoimento.','err'); return; }
  const url = id ? '/api/landing/depoimentos/'+id : '/api/landing/depoimentos';
  const method = id ? 'PUT' : 'POST';
  const r = await fetch(url, {method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const j = await r.json();
  if(j.ok){ fecharFormDep(); carregarDeps(); }
  else mostrarMsg('depMsg', j.erro||'Erro ao salvar.','err');
}

async function removerDep(id){
  if(!confirm('Remover este depoimento?')) return;
  await fetch('/api/landing/depoimentos/'+id, {method:'DELETE'});
  carregarDeps();
}

function mostrarMsg(elId, txt, tipo){
  document.getElementById(elId).innerHTML=`<div class="msg ${tipo}">${txt}</div>`;
}

// Init
carregarMidias();
carregarDeps();
</script>
</body>
</html>"""


@app.route("/landing-admin")
@requer_login
def landing_admin():
    return render_template_string(LANDING_ADMIN_HTML)


# ── API Landing — Mídias ───────────────────────────────────────────────────────

LANDING_DIR = os.path.join(os.path.dirname(__file__), "static", "landing")
_MAX_MIDIA  = 50 * 1024 * 1024  # 50 MB


@app.route("/api/landing/midia", methods=["GET"])
@requer_login
def api_landing_midia_listar():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM landing_midia WHERE ativo=1 ORDER BY ordem, id"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/landing/midia", methods=["POST"])
@requer_login
def api_landing_midia_upload():
    f = request.files.get("arquivo")
    if not f:
        return jsonify({"erro": "Nenhum arquivo enviado"}), 400
    if f.content_length and f.content_length > _MAX_MIDIA:
        return jsonify({"erro": "Arquivo muito grande (máx 50MB)"}), 400

    dados = f.read()
    if len(dados) > _MAX_MIDIA:
        return jsonify({"erro": "Arquivo muito grande (máx 50MB)"}), 400

    mime = f.content_type or ""
    if mime.startswith("video"):
        tipo = "video"
        ext  = ".mp4"
    elif mime.startswith("image"):
        tipo = "foto"
        ext  = os.path.splitext(f.filename or ".jpg")[1] or ".jpg"
    else:
        return jsonify({"erro": "Tipo de arquivo não suportado"}), 400

    import uuid as _uuid, base64 as _b64
    nome = f"landing_{_uuid.uuid4().hex[:8]}{ext}"
    conteudo_b64 = _b64.b64encode(dados).decode()

    agora = _agora_str()
    with get_connection() as conn:
        # garante colunas existam (bancos antigos)
        try:
            add = "ADD COLUMN IF NOT EXISTS" if DATABASE_URL else "ADD COLUMN"
            cols = _get_cols(conn, "landing_midia")
            if "conteudo_b64" not in cols:
                conn.execute(f"ALTER TABLE landing_midia {add} conteudo_b64 TEXT")
            if "mime_type" not in cols:
                conn.execute(f"ALTER TABLE landing_midia {add} mime_type TEXT")
        except Exception:
            pass
        conn.execute(
            "INSERT INTO landing_midia (tipo, nome_arquivo, legenda, ordem, ativo, criado_em, conteudo_b64, mime_type) VALUES (?,?,?,?,1,?,?,?)",
            (tipo, nome, f.filename, 0, agora, conteudo_b64, mime)
        )
    return jsonify({"ok": True, "nome": nome})


@app.route("/api/landing/midia/<int:mid>/conteudo")
def api_landing_midia_conteudo(mid):
    """Serve a mídia direto do banco — sobrevive a restarts do Railway."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT conteudo_b64, mime_type, nome_arquivo FROM landing_midia WHERE id=? AND ativo=1", (mid,)
        ).fetchone()
    if not row or not row["conteudo_b64"]:
        # fallback: tenta servir do disco (fotos originais do git)
        nome = row["nome_arquivo"] if row else None
        if nome:
            pasta = os.path.join(os.path.dirname(__file__), "static", "landing")
            try:
                return send_from_directory(pasta, nome)
            except Exception:
                pass
        return "", 404
    import base64 as _b64
    dados = _b64.b64decode(row["conteudo_b64"])
    mime  = row["mime_type"] or "application/octet-stream"
    from flask import Response
    return Response(dados, mimetype=mime)


@app.route("/api/landing/midia/limpar-orfaos", methods=["POST"])
@requer_login
def api_landing_midia_limpar():
    pasta = os.path.join(os.path.dirname(__file__), "static", "landing")
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, nome_arquivo, conteudo_b64 FROM landing_midia WHERE ativo=1"
        ).fetchall()
        removidos = 0
        for r in rows:
            tem_b64 = r["conteudo_b64"]
            tem_disco = os.path.exists(os.path.join(pasta, r["nome_arquivo"] or ""))
            if not tem_b64 and not tem_disco:
                conn.execute("UPDATE landing_midia SET ativo=0 WHERE id=?", (r["id"],))
                removidos += 1
    return jsonify({"ok": True, "removidos": removidos})


@app.route("/api/landing/midia/ordem", methods=["POST"])
@requer_login
def api_landing_midia_ordem():
    ids = (request.json or {}).get("ids", [])
    with get_connection() as conn:
        for i, mid in enumerate(ids):
            conn.execute("UPDATE landing_midia SET ordem=? WHERE id=?", (i, mid))
    return jsonify({"ok": True})


@app.route("/api/landing/midia/<int:mid>", methods=["DELETE"])
@requer_login
def api_landing_midia_remover(mid):
    with get_connection() as conn:
        row = conn.execute("SELECT nome_arquivo FROM landing_midia WHERE id=?", (mid,)).fetchone()
        if not row:
            return jsonify({"erro": "Não encontrado"}), 404
        conn.execute("UPDATE landing_midia SET ativo=0 WHERE id=?", (mid,))
    return jsonify({"ok": True})


# ── API Landing — Depoimentos ──────────────────────────────────────────────────

@app.route("/api/landing/depoimentos", methods=["GET"])
@requer_login
def api_dep_listar():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM landing_depoimentos WHERE ativo=1 ORDER BY ordem, id"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/landing/depoimentos", methods=["POST"])
@requer_login
def api_dep_criar():
    d = request.json or {}
    if not d.get("nome") or not d.get("texto"):
        return jsonify({"erro": "Nome e texto são obrigatórios"}), 400
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO landing_depoimentos (nome, texto, tempo, cor, ordem, ativo) VALUES (?,?,?,?,0,1)",
            (d["nome"], d["texto"], d.get("tempo","1 mês atrás"), d.get("cor","av-orange"))
        )
    return jsonify({"ok": True})


@app.route("/api/landing/depoimentos/<int:did>", methods=["PUT"])
@requer_login
def api_dep_editar(did):
    d = request.json or {}
    with get_connection() as conn:
        conn.execute(
            "UPDATE landing_depoimentos SET nome=?, texto=?, tempo=?, cor=? WHERE id=?",
            (d.get("nome"), d.get("texto"), d.get("tempo","1 mês atrás"), d.get("cor","av-orange"), did)
        )
    return jsonify({"ok": True})


@app.route("/api/landing/depoimentos/<int:did>", methods=["DELETE"])
@requer_login
def api_dep_remover(did):
    with get_connection() as conn:
        conn.execute("UPDATE landing_depoimentos SET ativo=0 WHERE id=?", (did,))
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(port=port, debug=False)
