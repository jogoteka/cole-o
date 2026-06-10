from database import get_connection
from estoque import listar_jogos, jogos_em_alerta


def _linha(larguras, separador="-"):
    return "+" + "+".join(separador * (w + 2) for w in larguras) + "+"


def _row(valores, larguras):
    celulas = " | ".join(str(v).ljust(w) for v, w in zip(valores, larguras))
    return "| " + celulas + " |"


def relatorio_estoque():
    jogos = listar_jogos()
    if not jogos:
        print("\nNenhum jogo cadastrado.")
        return

    cabecalho = ["ID", "Nome", "Editora", "Categoria", "Jogadores", "Tempo(min)", "Qtd", "Mín"]
    larguras = [4, 30, 20, 15, 10, 10, 6, 6]

    print()
    print(_linha(larguras, "="))
    print(_row(cabecalho, larguras))
    print(_linha(larguras, "="))

    for j in jogos:
        jogadores = f"{j['min_jogadores'] or '?'}-{j['max_jogadores'] or '?'}"
        alerta = " !" if j["quantidade"] <= j["quantidade_minima"] else ""
        row = [
            j["id"],
            (j["nome"] or "")[:30],
            (j["editora"] or "")[:20],
            (j["categoria"] or "")[:15],
            jogadores,
            j["tempo_jogo"] or "?",
            str(j["quantidade"]) + alerta,
            j["quantidade_minima"],
        ]
        print(_row(row, larguras))

    print(_linha(larguras))
    print(f"  Total: {len(jogos)} jogo(s)  |  [!] = estoque no limite ou abaixo")


def historico_movimentacoes(jogo_id=None, desde=None):
    with get_connection() as conn:
        sql = """
            SELECT m.id, j.nome, m.tipo, m.quantidade, m.motivo, m.observacao, m.data
            FROM movimentacoes m
            JOIN jogos j ON j.id = m.jogo_id
            WHERE 1=1
        """
        params = []
        if jogo_id:
            sql += " AND m.jogo_id = ?"
            params.append(jogo_id)
        if desde:
            sql += " AND m.data >= ?"
            params.append(desde)
        sql += " ORDER BY m.data DESC LIMIT 100"
        rows = conn.execute(sql, params).fetchall()

    if not rows:
        print("\nNenhuma movimentação encontrada.")
        return

    cabecalho = ["ID", "Jogo", "Tipo", "Qtd", "Motivo", "Data"]
    larguras = [5, 28, 8, 5, 15, 19]

    print()
    print(_linha(larguras, "="))
    print(_row(cabecalho, larguras))
    print(_linha(larguras, "="))

    for r in rows:
        print(_row([
            r["id"],
            (r["nome"] or "")[:28],
            r["tipo"],
            r["quantidade"],
            (r["motivo"] or "")[:15],
            r["data"],
        ], larguras))

    print(_linha(larguras))
    print(f"  {len(rows)} registro(s) exibido(s)")


def resumo_geral():
    with get_connection() as conn:
        total_jogos = conn.execute("SELECT COUNT(*) FROM jogos").fetchone()[0]
        total_estoque = conn.execute("SELECT SUM(quantidade) FROM jogos").fetchone()[0] or 0
        total_mov = conn.execute("SELECT COUNT(*) FROM movimentacoes").fetchone()[0]

    alertas = jogos_em_alerta()

    print()
    print("=" * 40)
    print("  RESUMO GERAL DO ESTOQUE")
    print("=" * 40)
    print(f"  Jogos cadastrados : {total_jogos}")
    print(f"  Total em estoque  : {total_estoque} unidade(s)")
    print(f"  Movimentações     : {total_mov}")
    print(f"  Em alerta         : {len(alertas)} jogo(s)")
    print("=" * 40)

    if alertas:
        print("  Jogos em alerta:")
        for j in alertas:
            print(f"    [!] {j['nome']} — qtd: {j['quantidade']} (mín: {j['quantidade_minima']})")
        print("=" * 40)
