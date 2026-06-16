import os
import re

DATABASE_URL = os.environ.get("DATABASE_URL")

# ── PostgreSQL ────────────────────────────────────────────────────────────────
if DATABASE_URL:
    import psycopg2
    import psycopg2.extras

    class _Row:
        def __init__(self, d):
            self._d = dict(d)
        def __getitem__(self, k):
            return list(self._d.values())[k] if isinstance(k, int) else self._d[k]
        def keys(self): return self._d.keys()
        def __iter__(self): return iter(self._d.values())
        def get(self, k, d=None): return self._d.get(k, d)
        def __contains__(self, k): return k in self._d

    class _Cur:
        def __init__(self, c, lastrowid=None):
            self._c, self.lastrowid = c, lastrowid
        def fetchone(self):
            r = self._c.fetchone(); return _Row(r) if r else None
        def fetchall(self): return [_Row(r) for r in self._c.fetchall()]
        def __iter__(self):
            for r in self._c: yield _Row(r)

    class _Conn:
        def __init__(self): self._c = psycopg2.connect(DATABASE_URL)
        def execute(self, sql, params=()):
            sql = sql.replace("?", "%s")
            ins = bool(re.match(r'\s*INSERT', sql, re.I))
            if ins and "RETURNING" not in sql.upper():
                sql = sql.rstrip().rstrip(";") + " RETURNING id"
            cur = self._c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql, params or ())
            rid = None
            if ins:
                try:
                    r = cur.fetchone(); rid = r["id"] if r else None
                except: pass
            return _Cur(cur, rid)
        def executescript(self, sql):
            cur = self._c.cursor()
            for s in sql.split(";"):
                s = s.strip()
                if s: cur.execute(s)
        def __enter__(self): return self
        def __exit__(self, et, ev, tb):
            (self._c.rollback if et else self._c.commit)()
            self._c.close()

    def get_connection(): return _Conn()

# ── SQLite (local sem internet) ───────────────────────────────────────────────
else:
    import sqlite3
    _DATA_DIR = os.environ.get("DATA_DIR",
        os.path.join(os.path.expanduser("~"), "Desktop", "CLAUDE", "estoque_jogos"))
    DB_PATH = os.path.join(_DATA_DIR, "jogos.db")

    def get_connection():
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn


def _get_cols(conn, table):
    if DATABASE_URL:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name=%s AND table_schema='public'",
            (table,)
        ).fetchall()
        return {r["column_name"] for r in rows}
    else:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


_PG_SCHEMA = """
    CREATE TABLE IF NOT EXISTS jogos (
        id SERIAL PRIMARY KEY,
        nome TEXT NOT NULL, editora TEXT, categoria TEXT,
        min_jogadores INTEGER, max_jogadores INTEGER, tempo_jogo INTEGER,
        quantidade INTEGER NOT NULL DEFAULT 0,
        quantidade_minima INTEGER NOT NULL DEFAULT 1,
        preco_venda REAL, imagem TEXT, video_url TEXT,
        loc1_dias INTEGER, loc1_valor REAL,
        loc2_dias INTEGER, loc2_valor REAL,
        loc3_dias INTEGER, loc3_valor REAL,
        multa_dia REAL, faixa_etaria TEXT, resumo TEXT, destaque TEXT,
        em_destaque INTEGER NOT NULL DEFAULT 0,
        curtidas INTEGER NOT NULL DEFAULT 0,
        data_cadastro TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS movimentacoes (
        id SERIAL PRIMARY KEY,
        jogo_id INTEGER NOT NULL,
        tipo TEXT NOT NULL CHECK(tipo IN ('entrada','saida')),
        quantidade INTEGER NOT NULL, motivo TEXT, observacao TEXT,
        data TEXT NOT NULL,
        FOREIGN KEY (jogo_id) REFERENCES jogos(id)
    );
    CREATE TABLE IF NOT EXISTS compras (
        id SERIAL PRIMARY KEY,
        jogo_id INTEGER NOT NULL, fornecedor TEXT,
        data_compra TEXT NOT NULL, quantidade INTEGER NOT NULL,
        valor_pago REAL, forma_pagamento TEXT, parcelas INTEGER DEFAULT 1,
        pedido_compra TEXT, numero_nf TEXT, arquivo_nf TEXT, observacao TEXT,
        FOREIGN KEY (jogo_id) REFERENCES jogos(id)
    );
    CREATE TABLE IF NOT EXISTS clientes (
        id SERIAL PRIMARY KEY,
        nome TEXT NOT NULL, cpf TEXT, data_nascimento TEXT,
        instagram TEXT, telefone TEXT, data_cadastro TEXT NOT NULL,
        cep TEXT, logradouro TEXT, numero TEXT, complemento TEXT,
        bairro TEXT, cidade TEXT, estado TEXT
    );
    CREATE TABLE IF NOT EXISTS vendas (
        id SERIAL PRIMARY KEY,
        jogo_id INTEGER NOT NULL, cliente_id INTEGER,
        data_venda TEXT NOT NULL, quantidade INTEGER NOT NULL DEFAULT 1,
        preco_unitario REAL, desconto REAL DEFAULT 0, valor_final REAL,
        forma_pagamento TEXT, observacao TEXT, atendente TEXT,
        status_pagamento TEXT DEFAULT 'pendente',
        FOREIGN KEY (jogo_id) REFERENCES jogos(id),
        FOREIGN KEY (cliente_id) REFERENCES clientes(id)
    );
    CREATE TABLE IF NOT EXISTS locacoes (
        id SERIAL PRIMARY KEY,
        jogo_id INTEGER NOT NULL, cliente_id INTEGER,
        data_saida TEXT NOT NULL, data_prevista TEXT NOT NULL,
        data_devolucao TEXT, opcao_dias INTEGER,
        valor_locacao REAL, valor_multa REAL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'ativa',
        observacao TEXT, atendente TEXT, condicao_devolucao TEXT,
        avaria_descricao TEXT, status_pagamento TEXT DEFAULT 'pendente',
        forma_pagamento TEXT,
        contrato_token TEXT, contrato_status TEXT, contrato_request_id TEXT,
        FOREIGN KEY (jogo_id) REFERENCES jogos(id),
        FOREIGN KEY (cliente_id) REFERENCES clientes(id)
    );
    CREATE TABLE IF NOT EXISTS cupons (
        id SERIAL PRIMARY KEY,
        codigo TEXT NOT NULL UNIQUE,
        tipo TEXT NOT NULL CHECK(tipo IN ('pct','reais')),
        valor REAL NOT NULL, descricao TEXT, usos_maximos INTEGER,
        usos_realizados INTEGER NOT NULL DEFAULT 0,
        ativo INTEGER NOT NULL DEFAULT 1, data_criacao TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS cupom_usos (
        id SERIAL PRIMARY KEY,
        cupom_id INTEGER NOT NULL, tipo_operacao TEXT NOT NULL,
        referencia_id INTEGER, valor_desconto REAL NOT NULL,
        cliente_nome TEXT, jogo_nome TEXT, data_uso TEXT NOT NULL,
        FOREIGN KEY (cupom_id) REFERENCES cupons(id)
    );
    CREATE TABLE IF NOT EXISTS jogo_imagens (
        id SERIAL PRIMARY KEY,
        nome_original TEXT,
        conteudo_b64 TEXT NOT NULL,
        mime_type TEXT NOT NULL,
        data_upload TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS nf_arquivos (
        id SERIAL PRIMARY KEY,
        nome_original TEXT,
        conteudo_b64 TEXT NOT NULL,
        data_upload TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS usuarios (
        id SERIAL PRIMARY KEY,
        nome TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE,
        senha_hash TEXT NOT NULL,
        perfil TEXT NOT NULL DEFAULT 'vendedor',
        ativo INTEGER NOT NULL DEFAULT 1,
        data_cadastro TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS extratos (
        id SERIAL PRIMARY KEY,
        nome_arquivo TEXT NOT NULL, data_upload TEXT NOT NULL,
        total_lancamentos INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS extrato_lancamentos (
        id SERIAL PRIMARY KEY,
        extrato_id INTEGER NOT NULL, data TEXT NOT NULL,
        descricao TEXT, valor REAL NOT NULL,
        conciliado INTEGER NOT NULL DEFAULT 0,
        venda_id INTEGER, locacao_id INTEGER,
        FOREIGN KEY (extrato_id) REFERENCES extratos(id),
        FOREIGN KEY (venda_id) REFERENCES vendas(id),
        FOREIGN KEY (locacao_id) REFERENCES locacoes(id)
    );
    CREATE TABLE IF NOT EXISTS contrato_modelo (
        id INTEGER PRIMARY KEY,
        clausulas TEXT NOT NULL,
        template_pdf_b64 TEXT,
        template_docx_b64 TEXT,
        atualizado_em TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS mensagem_lembrete (
        id SERIAL PRIMARY KEY,
        nome TEXT NOT NULL DEFAULT 'Lembrete de Devolução',
        template TEXT NOT NULL,
        ativo INTEGER NOT NULL DEFAULT 1,
        atualizado_em TEXT
    );

    CREATE TABLE IF NOT EXISTS lembretes_log (
        id SERIAL PRIMARY KEY,
        locacao_id INTEGER,
        cliente_nome TEXT,
        cliente_tel TEXT,
        jogo_nome TEXT,
        data_prevista TEXT,
        status TEXT NOT NULL,
        erro TEXT,
        enviado_em TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS cron_status (
        id INTEGER PRIMARY KEY,
        ultimo_disparo TEXT,
        ultimo_resultado TEXT
    );

    CREATE TABLE IF NOT EXISTS landing_midia (
        id SERIAL PRIMARY KEY,
        tipo TEXT NOT NULL,
        nome_arquivo TEXT NOT NULL,
        legenda TEXT,
        ordem INTEGER NOT NULL DEFAULT 0,
        ativo INTEGER NOT NULL DEFAULT 1,
        criado_em TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS landing_depoimentos (
        id SERIAL PRIMARY KEY,
        nome TEXT NOT NULL,
        texto TEXT NOT NULL,
        tempo TEXT NOT NULL DEFAULT '1 mês atrás',
        cor TEXT NOT NULL DEFAULT 'av-orange',
        ordem INTEGER NOT NULL DEFAULT 0,
        ativo INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS categorias (
        id SERIAL PRIMARY KEY,
        nome TEXT NOT NULL UNIQUE
    );

    CREATE TABLE IF NOT EXISTS destaques_opcoes (
        id SERIAL PRIMARY KEY,
        nome TEXT NOT NULL UNIQUE
    );

    CREATE TABLE IF NOT EXISTS favoritos (
        id SERIAL PRIMARY KEY,
        cliente_id INTEGER NOT NULL,
        jogo_id INTEGER NOT NULL,
        data TEXT NOT NULL,
        UNIQUE(cliente_id, jogo_id),
        FOREIGN KEY (cliente_id) REFERENCES clientes(id),
        FOREIGN KEY (jogo_id) REFERENCES jogos(id)
    )
"""

_SQLITE_SCHEMA = _PG_SCHEMA.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")


def init_db():
    with get_connection() as conn:
        conn.executescript(_PG_SCHEMA if DATABASE_URL else _SQLITE_SCHEMA)

        add_col = "ADD COLUMN IF NOT EXISTS" if DATABASE_URL else "ADD COLUMN"

        cols_jogos = _get_cols(conn, "jogos")
        for col, tipo in [
            ("preco_venda","REAL"),("imagem","TEXT"),("video_url","TEXT"),
            ("loc1_dias","INTEGER"),("loc1_valor","REAL"),
            ("loc2_dias","INTEGER"),("loc2_valor","REAL"),
            ("loc3_dias","INTEGER"),("loc3_valor","REAL"),
            ("multa_dia","REAL"),("faixa_etaria","TEXT"),("resumo","TEXT"),("destaque","TEXT"),
            ("em_destaque","INTEGER"),("curtidas","INTEGER"),("cidades","TEXT"),
        ]:
            if col not in cols_jogos:
                conn.execute(f"ALTER TABLE jogos {add_col} {col} {tipo}")

        cols_compras = _get_cols(conn, "compras")
        for col, tipo in [("pedido_compra","TEXT"),("numero_nf","TEXT"),("arquivo_nf","TEXT")]:
            if col not in cols_compras:
                conn.execute(f"ALTER TABLE compras {add_col} {col} {tipo}")

        cols_vendas = _get_cols(conn, "vendas")
        for col, tipo in [("atendente","TEXT"),("status_pagamento","TEXT")]:
            if col not in cols_vendas:
                conn.execute(f"ALTER TABLE vendas {add_col} {col} {tipo}")

        cols_locacoes = _get_cols(conn, "locacoes")
        for col, tipo in [
            ("atendente","TEXT"),("condicao_devolucao","TEXT"),
            ("avaria_descricao","TEXT"),("status_pagamento","TEXT"),
            ("forma_pagamento","TEXT"),
            ("contrato_token","TEXT"),("contrato_status","TEXT"),("contrato_request_id","TEXT"),
        ]:
            if col not in cols_locacoes:
                conn.execute(f"ALTER TABLE locacoes {add_col} {col} {tipo}")

        cols_clientes = _get_cols(conn, "clientes")
        for col, tipo in [
            ("cep","TEXT"),("logradouro","TEXT"),("numero","TEXT"),
            ("complemento","TEXT"),("bairro","TEXT"),("cidade","TEXT"),("estado","TEXT"),
            ("catalogo_token","TEXT"),
        ]:
            if col not in cols_clientes:
                conn.execute(f"ALTER TABLE clientes {add_col} {col} {tipo}")

        # índice único no catalogo_token (ignora se já existe)
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_clientes_catalogo_token ON clientes(catalogo_token)")
        except Exception:
            pass

        # tabela favoritos
        try:
            _get_cols(conn, "favoritos")
        except Exception:
            sql_fav = (
                "CREATE TABLE IF NOT EXISTS favoritos ("
                "id SERIAL PRIMARY KEY, cliente_id INTEGER NOT NULL, "
                "jogo_id INTEGER NOT NULL, data TEXT NOT NULL, "
                "UNIQUE(cliente_id, jogo_id), "
                "FOREIGN KEY (cliente_id) REFERENCES clientes(id), "
                "FOREIGN KEY (jogo_id) REFERENCES jogos(id))"
            )
            if not DATABASE_URL:
                sql_fav = sql_fav.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
            conn.execute(sql_fav)

        # tabelas categorias e destaques_opcoes
        for tbl, sql_base in [
            ("categorias",
             "CREATE TABLE IF NOT EXISTS categorias (id SERIAL PRIMARY KEY, nome TEXT NOT NULL UNIQUE)"),
            ("destaques_opcoes",
             "CREATE TABLE IF NOT EXISTS destaques_opcoes (id SERIAL PRIMARY KEY, nome TEXT NOT NULL UNIQUE)"),
        ]:
            try:
                _get_cols(conn, tbl)
            except Exception:
                s = sql_base
                if not DATABASE_URL:
                    s = s.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
                conn.execute(s)

        # seed categorias com as que já existem nos jogos
        try:
            cats_existentes = conn.execute(
                "SELECT DISTINCT categoria FROM jogos WHERE categoria IS NOT NULL AND categoria != ''"
            ).fetchall()
            for row in cats_existentes:
                try:
                    if DATABASE_URL:
                        conn.execute("INSERT INTO categorias (nome) VALUES (%s) ON CONFLICT DO NOTHING", (row[0],))
                    else:
                        conn.execute("INSERT OR IGNORE INTO categorias (nome) VALUES (?)", (row[0],))
                except Exception:
                    pass
        except Exception:
            pass

        # seed destaques com opções padrão
        try:
            for opt in ["Top 1", "Novo", "Clássico", "Mais Difícil", "Mais Vendido", "Lançamento"]:
                try:
                    if DATABASE_URL:
                        conn.execute("INSERT INTO destaques_opcoes (nome) VALUES (%s) ON CONFLICT DO NOTHING", (opt,))
                    else:
                        conn.execute("INSERT OR IGNORE INTO destaques_opcoes (nome) VALUES (?)", (opt,))
                except Exception:
                    pass
        except Exception:
            pass

        # tabela jogo_imagens (pode não existir em bancos antigos)
        try:
            _get_cols(conn, "jogo_imagens")
        except Exception:
            sql_ji = (
                "CREATE TABLE IF NOT EXISTS jogo_imagens ("
                "id SERIAL PRIMARY KEY, nome_original TEXT, "
                "conteudo_b64 TEXT NOT NULL, mime_type TEXT NOT NULL, data_upload TEXT NOT NULL)"
            )
            if not DATABASE_URL:
                sql_ji = sql_ji.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
            conn.execute(sql_ji)

        # tabela nf_arquivos (pode não existir em bancos antigos)
        try:
            _get_cols(conn, "nf_arquivos")
        except Exception:
            sql_nf = (
                "CREATE TABLE IF NOT EXISTS nf_arquivos ("
                "id SERIAL PRIMARY KEY, nome_original TEXT, "
                "conteudo_b64 TEXT NOT NULL, data_upload TEXT NOT NULL)"
            )
            if not DATABASE_URL:
                sql_nf = sql_nf.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
            conn.execute(sql_nf)

        # tabela usuarios (pode não existir em bancos antigos)
        try:
            _get_cols(conn, "usuarios")
        except Exception:
            sql_u = (
                "CREATE TABLE IF NOT EXISTS usuarios ("
                "id SERIAL PRIMARY KEY, nome TEXT NOT NULL, "
                "email TEXT NOT NULL UNIQUE, senha_hash TEXT NOT NULL, "
                "perfil TEXT NOT NULL DEFAULT 'vendedor', "
                "ativo INTEGER NOT NULL DEFAULT 1, data_cadastro TEXT NOT NULL)"
            )
            if not DATABASE_URL:
                sql_u = sql_u.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
            conn.execute(sql_u)

        # tabela contrato_modelo (pode não existir em bancos antigos)
        try:
            cols_cm = _get_cols(conn, "contrato_modelo")
            if "template_pdf_b64" not in cols_cm:
                conn.execute(f"ALTER TABLE contrato_modelo {add_col} template_pdf_b64 TEXT")
            if "template_docx_b64" not in cols_cm:
                conn.execute(f"ALTER TABLE contrato_modelo {add_col} template_docx_b64 TEXT")
        except Exception:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS contrato_modelo "
                "(id INTEGER PRIMARY KEY, clausulas TEXT NOT NULL, "
                "template_pdf_b64 TEXT, atualizado_em TEXT NOT NULL)"
            )
