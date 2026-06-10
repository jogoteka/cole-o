from datetime import datetime, date, timedelta
from database import get_connection
import estoque as est


def _agora():
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def _hoje():
    return date.today().isoformat()


# ── Clientes ──────────────────────────────────────────────────────────────────

def buscar_ou_criar_cliente(dados):
    nome = dados.get("nome", "").strip()
    cpf  = (dados.get("cpf") or "").strip() or None
    with get_connection() as conn:
        if cpf:
            row = conn.execute("SELECT id FROM clientes WHERE cpf = ?", (cpf,)).fetchone()
            if row:
                conn.execute("""UPDATE clientes SET nome=?, instagram=?, telefone=?, data_nascimento=?,
                                cep=?, logradouro=?, numero=?, complemento=?, bairro=?, cidade=?, estado=?
                                WHERE id=?""",
                             (nome, dados.get("instagram"), dados.get("telefone"),
                              dados.get("data_nascimento"),
                              dados.get("cep"), dados.get("logradouro"), dados.get("numero"),
                              dados.get("complemento"), dados.get("bairro"), dados.get("cidade"),
                              dados.get("estado"), row["id"]))
                return row["id"]
        cur = conn.execute("""
            INSERT INTO clientes (nome, cpf, data_nascimento, instagram, telefone, data_cadastro,
                                  cep, logradouro, numero, complemento, bairro, cidade, estado)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (nome, cpf, dados.get("data_nascimento"), dados.get("instagram"),
             dados.get("telefone"), _agora(),
             dados.get("cep"), dados.get("logradouro"), dados.get("numero"),
             dados.get("complemento"), dados.get("bairro"), dados.get("cidade"),
             dados.get("estado")))
        return cur.lastrowid


def listar_clientes(busca=None):
    with get_connection() as conn:
        if busca:
            like = f"%{busca}%"
            return conn.execute(
                "SELECT * FROM clientes WHERE nome LIKE ? OR cpf LIKE ? OR instagram LIKE ? ORDER BY nome",
                (like, like, like)
            ).fetchall()
        return conn.execute("SELECT * FROM clientes ORDER BY nome").fetchall()


# ── Vendas ────────────────────────────────────────────────────────────────────

def registrar_venda(dados):
    jogo_id     = dados["jogo_id"]
    quantidade  = dados.get("quantidade", 1)
    cliente_dados = dados.get("cliente", {})

    jogo = est.buscar_jogo(jogo_id)
    if not jogo:
        raise ValueError("Jogo não encontrado")
    if jogo["quantidade"] < quantidade:
        raise ValueError(f"Estoque insuficiente. Disponível: {jogo['quantidade']}")

    cliente_id = buscar_ou_criar_cliente(cliente_dados) if cliente_dados.get("nome") else None

    preco_unitario = dados.get("preco_unitario") or jogo["preco_venda"] or 0
    desconto       = dados.get("desconto") or 0
    valor_final    = (preco_unitario * quantidade) - desconto

    with get_connection() as conn:
        cur = conn.execute("""
            INSERT INTO vendas
                (jogo_id, cliente_id, data_venda, quantidade, preco_unitario,
                 desconto, valor_final, forma_pagamento, observacao, atendente)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (jogo_id, cliente_id,
             dados.get("data_venda") or _agora()[:10],
             quantidade, preco_unitario, desconto, valor_final,
             dados.get("forma_pagamento"), dados.get("observacao"),
             dados.get("atendente")))
        venda_id = cur.lastrowid

    est.movimentar(jogo_id, "saida", quantidade, "venda", dados.get("observacao", ""))
    return {"valor_final": valor_final, "venda_id": venda_id}


def listar_vendas(limite=100):
    with get_connection() as conn:
        return conn.execute("""
            SELECT v.*, j.nome AS jogo_nome, c.nome AS cliente_nome, c.telefone AS cliente_tel,
                   (SELECT ROUND(cp.valor_pago * 1.0 / cp.quantidade, 2)
                    FROM compras cp
                    WHERE cp.jogo_id = v.jogo_id AND cp.valor_pago IS NOT NULL
                    ORDER BY cp.data_compra DESC LIMIT 1) AS custo_unitario
            FROM vendas v
            JOIN jogos j ON j.id = v.jogo_id
            LEFT JOIN clientes c ON c.id = v.cliente_id
            ORDER BY v.data_venda DESC LIMIT ?""", (limite,)).fetchall()


# ── Locações ──────────────────────────────────────────────────────────────────

def registrar_locacao(dados):
    jogo_id = dados["jogo_id"]
    jogo = est.buscar_jogo(jogo_id)
    if not jogo:
        raise ValueError("Jogo não encontrado")
    if jogo["quantidade"] < 1:
        raise ValueError("Jogo sem estoque disponível para locação")

    cliente_id = buscar_ou_criar_cliente(dados.get("cliente", {})) if dados.get("cliente", {}).get("nome") else None

    opcao_dias    = dados["opcao_dias"]
    data_saida    = dados.get("data_saida") or _hoje()
    data_prevista = (date.fromisoformat(data_saida) + timedelta(days=opcao_dias)).isoformat()
    valor_locacao = dados.get("valor_locacao", 0)

    with get_connection() as conn:
        cur = conn.execute("""
            INSERT INTO locacoes
                (jogo_id, cliente_id, data_saida, data_prevista,
                 opcao_dias, valor_locacao, status, observacao, atendente, forma_pagamento)
            VALUES (?, ?, ?, ?, ?, ?, 'ativa', ?, ?, ?)""",
            (jogo_id, cliente_id, data_saida, data_prevista,
             opcao_dias, valor_locacao, dados.get("observacao"),
             dados.get("atendente"), dados.get("forma_pagamento")))
        loc_id = cur.lastrowid

    est.movimentar(jogo_id, "saida", 1, "empréstimo", f"Locação #{loc_id}")
    return {"locacao_id": loc_id, "data_prevista": data_prevista}


def listar_locacoes(status=None):
    with get_connection() as conn:
        sql = """
            SELECT l.*, j.nome AS jogo_nome, j.imagem AS jogo_imagem,
                   j.multa_dia, c.nome AS cliente_nome, c.telefone AS cliente_tel
            FROM locacoes l
            JOIN jogos j ON j.id = l.jogo_id
            LEFT JOIN clientes c ON c.id = l.cliente_id
        """
        params = []
        if status:
            sql += " WHERE l.status = ?"
            params.append(status)
        sql += " ORDER BY l.data_prevista ASC"
        return conn.execute(sql, params).fetchall()


def registrar_devolucao(locacao_id, data_devolucao=None, dados=None):
    dados = dados or {}
    data_dev = data_devolucao or _hoje()
    with get_connection() as conn:
        loc = conn.execute("SELECT * FROM locacoes WHERE id = ?", (locacao_id,)).fetchone()
        if not loc:
            raise ValueError("Locação não encontrada")
        if loc["status"] == "devolvido":
            raise ValueError("Jogo já foi devolvido")

        # Calcula multa
        prevista = date.fromisoformat(loc["data_prevista"])
        devolvida = date.fromisoformat(data_dev)
        dias_atraso = max(0, (devolvida - prevista).days)

        jogo = conn.execute(
            "SELECT multa_dia FROM jogos WHERE id = ?", (loc["jogo_id"],)
        ).fetchone()
        multa_dia  = (jogo["multa_dia"] or 0) if jogo else 0
        valor_multa = dias_atraso * multa_dia

        conn.execute("""UPDATE locacoes
                        SET status='devolvido', data_devolucao=?, valor_multa=?,
                            condicao_devolucao=?, avaria_descricao=?
                        WHERE id=?""",
                     (data_dev, valor_multa,
                      dados.get("condicao_devolucao"),
                      dados.get("avaria_descricao"),
                      locacao_id))

    est.movimentar(loc["jogo_id"], "entrada", 1, "devolução", f"Locação #{locacao_id}")
    return {"dias_atraso": dias_atraso, "valor_multa": valor_multa}


def excluir_locacao(locacao_id):
    with get_connection() as conn:
        loc = conn.execute("SELECT * FROM locacoes WHERE id = ?", (locacao_id,)).fetchone()
        if not loc:
            raise ValueError("Locação não encontrada")
        conn.execute("DELETE FROM locacoes WHERE id = ?", (locacao_id,))
    # Se ainda estava ativa, devolve o jogo ao estoque
    if loc["status"] == "ativa":
        est.movimentar(loc["jogo_id"], "entrada", 1, "exclusão", f"Locação #{locacao_id} excluída")


# ── Cupons ────────────────────────────────────────────────────────────────────

def criar_cupom(dados):
    codigo = dados["codigo"].strip().upper()
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO cupons (codigo, tipo, valor, descricao, usos_maximos, ativo, data_criacao)
            VALUES (?, ?, ?, ?, ?, 1, ?)""",
            (codigo, dados["tipo"], dados["valor"],
             dados.get("descricao"), dados.get("usos_maximos") or None, _agora()))


def listar_cupons():
    with get_connection() as conn:
        return conn.execute("SELECT * FROM cupons ORDER BY data_criacao DESC").fetchall()


def validar_cupom(codigo):
    with get_connection() as conn:
        c = conn.execute("SELECT * FROM cupons WHERE codigo=? AND ativo=1", (codigo.upper(),)).fetchone()
        if not c:
            return None, "Cupom inválido ou inativo"
        if c["usos_maximos"] and c["usos_realizados"] >= c["usos_maximos"]:
            return None, "Cupom esgotado"
        return dict(c), None


def registrar_uso_cupom(cupom_id, tipo_operacao, referencia_id, valor_desconto, cliente_nome, jogo_nome):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO cupom_usos (cupom_id, tipo_operacao, referencia_id, valor_desconto, cliente_nome, jogo_nome, data_uso)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (cupom_id, tipo_operacao, referencia_id, valor_desconto, cliente_nome, jogo_nome, _agora()))
        conn.execute("UPDATE cupons SET usos_realizados = usos_realizados + 1 WHERE id=?", (cupom_id,))


def listar_usos_cupom(cupom_id=None):
    with get_connection() as conn:
        sql = """
            SELECT u.*, c.codigo, c.tipo, c.valor AS cupom_valor
            FROM cupom_usos u JOIN cupons c ON c.id = u.cupom_id
        """
        params = []
        if cupom_id:
            sql += " WHERE u.cupom_id = ?"
            params.append(cupom_id)
        sql += " ORDER BY u.data_uso DESC"
        return conn.execute(sql, params).fetchall()


def desativar_cupom(cupom_id):
    with get_connection() as conn:
        conn.execute("UPDATE cupons SET ativo=0 WHERE id=?", (cupom_id,))
