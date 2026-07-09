"""
fabrica.py — Sugestão tática de produção por SKU (PCP)

Emite o pedido de uma rodada de abastecimento por SKU, a partir da política
order-up-to com projeção forward de etl/demanda.py (o mesmo motor usado pela
Visão Geral / planejamento anual). A sugestão de cada SKU é o Pedido daquela
rodada = nível-alvo S − estoque projetado na chegada.

Uso:
    from etl.fabrica import processar_fabrica
    df = processar_fabrica(dados, config)                       # rodada iminente
    df = processar_fabrica(dados, config, mes_disparo=10, ano_disparo=2026)
"""

import pandas as pd

from etl import demanda


def processar_fabrica(dados: dict, config: dict, pipeline: pd.DataFrame = None,
                      mes_disparo: int = None, ano_disparo: int = None,
                      ativo_crescimento: bool = None) -> pd.DataFrame:
    """
    Pedido de produção por SKU para uma rodada de abastecimento.

    Args:
        mes_disparo/ano_disparo: rodada escolhida (None = rodada iminente).
        ativo_crescimento: liga/desliga o fator de crescimento (toggle). None
                           = lê config["demanda"]["aplicar_crescimento_fabrica"].
        pipeline: mantido por compatibilidade; não usado (sempre 0).

    Retorna DataFrame por SKU com colunas:
        SKU, Produto, Categoria, SuperCategoria, Grupo, Colegio, Tamanho,
        VendasHist, MediaMensal, EstoqueRede, Backlog, Pipeline,
        DemandaProjetada, EstoqueSeguranca, EstoqueMeta, EstoqueProjetado,
        NecessidadeBruta, SugestaoProducao, NivelServico, CustoUnit,
        InvestimentoFabril, JanelaLabel
    """
    produtos = dados["produtos"]
    detalhes = dados["detalhes"]
    estoque = dados["estoque"]

    # Demanda + simulação order-up-to (uma vez cada)
    dem = demanda.calcular_demanda_mensal_por_sku(dados, config, ativo_crescimento)
    sim = demanda.simular_politica_reabastecimento(dados, config, dem=dem)

    # Rodada selecionada (default = iminente = primeira da sequência)
    if len(sim) == 0:
        rodadas_disp = []
    else:
        rodadas_disp = (
            sim[["rodada", "mes_disparo", "ano_disparo", "data_disparo", "data_chegada",
                 "data_chegada_seguinte", "MesesIntervalo"]]
            .drop_duplicates().sort_values("data_chegada").to_dict("records")
        )

    if not rodadas_disp:
        sel = None
        janela_label = "Nenhuma rodada de produção configurada"
        sim_rod = sim.iloc[0:0]
    else:
        if mes_disparo is not None:
            match = [r for r in rodadas_disp
                     if r["mes_disparo"] == mes_disparo and r["ano_disparo"] == ano_disparo]
            sel = match[0] if match else rodadas_disp[0]
        else:
            sel = rodadas_disp[0]
        sim_rod = sim[(sim["mes_disparo"] == sel["mes_disparo"]) &
                      (sim["ano_disparo"] == sel["ano_disparo"])]
        chegada = pd.Timestamp(sel["data_chegada"])
        fim_intervalo = pd.Timestamp(sel["data_chegada_seguinte"])
        janela_label = (
            f"Rodada {int(sel['rodada'])} — disparo "
            f"{demanda.NOMES_MES[int(sel['mes_disparo']) - 1]}/{int(sel['ano_disparo'])} "
            f"→ chega {demanda.NOMES_MES[chegada.month - 1]}/{chegada.year} · "
            f"cobre {demanda.NOMES_MES[chegada.month - 1]}/{chegada.year}"
            f"→{demanda.NOMES_MES[fim_intervalo.month - 1]}/{fim_intervalo.year} "
            f"({float(sel['MesesIntervalo']):.1f} meses)"
        )

    sim_map = sim_rod.set_index("SKU").to_dict("index")

    # Agregados por SKU vindos da demanda (exibição)
    dem_g = dem.groupby("SKU")
    demanda_anual = dem_g["DemandaMensalProjetada"].sum().to_dict()

    # Vendas históricas cruas da última alta (sem crescimento) p/ exibição
    temporada = demanda._ultima_temporada_alta(config)
    itens = dados["itens"]
    if temporada:
        meses_alta = temporada
        mask = pd.Series(False, index=itens.index)
        for m, ts in meses_alta.items():
            mask = mask | ((itens["Data"].dt.year == ts.year) & (itens["Data"].dt.month == ts.month))
        vendas_alta_crua = itens[mask].groupby("ID_produto")["Quantidade"].sum().to_dict()
    else:
        vendas_alta_crua = {}

    est_rede = estoque.groupby("ID_produto")["saldoFisico"].sum().to_dict()

    # Backlog por produto — informativo (já embutido no EstoqueProjetado da projeção forward)
    sit_backlog = config.get("fabrica", {}).get("situacoes_backlog", [])
    map_ped_sit = dados["pedidos"].set_index("ID")["id_situacao"].to_dict()
    _it = itens.copy()
    _it["_sit"] = _it["ID_pedido"].map(map_ped_sit)
    backlog_map = _it[_it["_sit"].isin(sit_backlog)].groupby("ID_produto")["Quantidade"].sum().to_dict()

    det_map = detalhes.set_index("ID_produto")[
        ["categoria", "Super_categoria", "Grupo", "Tamanho", "Marca_sku"]
    ].to_dict("index")

    ns_alta = config.get("demanda", {}).get("nivel_servico_alta", 99)
    ns_baixa = config.get("demanda", {}).get("nivel_servico_baixa", 92)

    resultados = []
    for _, prod in produtos.iterrows():
        id_prod = str(prod["ID"]).strip()
        sku = prod["codigo"]
        preco_custo = prod["preco_custo"]
        det = det_map.get(id_prod, {})

        info = sim_map.get(sku)
        if info is None:
            demanda_periodo = dem_periodo_alta = dem_periodo_baixa = 0.0
            seguranca = estoque_alvo = estoque_projetado = 0.0
            pedido = 0
            contem_alta = False
        else:
            demanda_periodo = info["DemandaPeriodo"]
            dem_periodo_alta = info.get("DemandaPeriodoAlta", 0.0)
            dem_periodo_baixa = info.get("DemandaPeriodoBaixa", 0.0)
            seguranca = info["EstoqueSeguranca"]
            estoque_alvo = info["EstoqueAlvo"]
            estoque_projetado = info["EstoqueProjetado"]
            pedido = int(info["Pedido"])
            contem_alta = bool(info["contem_alta"])

        resultados.append({
            "SKU": sku,
            "Produto": prod["Descricao"],
            "Categoria": det.get("categoria", ""),
            "SuperCategoria": det.get("Super_categoria", ""),
            "Grupo": det.get("Grupo", ""),
            "Colegio": det.get("Marca_sku", ""),
            "Tamanho": det.get("Tamanho", ""),
            "VendasHist": int(vendas_alta_crua.get(id_prod, 0)),   # última alta (cru)
            "MediaMensal": round(demanda_anual.get(sku, 0) / 12, 2),
            "EstoqueRede": est_rede.get(id_prod, 0),
            "Backlog": int(backlog_map.get(id_prod, 0)),   # informativo (já embutido no EstoqueProjetado)
            "Pipeline": 0,
            "DemandaProjetada": round(demanda_periodo, 2),         # demanda do período da rodada
            "DemandaPeriodoAlta": round(dem_periodo_alta, 2),      # parcela nos meses de pico
            "DemandaPeriodoBaixa": round(dem_periodo_baixa, 2),    # parcela nos meses de baixa
            "EstoqueSeguranca": round(seguranca, 1),
            "EstoqueMeta": round(estoque_alvo),                    # Estoque-Alvo = DemandaPeriodo + EstoqueSeguranca
            "EstoqueProjetado": round(estoque_projetado, 1),       # estoque projetado na chegada
            "NecessidadeBruta": round(max(estoque_alvo - estoque_projetado, 0), 2),
            "SugestaoProducao": pedido,
            "NivelServico": ns_alta if contem_alta else ns_baixa,
            "CustoUnit": preco_custo,
            "InvestimentoFabril": pedido * preco_custo,
            "JanelaLabel": janela_label,
        })

    df = pd.DataFrame(resultados)
    df = df.sort_values("SugestaoProducao", ascending=False).reset_index(drop=True)
    return df
