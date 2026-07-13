"""
verificar_pedidos_e2e.py — Verificação end-to-end do schema `app` (Pedidos de Compra).

Exercita o caminho de código REAL do dashboard (pedidos/repositorio.py →
PostgREST → Supabase) contra o banco de verdade: congelamento, idempotência
(unique 23505), edição, CAS de estados, triggers e cascade. Usa dados de
teste marcados (ano 2099, congelada_por='verify_e2e') e faz limpeza total
no final — não deixa nada no banco.

Uso (da raiz do repo, com .streamlit/secrets.toml configurado):
    python scripts/verificar_pedidos_e2e.py

Pré-requisitos: DDL de docs/sql/001_app_pedidos.sql aplicado e schema `app`
exposto na Data API (Settings → API → Exposed schemas).
"""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pedidos import estados
from pedidos.repositorio import (
    RepositorioPedidos, _conn_app, RodadaJaCongelada, PedidoNaoEditavel,
    TAB_RODADA, TAB_PEDIDO, TAB_ITEM,
)

RESULTADOS = []


def check(nome, cond, detalhe=""):
    RESULTADOS.append((nome, bool(cond), detalhe))
    print(f"{'✅' if cond else '❌'} {nome}" + (f" — {detalhe}" if detalhe else ""))


def snapshot_teste():
    return {
        "mes_disparo": 12, "ano_disparo": 2099,
        "data_disparo": "2099-12-01", "data_chegada": "2099-12-29",
        "data_chegada_seguinte": "2100-03-01", "rodada_numero": 1,
        "janela_label": "TESTE E2E — ignorar", "data_referencia": "2026-07-12",
        "congelada_por": "verify_e2e", "ativo_crescimento": False,
        "config_snapshot": {"teste": True},
        "resultado_skus": [{"SKU": "TESTE-P", "SugestaoProducao": 10}],
    }


def grupos_teste():
    item = {"sku": "TESTE-P", "id_produto_bling": "999", "produto": "Produto Teste",
            "tamanho": "P", "categoria": "Teste",
            "quantidade_sugerida": 10, "quantidade_final": 10, "custo_unit": 5.0}
    return [
        {"colegio": "TESTE_COL", "super_categoria": "TESTE_CAT",
         "titulo": "AKU-PC · TESTE_COL · TESTE_CAT · R12/2099",
         "criado_por": "verify_e2e",
         "itens": [item, {**item, "sku": "TESTE-M", "tamanho": "M",
                          "quantidade_sugerida": 4, "quantidade_final": 4}]},
        {"colegio": "TESTE_COL", "super_categoria": "TESTE_CAT2",
         "titulo": "AKU-PC · TESTE_COL · TESTE_CAT2 · R12/2099",
         "criado_por": "verify_e2e",
         "itens": [{**item, "sku": "TESTE-G", "tamanho": "G"}]},
    ]


def main():
    repo = RepositorioPedidos(_conn_app())
    rodada_id = None
    try:
        # 0. Limpeza preventiva de execuções anteriores
        for r in repo._selecionar(TAB_RODADA, {"congelada_por": "verify_e2e"}, "id"):
            repo._deletar(TAB_RODADA, {"id": r["id"]})

        # 1. Leitura do schema app (schema exposto + grants ok)
        rodadas = repo.listar_rodadas()
        check("1. Leitura do schema app (exposto na Data API)", True,
              f"{len(rodadas)} rodada(s) existente(s)")

        # 2. Congelamento completo (5 passos, commit lógico)
        res = repo.congelar_rodada(snapshot_teste(), grupos_teste())
        rodada_id = res["id"]
        check("2. Congelar rodada (CONGELANDO→ABERTA)",
              res["status"] == estados.RODADA_ABERTA
              and res["n_pedidos"] == 2 and res["n_itens"] == 3,
              f"status={res['status']}, {res['n_pedidos']} pedidos, {res['n_itens']} itens")

        # 3. Idempotência: duplicado deve levantar RodadaJaCongelada (23505)
        try:
            repo.congelar_rodada(snapshot_teste(), grupos_teste())
            check("3. Anti duplo-clique (unique parcial 23505)", False,
                  "duplicado NÃO foi bloqueado!")
        except RodadaJaCongelada:
            check("3. Anti duplo-clique (unique parcial 23505)", True,
                  "RodadaJaCongelada levantada")

        # 4. Listagem com agregados
        pedidos = repo.listar_pedidos(rodada_id)
        p1 = pedidos[pedidos["super_categoria"] == "TESTE_CAT"].iloc[0]
        check("4. Pedidos + agregados de itens",
              len(pedidos) == 2 and p1["n_itens"] == 2 and p1["qtd_final"] == 14
              and abs(p1["investimento_final"] - 70.0) < 0.01,
              f"qtd_final={p1['qtd_final']}, invest={p1['investimento_final']}")

        # 5. Edição de quantidade (sugerida intacta)
        pid = p1["id"]
        itens = repo.listar_itens(pid)
        item0 = itens.iloc[0]
        n = repo.atualizar_quantidades(
            pid, [{"id": item0["id"], "quantidade_final": 99}], "verify_e2e")
        depois = repo.listar_itens(pid)
        d0 = depois[depois["id"] == item0["id"]].iloc[0]
        check("5. Editar quantidade_final (sugerida imutável)",
              n == 1 and d0["quantidade_final"] == 99
              and d0["quantidade_sugerida"] == item0["quantidade_sugerida"],
              f"final={d0['quantidade_final']}, sugerida={d0['quantidade_sugerida']}")

        # 6. Transição CAS: RASCUNHO→PRONTO ok; repetição (stale) → False
        ok1 = repo.transicionar_pedido(pid, estados.RASCUNHO, estados.PRONTO, "verify_e2e")
        ok2 = repo.transicionar_pedido(pid, estados.RASCUNHO, estados.PRONTO, "verify_e2e")
        check("6. CAS de estado (corrida perdida → False)",
              ok1 is True and ok2 is False, f"1ª={ok1}, 2ª={ok2}")

        # 7a. Guarda app-level: editar pedido PRONTO → PedidoNaoEditavel
        try:
            repo.atualizar_quantidades(
                pid, [{"id": item0["id"], "quantidade_final": 1}], "verify_e2e")
            check("7a. Guarda app-level (PRONTO não editável)", False, "edição passou!")
        except PedidoNaoEditavel:
            check("7a. Guarda app-level (PRONTO não editável)", True)

        # 7b. TRIGGER no banco: update direto (bypass da guarda) deve falhar
        try:
            repo._atualizar(TAB_ITEM, {"id": item0["id"]}, {"quantidade_final": 1})
            check("7b. Trigger no banco bloqueia item fora de RASCUNHO", False,
                  "update direto passou!")
        except Exception as exc:
            check("7b. Trigger no banco bloqueia item fora de RASCUNHO",
                  "RASCUNHO" in str(exc), f"{str(exc)[:80]}")

        # 7c. TRIGGER: quantidade_sugerida imutável mesmo em RASCUNHO
        repo.transicionar_pedido(pid, estados.PRONTO, estados.RASCUNHO, "verify_e2e")
        try:
            repo._atualizar(TAB_ITEM, {"id": item0["id"]}, {"quantidade_sugerida": 1})
            check("7c. Trigger: quantidade_sugerida imutável", False, "update passou!")
        except Exception as exc:
            check("7c. Trigger: quantidade_sugerida imutável",
                  "imutável" in str(exc) or "imutavel" in str(exc), f"{str(exc)[:80]}")

        # 8. Cancelar rodada (todos RASCUNHO) → CANCELADA + pedidos CANCELADO
        repo.cancelar_rodada(rodada_id, "verify_e2e")
        rod = repo._selecionar(TAB_RODADA, {"id": rodada_id}, "id,status")[0]
        peds = repo._selecionar(TAB_PEDIDO, {"rodada_id": rodada_id}, "id,status")
        check("8. Cancelar rodada + pedidos",
              rod["status"] == estados.RODADA_CANCELADA
              and all(p["status"] == estados.CANCELADO for p in peds))

        # 9. Cancelada libera novo congelamento
        res2 = repo.congelar_rodada(snapshot_teste(), grupos_teste())
        check("9. Rodada cancelada libera novo congelamento",
              res2["status"] == estados.RODADA_ABERTA)
        repo._deletar(TAB_RODADA, {"id": res2["id"]})

    except Exception:
        traceback.print_exc()
        check("EXECUÇÃO COMPLETA", False, "exceção não tratada acima")
    finally:
        # 10. Limpeza total (delete cascade — também valida o trigger no CASCADE)
        try:
            limpo = RepositorioPedidos(_conn_app())
            for r in limpo._selecionar(TAB_RODADA, {"congelada_por": "verify_e2e"}, "id"):
                limpo._deletar(TAB_RODADA, {"id": r["id"]})
            sobras = limpo._selecionar(TAB_RODADA, {"congelada_por": "verify_e2e"}, "id")
            check("10. Limpeza total (cascade rodada→pedidos→itens)", len(sobras) == 0)
        except Exception as exc:
            check("10. Limpeza total", False, str(exc)[:120])

    falhas = [r for r in RESULTADOS if not r[1]]
    print(f"\n{'=' * 60}\n{len(RESULTADOS) - len(falhas)}/{len(RESULTADOS)} checks OK")
    sys.exit(1 if falhas else 0)


if __name__ == "__main__":
    main()
