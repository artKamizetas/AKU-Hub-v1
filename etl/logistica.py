"""
logistica.py — Reposição de Loja (vetorizado)

Sugestão baseada exclusivamente em VM + Pulmão:
  Sugestão = max(Total - Estoque_loja, 0), limitado pelo estoque central.

Giro e cobertura são mantidos como indicadores informativos,
mas NÃO influenciam a quantidade sugerida.
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from etl import demanda


def processar_logistica(dados: dict, config: dict, vm_map: dict = None) -> pd.DataFrame:
    cfg_log = config["logistica"]
    cfg_dep = config["depositos"]
    excecoes = config.get("excecoes_sku", {}) or {}

    id_central = str(cfg_dep["central"]["deposito_id"]).strip()
    vm_padrao = cfg_log["vm_padrao"]
    dias_analise = cfg_log["dias_analise_giro"]

    produtos = dados["produtos"]
    estoque = dados["estoque"]
    itens = dados["itens"]
    pedidos = dados["pedidos"]
    detalhes = demanda.aplicar_alias_colegio(dados["detalhes"], config)

    # =================================================================
    # PRÉ-CÁLCULOS VETORIZADOS
    # =================================================================

    # Estoque: dict[(id_produto, id_deposito)] → saldo
    est_dict = (
        estoque.groupby(["ID_produto", "ID_deposito"])["saldoFisico"]
        .sum().to_dict()
    )

    # Vendas recentes (para giro informativo)
    data_corte = datetime.now() - timedelta(days=dias_analise)
    itens_recentes = itens[itens["Data"] >= data_corte].copy()
    mapa_pedido_loja = pedidos.set_index("ID")["Loja ID"].to_dict()
    itens_recentes["loja_venda"] = itens_recentes["ID_pedido"].map(mapa_pedido_loja)

    vendas_global_dict = itens_recentes.groupby("ID_produto")["Quantidade"].sum().to_dict()
    vendas_loja_dict = (
        itens_recentes.groupby(["ID_produto", "loja_venda"])["Quantidade"]
        .sum().to_dict()
    )

    # Detalhes de produto
    det_map = detalhes.set_index("ID_produto")[
        ["categoria", "Super_categoria", "Grupo", "Tamanho", "Marca_sku"]
    ].to_dict("index")

    # =================================================================
    # LOOP
    # =================================================================
    resultados = []

    for loja_cfg in cfg_dep["lojas"]:
        id_dep_loja = str(loja_cfg["deposito_id"]).strip()
        id_loja_vendas = str(loja_cfg["loja_id"]).strip()
        nome_loja = loja_cfg["nome"]

        for _, prod in produtos.iterrows():
            id_prod = str(prod["ID"]).strip()
            sku = prod["codigo"]
            nome = prod["Descricao"]

            # VM dinâmico ou fixo
            if vm_map and sku in vm_map:
                vi = vm_map[sku]
                vm = vi["vm"]
                pulmao = vi["pulmao"]
                vm_total = vi["total"]
                pa_sku = vi["pa"]
                sigma = vi["sigma"]
                d_alta = vi["d_alta"]
                fonte_vm = vi["fonte_vm"]
                taxa_cresc = vi["taxa_cresc"]
                correcao = vi["correcao"]
            else:
                exc = excecoes.get(sku, {}) if excecoes else {}
                vm = exc.get("vm", vm_padrao)
                pulmao = 0
                vm_total = vm
                pa_sku = sigma = d_alta = 0
                fonte_vm = "fixo"
                taxa_cresc = correcao = 1.0

            # Estoque
            est_central = est_dict.get((id_prod, id_central), 0)
            est_loja = est_dict.get((id_prod, id_dep_loja), 0)

            # Giro (informativo apenas)
            v_global = vendas_global_dict.get(id_prod, 0)
            v_loja = vendas_loja_dict.get((id_prod, id_loja_vendas), 0)
            giro_global = v_global / dias_analise if dias_analise > 0 else 0
            giro_loja = v_loja / dias_analise if dias_analise > 0 else 0

            # =============================================
            # SUGESTÃO = gap do VM+Pulmão (simples)
            # =============================================
            gap = max(vm_total - est_loja, 0)

            # Decisão
            if est_central < 0 or est_loja < 0:
                acao = "🚫 Estoque Negativo"
                sugestao = gap

            elif est_loja < vm_total and est_central > 0:
                acao = "✨ Repor"
                sugestao = min(gap, est_central)

            elif est_loja < vm_total and est_central <= 0:
                if giro_global > 0:
                    acao = "🚨 Ruptura"
                else:
                    acao = "🔵 Sem Venda"
                sugestao = gap  # para produção

            elif giro_global == 0 and est_central <= 0 and est_loja <= 0:
                acao = "🔵 Sem Venda"
                sugestao = 0

            else:
                acao = "✅ OK"
                sugestao = 0

            det = det_map.get(id_prod, {})

            resultados.append({
                "Loja": nome_loja,
                "SKU": sku,
                "Produto": nome,
                "Categoria": det.get("categoria", ""),
                "SuperCategoria": det.get("Super_categoria", ""),
                "Colegio": det.get("Marca_sku", ""),
                "Tamanho": det.get("Tamanho", ""),
                "EstoqueCentral": est_central,
                "EstoqueLoja": est_loja,
                "VM": vm,
                "Pulmao": pulmao,
                "Total": vm_total,
                "GiroGlobal": round(giro_global, 2),
                "GiroLoja": round(giro_loja, 2),
                "SugestaoQtd": round(sugestao),
                "Acao": acao,
                # Diagnóstico
                "PA": pa_sku,
                "Sigma": sigma,
                "D_Alta": d_alta,
                "FonteVM": fonte_vm,
                "TaxaCresc": taxa_cresc,
                "Correcao": correcao,
            })

    df = pd.DataFrame(resultados)

    # Ordenar: ações urgentes primeiro
    ordem_acao = {
        "🚫 Estoque Negativo": 0,
        "🚨 Ruptura": 1,
        "✨ Repor": 2,
        "🔵 Sem Venda": 3,
        "✅ OK": 4,
    }
    df["_ordem"] = df["Acao"].map(ordem_acao).fillna(5)
    df = df.sort_values(["_ordem", "SugestaoQtd"], ascending=[True, False]).reset_index(drop=True)
    df = df.drop(columns=["_ordem"])
    return df
