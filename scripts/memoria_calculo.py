"""
memoria_calculo.py — Memória de Cálculo do VM Dinâmico + Pulmão

Mostra passo a passo como o VM e Pulmão foram calculados para um SKU.

Uso (a partir da raiz do projeto):
    python scripts/memoria_calculo.py                        # SKU padrão
    python scripts/memoria_calculo.py NEV020CAMEDF-PP        # SKU específico
"""

import sys
import math
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
from etl.loader import carregar_dados, carregar_config


# ==============================
# Helpers de formatação
# ==============================
def linha(titulo):
    print(f"\n{'=' * 70}")
    print(f"  {titulo}")
    print(f"{'=' * 70}")

def sub(titulo):
    print(f"\n  --- {titulo} ---")

def calc(nome, formula, resultado):
    print(f"  {nome}")
    print(f"    {formula}")
    print(f"    = {resultado}")


# ==============================
# Main
# ==============================
def main():
    sku_alvo = sys.argv[1] if len(sys.argv) > 1 else "TESTENUM"

    config = carregar_config()   # yaml (defaults) + app.parametros (Supabase)

    dados = carregar_dados()

    # Encontra o produto
    produtos = dados["produtos"]
    match = produtos[produtos["codigo"] == sku_alvo]
    if len(match) == 0:
        print(f"SKU '{sku_alvo}' não encontrado.")
        return

    prod = match.iloc[0]
    id_prod = str(prod["ID"]).strip()

    detalhes = dados["detalhes"]
    det = detalhes[detalhes["ID_produto"] == id_prod]
    colegio = det["Marca_sku"].values[0] if len(det) > 0 else ""
    categoria = det["categoria"].values[0] if len(det) > 0 else ""
    super_cat = det["Super_categoria"].values[0] if len(det) > 0 else ""
    tamanho = det["Tamanho"].values[0] if len(det) > 0 else ""

    linha(f"MEMÓRIA DE CÁLCULO — SKU: {sku_alvo}")
    print(f"\n  Produto: {prod['Descricao']}")
    print(f"  ID Bling: {id_prod}")
    print(f"  Preço Custo: R$ {prod.get('precoCusto', 0):.2f}")

    # ================================================================
    # ETAPA 1 — PARÂMETROS
    # ================================================================
    linha("ETAPA 1 — PARÂMETROS DO SKU")

    glob = config.get("vm", {})
    dias_cobertura = glob.get("dias_cobertura", 15)
    inicio_alta = int(glob.get("inicio_alta", 10))
    fim_alta = int(glob.get("fim_alta", 3))
    mult_pa = glob.get("mult_pa", 2.0)
    vm_minimo = int(glob.get("vm_minimo", 2))
    lead_time = glob.get("lead_time", 3)
    ns_default = glob.get("nivel_servico_default", 95)

    map_colegios = config.get("colegios") or {}
    col_params = map_colegios.get(colegio, {})
    taxa_cresc = col_params.get("taxa_crescimento", 1.0) if col_params else 1.0
    nivel_servico = col_params.get("nivel_servico", ns_default) if col_params else ns_default

    excecoes = config.get("excecoes_sku") or {}
    correcao = excecoes.get(sku_alvo, {}).get("correcao", 1.0) if isinstance(excecoes.get(sku_alvo), dict) else 1.0

    fator_map = {90: 1.28, 95: 1.65, 97: 1.88, 98: 2.05, 99: 2.33}
    ns_int = round(nivel_servico if nivel_servico > 1 else nivel_servico * 100)
    fator_servico = fator_map.get(ns_int, 1.65)

    sub("Dados do Produto")
    print(f"  Colégio (Marca_sku): '{colegio}'")
    print(f"  Categoria: '{categoria}' | Super: '{super_cat}' | Tamanho: '{tamanho}'")

    sub("Parâmetros Globais (config.yaml → vm)")
    print(f"  Dias de Cobertura VM:   {dias_cobertura}")
    print(f"  Alta Temporada:         mês {inicio_alta} a mês {fim_alta}")
    print(f"  Multiplicador PA:       {mult_pa}x")
    print(f"  VM Mínimo Absoluto:     {vm_minimo}")
    print(f"  Lead Time Reposição:    {lead_time} dias")
    print(f"  Nível Serviço Padrão:   {ns_default}%")

    sub("Parâmetros do Colégio (config.yaml → colegios)")
    print(f"  Colégio '{colegio}' → Taxa Crescimento = {taxa_cresc}")
    print(f"  Colégio '{colegio}' → Nível de Serviço = {nivel_servico}% (Fator de Serviço = {fator_servico:.2f})")
    if colegio not in map_colegios:
        print(f"  ⚠️  Colégio não cadastrado em config.yaml. Usando defaults.")

    sub("Correção do SKU (config.yaml → excecoes_sku)")
    print(f"  SKU '{sku_alvo}' → Correção Manual = {correcao}")
    if sku_alvo not in excecoes:
        print(f"  ⚠️  SKU não cadastrado em excecoes_sku. Usando default 1.0.")

    # ================================================================
    # ETAPA 2 — VENDAS NA ALTA
    # ================================================================
    linha("ETAPA 2 — VENDAS NA ALTA TEMPORADA")

    itens = dados["itens"]
    vendas_sku = itens[itens["ID_produto"] == id_prod].copy()

    print(f"\n  Total de registros de venda (todas as datas): {len(vendas_sku)}")
    print(f"  Total de peças vendidas (all time): {vendas_sku['Quantidade'].sum():.0f}")

    if inicio_alta <= fim_alta:
        meses_alta = list(range(inicio_alta, fim_alta + 1))
    else:
        meses_alta = list(range(inicio_alta, 13)) + list(range(1, fim_alta + 1))
    dias_alta = len(meses_alta) * 30

    mes = vendas_sku["Data"].dt.month
    if inicio_alta <= fim_alta:
        vendas_alta = vendas_sku[(mes >= inicio_alta) & (mes <= fim_alta)]
    else:
        vendas_alta = vendas_sku[(mes >= inicio_alta) | (mes <= fim_alta)]

    pecas_alta = vendas_alta["Quantidade"].sum()
    pedidos_alta = vendas_alta["ID_pedido"].nunique()

    sub(f"Filtro: meses {meses_alta} ({dias_alta} dias)")
    print(f"  Registros de venda na alta: {len(vendas_alta)}")
    print(f"  Peças vendidas na alta:     {pecas_alta:.0f}")
    print(f"  Pedidos distintos na alta:  {pedidos_alta}")

    if len(vendas_alta) > 0:
        print(f"\n  Período coberto: {vendas_alta['Data'].min().strftime('%d/%m/%Y')} "
              f"a {vendas_alta['Data'].max().strftime('%d/%m/%Y')}")

    # ================================================================
    # ETAPA 3 — CÁLCULO DO VM
    # ================================================================
    linha("ETAPA 3 — CÁLCULO DO VM DINÂMICO")

    d_alta = pecas_alta / dias_alta if dias_alta > 0 else 0
    calc("Demanda Média Diária na Alta (D_alta):",
         f"Peças na alta / Dias na alta = {pecas_alta:.0f} / {dias_alta}",
         f"{d_alta:.4f} peças/dia")

    pa = pecas_alta / pedidos_alta if pedidos_alta > 0 else 1.0
    if pedidos_alta > 0:
        calc("PA — Peças por Atendimento:",
             f"Peças / Pedidos = {pecas_alta:.0f} / {pedidos_alta}",
             f"{pa:.2f} peças/atendimento")
    else:
        print(f"\n  PA — Peças por Atendimento:")
        print(f"    Sem pedidos na alta → usando PA = 1.0 (default)")

    pedidos_dia = pedidos_alta / dias_alta if dias_alta > 0 else 0
    calc("Pedidos por Dia (indicador de risco):",
         f"Pedidos / Dias = {pedidos_alta} / {dias_alta}",
         f"{pedidos_dia:.4f} pedidos/dia")

    vm_cob = d_alta * dias_cobertura * taxa_cresc * correcao
    calc("VM Cobertura:",
         f"D_alta × Dias_cob × Taxa_cresc × Correção = "
         f"{d_alta:.4f} × {dias_cobertura} × {taxa_cresc} × {correcao}",
         f"{vm_cob:.2f}")

    vm_piso = pa * mult_pa
    calc("VM Piso (PA):",
         f"PA × Multiplicador = {pa:.2f} × {mult_pa}",
         f"{vm_piso:.2f}")

    vm_bruto = max(vm_cob, vm_piso, vm_minimo)
    vm_final = math.ceil(vm_bruto)

    print(f"\n  DECISÃO DO VM:")
    print(f"    max(VM_cobertura={vm_cob:.2f}, VM_piso={vm_piso:.2f}, VM_mínimo={vm_minimo})")
    print(f"    = {vm_bruto:.2f}")
    print(f"    Arredondado para cima: {vm_final}")

    if vm_bruto <= vm_minimo:
        print(f"    Fonte: MÍNIMO ABSOLUTO (nem cobertura nem PA atingiram {vm_minimo})")
    elif vm_piso >= vm_cob:
        print(f"    Fonte: PISO PA (PA×{mult_pa} = {vm_piso:.1f} > cobertura {vm_cob:.1f})")
    else:
        print(f"    Fonte: COBERTURA (demanda×dias = {vm_cob:.1f} > PA×{mult_pa} = {vm_piso:.1f})")

    print(f"\n  ┌────────────────────────────────┐")
    print(f"  │  VM (prateleira) = {vm_final:>4} peças   │")
    print(f"  └────────────────────────────────┘")

    # ================================================================
    # ETAPA 3B — CÁLCULO DO PULMÃO
    # ================================================================
    linha("ETAPA 3B — CÁLCULO DO PULMÃO (ARMÁRIO)")

    if len(vendas_alta) > 0:
        vendas_alta_c = vendas_alta.copy()
        vendas_alta_c["dia"] = vendas_alta_c["Data"].dt.date
        vd_por_dia = vendas_alta_c.groupby("dia")["Quantidade"].sum()

        datas_alta_range = pd.date_range(vendas_alta["Data"].min(), vendas_alta["Data"].max(), freq="D")
        n_dias_reais = len(datas_alta_range)
        dias_com_venda = len(vd_por_dia)
        dias_sem_venda = max(0, n_dias_reais - dias_com_venda)

        todas_qtds = np.concatenate([vd_por_dia.values, np.zeros(dias_sem_venda)])
        sigma = float(np.std(todas_qtds, ddof=1)) if len(todas_qtds) > 1 else 0.0
        if np.isnan(sigma):
            sigma = 0.0

        print(f"\n  Análise da variabilidade diária na alta:")
        print(f"    Dias no range: {n_dias_reais}")
        print(f"    Dias COM venda: {dias_com_venda}")
        print(f"    Dias SEM venda: {dias_sem_venda}")
        if dias_com_venda > 0:
            print(f"    Vendas nos dias com venda: min={vd_por_dia.min():.0f}, "
                  f"max={vd_por_dia.max():.0f}, média={vd_por_dia.mean():.2f}")
        print(f"    Média diária (c/ zeros): {todas_qtds.mean():.4f}")

        calc("Desvio-Padrão da demanda diária (σ):",
             f"std({n_dias_reais} dias, incluindo {dias_sem_venda} zeros)",
             f"{sigma:.4f} peças/dia")

        print(f"\n  📖 O QUE É O DESVIO-PADRÃO (σ):")
        print(f"     Mede quanto a demanda diária varia em torno da média.")
        print(f"     σ alto = vendas muito irregulares (dias com 0, dias com 12).")
        print(f"     σ baixo = vendas estáveis (sempre perto da média).")
    else:
        sigma = 0.0
        print(f"\n  Sem vendas na alta → σ = 0 (pulmão será zero)")

    pulmao_bruto = fator_servico * sigma * math.sqrt(lead_time)
    pulmao_final = math.ceil(pulmao_bruto)

    calc("Pulmão (estoque de segurança):",
         f"Fator de Serviço × Desvio-Padrão × √lead time = {fator_servico:.2f} × {sigma:.4f} × √{lead_time}",
         f"{fator_servico:.2f} × {sigma:.4f} × {math.sqrt(lead_time):.2f} = {pulmao_bruto:.2f}")
    print(f"    Arredondado para cima: {pulmao_final}")

    vm_total = vm_final + pulmao_final

    print(f"\n  ┌──────────────────────────────────────────┐")
    print(f"  │  VM (prateleira) = {vm_final:>4} peças             │")
    print(f"  │  Pulmão (armário) = {pulmao_final:>3} peças             │")
    print(f"  │  ─────────────────────────                │")
    print(f"  │  TOTAL NA LOJA   = {vm_total:>4} peças             │")
    print(f"  │  (repõe quando estoque < {vm_total})              │")
    print(f"  └──────────────────────────────────────────┘")

    # ================================================================
    # RESUMO DE REPOSIÇÃO (simplificado)
    # ================================================================
    linha("SUGESTÃO DE REPOSIÇÃO")

    cfg_dep = config["depositos"]
    id_central = str(cfg_dep["central"]["deposito_id"]).strip()
    estoque = dados["estoque"]
    est_pivot = estoque.groupby(["ID_produto", "ID_deposito"])["saldoFisico"].sum()

    est_central = est_pivot.get((id_prod, id_central), 0)
    print(f"\n  Estoque Central: {est_central:.0f} peças")

    for loja_cfg in cfg_dep["lojas"]:
        id_dep_loja = str(loja_cfg["deposito_id"]).strip()
        nome_loja = loja_cfg["nome"]

        est_loja = est_pivot.get((id_prod, id_dep_loja), 0)
        necessidade = max(vm_total - est_loja, 0)
        sugestao = min(necessidade, est_central) if est_central > 0 else necessidade

        sub(f"LOJA: {nome_loja}")
        print(f"  Estoque na Loja: {est_loja:.0f} peças")

        calc("Necessidade:",
             f"max(Total - Est.Loja, 0) = max({vm_total} - {est_loja:.0f}, 0)",
             f"{necessidade:.0f} peças")

        if necessidade == 0:
            acao = "✅ OK"
        elif est_central > 0:
            acao = "✨ Repor VM"
            print(f"  Sugestão = min(Necessidade={necessidade:.0f}, Est.Central={est_central:.0f}) = {sugestao:.0f}")
        elif est_central <= 0:
            acao = "🚨 Ruptura Total"

        print(f"\n  ┌────────────────────────────────────────────┐")
        print(f"  │  AÇÃO: {acao:<38}│")
        print(f"  │  SUGESTÃO: {sugestao:>4} peças                      │")
        print(f"  └────────────────────────────────────────────┘")


if __name__ == "__main__":
    main()
