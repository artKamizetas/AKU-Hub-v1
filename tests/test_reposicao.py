"""
Testes da Reposição de Loja v2: regras puras (etl/reposicao.py) e o
orquestrador (etl/logistica.py::processar_logistica).

As fixtures `dados_reposicao`/`config_reposicao` (conftest) preenchem todas as
altas com a mesma venda e desligam o crescimento, então a demanda de Janeiro é
previsível qualquer que seja a temporada que o motor ancorar.
"""

import math

import pandas as pd
import pytest

from etl import logistica, reposicao as r
from tests.conftest import (
    REP_CAM_P, REP_CAL_P, REP_OUT_P, REP_NATAL, REP_MOSSORO,
    REP_DEP_NATAL, REP_DEP_MOSSORO, REP_DEP_CD,
)


def _linha(df, loja, sku):
    return df[(df["Loja"] == loja) & (df["SKU"] == sku)].iloc[0]


def _com_estoque(dados, saldos):
    """Copia `dados` trocando/adicionando saldos {(id_produto, id_deposito): qtd}."""
    est = dados["estoque"].copy()
    for (idp, dep), qtd in saldos.items():
        mask = (est["ID_produto"] == idp) & (est["ID_deposito"] == dep)
        if mask.any():
            est.loc[mask, "saldoFisico"] = qtd
        else:
            est = pd.concat([est, pd.DataFrame(
                {"ID_produto": [idp], "ID_deposito": [dep], "saldoFisico": [qtd]})], ignore_index=True)
    return {**dados, "estoque": est}


@pytest.fixture
def pico(hoje):
    """10 de janeiro: dentro da alta, janela inteira dentro do mês."""
    return pd.Timestamp(year=hoje.year, month=1, day=10)


# ---------------------------------------------------------------------------
# Parâmetros
# ---------------------------------------------------------------------------
class TestParametros:
    def test_defaults_sem_bloco(self):
        p = r.parametros({})
        assert p["exposicao_minima"] == 2
        assert p["lojas"] == {} and p["sortimento"] == {} and p["capacidade_gaveta"] == {}

    def test_legado_vm_vale_enquanto_reposicao_nao_define(self):
        p = r.parametros({"vm": {"nivel_servico_default": 99, "aplicar_crescimento": False,
                                 "dias_cobertura": 40}})
        assert p["nivel_servico_default"] == 99
        assert p["aplicar_crescimento"] is False
        assert p["cobertura_dias_baixa"] == 15          # dias_cobertura antigo NÃO migra

    def test_reposicao_vence_o_legado(self):
        p = r.parametros({"vm": {"nivel_servico_default": 99},
                          "reposicao": {"nivel_servico_default": 90}})
        assert p["nivel_servico_default"] == 90

    def test_janela_soma_cobertura_e_prazo(self, config_reposicao):
        p = r.parametros(config_reposicao)
        assert r.janela_protecao_dias(p, r.FASE_ALTA, "Natal") == 4
        assert r.janela_protecao_dias(p, r.FASE_ALTA, "Mossoró") == 5
        assert r.janela_protecao_dias(p, r.FASE_BAIXA, "Natal") == 16
        assert r.janela_protecao_dias(p, r.FASE_BAIXA, "Loja nova") == 15

    def test_fase_segue_a_janela_do_simulador(self, config_reposicao):
        assert r.fase_atual(config_reposicao, "2026-01-10") == r.FASE_ALTA
        assert r.fase_atual(config_reposicao, "2026-12-01") == r.FASE_ALTA
        assert r.fase_atual(config_reposicao, "2026-10-09") == r.FASE_BAIXA

    def test_capacidade_por_super_categoria(self):
        p = r.parametros({"reposicao": {"capacidade_gaveta": {"_padrao": 50, "Calça": 30}}})
        assert r.capacidade_da_gaveta(p, "Calça") == 30
        assert r.capacidade_da_gaveta(p, "Camiseta") == 50
        assert r.capacidade_da_gaveta(r.parametros({}), "Qualquer") == 50

    def test_gavetas_none_e_sem_cadastro(self):
        p = r.parametros({"reposicao": {"lojas": {"Natal": {"gavetas": 20}, "Mossoró": {}}}})
        assert r.gavetas_da_loja(p, "Natal") == 20
        assert r.gavetas_da_loja(p, "Mossoró") is None


# ---------------------------------------------------------------------------
# Demanda por loja
# ---------------------------------------------------------------------------
class TestDemandaPorLoja:
    def test_institucional_fica_fora(self, dados_reposicao, config_reposicao):
        part, pa = r.participacao_por_loja(dados_reposicao, config_reposicao)
        # CAM-P: rede 124/mês = Natal 62 + Mossoró 31 + institucional 31
        assert part[(REP_CAM_P, REP_NATAL)] == pytest.approx(0.5)
        assert part[(REP_CAM_P, REP_MOSSORO)] == pytest.approx(0.25)
        soma_lojas = part[(REP_CAM_P, REP_NATAL)] + part[(REP_CAM_P, REP_MOSSORO)]
        assert soma_lojas == pytest.approx(0.75)        # o institucional não é de loja nenhuma
        assert pa[REP_CAM_P] == pytest.approx(2.0, abs=0.05)

    def test_loja_sem_venda_nao_tem_participacao(self, dados_reposicao, config_reposicao):
        part, _ = r.participacao_por_loja(dados_reposicao, config_reposicao)
        assert (REP_OUT_P, REP_MOSSORO) not in part
        assert part[(REP_OUT_P, REP_NATAL)] == pytest.approx(1.0)

    def test_janela_cruza_o_mes(self):
        from etl import demanda
        fracoes = demanda.fracionar_janela_por_mes(pd.Timestamp("2026-12-30"), pd.Timestamp("2027-01-03"))
        # 2 dias de dezembro (31) + 2 de janeiro (31)
        assert r.demanda_da_janela({12: 31.0, 1: 310.0}, fracoes) == pytest.approx(2 + 20)

    def test_seguranca_cresce_com_raiz_e_com_pa(self):
        base = r.seguranca_loja(4.0, 1.0, 95)
        assert base == pytest.approx(1.65 * 2.0)
        assert r.seguranca_loja(16.0, 1.0, 95) == pytest.approx(2 * base)
        assert r.seguranca_loja(4.0, 4.0, 95) == pytest.approx(2 * base)
        assert r.seguranca_loja(0.0, 3.0, 99) == 0.0

    def test_alvo_ideal_nunca_abaixo_da_exposicao(self):
        assert r.alvo_ideal(0.0, 0.0, 2) == 2
        assert r.alvo_ideal(8.0, 6.6, 2) == 15
        assert r.alvo_ideal(3.0, 0.0, 2) == 3           # inteiro exato não sobe


# ---------------------------------------------------------------------------
# Sortimento
# ---------------------------------------------------------------------------
class TestSortimento:
    def test_cadastro_vence_e_falta_cai_nas_vendas(self):
        p = r.parametros({"reposicao": {"sortimento": {"Natal": ["A", " B "]}}})
        sort, origem = r.sortimento_efetivo(p, ["Natal", "Mossoró"], {"Natal": {"Z"}, "Mossoró": {"C"}})
        assert sort == {"Natal": {"A", "B"}, "Mossoró": {"C"}}
        assert origem == {"Natal": r.ORIGEM_CADASTRO, "Mossoró": r.ORIGEM_VENDAS}

    def test_lista_vazia_cadastrada_e_respeitada(self):
        p = r.parametros({"reposicao": {"sortimento": {"Natal": ["A"], "Mossoró": []}}})
        sort, origem = r.sortimento_efetivo(p, ["Natal", "Mossoró"], {"Mossoró": {"C"}})
        assert sort["Mossoró"] == set() and origem["Mossoró"] == r.ORIGEM_CADASTRO

    def test_derivado_exige_volume_minimo(self, dados_reposicao, config_reposicao, pico):
        # OUT vende 31/mês só em Natal; em Mossoró nunca
        vendidos = r.colegios_vendidos_por_loja(dados_reposicao, config_reposicao, pico)
        assert vendidos == {"Natal": {"COL", "OUT"}, "Mossoró": {"COL"}}
        alto = r.colegios_vendidos_por_loja(dados_reposicao, config_reposicao, pico, min_pecas=10_000)
        assert alto == {"Natal": set(), "Mossoró": set()}


# ---------------------------------------------------------------------------
# Gavetas
# ---------------------------------------------------------------------------
class TestGavetas:
    def test_vai_para_quem_tem_mais_fundo(self):
        g = r.distribuir_gavetas({"A": 80, "B": 40, "C": 10}, {"A": 50, "B": 50, "C": 50}, 2)
        assert g == {"A": (1, 50), "B": (1, 40), "C": (0, 0)}

    def test_campeao_leva_mais_de_uma(self):
        g = r.distribuir_gavetas({"A": 120, "B": 20}, {"A": 50, "B": 50}, 2)
        assert g == {"A": (2, 100), "B": (0, 0)}

    def test_sem_cadastro_nao_tem_teto(self):
        g = r.distribuir_gavetas({"A": 120, "B": 20}, {"A": 50, "B": 50}, None)
        assert g == {"A": (3, 120), "B": (1, 20)}

    def test_zero_gavetas_ninguem_recebe(self):
        assert r.distribuir_gavetas({"A": 120}, {"A": 50}, 0) == {"A": (0, 0)}

    def test_capacidade_por_modelo(self):
        # a calça cabe 30: a 1ª gaveta dela cobre menos que a da camiseta
        g = r.distribuir_gavetas({"CAM": 50, "CAL": 50}, {"CAM": 50, "CAL": 30}, 2)
        assert g == {"CAM": (1, 50), "CAL": (1, 30)}

    def test_nunca_passa_do_cadastrado(self):
        fundo = {f"M{i}": 30 + i for i in range(40)}
        g = r.distribuir_gavetas(fundo, {m: 50 for m in fundo}, 20)
        assert sum(n for n, _ in g.values()) == 20

    def test_teto_coberto_devolve_o_ideal(self):
        assert r.aplicar_teto_espaco({"P": 10, "M": 6}, 2, 12) == {"P": 10, "M": 6}

    def test_teto_parcial_reduz_proporcional_e_fecha_a_soma(self):
        # fundo: P 8, M 4 (total 12); cabem 6 → P 4, M 2
        alvos = r.aplicar_teto_espaco({"P": 10, "M": 6}, 2, 6)
        assert alvos == {"P": 6, "M": 4}
        # cabem 5 → cotas 3,33 e 1,67: a sobra vai para o maior resto (M)
        alvos = r.aplicar_teto_espaco({"P": 10, "M": 6}, 2, 5)
        assert alvos == {"P": 5, "M": 4}
        assert sum(alvos.values()) == 2 + 2 + 5

    def test_sem_gaveta_fica_na_exposicao(self):
        assert r.aplicar_teto_espaco({"P": 10, "M": 6}, 2, 0) == {"P": 2, "M": 2}


# ---------------------------------------------------------------------------
# Rateio do CD e excesso
# ---------------------------------------------------------------------------
class TestRateio:
    def test_cd_cobre_todo_mundo(self):
        assert r.ratear_cd({"N": 5, "M": 3}, {"N": 0, "M": 0}, {"N": 1, "M": 1}, 8) == {"N": 5, "M": 3}

    def test_nunca_promete_o_mesmo_estoque_duas_vezes(self):
        # o bug do motor antigo: CD com 5 e as duas lojas recebiam 5
        aloc = r.ratear_cd({"N": 5, "M": 5}, {"N": 0, "M": 0}, {"N": 1, "M": 1}, 5)
        assert sum(aloc.values()) == 5

    def test_recebe_quem_zera_primeiro(self):
        # N tem 4 dias de cobertura (8 ÷ 2), M tem 0: as 3 peças vão para M
        aloc = r.ratear_cd({"N": 6, "M": 6}, {"N": 8, "M": 0}, {"N": 2.0, "M": 1.0}, 3)
        assert aloc == {"N": 0, "M": 3}

    def test_equaliza_a_cobertura(self):
        # M sobe até alcançar os 2 dias de N; depois alternam
        aloc = r.ratear_cd({"N": 10, "M": 10}, {"N": 4, "M": 0}, {"N": 2.0, "M": 1.0}, 5)
        assert aloc["M"] >= 2 and sum(aloc.values()) == 5
        cobertura = {"N": (4 + aloc["N"]) / 2.0, "M": aloc["M"] / 1.0}
        assert abs(cobertura["N"] - cobertura["M"]) <= 1.0

    def test_loja_sem_venda_prevista_fica_por_ultimo(self):
        aloc = r.ratear_cd({"N": 2, "M": 2}, {"N": 0, "M": 0}, {"N": 0.0, "M": 0.5}, 2)
        assert aloc == {"N": 0, "M": 2}

    def test_saldo_negativo_do_cd_nao_aloca(self):
        assert r.ratear_cd({"N": 2}, {"N": 0}, {"N": 1}, -3) == {"N": 0}

    def test_excesso_respeita_o_horizonte(self):
        # 20 na loja, alvo 4, mas vende 15 no horizonte → só 5 são excesso
        assert r.excesso_loja(20, 4, 15.0, True) == 5
        assert r.excesso_loja(20, 4, 30.0, True) == 0
        assert r.excesso_loja(3, 4, 0.0, True) == 0

    def test_fora_do_sortimento_tudo_e_excesso(self):
        assert r.excesso_loja(7, 0, 50.0, False) == 7
        assert r.excesso_loja(-2, 0, 0.0, False) == 0


# ---------------------------------------------------------------------------
# Orquestrador
# ---------------------------------------------------------------------------
class TestProcessarLogistica:
    def test_colunas_e_fase(self, dados_reposicao, config_reposicao, pico):
        df = logistica.processar_logistica(dados_reposicao, config_reposicao, data_hoje=pico)
        assert list(df.columns) == logistica.COLUNAS
        assert set(df["Fase"]) == {r.FASE_ALTA}

    def test_alvo_do_pico_na_loja(self, dados_reposicao, config_reposicao, pico):
        df = logistica.processar_logistica(dados_reposicao, config_reposicao, data_hoje=pico)
        natal = _linha(df, "Natal", "CAM-P")
        # Janeiro em Natal = 62 peças → 2/dia × janela de 4 dias (3 + prazo 1)
        assert natal["JanelaDias"] == 4
        assert natal["DemandaJanela"] == pytest.approx(8.0)
        seguranca = 1.65 * math.sqrt(8.0 * natal["PA"])
        assert natal["Seguranca"] == pytest.approx(seguranca, abs=0.01)
        assert natal["AlvoIdeal"] == math.ceil(8.0 + seguranca)
        assert natal["Alvo"] == natal["AlvoIdeal"]      # sem gavetas cadastradas = sem teto
        assert natal["Separar"] == natal["Alvo"] and natal["Falta"] == 0
        assert natal["Acao"] == logistica.ACAO_REPOR

    def test_cada_loja_tem_a_sua_demanda(self, dados_reposicao, config_reposicao, pico):
        df = logistica.processar_logistica(dados_reposicao, config_reposicao, data_hoje=pico)
        natal, mossoro = _linha(df, "Natal", "CAM-P"), _linha(df, "Mossoró", "CAM-P")
        # Mossoró vende metade de Natal, mas a janela dela tem 1 dia a mais de prazo
        assert mossoro["JanelaDias"] == 5
        assert mossoro["DemandaJanela"] == pytest.approx(5.0)
        assert mossoro["Alvo"] < natal["Alvo"]

    def test_fora_do_sortimento_nao_entra(self, dados_reposicao, config_reposicao, pico):
        df = logistica.processar_logistica(dados_reposicao, config_reposicao, data_hoje=pico)
        assert len(df[(df["Loja"] == "Mossoró") & (df["SKU"] == "OUT-P")]) == 0
        assert _linha(df, "Natal", "OUT-P")["Alvo"] >= 2

    def test_estoque_fora_do_sortimento_vira_recolher(self, dados_reposicao, config_reposicao, pico):
        dados = _com_estoque(dados_reposicao, {(REP_OUT_P, REP_DEP_MOSSORO): 9})
        df = logistica.processar_logistica(dados, config_reposicao, data_hoje=pico)
        linha = _linha(df, "Mossoró", "OUT-P")
        assert linha["Alvo"] == 0 and linha["Separar"] == 0
        assert linha["Excesso"] == 9 and linha["Acao"] == logistica.ACAO_RECOLHER

    def test_cd_curto_rateia_e_marca_a_falta(self, dados_reposicao, config_reposicao, pico):
        dados = _com_estoque(dados_reposicao, {(REP_CAL_P, REP_DEP_CD): 3})
        df = logistica.processar_logistica(dados, config_reposicao, data_hoje=pico)
        linhas = df[df["SKU"] == "CAL-P"]
        assert linhas["Separar"].sum() == 3
        assert (linhas["Separar"] + linhas["Falta"] == linhas["Necessidade"]).all()
        assert set(linhas["Acao"]) <= {logistica.ACAO_PARCIAL, logistica.ACAO_SEM_CD}

    def test_separar_nunca_passa_do_cd(self, dados_reposicao, config_reposicao, pico):
        dados = _com_estoque(dados_reposicao, {
            (REP_CAM_P, REP_DEP_CD): 4, (REP_CAL_P, REP_DEP_CD): 0, (REP_OUT_P, REP_DEP_CD): 1})
        df = logistica.processar_logistica(dados, config_reposicao, data_hoje=pico)
        por_sku = df.groupby("SKU").agg(separar=("Separar", "sum"), cd=("EstoqueCentral", "first"))
        assert (por_sku["separar"] <= por_sku["cd"]).all()

    def test_cd_zerado_e_sem_estoque_no_cd(self, dados_reposicao, config_reposicao, pico):
        dados = _com_estoque(dados_reposicao, {(REP_OUT_P, REP_DEP_CD): 0})
        linha = _linha(logistica.processar_logistica(dados, config_reposicao, data_hoje=pico),
                       "Natal", "OUT-P")
        assert linha["Separar"] == 0 and linha["Falta"] == linha["Necessidade"] > 0
        assert linha["Acao"] == logistica.ACAO_SEM_CD

    def test_saldo_negativo_pede_correcao_sem_sugestao(self, dados_reposicao, config_reposicao, pico):
        dados = _com_estoque(dados_reposicao, {(REP_CAM_P, REP_DEP_NATAL): -3})
        linha = _linha(logistica.processar_logistica(dados, config_reposicao, data_hoje=pico),
                       "Natal", "CAM-P")
        assert linha["Acao"] == logistica.ACAO_CORRIGIR
        assert linha["Separar"] == 0 and linha["Falta"] == 0

    def test_loja_abastecida_fica_ok(self, dados_reposicao, config_reposicao, pico):
        base = logistica.processar_logistica(dados_reposicao, config_reposicao, data_hoje=pico)
        alvo = int(_linha(base, "Natal", "CAM-P")["Alvo"])
        dados = _com_estoque(dados_reposicao, {(REP_CAM_P, REP_DEP_NATAL): alvo})
        linha = _linha(logistica.processar_logistica(dados, config_reposicao, data_hoje=pico),
                       "Natal", "CAM-P")
        assert linha["Separar"] == 0 and linha["Excesso"] == 0
        assert linha["Acao"] == logistica.ACAO_OK

    def test_gavetas_limitam_o_alvo(self, dados_reposicao, config_reposicao, pico):
        livre = logistica.processar_logistica(dados_reposicao, config_reposicao, data_hoje=pico)
        config_reposicao["reposicao"]["lojas"]["Natal"]["gavetas"] = 0
        df = logistica.processar_logistica(dados_reposicao, config_reposicao, data_hoje=pico)
        natal = df[df["Loja"] == "Natal"]
        assert (natal["Alvo"] == 2).all()                # sem gaveta: só a arara
        assert natal["LimitadoPorEspaco"].all()
        ideal_livre = livre[livre["Loja"] == "Natal"].set_index("SKU")["AlvoIdeal"]
        assert natal.set_index("SKU")["AlvoIdeal"].to_dict() == ideal_livre.to_dict()
        # Mossoró não tem gavetas cadastradas: segue sem teto
        assert not df[df["Loja"] == "Mossoró"]["LimitadoPorEspaco"].any()

    def test_gaveta_unica_vai_para_o_modelo_campeao(self, dados_reposicao, config_reposicao, pico):
        config_reposicao["reposicao"]["lojas"]["Natal"]["gavetas"] = 1
        df = logistica.processar_logistica(dados_reposicao, config_reposicao, data_hoje=pico)
        natal = df[df["Loja"] == "Natal"].set_index("SKU")
        # CAM (P+M, 124/mês em Natal) tem mais fundo que CAL e OUT
        assert natal.loc["CAM-P", "Gavetas"] == 1 and natal.loc["CAM-M", "Gavetas"] == 1
        assert natal.loc["CAL-P", "Gavetas"] == 0 and natal.loc["CAL-P", "Alvo"] == 2

    def test_baixa_usa_cobertura_maior_e_demanda_menor(self, dados_reposicao, config_reposicao, hoje):
        baixa = pd.Timestamp(year=hoje.year, month=6, day=10)
        df = logistica.processar_logistica(dados_reposicao, config_reposicao, data_hoje=baixa)
        natal = _linha(df, "Natal", "CAM-P")
        assert natal["Fase"] == r.FASE_BAIXA and natal["JanelaDias"] == 16
        assert natal["Alvo"] >= 2

    def test_em_transito_ausente_deixa_colunas_vazias(self, dados_reposicao, config_reposicao, pico):
        df = logistica.processar_logistica(dados_reposicao, config_reposicao, data_hoje=pico)
        assert df["EmTransito"].isna().all() and df["ChegadaPrevista"].isna().all()

    def test_em_transito_soma_e_pega_a_primeira_chegada(self, dados_reposicao, config_reposicao, pico):
        transito = pd.DataFrame({
            "ID_produto": [REP_CAM_P, REP_CAM_P],
            "Quantidade": [40, 10],
            "DataPrevista": ["2026-02-20", "2026-02-05"],
        })
        df = logistica.processar_logistica(dados_reposicao, config_reposicao,
                                           em_transito=transito, data_hoje=pico)
        linha = _linha(df, "Natal", "CAM-P")
        assert linha["EmTransito"] == 50
        assert linha["ChegadaPrevista"] == pd.Timestamp("2026-02-05")
        assert pd.isna(_linha(df, "Natal", "CAL-P")["EmTransito"])

    def test_produto_sem_demanda_na_rede_nao_ganha_exposicao(self, dados_reposicao, config_reposicao, pico):
        # produto ativo do colégio, mas que nunca vendeu (cadastro morto)
        dados = dict(dados_reposicao)
        dados["produtos"] = pd.concat([dados["produtos"], pd.DataFrame({
            "ID": ["299"], "codigo": ["MORTO-P"], "Descricao": ["Morto Tamanho:P"],
            "preco_custo": [1.0]})], ignore_index=True)
        dados["detalhes"] = pd.concat([dados["detalhes"], pd.DataFrame({
            "ID_produto": ["299"], "Marca_sku": ["COL"], "Grupo": ["EME"], "categoria": ["Camiseta"],
            "Super_categoria": ["Camiseta"], "Tamanho": ["P"]})], ignore_index=True)
        df = logistica.processar_logistica(dados, config_reposicao, data_hoje=pico)
        assert "MORTO-P" not in set(df["SKU"])

    def test_produto_sem_colegio_fica_onde_a_loja_vende(self, dados_reposicao, config_reposicao, pico):
        # OUT-P sem colégio (revenda): não cabe no cadastro por colégio. Natal o
        # vende → tem alvo; Mossoró não → o estoque de lá é excesso, com o motivo certo.
        dados = _com_estoque(dados_reposicao, {(REP_OUT_P, REP_DEP_MOSSORO): 4})
        det = dados["detalhes"].copy()
        det.loc[det["ID_produto"] == REP_OUT_P, "Marca_sku"] = ""
        dados = {**dados, "detalhes": det}
        config_reposicao["reposicao"]["sortimento"] = {"Natal": ["COL"], "Mossoró": ["COL"]}
        df = logistica.processar_logistica(dados, config_reposicao, data_hoje=pico)
        assert _linha(df, "Natal", "OUT-P")["Alvo"] >= 2
        mossoro = _linha(df, "Mossoró", "OUT-P")
        assert mossoro["Alvo"] == 0 and mossoro["Excesso"] == 4
        assert mossoro["MotivoExcesso"] == "Sem colégio e sem venda na loja"

    def test_motivo_do_excesso(self, dados_reposicao, config_reposicao, pico):
        dados = _com_estoque(dados_reposicao, {
            (REP_OUT_P, REP_DEP_MOSSORO): 9, (REP_CAM_P, REP_DEP_NATAL): 900})
        df = logistica.processar_logistica(dados, config_reposicao, data_hoje=pico)
        assert _linha(df, "Mossoró", "OUT-P")["MotivoExcesso"] == "Colégio fora do sortimento da loja"
        assert _linha(df, "Natal", "CAM-P")["MotivoExcesso"] == "Acima do que a loja vende em 120 dias"
        assert _linha(df, "Natal", "CAL-P")["MotivoExcesso"] == ""
