"""
daily.py — ETL Comercial / Acompanhamento de Metas

Gera tabela detalhada de vendas e KPIs de meta com Run Rate.
Equivale ao Daily.gs V3/V4 do Google Apps Script.

Uso:
    from etl.daily import processar_daily
    detalhado, metas = processar_daily(dados, config)
"""

import pandas as pd
import numpy as np
from datetime import datetime

from etl import demanda


def processar_daily(dados: dict, config: dict) -> tuple:
    """
    Processa vendas e calcula metas.

    Retorna:
        (df_detalhado, df_metas)

        df_detalhado: cada pedido enriquecido com Vendedor, Loja, Situação, Colégio
        df_metas: resumo por loja com % atingido, run rate, status
    """
    cfg_daily = config["daily"]
    cfg_dep = config["depositos"]
    situacoes_venda = cfg_daily["situacoes_venda"]
    metas_config = cfg_daily["metas"]

    pedidos = dados["pedidos"]
    itens = dados["itens"]
    vendedores = dados["vendedores"]
    lojas = dados["lojas"]
    situacoes = dados["situacoes"]
    detalhes = demanda.aplicar_alias_colegio(dados["detalhes"], config)

    # ---------------------------------------------------------------
    # 1. Mapeamentos
    # ---------------------------------------------------------------
    map_vend = vendedores.set_index("ID")["nome"].to_dict()
    map_loja = lojas.set_index("ID")["descricao"].to_dict()
    map_sit = situacoes.set_index("ID")["descricao"].to_dict()

    # Mapa: loja_id da config → nome loja
    map_id_nome = {}
    for loja_cfg in cfg_dep["lojas"]:
        map_id_nome[str(loja_cfg["loja_id"]).strip()] = loja_cfg["nome"]

    # Mapa: ID_produto → Marca_sku (Colégio)
    map_colegio = detalhes.set_index("ID_produto")["Marca_sku"].to_dict()

    # ---------------------------------------------------------------
    # 2. Enriquecer Itens com dados do Pedido e Produto
    # ---------------------------------------------------------------
    # Pega colunas necessárias do pedido
    ped = pedidos.drop_duplicates(subset=["ID"]).copy()
    ped["NomeLoja"] = ped["Loja ID"].map(map_loja).fillna("Loja " + ped["Loja ID"])
    ped["NomeVendedor"] = ped["Vendedor"].map(map_vend).fillna("Vend " + ped["Vendedor"])
    ped["Situacao"] = ped["id_situacao"].map(map_sit).fillna("Sit " + ped["id_situacao"].astype(str))
    ped["LojaConfig"] = ped["Loja ID"].map(map_id_nome).fillna("")

    ped_cols = ped[["ID", "Data", "NomeLoja", "NomeVendedor", "Cliente",
                     "Pedido", "Situacao", "id_situacao", "Loja ID", "LojaConfig", "Desconto",
                     ]].rename(columns={"ID": "ID_pedido"})

    # Enriquecer itens com colégio
    itens_c = itens.copy()
    itens_c["Colegio"] = itens_c["ID_produto"].map(map_colegio).fillna("").astype(str)
    itens_c.loc[itens_c["Colegio"].isin(["", "nan"]), "Colegio"] = "Sem Colégio"

    # ---------------------------------------------------------------
    # 3. Agregar Itens por Pedido (peças reais + valor + desconto)
    # ---------------------------------------------------------------
    # Peças reais: soma de Quantidade na aba Itens
    pecas_por_pedido = itens_c.groupby("ID_pedido")["Quantidade"].sum().to_dict()

    # Valor bruto por pedido: Σ(Quantidade × Valor Unidade)
    itens_c["_valor_bruto"] = itens_c["Quantidade"] * itens_c["Valor Unidade"]
    valor_bruto_por_pedido = itens_c.groupby("ID_pedido")["_valor_bruto"].sum().to_dict()

    # Desconto por pedido: Σ(Desconto Item)
    desconto_por_pedido = itens_c.groupby("ID_pedido")["Desconto Item"].sum().to_dict()

    # Colégio dominante = o que tem mais peças no pedido
    colegio_por_pedido = (
        itens_c.groupby("ID_pedido")
        .apply(lambda g: g.loc[g["Quantidade"].idxmax(), "Colegio"] if len(g) > 0 else "Sem Colégio",
               include_groups=False)
        .reset_index()
    )
    colegio_por_pedido.columns = ["ID_pedido", "Colegio"]

    # Merge pedido com colégio, peças, valor e desconto
    df_detalhado = ped_cols.merge(colegio_por_pedido, on="ID_pedido", how="left")
    df_detalhado["Colegio"] = df_detalhado["Colegio"].fillna("Sem Colégio")
    df_detalhado["Qtd Peças"] = df_detalhado["ID_pedido"].map(pecas_por_pedido).fillna(0).astype(int)
    df_detalhado["Valor Bruto"] = df_detalhado["ID_pedido"].map(valor_bruto_por_pedido).fillna(0)

    # Desconto total = Desconto do Pedido + Soma de Descontos dos Itens
    desconto_itens = df_detalhado["ID_pedido"].map(desconto_por_pedido).fillna(0)
    df_detalhado["Desconto"] = pd.to_numeric(df_detalhado["Desconto"], errors="coerce").fillna(0) + desconto_itens

    # Valor = bruto - desconto total
    df_detalhado["Valor"] = df_detalhado["Valor Bruto"] - df_detalhado["Desconto"]

    df_detalhado = df_detalhado.rename(columns={
        "NomeLoja": "Loja",
        "NomeVendedor": "Vendedor",
    }).sort_values("Data", ascending=False).reset_index(drop=True)

    # ---------------------------------------------------------------
    # 4. Acumulado do Mês Atual (para Metas)
    # ---------------------------------------------------------------
    hoje = datetime.now()
    mes_atual = hoje.month
    ano_atual = hoje.year
    dia_atual = hoje.day
    dias_no_mes = pd.Timestamp(ano_atual, mes_atual, 1).days_in_month

    # Filtra: mês atual + situações que contam como venda
    mask_mes = (df_detalhado["Data"].dt.month == mes_atual) & (df_detalhado["Data"].dt.year == ano_atual)
    mask_sit = df_detalhado["id_situacao"].isin(situacoes_venda)
    vendas_mes = df_detalhado[mask_mes & mask_sit]

    # Acumulado por loja
    acumulado = {}
    acumulado_pecas = {}
    acumulado_desconto = {}
    for loja_cfg in cfg_dep["lojas"]:
        id_loja = str(loja_cfg["loja_id"]).strip()
        nome_loja = loja_cfg["nome"]
        vendas_loja = vendas_mes[vendas_mes["Loja ID"] == id_loja]
        acumulado[nome_loja] = vendas_loja["Valor"].sum()
        acumulado_pecas[nome_loja] = vendas_loja["Qtd Peças"].sum()
        acumulado_desconto[nome_loja] = vendas_loja["Desconto"].sum()

    # ---------------------------------------------------------------
    # 5. Tabela de Metas + Run Rate
    # ---------------------------------------------------------------
    linhas_meta = []
    total_vendido = 0
    total_meta = 0
    total_pecas = 0
    total_desconto = 0

    for loja_cfg in cfg_dep["lojas"]:
        nome_loja = loja_cfg["nome"]
        vendido = acumulado.get(nome_loja, 0)
        pecas = acumulado_pecas.get(nome_loja, 0)
        desconto = acumulado_desconto.get(nome_loja, 0)
        meta = metas_config.get(nome_loja, 1)

        pct_atingido = vendido / meta if meta > 0 else 0
        falta = meta - vendido

        # Run Rate: projeção linear
        run_rate = (vendido / dia_atual * dias_no_mes) if dia_atual > 0 else 0
        pct_projetado = run_rate / meta if meta > 0 else 0

        # Status
        if pct_atingido >= 1:
            status = "🏆 Batida"
        elif pct_projetado >= 1:
            status = "📈 No Ritmo"
        elif pct_projetado >= 0.8:
            status = "🏃 Correndo"
        else:
            status = "⚠️ Abaixo"

        linhas_meta.append({
            "Loja": nome_loja,
            "Mês": f"{mes_atual:02d}/{ano_atual}",
            "Vendido": vendido,
            "Pecas": pecas,
            "Desconto": desconto,
            "Meta": meta,
            "% Atingido": pct_atingido,
            "Falta Vender": falta,
            "Run Rate": run_rate,
            "% Projetado": pct_projetado,
            "Status": status,
        })

        total_vendido += vendido
        total_meta += meta
        total_pecas += pecas
        total_desconto += desconto

    # Linha Total
    pct_total = total_vendido / total_meta if total_meta > 0 else 0
    rr_total = (total_vendido / dia_atual * dias_no_mes) if dia_atual > 0 else 0
    pct_proj_total = rr_total / total_meta if total_meta > 0 else 0

    linhas_meta.append({
        "Loja": "TOTAL",
        "Mês": f"{mes_atual:02d}/{ano_atual}",
        "Vendido": total_vendido,
        "Pecas": total_pecas,
        "Desconto": total_desconto,
        "Meta": total_meta,
        "% Atingido": pct_total,
        "Falta Vender": total_meta - total_vendido,
        "Run Rate": rr_total,
        "% Projetado": pct_proj_total,
        "Status": "🏆 Batida" if pct_total >= 1 else ("📈 No Ritmo" if pct_proj_total >= 1 else "🏃 Correndo"),
    })

    df_metas = pd.DataFrame(linhas_meta)
    return df_detalhado, df_metas
