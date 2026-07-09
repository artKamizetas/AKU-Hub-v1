"""
vm_dinamico.py — VM Dinâmico + Pulmão por SKU

VM (prateleira): demanda média × cobertura × taxa × correção
Pulmão (armário): Fator de Serviço × Desvio-Padrão diário × √lead time
"""

import pandas as pd
import numpy as np
import math

from etl import demanda

NIVEL_SERVICO_Z = {90: 1.28, 95: 1.65, 97: 1.88, 98: 2.05, 99: 2.33}


def _nivel_para_z(nivel: float) -> float:
    if nivel <= 1:
        nivel = nivel * 100
    return NIVEL_SERVICO_Z.get(round(nivel), 1.65)


def _filtrar_alta(df, inicio_mes, fim_mes):
    mes = df["Data"].dt.month
    if inicio_mes <= fim_mes:
        return df[(mes >= inicio_mes) & (mes <= fim_mes)]
    return df[(mes >= inicio_mes) | (mes <= fim_mes)]


def calcular_vm_por_sku(dados: dict, config: dict) -> dict:
    glob = config.get("vm", {})
    map_colegios = config.get("colegios") or {}
    excecoes = config.get("excecoes_sku") or {}
    map_skus_correcao = {
        sku: v["correcao"] for sku, v in excecoes.items()
        if isinstance(v, dict) and "correcao" in v
    }

    dias_cobertura = glob.get("dias_cobertura", 15)
    inicio_alta, fim_alta = int(glob.get("inicio_alta", 10)), int(glob.get("fim_alta", 3))
    mult_pa = glob.get("mult_pa", 2.0)
    vm_minimo = int(glob.get("vm_minimo", 2))
    lead_time = glob.get("lead_time", 3)
    ns_default = glob.get("nivel_servico_default", 95)
    ativo_cresc = glob.get("aplicar_crescimento", True)
    obs_cresc = (demanda.calcular_crescimento_observado(dados, config)
                 if (config.get("demanda", {}) or {}).get("crescimento_observado_ativo", True)
                 else None)

    itens = dados["itens"]
    produtos = dados["produtos"]
    detalhes = dados["detalhes"]

    map_id_colegio = detalhes.set_index("ID_produto")["Marca_sku"].to_dict()
    map_id_grupo = detalhes.set_index("ID_produto")["Grupo"].to_dict()

    if inicio_alta <= fim_alta:
        meses_alta = list(range(inicio_alta, fim_alta + 1))
    else:
        meses_alta = list(range(inicio_alta, 13)) + list(range(1, fim_alta + 1))
    dias_alta = len(meses_alta) * 30

    itens_alta = _filtrar_alta(itens, inicio_alta, fim_alta)

    # === Pré-cálculos vetorizados ===
    if len(itens_alta) > 0:
        agg_alta = itens_alta.groupby("ID_produto").agg(
            pecas=("Quantidade", "sum"), pedidos=("ID_pedido", "nunique")
        )

        # Vendas diárias por produto × dia
        itens_alta_c = itens_alta.copy()
        itens_alta_c["dia"] = itens_alta_c["Data"].dt.date
        vd_diario = itens_alta_c.groupby(["ID_produto", "dia"])["Quantidade"].sum()

        # Dict: id_prod → array de quantidades por dia COM venda
        _vd_grouped = vd_diario.groupby("ID_produto").apply(lambda x: x.values).to_dict()

        # Range de dias POR PRODUTO (corrige bug do σ global)
        _date_range = itens_alta_c.groupby("ID_produto")["dia"].agg(["min", "max"])
        _n_dias_por_prod = {}
        for id_p, row in _date_range.iterrows():
            _n_dias_por_prod[id_p] = (pd.Timestamp(row["max"]) - pd.Timestamp(row["min"])).days + 1
    else:
        agg_alta = pd.DataFrame(columns=["pecas", "pedidos"])
        _vd_grouped = {}
        _n_dias_por_prod = {}

    # === Loop (só dict lookups) ===
    resultado = {}

    for _, prod in produtos.iterrows():
        id_prod = str(prod["ID"]).strip()
        sku = prod["codigo"]

        pecas_alta = agg_alta.loc[id_prod, "pecas"] if id_prod in agg_alta.index else 0
        pedidos_alta = agg_alta.loc[id_prod, "pedidos"] if id_prod in agg_alta.index else 0

        d_alta = pecas_alta / dias_alta if dias_alta > 0 else 0
        pa = pecas_alta / pedidos_alta if pedidos_alta > 0 else 1.0
        pedidos_dia = pedidos_alta / dias_alta if dias_alta > 0 else 0

        # σ por SKU (range específico do produto, não global)
        qtds_dias = _vd_grouped.get(id_prod, np.array([]))
        n_dias_reais = _n_dias_por_prod.get(id_prod, dias_alta)
        if len(qtds_dias) > 0 and n_dias_reais > 1:
            dias_sem = max(0, n_dias_reais - len(qtds_dias))
            todas = np.concatenate([qtds_dias, np.zeros(dias_sem)])
            sigma = float(np.std(todas, ddof=1)) if len(todas) > 1 else 0.0
            if np.isnan(sigma):
                sigma = 0.0
        else:
            sigma = 0.0

        colegio = str(map_id_colegio.get(id_prod, "")).strip()
        grupo = str(map_id_grupo.get(id_prod, "")).strip()
        col_p = map_colegios.get(colegio, {})
        taxa_cresc = demanda.taxa_crescimento_efetiva(colegio, config, grupo, ativo_cresc, obs_cresc)
        nivel_servico = col_p.get("nivel_servico", ns_default) if col_p else ns_default
        fator_servico = _nivel_para_z(nivel_servico)
        correcao = map_skus_correcao.get(sku, 1.0)

        # VM
        vm_cobertura = d_alta * dias_cobertura * taxa_cresc * correcao
        vm_piso = pa * mult_pa
        vm_bruto = max(vm_cobertura, vm_piso, vm_minimo)
        vm_final = math.ceil(vm_bruto)

        if vm_bruto <= vm_minimo:
            fonte = "minimo_absoluto"
        elif vm_piso >= vm_cobertura:
            fonte = "piso_PA"
        else:
            fonte = "cobertura"

        # Pulmão
        pulmao = math.ceil(fator_servico * sigma * math.sqrt(lead_time))
        total = vm_final + pulmao

        resultado[sku] = {
            "vm": vm_final, "pulmao": pulmao, "total": total,
            "d_alta": round(d_alta, 4), "sigma": round(sigma, 4),
            "pa": round(pa, 2), "pedidos_dia": round(pedidos_dia, 4),
            "taxa_cresc": taxa_cresc, "correcao": correcao,
            "colegio": colegio, "fonte_vm": fonte,
        }

    return resultado
