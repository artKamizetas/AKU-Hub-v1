"""
Testes do ETL Comercial (etl/daily.py) com metas escalonadas.

Usa competência FIXA ("2026-03", as fixtures dados_daily/config_daily do
conftest) — o default do módulo é o mês corrente, que não serviria para
teste determinístico. Cobre: filtro de situação de venda, desconto do
pedido + do item, metas por loja, linha TOTAL agregada, rateio da meta do
vendedor e o caminho do mês fechado.
"""

import pandas as pd

from etl.daily import processar_daily


COMP = "2026-03"


def _linha(df, loja):
    return df[df["Loja"] == loja].iloc[0]


# ---------------------------------------------------------------------
# Detalhado
# ---------------------------------------------------------------------

def test_detalhado_calcula_valor_liquido_de_desconto(dados_daily, config_daily):
    det, _, _ = processar_daily(dados_daily, config_daily, competencia=COMP)

    p1 = det[det["ID_pedido"] == "P1"].iloc[0]
    assert p1["Valor Bruto"] == 1000.0   # 2 x 500
    assert p1["Valor"] == 1000.0

    p2 = det[det["ID_pedido"] == "P2"].iloc[0]
    assert p2["Valor Bruto"] == 1200.0   # 3 x 400
    assert p2["Desconto"] == 200.0
    assert p2["Valor"] == 1000.0         # bruto - desconto


def test_detalhado_traz_todos_os_pedidos_inclusive_nao_atendidos(dados_daily, config_daily):
    det, _, _ = processar_daily(dados_daily, config_daily, competencia=COMP)
    assert len(det) == 4                 # P4 (em aberto) está no detalhado
    assert set(det["ID_pedido"]) == {"P1", "P2", "P3", "P4"}


# ---------------------------------------------------------------------
# Metas por loja
# ---------------------------------------------------------------------

def test_metas_loja_ignora_pedido_fora_das_situacoes_de_venda(dados_daily, config_daily):
    _, loja, _ = processar_daily(dados_daily, config_daily, competencia=COMP)
    natal = _linha(loja, "Natal")
    # P4 (situação 6, 5 peças x 100) NÃO entra: só P1 (1000) + P2 (1000)
    assert natal["Vendido"] == 2000.0
    assert natal["Pecas"] == 5           # 2 + 3, sem as 5 do P4
    assert natal["Pedidos"] == 2


def test_metas_loja_classifica_nivel_e_falta(dados_daily, config_daily):
    _, loja, _ = processar_daily(dados_daily, config_daily, competencia=COMP)
    natal = _linha(loja, "Natal")
    # Vendido 2000, metas 1000/2000/3000 -> bateu exatamente o Ouro
    assert natal["Meta Prata"] == 1000.0
    assert natal["Meta Ouro"] == 2000.0
    assert natal["Nivel"] == "Ouro"
    assert natal["Proximo Nivel"] == "Diamante"
    assert natal["Falta Proximo"] == 1000.0
    assert natal["Origem Meta"] == "configurada"


def test_metas_loja_calcula_pa(dados_daily, config_daily):
    _, loja, _ = processar_daily(dados_daily, config_daily, competencia=COMP)
    natal = _linha(loja, "Natal")
    assert natal["PA"] == 2.5            # 5 peças / 2 pedidos
    assert natal["PA Nivel"] == "Diamante"   # metas PA 1.5/2.0/2.5


def test_metas_loja_sem_meta_cadastrada_cai_no_ausente(dados_daily, config_daily):
    _, loja, _ = processar_daily(dados_daily, config_daily, competencia=COMP)
    mossoro = _linha(loja, "Mossoró")
    # Mossoró não tem metas_mensais nem entrada no legado daily.metas
    assert mossoro["Origem Meta"] == "ausente"
    # Numérico sem meta -> NaN (célula vazia); texto sem nível -> "" (nunca NaN)
    assert pd.isna(mossoro["Meta Ouro"])
    assert mossoro["Nivel"] == ""
    assert mossoro["Vendido"] == 300.0   # o realizado continua sendo apurado


def test_metas_loja_fallback_legado_em_mes_sem_cadastro(dados_daily, config_daily):
    _, loja, _ = processar_daily(dados_daily, config_daily, competencia="2026-04")
    natal = _linha(loja, "Natal")
    assert natal["Origem Meta"] == "estimada"
    assert natal["Meta Ouro"] == 10000.0     # daily.metas legado
    assert natal["Vendido"] == 0.0           # não há venda em abril


# ---------------------------------------------------------------------
# Linha TOTAL
# ---------------------------------------------------------------------

def test_linha_total_soma_faturamento_e_pondera_pa(dados_daily, config_daily):
    _, loja, _ = processar_daily(dados_daily, config_daily, competencia=COMP)
    total = _linha(loja, "TOTAL")
    assert total["Vendido"] == 2300.0    # Natal 2000 + Mossoró 300
    assert total["Pecas"] == 6           # 5 + 1
    assert total["Pedidos"] == 3
    # PA agregado = 6/3 = 2.0, NÃO a média de 2.5 (Natal) e 1.0 (Mossoró) = 1.75
    assert total["PA"] == 2.0


def test_linha_total_agrega_meta_apenas_das_lojas_com_meta(dados_daily, config_daily):
    _, loja, _ = processar_daily(dados_daily, config_daily, competencia=COMP)
    total = _linha(loja, "TOTAL")
    # Só Natal tem meta; Mossoró (sem meta) não contribui
    assert total["Meta Ouro"] == 2000.0


# ---------------------------------------------------------------------
# Metas por vendedor (rateio)
# ---------------------------------------------------------------------

def test_meta_vendedor_e_rateada_da_loja(dados_daily, config_daily):
    _, _, vend = processar_daily(dados_daily, config_daily, competencia=COMP)
    v1 = vend[vend["VendedorID"] == "V1"].iloc[0]
    v2 = vend[vend["VendedorID"] == "V2"].iloc[0]

    # V1 e V2 têm peso 1.0 cada -> metade da meta da loja Natal (2000 ouro)
    assert v1["Meta Ouro"] == 1000.0
    assert v2["Meta Ouro"] == 1000.0
    # PA não se rateia: repete a meta da loja
    assert v1["PA Meta Ouro"] == 2.0

    # V1 vendeu 1000 (P1) -> bateu exatamente sua meta Ouro rateada
    assert v1["Vendido"] == 1000.0
    assert v1["Nivel"] == "Ouro"


def test_soma_das_metas_dos_vendedores_fecha_a_meta_da_loja(dados_daily, config_daily):
    _, loja, vend = processar_daily(dados_daily, config_daily, competencia=COMP)
    natal_meta = _linha(loja, "Natal")["Meta Ouro"]
    soma_vend = vend[vend["Loja"] == "Natal"]["Meta Ouro"].sum()
    assert abs(soma_vend - natal_meta) < 1e-9


def test_vendedor_sem_atribuicao_aparece_sem_meta(dados_daily, config_daily):
    _, _, vend = processar_daily(dados_daily, config_daily, competencia=COMP)
    v3 = vend[vend["VendedorID"] == "V3"].iloc[0]
    assert v3["Loja"] == "Sem atribuição"
    assert pd.isna(v3["Meta Ouro"])
    assert v3["Vendido"] == 300.0        # o realizado continua visível


def test_vendedor_atribuido_sem_venda_ainda_aparece(dados_daily, config_daily):
    """Quem tem meta mas não vendeu precisa aparecer com 0 — senão some do
    ranking justamente quem mais precisa de atenção."""
    _, _, vend = processar_daily(dados_daily, config_daily, competencia="2026-04")
    v1 = vend[vend["VendedorID"] == "V1"].iloc[0]
    assert v1["Vendido"] == 0.0
    assert v1["Loja"] == "Natal"


# ---------------------------------------------------------------------
# Competência
# ---------------------------------------------------------------------

def test_mes_fechado_usa_mes_inteiro_no_run_rate(dados_daily, config_daily):
    """Março/2026 é passado: run rate == realizado (o mês não tem mais o que
    projetar), diferente do mês corrente onde extrapola pelos dias decorridos."""
    _, loja, _ = processar_daily(dados_daily, config_daily, competencia=COMP)
    natal = _linha(loja, "Natal")
    assert natal["Run Rate"] == natal["Vendido"]


def test_competencia_default_usa_mes_corrente(dados_daily, config_daily):
    _, loja, _ = processar_daily(dados_daily, config_daily)
    natal = _linha(loja, "Natal")
    hoje = pd.Timestamp.now()
    # As vendas da fixture são de mar/2026; só coincide se hoje for mar/2026
    esperado = 2000.0 if (hoje.year, hoje.month) == (2026, 3) else 0.0
    assert natal["Vendido"] == esperado
