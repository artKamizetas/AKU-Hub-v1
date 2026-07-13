"""
memoria_calculo_fabrica.py — Memória de Cálculo da Fábrica (PCP)

Mostra passo a passo o pedido de produção de um SKU no modelo atual:
demanda ANCORADA NA ALTA + política order-up-to com projeção forward.

Uso (a partir da raiz do projeto):
    python scripts/memoria_calculo_fabrica.py                     # SKU padrão
    python scripts/memoria_calculo_fabrica.py NEV020CAMEDF-PP     # SKU específico
"""

import sys
import yaml
import pandas as pd
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
from etl.loader import carregar_dados
from etl import demanda


def linha(t):
    print(f"\n{'=' * 70}\n  {t}\n{'=' * 70}")

def sub(t):
    print(f"\n  --- {t} ---")


def main():
    with open(RAIZ / "config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    dados = carregar_dados()
    produtos = dados["produtos"]

    sku_alvo = sys.argv[1] if len(sys.argv) > 1 else produtos.iloc[0]["codigo"]
    prod = produtos[produtos["codigo"] == sku_alvo]
    if len(prod) == 0:
        print(f"❌ SKU '{sku_alvo}' não encontrado.")
        return
    prod = prod.iloc[0]
    id_prod = str(prod["ID"]).strip()

    det = dados["detalhes"]
    d = det[det["ID_produto"] == id_prod]
    colegio = str(d["Marca_sku"].values[0]).strip() if len(d) else ""
    grupo = str(d["Grupo"].values[0]).strip() if len(d) else ""
    categoria = str(d["Super_categoria"].values[0]).strip() if len(d) else ""

    linha(f"MEMÓRIA DE CÁLCULO FÁBRICA — SKU: {sku_alvo}")
    print(f"\n  Produto: {prod['Descricao']}")
    print(f"  Colégio: {colegio} | Grupo: {grupo} | Super-cat: {categoria}")
    print(f"  Preço custo: R$ {prod['preco_custo']:.2f}")

    # ETAPA 1 — Âncora na alta
    linha("ETAPA 1 — DEMANDA ANCORADA NA ALTA")
    temporada = demanda._ultima_temporada_alta(config)
    print(f"\n  Última temporada de alta: " +
          ", ".join(f"{demanda.NOMES_MES[m-1]}/{ts.year}" for m, ts in sorted(temporada.items())))

    ativo = config.get("demanda", {}).get("aplicar_crescimento_fabrica", True)
    # A camada OBSERVADA precisa ser passada aqui igual o motor faz em
    # calcular_demanda_mensal_por_sku — sem ela, a taxa cai no fallback cego
    # (+10%) e não bate com a demanda das etapas seguintes.
    obs = (demanda.calcular_crescimento_observado(dados, config)
           if config.get("demanda", {}).get("crescimento_observado_ativo", True) else None)
    taxa = demanda.taxa_crescimento_efetiva(colegio, config, grupo, ativo, obs)

    # Origem do fator (mesma cascata de taxa_crescimento_efetiva) — deixa
    # explícito DE ONDE veio o crescimento e não confunde com o fallback.
    col_cfg = (config.get("colegios") or {}).get(colegio) or {}
    seg = demanda.segmento_do_grupo(grupo, config)
    obs_col = (obs or {}).get(colegio, {}) if obs else {}
    if not ativo:
        origem_cresc = "toggle desligado"
    elif grupo in (col_cfg.get("crescimento_grupos") or {}):
        origem_cresc = f"manual grupo {grupo}"
    elif "taxa_crescimento" in col_cfg:
        origem_cresc = f"manual colégio {colegio}"
    elif (obs_col.get("segmentos") or {}).get(seg) is not None:
        origem_cresc = f"observado {colegio}×{seg}"
    elif obs_col.get("__geral__") is not None:
        origem_cresc = f"observado {colegio} (geral)"
    else:
        origem_cresc = f"fallback +{config.get('fabrica', {}).get('crescimento_pct', 0):.0f}%"
    print(f"  Crescimento efetivo (colégio×grupo): {taxa:.3f}x  [{origem_cresc}]  (ativo={ativo})")

    itens = dados["itens"]
    sub("Vendas reais na última alta")
    vendas_ultima_alta = 0
    for m, ts in sorted(temporada.items()):
        mask = (itens["ID_produto"] == id_prod) & (itens["Data"].dt.year == ts.year) & (itens["Data"].dt.month == ts.month)
        q = itens[mask]["Quantidade"].sum()
        vendas_ultima_alta += q
        print(f"    {demanda.NOMES_MES[m-1]}/{ts.year}: {q:.0f} peças")
    print(f"  Vendas da última alta: {vendas_ultima_alta:.0f} "
          f"→ × crescimento = demanda de alta = {vendas_ultima_alta * taxa:.1f}")

    prop_global = demanda.calcular_proporcao_baixa(dados, config)
    prop_baixa = demanda.proporcao_baixa_efetiva(sku_alvo, colegio, config, prop_global)
    origem = "global" if abs(prop_baixa - prop_global) < 1e-9 else "override manual"
    print(f"  Proporção da baixa ({origem}): {prop_baixa:.3f} (global={prop_global:.3f}) "
          f"→ demanda de baixa = {vendas_ultima_alta * taxa * prop_baixa:.1f}")

    # ETAPA 2 — Demanda mensal projetada
    linha("ETAPA 2 — DEMANDA MENSAL PROJETADA (12 meses)")
    dem = demanda.calcular_demanda_mensal_por_sku(dados, config)
    dsku = dem[dem["SKU"] == sku_alvo].sort_values("Mes")
    for _, r in dsku.iterrows():
        print(f"    {r['NomeMes']} ({r['Fase']}): {r['DemandaMensalProjetada']:.2f}")
    print(f"  Demanda anual projetada: {dsku['DemandaMensalProjetada'].sum():.1f}")

    # ETAPA 3 — Política order-up-to (todas as rodadas)
    linha("ETAPA 3 — POLÍTICA ORDER-UP-TO (projeção forward)")
    cfg_d = config.get("demanda", {})
    print(f"\n  Nível de serviço: alta {cfg_d.get('nivel_servico_alta', 99)}% / "
          f"baixa {cfg_d.get('nivel_servico_baixa', 92)}% | Variação da Demanda {cfg_d.get('variacao_demanda', 0.25)}")

    est = dados["estoque"]
    est_rede = est[est["ID_produto"] == id_prod]["saldoFisico"].sum()
    print(f"  Estoque atual da rede: {est_rede:.0f} peças")

    sim = demanda.simular_politica_reabastecimento(dados, config, dem=dem)
    s_sku = sim[sim["SKU"] == sku_alvo].sort_values("data_chegada")
    if len(s_sku) == 0:
        print("\n  ⚠️ Nenhuma rodada no horizonte.")
        return

    for _, r in s_sku.iterrows():
        sub(f"Rodada {int(r['rodada'])} — disparo {demanda.NOMES_MES[int(r['mes_disparo'])-1]}/{int(r['ano_disparo'])} "
            f"→ chega {pd.Timestamp(r['data_chegada']).strftime('%b/%Y')}")
        ns = cfg_d.get("nivel_servico_alta", 99) if r["contem_alta"] else cfg_d.get("nivel_servico_baixa", 92)
        print(f"    Estoque projetado na chegada: {r['EstoqueProjetado']:.1f}")
        print(f"    Demanda do período (cobre até a próxima rodada): {r['DemandaPeriodo']:.1f}")
        print(f"    Estoque de segurança (nível de serviço {ns}%): {r['EstoqueSeguranca']:.1f}")
        print(f"    Estoque-alvo = Demanda do período + Estoque de segurança = {r['EstoqueAlvo']:.1f}")
        print(f"    Pedido = arredonda_par(máx(Estoque-alvo − Estoque projetado, 0)) = {int(r['Pedido'])} pares")

    total = s_sku["Pedido"].sum()
    print(f"\n  ┌────────────────────────────────────────────┐")
    print(f"  │  PEDIDO TOTAL NO ANO = {total:>5.0f} pares          │")
    print(f"  └────────────────────────────────────────────┘")


if __name__ == "__main__":
    main()
