"""
Testes de integração do motor de demanda e da política order-up-to.

O motor ancora na última alta relativa a now(); as fixtures preenchem as altas
de forma constante e desligam o crescimento, então os totais projetados são
previsíveis a partir dos números de entrada (ver conftest.py).
"""

import pandas as pd
import pytest

from etl import demanda as d


# ---------------------------------------------------------------------------
# calcular_demanda_mensal_por_sku — âncora na alta + baixa proporcional
# ---------------------------------------------------------------------------
class TestDemandaMensalPorSku:
    def test_estrutura(self, dados, config):
        dem = d.calcular_demanda_mensal_por_sku(dados, config)
        # 2 SKUs × 12 meses
        assert len(dem) == 24
        assert set(dem["SKU"]) == {"A-PP", "B-PP"}
        assert set(dem["Mes"]) == set(range(1, 13))

    def test_sku_normal_ancora_na_alta(self, dados, config):
        dem = d.calcular_demanda_mensal_por_sku(dados, config)
        a = dem[dem["SKU"] == "A-PP"]

        alta = a[a["Fase"] == "alta"]
        baixa = a[a["Fase"] == "baixa"]
        # janela de alta = Dez/Jan/Fev
        assert set(alta["Mes"]) == {12, 1, 2}
        assert len(baixa) == 9

        # crescimento OFF → âncora = 30 (10 por mês de alta)
        assert alta["DemandaMensalProjetada"].sum() == pytest.approx(30.0)
        assert alta["DemandaMensalProjetada"].tolist() == pytest.approx([10.0, 10.0, 10.0])

        # proporção da baixa (override colégio) = 0.5 → baixa soma 15
        assert baixa["DemandaMensalProjetada"].sum() == pytest.approx(15.0)

        # taxa de crescimento aplicada = 1.0 (desligada)
        assert all(a["TaxaCrescimento"] == 1.0)

    def test_distribuicao_baixa_soma_um(self, dados, config):
        dist = d.distribuicao_mensal_baixa(dados, config)
        # meses de alta recebem 0; o resto soma 1
        assert dist[12] == 0.0 and dist[1] == 0.0 and dist[2] == 0.0
        assert sum(dist.values()) == pytest.approx(1.0)

    def test_sku_so_de_baixa(self, dados, config):
        dem = d.calcular_demanda_mensal_por_sku(dados, config)
        b = dem[dem["SKU"] == "B-PP"]
        # sem venda na alta → todos os meses de alta zerados
        assert b[b["Fase"] == "alta"]["DemandaMensalProjetada"].sum() == pytest.approx(0.0)
        # base de baixa = 8 peças de Junho, espalhadas pela distribuição da baixa
        assert b[b["Fase"] == "baixa"]["DemandaMensalProjetada"].sum() == pytest.approx(8.0)

    def test_correcao_manual_global_soma_em_todo_mes(self, dados, config):
        base = d.calcular_demanda_mensal_por_sku(dados, config)
        total_base = base[base["SKU"] == "A-PP"]["DemandaMensalProjetada"].sum()

        config["fabrica"]["correcao_manual"] = 5
        ajustado = d.calcular_demanda_mensal_por_sku(dados, config)
        total_aj = ajustado[ajustado["SKU"] == "A-PP"]["DemandaMensalProjetada"].sum()

        # +5 em cada um dos 12 meses
        assert total_aj == pytest.approx(total_base + 12 * 5)

    def test_crescimento_liga_multiplica_a_alta(self, dados, config):
        # liga o crescimento e força taxa 2.0 no colégio → alta dobra
        config["demanda"]["aplicar_crescimento_fabrica"] = True
        config["colegios"]["COL"]["taxa_crescimento"] = 2.0
        dem = d.calcular_demanda_mensal_por_sku(dados, config)
        a = dem[dem["SKU"] == "A-PP"]
        assert a[a["Fase"] == "alta"]["DemandaMensalProjetada"].sum() == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# agregar_demanda_mensal_total / custo_medio_ponderado
# ---------------------------------------------------------------------------
class TestAgregados:
    def test_agregar_soma_bottom_up(self, dados, config):
        dem = d.calcular_demanda_mensal_por_sku(dados, config)
        saz = d.calcular_sazonalidade_empresa(dados, config)
        agg = d.agregar_demanda_mensal_total(dem, saz)

        assert list(agg["Mes"]) == list(range(1, 13))
        assert agg["Demanda"].dtype.kind in "iu"   # inteiro
        # total agregado = total por SKU (A: 45, B: 8), arredondado por mês
        assert agg["Demanda"].sum() == pytest.approx(45 + 8, abs=2)

    def test_custo_medio_ponderado(self):
        demanda = pd.DataFrame({
            "SKU": ["A-PP", "B-PP"],
            "DemandaMensalProjetada": [30.0, 10.0],
        })
        produtos = pd.DataFrame({
            "codigo": ["A-PP", "B-PP"],
            "preco_custo": [100.0, 40.0],
        })
        # (30×100 + 10×40) / 40 = 85
        assert d.custo_medio_ponderado(demanda, produtos) == pytest.approx(85.0)

    def test_custo_medio_fallback_sem_demanda(self):
        demanda = pd.DataFrame({"SKU": ["A-PP"], "DemandaMensalProjetada": [0.0]})
        produtos = pd.DataFrame({"codigo": ["A-PP", "B-PP"], "preco_custo": [100.0, 40.0]})
        # demanda zero → média simples do catálogo = 70
        assert d.custo_medio_ponderado(demanda, produtos) == pytest.approx(70.0)


# ---------------------------------------------------------------------------
# simular_politica_reabastecimento — política order-up-to (R,S)
# ---------------------------------------------------------------------------
def _dem_flat(qtd_mes=10.0):
    """DemandaMensalProjetada constante p/ 1 SKU, desacoplando a política do motor."""
    linhas = [{"SKU": "X", "ID_produto": "1", "Mes": m,
               "DemandaMensalProjetada": qtd_mes} for m in range(1, 13)]
    return pd.DataFrame(linhas)


def _dados_estoque(saldo):
    return {
        "estoque": pd.DataFrame({"ID_produto": ["1"], "saldoFisico": [saldo]}),
        "pedidos": pd.DataFrame({"ID": pd.Series([], dtype="object"),
                                 "id_situacao": pd.Series([], dtype="object")}),
        "itens": pd.DataFrame({"ID_produto": pd.Series([], dtype="object"),
                               "ID_pedido": pd.Series([], dtype="object"),
                               "Quantidade": pd.Series([], dtype="float")}),
    }


def _cfg_policy():
    return {
        "demanda": {"janela_alta": [12, 1, 2], "nivel_servico_alta": 99,
                    "nivel_servico_baixa": 92, "variacao_demanda": 0.25},
        "planejamento": {"rodadas_datas": ["2026-08-01", "2026-11-01", "2027-02-01"],
                         "lead_time_semanas": 4},
        "fabrica": {"situacoes_backlog": [6, 15]},
    }


HOJE = pd.Timestamp("2026-07-15")


class TestPolitica:
    def test_sem_rodadas_retorna_vazio_com_colunas(self):
        cfg = {"demanda": {"janela_alta": [12, 1, 2]},
               "planejamento": {"rodadas_datas": [], "lead_time_semanas": 4},
               "fabrica": {"situacoes_backlog": []}}
        out = d.simular_politica_reabastecimento(
            _dados_estoque(0), cfg, dem=_dem_flat(), data_hoje=HOJE)
        assert len(out) == 0
        for col in ("SKU", "DemandaPeriodo", "EstoqueSeguranca", "EstoqueAlvo",
                    "EstoqueProjetado", "Pedido", "contem_alta"):
            assert col in out.columns

    def test_invariantes_da_politica(self):
        cfg = _cfg_policy()
        out = d.simular_politica_reabastecimento(
            _dados_estoque(0), cfg, dem=_dem_flat(10.0), data_hoje=HOJE)
        # 3 rodadas → 2 na sequência (a última não tem seguinte)
        assert len(out) == 2

        for _, r in out.iterrows():
            # split alta/baixa fecha o total
            assert r["DemandaPeriodo"] == pytest.approx(
                r["DemandaPeriodoAlta"] + r["DemandaPeriodoBaixa"])
            # EstoqueAlvo = DemandaPeriodo + EstoqueSeguranca
            assert r["EstoqueAlvo"] == pytest.approx(
                r["DemandaPeriodo"] + r["EstoqueSeguranca"])
            # segurança bate com a fórmula, recomputada
            seg_esp = d.estoque_seguranca(
                r["DemandaPeriodo"], r["contem_alta"], cfg, r["MesesIntervalo"])
            assert r["EstoqueSeguranca"] == pytest.approx(seg_esp)
            # pedido é par e não-negativo
            assert r["Pedido"] >= 0 and r["Pedido"] % 2 == 0

    def test_demanda_periodo_bate_com_fracionamento(self):
        cfg = _cfg_policy()
        out = d.simular_politica_reabastecimento(
            _dados_estoque(0), cfg, dem=_dem_flat(10.0), data_hoje=HOJE)
        r0 = out.iloc[0]
        # DemandaPeriodo = 10 × soma das frações do intervalo [chegada, chegada_seguinte)
        esperado = sum(10.0 * frac for _, frac in d.fracionar_janela_por_mes(
            r0["data_chegada"], r0["data_chegada_seguinte"]))
        assert r0["DemandaPeriodo"] == pytest.approx(esperado)

    def test_intervalo_com_alta_marca_contem_alta(self):
        cfg = _cfg_policy()
        out = d.simular_politica_reabastecimento(
            _dados_estoque(0), cfg, dem=_dem_flat(10.0), data_hoje=HOJE)
        # rodada 1: cobre Ago–Nov (sem alta); rodada 2: cobre Nov–Mar (com Dez/Jan/Fev)
        assert out.iloc[0]["contem_alta"] == False   # noqa: E712
        assert out.iloc[1]["contem_alta"] == True    # noqa: E712

    def test_estoque_alto_zera_pedido(self):
        cfg = _cfg_policy()
        out = d.simular_politica_reabastecimento(
            _dados_estoque(100000), cfg, dem=_dem_flat(10.0), data_hoje=HOJE)
        # estoque cobre tudo com folga → nada a pedir
        assert (out["Pedido"] == 0).all()

    def test_backlog_reduz_estoque_projetado(self):
        cfg = _cfg_policy()
        dados = _dados_estoque(50)
        # 20 peças em pedido de backlog (situação 6) consomem o estoque
        dados["pedidos"] = pd.DataFrame({"ID": ["P1"], "id_situacao": [6]})
        dados["itens"] = pd.DataFrame({"ID_produto": ["1"], "ID_pedido": ["P1"],
                                       "Quantidade": [20.0]})
        com_backlog = d.simular_politica_reabastecimento(
            dados, cfg, dem=_dem_flat(10.0), data_hoje=HOJE)
        sem_backlog = d.simular_politica_reabastecimento(
            _dados_estoque(50), cfg, dem=_dem_flat(10.0), data_hoje=HOJE)
        # estoque projetado da 1ª rodada cai exatamente pelo backlog (20)
        assert (sem_backlog.iloc[0]["EstoqueProjetado"]
                - com_backlog.iloc[0]["EstoqueProjetado"]) == pytest.approx(20.0)
