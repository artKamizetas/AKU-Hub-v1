"""
daily.py — ETL Comercial / Acompanhamento de Metas Escalonadas

Gera a tabela detalhada de vendas e os resumos de meta (Prata/Ouro/Diamante,
por Faturamento e PA) por Loja e por Vendedor, para uma competência (mês)
dada. A regra de meta em si (classificação de nível, rateio vendedor←loja,
agregação) vive em etl/metas.py — este módulo só monta os DataFrames a
partir dos dados brutos. Modelo em docs/requisitos/metas-escalonadas.md.

Uso:
    from etl.daily import processar_daily
    detalhado, metas_loja, metas_vendedor = processar_daily(dados, config)
    # ou revisando um mês fechado:
    detalhado, metas_loja, metas_vendedor = processar_daily(dados, config, competencia="2026-03")
"""

import pandas as pd
from datetime import datetime

from etl import demanda
from etl import metas


def _linha_resumo(vendido, pecas, pedidos, desconto, metas_fat, metas_pa,
                   dia_atual, dias_no_mes, origem) -> dict:
    """Colunas comuns a uma linha de Loja ou Vendedor: realizado + resumo de
    meta (faturamento e PA), delegando a classificação a etl/metas.py."""
    rf = metas.resumo_faturamento(vendido, metas_fat, dia_atual, dias_no_mes)
    rp = metas.resumo_pa(pecas, pedidos, metas_pa)
    ticket_medio = (vendido / pedidos) if pedidos > 0 else 0.0

    # Colunas de TEXTO saem como "" (nunca None): o pandas converteria None
    # para NaN numa coluna string, e a UI teria que distinguir os dois.
    # Os campos numéricos seguem None → NaN (célula vazia, comportamento certo).
    def _txt(v):
        return v if v else ""

    return {
        "Vendido": rf["vendido"],
        "Pecas": pecas,
        "Pedidos": pedidos,
        "Desconto": desconto,
        "Ticket Medio": ticket_medio,
        "Meta Prata": (metas_fat or {}).get("prata"),
        "Meta Ouro": (metas_fat or {}).get("ouro"),
        "Meta Diamante": (metas_fat or {}).get("diamante"),
        "Nivel": _txt(rf["nivel"]),
        "Proximo Nivel": _txt(rf["proximo_nivel"]),
        "Falta Proximo": rf["falta"],
        "Run Rate": rf["run_rate"],
        "Nivel Projetado": _txt(rf["nivel_projetado"]),
        "Ritmo Necessario": rf["ritmo_necessario"],
        "PA": rp["pa"],
        "PA Meta Prata": (metas_pa or {}).get("prata"),
        "PA Meta Ouro": (metas_pa or {}).get("ouro"),
        "PA Meta Diamante": (metas_pa or {}).get("diamante"),
        "PA Nivel": _txt(rp["nivel"]),
        "PA Proximo Nivel": _txt(rp["proximo_nivel"]),
        "PA Falta Proximo": rp["falta"],
        "Origem Meta": origem,
    }


def _metas_dict(row_prefixo: str, row: dict) -> dict:
    """Reconstrói {prata,ouro,diamante} a partir das colunas 'Meta *'/'PA Meta *'
    de uma linha já montada — usado para agregar a linha TOTAL sem duplicar
    a leitura do config."""
    p, o, d = row[f"{row_prefixo} Prata"], row[f"{row_prefixo} Ouro"], row[f"{row_prefixo} Diamante"]
    if o is None:
        return None
    return {"prata": p, "ouro": o, "diamante": d}


def processar_daily(dados: dict, config: dict, competencia: str = None) -> tuple:
    """
    Processa vendas e calcula metas escalonadas.

    Args:
        competencia: "AAAA-MM" a revisar; None = mês corrente (default).

    Retorna:
        (df_detalhado, df_metas_loja, df_metas_vendedor)

        df_detalhado: cada pedido enriquecido com Vendedor, Loja, Situação, Colégio
        df_metas_loja: resumo por loja (+ linha TOTAL) com metas Prata/Ouro/
            Diamante de Faturamento e PA, nível, falta, projeção
        df_metas_vendedor: idem por vendedor, com meta DERIVADA por rateio
            da loja a que está atribuído na competência (etl/metas.py)
    """
    cfg_daily = config["daily"]
    cfg_dep = config["depositos"]
    situacoes_venda = cfg_daily["situacoes_venda"]

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
    ped = pedidos.drop_duplicates(subset=["ID"]).copy()
    ped["NomeLoja"] = ped["Loja ID"].map(map_loja).fillna("Loja " + ped["Loja ID"])
    ped["NomeVendedor"] = ped["Vendedor"].map(map_vend).fillna("Vend " + ped["Vendedor"])
    ped["Situacao"] = ped["id_situacao"].map(map_sit).fillna("Sit " + ped["id_situacao"].astype(str))
    ped["LojaConfig"] = ped["Loja ID"].map(map_id_nome).fillna("")

    ped_cols = ped[["ID", "Data", "NomeLoja", "NomeVendedor", "Vendedor", "Cliente",
                     "Pedido", "Situacao", "id_situacao", "Loja ID", "LojaConfig", "Desconto",
                     ]].rename(columns={"ID": "ID_pedido", "Vendedor": "VendedorID"})

    # Enriquecer itens com colégio
    itens_c = itens.copy()
    itens_c["Colegio"] = itens_c["ID_produto"].map(map_colegio).fillna("").astype(str)
    itens_c.loc[itens_c["Colegio"].isin(["", "nan"]), "Colegio"] = "Sem Colégio"

    # ---------------------------------------------------------------
    # 3. Agregar Itens por Pedido (peças reais + valor + desconto)
    # ---------------------------------------------------------------
    pecas_por_pedido = itens_c.groupby("ID_pedido")["Quantidade"].sum().to_dict()

    itens_c["_valor_bruto"] = itens_c["Quantidade"] * itens_c["Valor Unidade"]
    valor_bruto_por_pedido = itens_c.groupby("ID_pedido")["_valor_bruto"].sum().to_dict()

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
    # 4. Competência (mês de referência das metas — pode ser um mês fechado)
    # ---------------------------------------------------------------
    hoje = datetime.now()
    competencia_atual = metas.chave_competencia(hoje.year, hoje.month)
    if competencia is None:
        competencia = competencia_atual
    ano_c, mes_c = int(competencia[:4]), int(competencia[5:7])
    dias_no_mes = pd.Timestamp(ano_c, mes_c, 1).days_in_month
    # Mês fechado (≠ mês corrente): já está completo, run rate = realizado.
    dia_atual = hoje.day if competencia == competencia_atual else dias_no_mes

    mask_mes = (df_detalhado["Data"].dt.month == mes_c) & (df_detalhado["Data"].dt.year == ano_c)
    mask_sit = df_detalhado["id_situacao"].isin(situacoes_venda)
    vendas_mes = df_detalhado[mask_mes & mask_sit]

    # ---------------------------------------------------------------
    # 5. Metas por Loja (+ linha TOTAL)
    # ---------------------------------------------------------------
    linhas_loja = []
    for loja_cfg in cfg_dep["lojas"]:
        nome_loja = loja_cfg["nome"]
        id_loja = str(loja_cfg["loja_id"]).strip()
        vendas_loja = vendas_mes[vendas_mes["Loja ID"] == id_loja]

        vendido = float(vendas_loja["Valor"].sum())
        pecas = float(vendas_loja["Qtd Peças"].sum())
        desconto = float(vendas_loja["Desconto"].sum())
        n_pedidos = float(vendas_loja["ID_pedido"].nunique())

        m = metas.metas_da_loja(config, nome_loja, competencia)
        linha = {"Loja": nome_loja}
        linha.update(_linha_resumo(vendido, pecas, n_pedidos, desconto,
                                    m["faturamento"], m["pa"], dia_atual, dias_no_mes, m["origem"]))
        linhas_loja.append(linha)

    # Linha TOTAL: soma direta do realizado; meta agregada via etl/metas.py
    # (faturamento soma, PA pondera por pedidos — nunca a média simples)
    agg_fat = metas.agregar_faturamento([
        {"vendido": r["Vendido"], "metas": _metas_dict("Meta", r)} for r in linhas_loja
    ])
    agg_pa = metas.agregar_pa([
        {"pecas": r["Pecas"], "pedidos": r["Pedidos"], "metas": _metas_dict("PA Meta", r)} for r in linhas_loja
    ])
    total_desconto = sum(r["Desconto"] for r in linhas_loja)
    linha_total = {"Loja": "TOTAL"}
    linha_total.update(_linha_resumo(agg_fat["vendido"], agg_pa["pecas"], agg_pa["pedidos"], total_desconto,
                                      agg_fat["metas"], agg_pa["metas"], dia_atual, dias_no_mes, "agregada"))
    linhas_loja.append(linha_total)

    df_metas_loja = pd.DataFrame(linhas_loja)

    # ---------------------------------------------------------------
    # 6. Metas por Vendedor (meta DERIVADA por rateio da loja atribuída)
    # ---------------------------------------------------------------
    atrib = metas.atribuicao_vendedores(config, competencia)

    if len(vendas_mes) > 0:
        grp = vendas_mes.groupby("VendedorID").agg(
            Vendido=("Valor", "sum"), Pecas=("Qtd Peças", "sum"),
            Desconto=("Desconto", "sum"), Pedidos=("ID_pedido", "nunique"),
        )
        realizado_vendedor = grp.to_dict("index")
    else:
        realizado_vendedor = {}

    vendedor_ids = set(realizado_vendedor.keys()) | set(atrib.keys())
    linhas_vend = []
    for vid in sorted(vendedor_ids):
        dado = realizado_vendedor.get(vid, {"Vendido": 0.0, "Pecas": 0.0, "Desconto": 0.0, "Pedidos": 0.0})
        nome_v = map_vend.get(vid, f"Vend {vid}")
        info = atrib.get(vid)

        if info and info.get("ativo", True) and info.get("loja"):
            loja_v = info["loja"]
            m = metas.metas_do_vendedor(config, vid, loja_v, competencia)
            fat_m, pa_m, origem, peso_v = m["faturamento"], m["pa"], m["origem"], m["peso"]
        else:
            loja_v, fat_m, pa_m, origem, peso_v = "Sem atribuição", None, None, "ausente", None

        linha = {"VendedorID": vid, "Vendedor": nome_v, "Loja": loja_v, "Peso": peso_v}
        linha.update(_linha_resumo(float(dado["Vendido"]), float(dado["Pecas"]), float(dado["Pedidos"]),
                                    float(dado["Desconto"]), fat_m, pa_m, dia_atual, dias_no_mes, origem))
        linhas_vend.append(linha)

    df_metas_vendedor = pd.DataFrame(linhas_vend)

    return df_detalhado, df_metas_loja, df_metas_vendedor
