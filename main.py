import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from database import init_db
import estoque as est
import relatorios as rel


def limpar():
    os.system("cls" if os.name == "nt" else "clear")


def pausar():
    input("\nPressione Enter para continuar...")


def cabecalho(titulo="ESTOQUE DE JOGOS DE TABULEIRO"):
    alertas = est.jogos_em_alerta()
    print("╔" + "═" * 42 + "╗")
    print(f"║  {titulo.center(40)}  ║")
    print("╠" + "═" * 42 + "╣")
    if alertas:
        msg = f"[!] {len(alertas)} jogo(s) com estoque baixo"
        print(f"║  {msg.ljust(40)}  ║")
        print("╠" + "═" * 42 + "╣")


def pedir(prompt, obrigatorio=True, tipo=str, minimo=None):
    while True:
        val = input(prompt).strip()
        if not val:
            if not obrigatorio:
                return None
            print("  Campo obrigatório.")
            continue
        try:
            val = tipo(val)
        except (ValueError, TypeError):
            print(f"  Valor inválido. Esperado: {tipo.__name__}")
            continue
        if minimo is not None and val < minimo:
            print(f"  Valor mínimo: {minimo}")
            continue
        return val


def confirmar(msg):
    return input(f"{msg} (s/N): ").strip().lower() == "s"


# ── CADASTRO ──────────────────────────────────────────────────────────────────

def menu_cadastro():
    while True:
        limpar()
        cabecalho("CADASTRO DE JOGOS")
        print("║  1. Listar jogos                          ║")
        print("║  2. Adicionar jogo                        ║")
        print("║  3. Editar jogo                           ║")
        print("║  4. Remover jogo                          ║")
        print("║  0. Voltar                                ║")
        print("╚" + "═" * 42 + "╝")
        op = input("  Opção: ").strip()

        if op == "1":
            rel.relatorio_estoque()
            pausar()
        elif op == "2":
            _adicionar_jogo()
        elif op == "3":
            _editar_jogo()
        elif op == "4":
            _remover_jogo()
        elif op == "0":
            break


def _adicionar_jogo():
    limpar()
    print("── ADICIONAR JOGO ──")
    dados = {
        "nome": pedir("  Nome do jogo: "),
        "editora": pedir("  Editora: ", obrigatorio=False),
        "categoria": pedir("  Categoria (ex: Estratégia, Família, RPG): ", obrigatorio=False),
        "min_jogadores": pedir("  Mín. jogadores: ", obrigatorio=False, tipo=int, minimo=1),
        "max_jogadores": pedir("  Máx. jogadores: ", obrigatorio=False, tipo=int, minimo=1),
        "tempo_jogo": pedir("  Tempo médio (min): ", obrigatorio=False, tipo=int, minimo=1),
        "quantidade": pedir("  Quantidade inicial: ", obrigatorio=False, tipo=int, minimo=0) or 0,
        "quantidade_minima": pedir("  Quantidade mínima p/ alerta: ", obrigatorio=False, tipo=int, minimo=0) or 1,
    }
    jogo_id = est.adicionar_jogo(dados)
    print(f"\n  Jogo cadastrado com ID {jogo_id}.")
    pausar()


def _editar_jogo():
    limpar()
    rel.relatorio_estoque()
    jogo_id = pedir("\n  ID do jogo a editar: ", tipo=int, minimo=1)
    jogo = est.buscar_jogo(jogo_id)
    if not jogo:
        print("  Jogo não encontrado.")
        pausar()
        return

    print(f"\n  Editando: {jogo['nome']} (Enter para manter o valor atual)")

    def pedir_campo(prompt, atual, tipo=str, minimo=None):
        val = input(f"  {prompt} [{atual}]: ").strip()
        if not val:
            return atual
        try:
            val = tipo(val)
        except (ValueError, TypeError):
            return atual
        if minimo is not None and val < minimo:
            return atual
        return val

    dados = {
        "nome": pedir_campo("Nome", jogo["nome"]),
        "editora": pedir_campo("Editora", jogo["editora"] or ""),
        "categoria": pedir_campo("Categoria", jogo["categoria"] or ""),
        "min_jogadores": pedir_campo("Mín. jogadores", jogo["min_jogadores"], int, 1),
        "max_jogadores": pedir_campo("Máx. jogadores", jogo["max_jogadores"], int, 1),
        "tempo_jogo": pedir_campo("Tempo médio (min)", jogo["tempo_jogo"], int, 1),
        "quantidade_minima": pedir_campo("Qtd mínima p/ alerta", jogo["quantidade_minima"], int, 0),
    }
    est.editar_jogo(jogo_id, dados)
    print("  Jogo atualizado.")
    pausar()


def _remover_jogo():
    limpar()
    rel.relatorio_estoque()
    jogo_id = pedir("\n  ID do jogo a remover: ", tipo=int, minimo=1)
    jogo = est.buscar_jogo(jogo_id)
    if not jogo:
        print("  Jogo não encontrado.")
        pausar()
        return

    print(f"\n  Jogo: {jogo['nome']}")
    if confirmar("  Tem certeza? Todo o histórico também será removido."):
        est.remover_jogo(jogo_id)
        print("  Jogo removido.")
    else:
        print("  Cancelado.")
    pausar()


# ── MOVIMENTAÇÕES ─────────────────────────────────────────────────────────────

MOTIVOS_ENTRADA = ["compra", "devolução", "ajuste", "outro"]
MOTIVOS_SAIDA = ["venda", "empréstimo", "ajuste", "perda", "outro"]


def menu_movimentacao():
    while True:
        limpar()
        cabecalho("MOVIMENTAR ESTOQUE")
        print("║  1. Registrar entrada                     ║")
        print("║  2. Registrar saída                       ║")
        print("║  0. Voltar                                ║")
        print("╚" + "═" * 42 + "╝")
        op = input("  Opção: ").strip()

        if op == "1":
            _movimentar("entrada")
        elif op == "2":
            _movimentar("saida")
        elif op == "0":
            break


def _movimentar(tipo):
    limpar()
    titulo = "ENTRADA DE ESTOQUE" if tipo == "entrada" else "SAÍDA DE ESTOQUE"
    print(f"── {titulo} ──")
    rel.relatorio_estoque()

    jogo_id = pedir("\n  ID do jogo: ", tipo=int, minimo=1)
    jogo = est.buscar_jogo(jogo_id)
    if not jogo:
        print("  Jogo não encontrado.")
        pausar()
        return

    print(f"  Jogo: {jogo['nome']} | Estoque atual: {jogo['quantidade']}")
    quantidade = pedir("  Quantidade: ", tipo=int, minimo=1)

    motivos = MOTIVOS_ENTRADA if tipo == "entrada" else MOTIVOS_SAIDA
    print("  Motivos: " + ", ".join(f"{i+1}.{m}" for i, m in enumerate(motivos)))
    idx = pedir(f"  Escolha (1-{len(motivos)}): ", tipo=int, minimo=1)
    motivo = motivos[min(idx, len(motivos)) - 1]

    observacao = pedir("  Observação (opcional): ", obrigatorio=False) or ""

    try:
        est.movimentar(jogo_id, tipo, quantidade, motivo, observacao)
        jogo_atualizado = est.buscar_jogo(jogo_id)
        print(f"\n  Movimentação registrada. Novo estoque: {jogo_atualizado['quantidade']}")
        if jogo_atualizado["quantidade"] <= jogo_atualizado["quantidade_minima"]:
            print(f"  [!] ALERTA: estoque abaixo do mínimo ({jogo_atualizado['quantidade_minima']})!")
    except ValueError as e:
        print(f"\n  Erro: {e}")
    pausar()


# ── RELATÓRIOS ────────────────────────────────────────────────────────────────

def menu_relatorios():
    while True:
        limpar()
        cabecalho("RELATÓRIOS")
        print("║  1. Estoque atual                         ║")
        print("║  2. Histórico de movimentações            ║")
        print("║  3. Jogos em alerta de estoque            ║")
        print("║  4. Resumo geral                          ║")
        print("║  0. Voltar                                ║")
        print("╚" + "═" * 42 + "╝")
        op = input("  Opção: ").strip()

        if op == "1":
            limpar()
            rel.relatorio_estoque()
            pausar()
        elif op == "2":
            _menu_historico()
        elif op == "3":
            limpar()
            alertas = est.jogos_em_alerta()
            if alertas:
                print("\n  Jogos com estoque no limite ou abaixo:\n")
                for j in alertas:
                    print(f"    [!] {j['nome']:30} qtd: {j['quantidade']:4}  mín: {j['quantidade_minima']}")
            else:
                print("\n  Nenhum jogo em alerta.")
            pausar()
        elif op == "4":
            limpar()
            rel.resumo_geral()
            pausar()
        elif op == "0":
            break


def _menu_historico():
    limpar()
    print("── HISTÓRICO DE MOVIMENTAÇÕES ──")
    print("  1. Todas as movimentações")
    print("  2. Por jogo")
    op = input("  Opção: ").strip()

    jogo_id = None
    if op == "2":
        rel.relatorio_estoque()
        jogo_id = pedir("\n  ID do jogo: ", tipo=int, minimo=1)

    desde = pedir(
        "  Data inicial (AAAA-MM-DD, Enter para todas): ",
        obrigatorio=False
    )

    limpar()
    rel.historico_movimentacoes(jogo_id=jogo_id, desde=desde)
    pausar()


# ── PRINCIPAL ─────────────────────────────────────────────────────────────────

def main():
    init_db()

    while True:
        limpar()
        cabecalho()
        print("║  1. Cadastro de jogos                     ║")
        print("║  2. Movimentar estoque                    ║")
        print("║  3. Relatórios                            ║")
        print("║  0. Sair                                  ║")
        print("╚" + "═" * 42 + "╝")
        op = input("  Opção: ").strip()

        if op == "1":
            menu_cadastro()
        elif op == "2":
            menu_movimentacao()
        elif op == "3":
            menu_relatorios()
        elif op == "0":
            print("\n  Até logo!\n")
            break


if __name__ == "__main__":
    main()
