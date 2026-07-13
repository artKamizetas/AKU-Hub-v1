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
