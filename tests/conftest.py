"""
Fixtures compartilhadas da suíte.

Estratégia p/ determinismo: o motor de demanda ancora na "última temporada de
alta completa" relativa a `pd.Timestamp.now()` (não aceita data_hoje). Então os
dados sintéticos preenchem TODAS as altas dos últimos anos com a mesma
quantidade — qualquer que seja a temporada escolhida, o total ancorado é o
mesmo. O crescimento é desligado (aplicar_crescimento_fabrica=False) e a
proporção da baixa é fixada por override de colégio, deixando a demanda
projetada previsível a partir dos números de entrada.
"""

import pandas as pd
import pytest


# IDs dos produtos usados nas fixtures
ID_A = "101"   # SKU normal, vende na alta
ID_B = "102"   # SKU só de baixa (sem venda na alta)


@pytest.fixture
def hoje():
    return pd.Timestamp.now().normalize()


@pytest.fixture
def config():
    """Config no formato do config.yaml, com crescimento OFF p/ determinismo."""
    return {
        "demanda": {
            "janela_alta": [12, 1, 2],
            "nivel_servico_alta": 99,
            "nivel_servico_baixa": 92,
            "variacao_demanda": 0.25,
            "min_vendas_colegio": 30,
            "min_meses_colegio": 6,
            "aplicar_crescimento_fabrica": False,
            "crescimento_observado_ativo": False,
        },
        "fabrica": {
            "crescimento_pct": 10.0,
            "cobertura_meses": 2,
            "correcao_manual": 0,
            "situacoes_backlog": [6, 15],
        },
        "planejamento": {
            "rodadas_datas": [],
            "lead_time_semanas": 4,
            # Janela histórica ampla e relativa a hoje, cobrindo altas e baixas
            "periodo_historico_inicio": (
                pd.Timestamp.now().normalize() - pd.DateOffset(months=24)
            ).strftime("%Y-%m-%d"),
            "periodo_historico_fim": pd.Timestamp.now().normalize().strftime("%Y-%m-%d"),
        },
        # Colégio "COL" com proporção da baixa fixa → demanda de baixa determinística
        "colegios": {
            "COL": {"proporcao_baixa": 0.5},
        },
        "excecoes_sku": {},
        "grupo_segmento": {"EME": "Médio", "EDF": "Ed. Física"},
    }


@pytest.fixture
def dados(hoje):
    """
    dict de DataFrames sintético mínimo aceito pelo motor de demanda.

    SKU A-PP (ID 101, colégio COL, grupo EME): 10 peças em cada mês de alta
      (Dez/Jan/Fev) de vários anos → âncora = 30, independente de qual temporada
      completa o motor escolher.
    SKU B-PP (ID 102, colégio COL, grupo EDF): 8 peças em Junho do ano passado
      (mês de baixa, dentro da janela histórica), zero na alta → exercita o
      fallback "SKU só de baixa".
    """
    linhas = []
    # Altas: Dez/Jan/Fev de uma faixa ampla de anos, sempre 10 peças p/ o A
    for ano in range(hoje.year - 6, hoje.year + 1):
        for mes in (12, 1, 2):
            linhas.append((ID_A, pd.Timestamp(year=ano, month=mes, day=15), 10, "PED-ALTA"))
    # Baixa: SKU B em Junho do ano passado (dentro da janela histórica)
    linhas.append((ID_B, pd.Timestamp(year=hoje.year - 1, month=6, day=10), 8, "PED-BAIXA"))
    # Um pouco de venda de baixa da empresa (Junho) p/ a sazonalidade não zerar
    linhas.append((ID_A, pd.Timestamp(year=hoje.year - 1, month=6, day=10), 4, "PED-BAIXA"))

    itens = pd.DataFrame(linhas, columns=["ID_produto", "Data", "Quantidade", "ID_pedido"])

    produtos = pd.DataFrame({
        "ID": [ID_A, ID_B],
        "codigo": ["A-PP", "B-PP"],
        "preco_custo": [100.0, 40.0],
    })

    detalhes = pd.DataFrame({
        "ID_produto": [ID_A, ID_B],
        "Marca_sku": ["COL", "COL"],
        "Grupo": ["EME", "EDF"],
        "categoria": ["Camisa", "Camisa"],
        "Super_categoria": ["Uniforme", "Uniforme"],
    })

    estoque = pd.DataFrame({
        "ID_produto": [ID_A, ID_B],
        "saldoFisico": [0, 0],
    })

    pedidos = pd.DataFrame({
        "ID": ["PED-ALTA", "PED-BAIXA"],
        "id_situacao": [9, 9],   # 9 = atendido, não é backlog
    })

    return {
        "itens": itens,
        "produtos": produtos,
        "detalhes": detalhes,
        "estoque": estoque,
        "pedidos": pedidos,
    }


# =====================================================================
# Fixtures do COMERCIAL (etl/daily.py) — independentes das de demanda:
# o Daily precisa de colunas que o motor de PCP não usa (Vendedor, Loja
# ID, Valor Unidade, Desconto) e de metas escalonadas no config.
# =====================================================================

LOJA_A = "900001"    # "Natal" na config_daily
LOJA_B = "900002"    # "Mossoró"


@pytest.fixture
def config_daily():
    """Config mínimo do Comercial, com metas escalonadas em 2026-03."""
    return {
        "daily": {
            "situacoes_venda": [9],
            "status_ids": {"em_aberto": 6, "em_andamento": 15, "pronto_retirada": 28488},
            "metas": {"Natal": 10000.0},           # legado → fallback
            "metas_mensais": {
                "2026-03": {
                    "Natal": {
                        "faturamento": {"prata": 1000, "ouro": 2000, "diamante": 3000},
                        "pa": {"prata": 1.5, "ouro": 2.0, "diamante": 2.5},
                    },
                },
            },
            "vendedores_loja": {
                "2026-03": {
                    "V1": {"loja": "Natal", "peso": 1.0, "ativo": True},
                    "V2": {"loja": "Natal", "peso": 1.0, "ativo": True},
                },
            },
        },
        "depositos": {
            "lojas": [
                {"nome": "Natal", "loja_id": LOJA_A, "deposito_id": "1"},
                {"nome": "Mossoró", "loja_id": LOJA_B, "deposito_id": "2"},
            ],
        },
        "colegios_alias": {},
    }


@pytest.fixture
def dados_daily():
    """
    Vendas de Março/2026 (competência fixa → determinismo):
      P1 · Natal · V1 · 2 peças × R$ 500 = 1000, sem desconto
      P2 · Natal · V2 · 3 peças × R$ 400 = 1200, desconto 200 → 1000
      P3 · Mossoró · V3 · 1 peça × R$ 300 = 300
      P4 · Natal · V1 · situação 6 (em aberto) → NÃO conta como venda
    Natal (vendas efetivas): R$ 2000, 5 peças, 2 pedidos → PA 2.5
    """
    pedidos = pd.DataFrame({
        "ID": ["P1", "P2", "P3", "P4"],
        "Pedido": ["1", "2", "3", "4"],
        "id_situacao": [9, 9, 9, 6],
        "Vendedor": ["V1", "V2", "V3", "V1"],
        "Loja ID": [LOJA_A, LOJA_A, LOJA_B, LOJA_A],
        "Data": [pd.Timestamp("2026-03-10")] * 4,
        "Cliente": ["C1", "C2", "C3", "C4"],
        "Desconto": [0.0, 200.0, 0.0, 0.0],
    })

    itens = pd.DataFrame({
        "ID_pedido": ["P1", "P2", "P3", "P4"],
        "ID_produto": [ID_A, ID_A, ID_B, ID_A],
        "Quantidade": [2, 3, 1, 5],
        "Valor Unidade": [500.0, 400.0, 300.0, 100.0],
        "Desconto Item": [0.0, 0.0, 0.0, 0.0],
    })

    detalhes = pd.DataFrame({
        "ID_produto": [ID_A, ID_B],
        "Marca_sku": ["COL", "OUTRO"],
        "Grupo": ["EME", "EDF"],
        "categoria": ["Camisa", "Camisa"],
        "Super_categoria": ["Uniforme", "Uniforme"],
    })

    vendedores = pd.DataFrame({
        "ID": ["V1", "V2", "V3"],
        "nome": ["Ana", "Bruno", "Carla"],
    })

    lojas = pd.DataFrame({
        "ID": [LOJA_A, LOJA_B],
        "descricao": ["Loja Natal", "Loja Mossoró"],
    })

    situacoes = pd.DataFrame({
        "ID": [9, 6, 15],
        "descricao": ["Atendido", "Em aberto", "Em andamento"],
    })

    return {
        "pedidos": pedidos,
        "itens": itens,
        "detalhes": detalhes,
        "vendedores": vendedores,
        "lojas": lojas,
        "situacoes": situacoes,
    }
