"""
memoria_calculo.py — Memória de Cálculo do Estoque-Alvo da loja

Mostra passo a passo como o alvo de um SKU foi formado em cada loja
(Reposição de Loja v2 — docs/requisitos/reposicao-loja-v2.md).

Uso (a partir da raiz do projeto):
    python scripts/memoria_calculo.py NEV020CAMEDF-PP              # hoje
    python scripts/memoria_calculo.py NEV020CAMEDF-PP 2027-01-12   # simulando uma data
"""

import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
from etl import demanda, reposicao
from etl.loader import carregar_dados, carregar_config
from etl.logistica import processar_logistica


def linha(titulo):
    print(f"\n{'=' * 70}")
    print(f"  {titulo}")
    print(f"{'=' * 70}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    sku = sys.argv[1]
    hoje = (pd.Timestamp(sys.argv[2]) if len(sys.argv) > 2 else pd.Timestamp.now()).normalize()

    config = carregar_config()   # yaml (defaults) + app.parametros (Supabase)
    dados = carregar_dados()

    produtos = dados["produtos"]
    match = produtos[produtos["codigo"] == sku]
    if len(match) == 0:
        print(f"SKU '{sku}' não encontrado entre os produtos ativos.")
        return
    id_prod = str(match.iloc[0]["ID"]).strip()
    params = reposicao.parametros(config)
    fase = reposicao.fase_atual(config, hoje)

    linha(f"MEMÓRIA DE CÁLCULO — {sku} — {hoje.strftime('%d/%m/%Y')} ({fase})")
    print(f"  Produto: {match.iloc[0]['Descricao']}")

    # ---------------- 1. Demanda da rede ----------------
    linha("1 — DEMANDA DA REDE (motor do Simulador: última alta × crescimento)")
    dem = demanda.calcular_demanda_mensal_por_sku(
        dados, config, ativo_crescimento=bool(params["aplicar_crescimento"]))
    dem = dem[dem["ID_produto"] == id_prod]
    por_mes = dict(zip(dem["Mes"].astype(int), dem["DemandaMensalProjetada"]))
    if len(dem) == 0:
        print("  Sem demanda projetada (produto sem venda na rede).")
    else:
        print(f"  Colégio: {dem['Colegio'].iloc[0]} | Crescimento aplicado: {dem['TaxaCrescimento'].iloc[0]:.2f}×")
        for mes in range(1, 13):
            fase_mes = dem.loc[dem["Mes"] == mes, "Fase"].iloc[0]
            print(f"    {demanda.NOMES_MES[mes - 1]}: {por_mes.get(mes, 0):7.2f}  ({fase_mes})")
        print(f"  Total do ano: {sum(por_mes.values()):.1f} peças")

    # ---------------- 2. Por loja ----------------
    participacao, pa = reposicao.participacao_por_loja(dados, config)
    df = processar_logistica(dados, config, data_hoje=hoje)
    for loja_cfg in config["depositos"]["lojas"]:
        nome, id_loja = loja_cfg["nome"], str(loja_cfg["loja_id"]).strip()
        linha(f"2 — LOJA {nome.upper()}")
        fatia = participacao.get((id_prod, id_loja), 0.0)
        janela = reposicao.janela_protecao_dias(params, fase, nome)
        fracoes = demanda.fracionar_janela_por_mes(hoje, hoje + timedelta(days=janela))
        print(f"  Participação da loja na venda do SKU: {fatia:.1%}")
        print(f"  Janela de proteção: {janela} dias "
              f"(cobertura da {fase} + prazo de entrega)")
        for mes, fracao in fracoes:
            print(f"    {demanda.NOMES_MES[mes - 1]}: {por_mes.get(mes, 0):.2f} × {fatia:.3f} "
                  f"× {fracao:.3f} do mês = {por_mes.get(mes, 0) * fatia * fracao:.2f}")

        res = df[(df["Loja"] == nome) & (df["SKU"] == sku)]
        if len(res) == 0:
            print("  → Fora da fila: colégio fora do sortimento (ou produto sem venda) e sem saldo na loja.")
            continue
        r = res.iloc[0]
        print(f"  Demanda da janela:  {r['DemandaJanela']:.2f} peças")
        print(f"  Segurança:          Fator de Serviço ({r['NivelServico']}%) × √({r['DemandaJanela']:.2f} × PA {r['PA']:.2f})"
              f" = {r['Seguranca']:.2f}")
        print(f"  Alvo ideal:         max(exposição {int(params['exposicao_minima'])}, "
              f"⌈{r['DemandaJanela']:.2f} + {r['Seguranca']:.2f}⌉) = {int(r['AlvoIdeal'])}")
        print(f"  Espaço:             modelo {r['Modelo']} com {int(r['Gavetas'])} gaveta(s)"
              f"{' — LIMITADO' if r['LimitadoPorEspaco'] else ''}")
        print(f"  ALVO:               {int(r['Alvo'])}")
        print(f"  Estoque da loja:    {r['EstoqueLoja']:.0f} | Estoque do CD: {r['EstoqueCentral']:.0f}")
        print(f"  Necessidade:        {int(r['Necessidade'])} → Separar {int(r['Separar'])} · "
              f"Falta {int(r['Falta'])} · Excesso {int(r['Excesso'])}")
        print(f"  AÇÃO:               {r['Acao']}  {r['MotivoExcesso']}")


if __name__ == "__main__":
    main()
