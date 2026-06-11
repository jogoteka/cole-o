from datetime import datetime
from database import get_connection


def _agora():
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def listar_jogos():
    with get_connection() as conn:
        return conn.execute("SELECT * FROM jogos ORDER BY nome").fetchall()


def buscar_jogo(jogo_id):
    with get_connection() as conn:
        return conn.execute("SELECT * FROM jogos WHERE id = ?", (jogo_id,)).fetchone()


def adicionar_jogo(dados):
    # Normaliza cidades: aceita lista ou string CSV
    cidades = dados.get("cidades")
    if isinstance(cidades, list):
        cidades = ",".join(cidades) or None
    elif isinstance(cidades, str):
        cidades = cidades.strip() or None

    sql = """
        INSERT INTO jogos
            (nome, editora, categoria, min_jogadores, max_jogadores,
             tempo_jogo, quantidade, quantidade_minima, preco_venda, imagem, video_url,
             loc1_dias, loc1_valor, loc2_dias, loc2_valor, loc3_dias, loc3_valor, multa_dia,
             faixa_etaria, resumo, destaque, em_destaque, cidades, data_cadastro)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    with get_connection() as conn:
        cur = conn.execute(sql, (
            dados["nome"], dados.get("editora"), dados.get("categoria"),
            dados.get("min_jogadores"), dados.get("max_jogadores"),
            dados.get("tempo_jogo"), 0,
            dados.get("quantidade_minima", 1),
            dados.get("preco_venda"),
            dados.get("imagem"),
            dados.get("video_url"),
            dados.get("loc1_dias"), dados.get("loc1_valor"),
            dados.get("loc2_dias"), dados.get("loc2_valor"),
            dados.get("loc3_dias"), dados.get("loc3_valor"),
            dados.get("multa_dia"),
            dados.get("faixa_etaria"),
            dados.get("resumo"),
            dados.get("destaque"),
            1 if dados.get("em_destaque") else 0,
            cidades,
            _agora()
        ))
        jogo_id = cur.lastrowid

    if dados.get("quantidade", 0) > 0:
        movimentar(jogo_id, "entrada", dados["quantidade"], "cadastro inicial", "")

    return jogo_id


def editar_jogo(jogo_id, dados):
    # Normaliza cidades: aceita lista ou string CSV
    if "cidades" in dados:
        cidades = dados["cidades"]
        if isinstance(cidades, list):
            dados = {**dados, "cidades": ",".join(cidades) or None}
        elif isinstance(cidades, str):
            dados = {**dados, "cidades": cidades.strip() or None}

    campos = ["nome", "editora", "categoria", "min_jogadores",
              "max_jogadores", "tempo_jogo", "quantidade_minima", "preco_venda", "imagem", "video_url",
              "loc1_dias", "loc1_valor", "loc2_dias", "loc2_valor", "loc3_dias", "loc3_valor", "multa_dia",
              "faixa_etaria", "resumo", "destaque", "em_destaque", "cidades"]
    sets = ", ".join(f"{c} = ?" for c in campos)
    valores = [dados.get(c) for c in campos] + [jogo_id]
    with get_connection() as conn:
        conn.execute(f"UPDATE jogos SET {sets} WHERE id = ?", valores)


def remover_jogo(jogo_id):
    with get_connection() as conn:
        conn.execute("DELETE FROM compras WHERE jogo_id = ?", (jogo_id,))
        conn.execute("DELETE FROM movimentacoes WHERE jogo_id = ?", (jogo_id,))
        conn.execute("DELETE FROM jogos WHERE id = ?", (jogo_id,))


def movimentar(jogo_id, tipo, quantidade, motivo, observacao):
    if tipo not in ("entrada", "saida"):
        raise ValueError("tipo deve ser 'entrada' ou 'saida'")

    with get_connection() as conn:
        jogo = conn.execute(
            "SELECT quantidade FROM jogos WHERE id = ?", (jogo_id,)
        ).fetchone()
        if jogo is None:
            raise ValueError("Jogo não encontrado")

        atual = jogo["quantidade"]
        if tipo == "saida" and quantidade > atual:
            raise ValueError(f"Estoque insuficiente. Disponível: {atual}")

        delta = quantidade if tipo == "entrada" else -quantidade
        conn.execute(
            "UPDATE jogos SET quantidade = quantidade + ? WHERE id = ?",
            (delta, jogo_id)
        )
        conn.execute(
            """INSERT INTO movimentacoes (jogo_id, tipo, quantidade, motivo, observacao, data)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (jogo_id, tipo, quantidade, motivo, observacao, _agora())
        )


def registrar_compra(jogo_id, dados):
    """Registra entrada de estoque + histórico financeiro da compra."""
    quantidade = dados["quantidade"]
    movimentar(jogo_id, "entrada", quantidade, "compra", dados.get("observacao", ""))

    with get_connection() as conn:
        conn.execute("""
            INSERT INTO compras
                (jogo_id, fornecedor, data_compra, quantidade, valor_pago,
                 forma_pagamento, parcelas, pedido_compra, numero_nf, arquivo_nf, observacao)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            jogo_id,
            dados.get("fornecedor"),
            dados.get("data_compra", _agora()[:10]),
            quantidade,
            dados.get("valor_pago"),
            dados.get("forma_pagamento"),
            dados.get("parcelas", 1),
            dados.get("pedido_compra"),
            dados.get("numero_nf"),
            dados.get("arquivo_nf"),
            dados.get("observacao"),
        ))


def historico_compras(jogo_id=None):
    with get_connection() as conn:
        sql = """
            SELECT c.*, j.nome AS jogo_nome
            FROM compras c
            JOIN jogos j ON j.id = c.jogo_id
        """
        params = []
        if jogo_id:
            sql += " WHERE c.jogo_id = ?"
            params.append(jogo_id)
        sql += " ORDER BY c.data_compra DESC"
        return conn.execute(sql, params).fetchall()


def jogos_em_alerta():
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM jogos WHERE quantidade <= quantidade_minima ORDER BY nome"
        ).fetchall()
