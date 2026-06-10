import csv
import io
import re
from datetime import datetime, date, timedelta
from database import get_connection


def _agora():
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def _parse_data(s):
    s = s.strip()
    # Remove parte de hora se vier junto (ex: "13/05/2026 14:30" ou "2026-05-13 14:30:00")
    s_data = s.split(" ")[0].split("T")[0]
    for fmt in ["%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y", "%Y/%m/%d"]:
        try:
            return datetime.strptime(s_data, fmt).date()
        except ValueError:
            pass
    return None


def _parse_valor(s):
    if not s:
        return None
    s = str(s).strip().replace("R$", "").replace(" ", "")
    negativo = s.startswith("(") and s.endswith(")")
    if negativo:
        s = s[1:-1]
    negativo = negativo or s.startswith("-")
    s = s.lstrip("-")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        v = float(s)
        return -v if negativo else v
    except (ValueError, TypeError):
        return None


def parse_csv(conteudo_bytes):
    texto = None
    for enc in ["utf-8-sig", "latin-1", "windows-1252"]:
        try:
            texto = conteudo_bytes.decode(enc)
            break
        except (UnicodeDecodeError, AttributeError):
            pass
    if not texto:
        raise ValueError("Não foi possível decodificar o arquivo")

    sep = ";" if texto.count(";") > texto.count(",") else ","
    reader = csv.reader(io.StringIO(texto), delimiter=sep)
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        raise ValueError("Arquivo vazio")

    # Detecta cabeçalho: procura a primeira linha de dados com data (até 15 linhas)
    # Mercado Pago tem várias linhas de info antes do cabeçalho real
    header_idx = 0
    for i, row in enumerate(rows[:15]):
        if any(_parse_data(c) for c in row):
            header_idx = max(0, i - 1)
            break

    header = [c.strip().lower() for c in rows[header_idx]]
    data_rows = rows[header_idx + 1:]

    col_count = max((len(r) for r in data_rows[:20]), default=0)
    if col_count == 0:
        raise ValueError("Nenhum dado encontrado")

    # Pontua colunas por tipo
    date_scores = [0] * col_count
    num_scores  = [0] * col_count
    pos_scores  = [0] * col_count  # para detectar coluna crédito (Inter-style)
    max_vals    = [0] * col_count  # valor máximo por coluna (para filtrar IDs)

    for row in data_rows[:30]:
        for i, cell in enumerate(row[:col_count]):
            if _parse_data(cell.strip()):
                date_scores[i] += 1
            v = _parse_valor(cell)
            if v is not None:
                num_scores[i] += 1
                if v > 0:
                    pos_scores[i] += 1
                if abs(v) > max_vals[i]:
                    max_vals[i] = abs(v)

    date_col = date_scores.index(max(date_scores)) if max(date_scores) > 0 else 0

    # Detecta separação débito/crédito (Inter, Bradesco PJ, etc.)
    # Exclui colunas com valores absurdamente altos (IDs, CPFs, timestamps numéricos)
    # Valores monetários de uma loja pequena dificilmente passam de R$ 100.000
    num_cols = [i for i, s in enumerate(num_scores)
                if s >= 2 and i != date_col and max_vals[i] <= 100_000]

    if len(num_cols) >= 2:
        # Coluna com mais positivos = crédito
        cred_col  = max(num_cols, key=lambda i: pos_scores[i])
        debit_col = [c for c in num_cols if c != cred_col][0]
        split_mode = True
    else:
        cred_col  = max(num_cols, key=lambda i: num_scores[i]) if num_cols else -1
        debit_col = None
        split_mode = False

    if cred_col == -1:
        raise ValueError("Não foi possível identificar coluna de valores")

    # Coluna de descrição: qualquer coluna de texto que não seja data nem valor
    valor_cols = set(num_cols)
    valor_cols.add(date_col)
    desc_col = next((i for i in range(col_count) if i not in valor_cols), None)

    lancamentos = []
    for row in data_rows:
        if len(row) <= max(date_col, cred_col):
            continue
        dt = _parse_data(row[date_col])
        if dt is None:
            continue

        if split_mode:
            v_cred  = _parse_valor(row[cred_col])  if cred_col  < len(row) else None
            v_debit = _parse_valor(row[debit_col]) if debit_col < len(row) else None
            valor = (v_cred or 0) - (v_debit or 0)
        else:
            valor = _parse_valor(row[cred_col])

        if valor is None:
            continue

        desc = row[desc_col].strip() if desc_col is not None and desc_col < len(row) else ""
        lancamentos.append({"data": dt.isoformat(), "descricao": desc, "valor": valor})

    if not lancamentos:
        raise ValueError("Nenhum lançamento válido encontrado no arquivo")
    return lancamentos


def salvar_extrato(nome_arquivo, lancamentos):
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO extratos (nome_arquivo, data_upload, total_lancamentos) VALUES (?, ?, ?)",
            (nome_arquivo, _agora(), len(lancamentos))
        )
        extrato_id = cur.lastrowid
        for l in lancamentos:
            conn.execute(
                "INSERT INTO extrato_lancamentos (extrato_id, data, descricao, valor) VALUES (?, ?, ?, ?)",
                (extrato_id, l["data"], l["descricao"], l["valor"])
            )
    return extrato_id


def conciliar(extrato_id, tolerancia_dias=3):
    with get_connection() as conn:
        lancamentos = conn.execute(
            "SELECT * FROM extrato_lancamentos WHERE extrato_id=? AND conciliado=0 AND valor>0",
            (extrato_id,)
        ).fetchall()

        conciliados = 0
        for l in lancamentos:
            dt = date.fromisoformat(l["data"])
            d_min = (dt - timedelta(days=tolerancia_dias)).isoformat()
            d_max = (dt + timedelta(days=tolerancia_dias)).isoformat()

            venda = conn.execute("""
                SELECT id FROM vendas
                WHERE ABS(valor_final - ?) < 0.02
                AND data_venda BETWEEN ? AND ?
                AND (status_pagamento IS NULL OR status_pagamento != 'recebido')
                LIMIT 1""", (l["valor"], d_min, d_max)).fetchone()
            if venda:
                conn.execute("UPDATE vendas SET status_pagamento='recebido' WHERE id=?", (venda["id"],))
                conn.execute("UPDATE extrato_lancamentos SET conciliado=1, venda_id=? WHERE id=?",
                             (venda["id"], l["id"]))
                conciliados += 1
                continue

            loc = conn.execute("""
                SELECT id FROM locacoes
                WHERE ABS(valor_locacao - ?) < 0.02
                AND data_saida BETWEEN ? AND ?
                AND (status_pagamento IS NULL OR status_pagamento != 'recebido')
                LIMIT 1""", (l["valor"], d_min, d_max)).fetchone()
            if loc:
                conn.execute("UPDATE locacoes SET status_pagamento='recebido' WHERE id=?", (loc["id"],))
                conn.execute("UPDATE extrato_lancamentos SET conciliado=1, locacao_id=? WHERE id=?",
                             (loc["id"], l["id"]))
                conciliados += 1

    creditos = sum(1 for l in lancamentos if l["valor"] > 0)
    return {"total": creditos, "conciliados": conciliados, "sem_match": creditos - conciliados}


def marcar_recebido(tipo, ref_id, recebido=True):
    status = "recebido" if recebido else "pendente"
    with get_connection() as conn:
        if tipo == "venda":
            conn.execute("UPDATE vendas SET status_pagamento=? WHERE id=?", (status, ref_id))
        else:
            conn.execute("UPDATE locacoes SET status_pagamento=? WHERE id=?", (status, ref_id))


def listar_extratos():
    with get_connection() as conn:
        return conn.execute("SELECT * FROM extratos ORDER BY data_upload DESC").fetchall()


def listar_lancamentos(extrato_id):
    with get_connection() as conn:
        return conn.execute("""
            SELECT l.*,
                   v.valor_final AS venda_valor, jv.nome AS venda_jogo,
                   lo.valor_locacao AS loc_valor, jl.nome AS loc_jogo
            FROM extrato_lancamentos l
            LEFT JOIN vendas v  ON v.id  = l.venda_id
            LEFT JOIN jogos  jv ON jv.id = v.jogo_id
            LEFT JOIN locacoes lo ON lo.id = l.locacao_id
            LEFT JOIN jogos  jl ON jl.id = lo.jogo_id
            WHERE l.extrato_id = ?
            ORDER BY l.data DESC""", (extrato_id,)).fetchall()
