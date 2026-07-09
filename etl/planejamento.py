"""
planejamento.py — Planejamento de Produção Anual (Visão Geral estratégica)

Agrega BOTTOM-UP a política order-up-to por SKU de etl/demanda.py — a mesma
base usada pela sugestão tática de etl/fabrica.py. As duas visões (anual e
tática) derivam sempre dos mesmos números.

Uso:
    from etl.planejamento import calcular_sazonalidade, simular_rodadas
    saz = calcular_sazonalidade(dados, config)
    sim = simular_rodadas(dados, config, saz)
"""

import pandas as pd

from etl import demanda as _demanda


NOMES_MES = _demanda.NOMES_MES

# Mantido como alias — quem já importa calcular_sazonalidade daqui continua
# funcionando; a implementação real vive em etl/demanda.py.
calcular_sazonalidade = _demanda.calcular_sazonalidade_empresa


def simular_rodadas(dados: dict, config: dict, sazonalidade: pd.DataFrame,
                    rodadas_override: list = None, buffer_override: float = None,
                    pct_por_rodada: dict = None, ativo_crescimento: bool = None) -> dict:
    """
    Simula as rodadas de produção agregando a política order-up-to por SKU.

    rodadas_override: calendário alternativo (meses de disparo) p/ cenários.
    ativo_crescimento: liga/desliga o fator de crescimento (toggle).
    buffer_override/pct_por_rodada: aceitos por compatibilidade de assinatura;
        não usados no modelo order-up-to (a margem vem do nível de serviço, e
        o tamanho de cada rodada emerge da projeção forward, não de um %).

    Retorna dict com: demanda_mensal, rodadas, totais, estoque_projetado.
    """
    produtos = dados["produtos"]

    # Demanda mensal por SKU + simulação order-up-to (bottom-up)
    dem = _demanda.calcular_demanda_mensal_por_sku(dados, config, ativo_crescimento)
    sim = _demanda.simular_politica_reabastecimento(
        dados, config, dem=dem, rodadas_meses=rodadas_override
    )

    df_demanda = _demanda.agregar_demanda_mensal_total(dem, sazonalidade)
    demanda_dict = df_demanda.set_index("Mes")["Demanda"].to_dict()
    demanda_anual = int(df_demanda["Demanda"].sum())

    custo_map = produtos.set_index("codigo")["preco_custo"].to_dict()
    colegio_map = dem.groupby("SKU")["Colegio"].first().to_dict()

    # ================================================================
    # Rodadas (agregação bottom-up da política)
    # ================================================================
    resultado_rodadas = []
    if len(sim) > 0:
        sim = sim.copy()
        sim["Custo"] = sim["SKU"].map(custo_map).fillna(0.0)
        sim["Colegio"] = sim["SKU"].map(colegio_map).fillna("")
        sim["Invest"] = sim["Pedido"] * sim["Custo"]

        rodadas_meta = (
            sim[["rodada", "mes_disparo", "ano_disparo", "data_disparo", "data_chegada"]]
            .drop_duplicates().sort_values("data_chegada")
        )

        for _, meta in rodadas_meta.iterrows():
            s = sim[(sim["mes_disparo"] == meta["mes_disparo"]) &
                    (sim["ano_disparo"] == meta["ano_disparo"])]
            producao = int(s["Pedido"].sum())
            investimento = float(s["Invest"].sum())
            demanda_periodo = int(round(s["DemandaPeriodo"].sum()))
            seguranca = int(round(s["EstoqueSeguranca"].sum()))
            alvo = int(round(s["EstoqueAlvo"].sum()))            # demanda intervalo + segurança
            sem_estoque = int(s["PedidoSemEstoque"].sum())       # pedido se ignorasse estoque
            abate_estoque = int(sem_estoque - producao)          # quanto o estoque existente abateu
            custo_med = (investimento / producao) if producao > 0 else (
                float(produtos["preco_custo"].mean()) if len(produtos) > 0 else 0.0
            )
            chegada = pd.Timestamp(meta["data_chegada"])
            pct_anual = (producao / demanda_anual * 100) if demanda_anual > 0 else 0

            detalhe_col = (
                s[s["Pedido"] > 0].groupby("Colegio")["Pedido"].sum()
                .round().astype(int).reset_index()
                .rename(columns={"Pedido": "Producao"})
                .sort_values("Producao", ascending=False).reset_index(drop=True)
            )

            resultado_rodadas.append({
                "rodada": int(meta["rodada"]),
                "mes_disparo": int(meta["mes_disparo"]),
                "nome_disparo": NOMES_MES[int(meta["mes_disparo"]) - 1],
                "mes_chegada": chegada.month,
                "nome_chegada": NOMES_MES[chegada.month - 1],
                "ano_chegada": chegada.year,
                "demanda_periodo": demanda_periodo,
                "seguranca": seguranca,
                "alvo": alvo,
                "sem_estoque": sem_estoque,
                "abate_estoque": abate_estoque,
                "producao": producao,
                "custo_medio_ponderado": round(custo_med, 2),
                "investimento": round(investimento, 2),
                "pct_anual": round(pct_anual, 1),
                "detalhe_por_colegio": detalhe_col,
                "nomes_cobertos": f"chega {NOMES_MES[chegada.month - 1]}/{chegada.year}",
                "n_meses": "—",
            })

    producao_total = sum(r["producao"] for r in resultado_rodadas)
    investimento_total = sum(r["investimento"] for r in resultado_rodadas)
    custo_medio_anual = (investimento_total / producao_total) if producao_total > 0 else (
        float(produtos["preco_custo"].mean()) if len(produtos) > 0 else 0.0
    )

    # ================================================================
    # Curva de estoque projetada — 12 meses A PARTIR DE HOJE (não Jan–Dez,
    # porque estamos no meio do ano). Começa no estoque líquido atual da rede.
    # ================================================================
    est_rede = dados["estoque"].groupby("ID_produto")["saldoFisico"].sum()
    estoque_inicial = float(est_rede.sum())

    # Chegadas por (ano, mês) reais
    entradas_por_data = {}
    for r in resultado_rodadas:
        entradas_por_data[(r["ano_chegada"], r["mes_chegada"])] = \
            entradas_por_data.get((r["ano_chegada"], r["mes_chegada"]), 0) + r["producao"]

    hoje = pd.Timestamp.now().normalize().replace(day=1)
    estoque_proj = []
    estoque_atual = estoque_inicial
    for i in range(12):
        dt = hoje + pd.DateOffset(months=i)
        m = dt.month
        entrada = entradas_por_data.get((dt.year, m), 0)
        demanda_mes = demanda_dict.get(m, 0)
        estoque_atual += entrada - demanda_mes
        estoque_proj.append({
            "Mes": m,
            "NomeMes": NOMES_MES[m - 1],
            "Entrada": round(entrada),
            "Demanda": round(demanda_mes),
            "EstoqueFinal": round(estoque_atual),
        })

    return {
        "demanda_mensal": df_demanda,
        "rodadas": resultado_rodadas,
        "totais": {
            "producao_total": int(producao_total),
            "investimento_total": round(float(investimento_total), 2),
            "demanda_anual": demanda_anual,
            "custo_medio": round(float(custo_medio_anual), 2),
            "estoque_inicial": round(estoque_inicial),
        },
        "estoque_projetado": pd.DataFrame(estoque_proj),
    }
