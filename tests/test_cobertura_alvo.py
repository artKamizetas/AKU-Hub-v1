"""
Testes da Cobertura Alvo por rodada (antecipação deliberada).

Spec: docs/requisitos/cobertura-alvo-rodada.md. Usa demanda mensal flat
injetada (dem=) p/ desacoplar a política do motor de demanda — invariantes:
sem override é byte-idêntico; override engorda a rodada e a SEGUINTE absorve
(rodadas posteriores intactas); a produção total do horizonte se conserva.
"""

import pandas as pd
import pytest

from etl import demanda as d


HOJE = pd.Timestamp("2026-07-15")

# 4 datas → 3 rodadas na simulação (a última só fecha o intervalo).
# Lead 4 semanas → chegadas: 29/08, 29/11, 01/03, (29/05 fecha a 3ª).
DATAS = ["2026-08-01", "2026-11-01", "2027-02-01", "2027-05-01"]


def _dem_flat(qtd_mes=10.0):
    linhas = [{"SKU": "X", "ID_produto": "1", "Mes": m,
               "DemandaMensalProjetada": qtd_mes} for m in range(1, 13)]
    return pd.DataFrame(linhas)


def _dados_estoque(saldo=0):
    return {
        "estoque": pd.DataFrame({"ID_produto": ["1"], "saldoFisico": [saldo]}),
        "pedidos": pd.DataFrame({"ID": pd.Series([], dtype="object"),
                                 "id_situacao": pd.Series([], dtype="object")}),
        "itens": pd.DataFrame({"ID_produto": pd.Series([], dtype="object"),
                               "ID_pedido": pd.Series([], dtype="object"),
                               "Quantidade": pd.Series([], dtype="float")}),
    }


def _cfg(override=None, ns_alta=99, ns_baixa=92):
    cfg = {
        "demanda": {"janela_alta": [12, 1, 2], "nivel_servico_alta": ns_alta,
                    "nivel_servico_baixa": ns_baixa, "variacao_demanda": 0.25},
        "planejamento": {"rodadas_datas": list(DATAS), "lead_time_semanas": 4},
        "fabrica": {"situacoes_backlog": [6, 15]},
    }
    if override is not None:
        cfg["planejamento"]["cobertura_override"] = override
    return cfg


def _simular(override=None, **kw):
    return d.simular_politica_reabastecimento(
        _dados_estoque(), _cfg(override, **kw), dem=_dem_flat(), data_hoje=HOJE)


# ---------------------------------------------------------------------------
# _data_por_demanda_acumulada — conversão % anual → data-fim
# ---------------------------------------------------------------------------
class TestDataPorDemandaAcumulada:
    def test_curva_flat_alvo_em_meses(self):
        # 10/mês; alvo 15 unidades a partir de 01/08 → ~1,5 meses → ~15/09
        curva = {m: 10.0 for m in range(1, 13)}
        fim = d._data_por_demanda_acumulada(pd.Timestamp("2026-08-01"), 15.0, curva)
        assert pd.Timestamp("2026-09-14") <= fim <= pd.Timestamp("2026-09-17")

    def test_sazonalidade_alta_compra_menos_tempo(self):
        # mesma quantidade de unidades: partindo da alta (Dez, 30/mês) o fim
        # chega mais cedo do que partindo da baixa (Jun, 5/mês)
        curva = {m: (30.0 if m in (12, 1, 2) else 5.0) for m in range(1, 13)}
        fim_alta = d._data_por_demanda_acumulada(pd.Timestamp("2026-12-01"), 30.0, curva)
        fim_baixa = d._data_por_demanda_acumulada(pd.Timestamp("2026-06-01"), 30.0, curva)
        dias_alta = (fim_alta - pd.Timestamp("2026-12-01")).days
        dias_baixa = (fim_baixa - pd.Timestamp("2026-06-01")).days
        assert dias_alta < dias_baixa

    def test_cap_no_horizonte(self):
        curva = {m: 0.0 for m in range(1, 13)}   # demanda zero: nunca atinge
        fim = d._data_por_demanda_acumulada(pd.Timestamp("2026-08-01"), 100.0, curva,
                                            horizonte_meses=6)
        assert fim <= pd.Timestamp("2027-02-02")


# ---------------------------------------------------------------------------
# Política com Cobertura Alvo
# ---------------------------------------------------------------------------
class TestCoberturaAlvo:
    def test_sem_override_identico_ao_atual(self):
        base = _simular(override=None)
        vazio = _simular(override={})
        pd.testing.assert_frame_equal(base, vazio)
        # e as colunas novas refletem o fim natural
        assert (base["FimCobertura"] == base["data_chegada_seguinte"]).all()
        assert (base["CoberturaPct"] == 0.0).all()

    def test_clamp_abaixo_do_natural_e_noop(self):
        # R1 natural cobre ~3 meses ≈ 30un ≈ 25% do ano (120). 10% < natural.
        base = _simular(override=None)
        clamped = _simular(override={"2026-08-01": 0.10})
        pd.testing.assert_frame_equal(base, clamped)

    def test_override_engorda_r1_e_r2_absorve(self):
        base = _simular(override=None)
        com = _simular(override={"2026-08-01": 0.50})   # 60un ≈ 6 meses

        b = base.set_index("rodada")
        c = com.set_index("rodada")

        # R1 engorda (janela estendida → alvo maior → pedido maior)
        assert c.loc[1, "Pedido"] > b.loc[1, "Pedido"]
        assert c.loc[1, "FimCobertura"] > b.loc[1, "data_chegada_seguinte"]
        assert c.loc[1, "CoberturaPct"] == pytest.approx(0.50)

        # R2 absorve: estoque projetado na chegada sobe, pedido cai
        assert c.loc[2, "EstoqueProjetado"] > b.loc[2, "EstoqueProjetado"]
        assert c.loc[2, "Pedido"] < b.loc[2, "Pedido"]
        # o ALVO da R2 não muda (função só da janela dela)
        assert c.loc[2, "EstoqueAlvo"] == pytest.approx(b.loc[2, "EstoqueAlvo"])

    def test_efeito_local_r3_intacta(self):
        # override MODERADO (cabe na absorção da R2, i.e. o excedente na
        # chegada da R2 fica abaixo do alvo dela) → R2 encolhe mas continua
        # >0, e a R3 fica idêntica: o efeito NÃO propaga. (Acima do teto de
        # absorção vira cascata — ver test_cascata_override_gigante.)
        base = _simular(override=None)
        com = _simular(override={"2026-08-01": 0.35})
        b = base.set_index("rodada")
        c = com.set_index("rodada")
        assert 0 < c.loc[2, "Pedido"] < b.loc[2, "Pedido"]
        assert c.loc[3, "Pedido"] == b.loc[3, "Pedido"]
        assert c.loc[3, "EstoqueProjetado"] == pytest.approx(
            b.loc[3, "EstoqueProjetado"], abs=2)

    def test_conservacao_da_producao_total(self):
        # níveis de serviço iguais p/ isolar o efeito (a troca baixa→alta do
        # SS na janela estendida é intencional, mas suja a conta exata)
        base = _simular(override=None, ns_alta=95, ns_baixa=95)
        com = _simular(override={"2026-08-01": 0.50}, ns_alta=95, ns_baixa=95)
        # tolerância: SS da R1 muda com √n da janela maior + arredondamento par
        assert com["Pedido"].sum() == pytest.approx(base["Pedido"].sum(), abs=8)

    def test_cascata_override_gigante(self):
        # 100% do ano na R1 → R2 vai a ~0 e o excedente escorre pra R3
        base = _simular(override=None)
        com = _simular(override={"2026-08-01": 1.0})
        c = com.set_index("rodada")
        b = base.set_index("rodada")
        assert c.loc[2, "Pedido"] == 0
        assert c.loc[3, "Pedido"] < b.loc[3, "Pedido"]

    def test_janela_estendida_ate_a_alta_sobe_seguranca(self):
        # R1 natural (29/08→29/11) NÃO contém alta; estendida a 50% entra em
        # Dez → contem_alta vira True e o SS muda para o nível da alta
        base = _simular(override=None)
        com = _simular(override={"2026-08-01": 0.50})
        b1 = base.set_index("rodada").loc[1]
        c1 = com.set_index("rodada").loc[1]
        assert not b1["contem_alta"]
        assert c1["contem_alta"]

    def test_chave_orfa_ignorada(self):
        base = _simular(override=None)
        orfa = _simular(override={"2099-01-01": 0.9})
        pd.testing.assert_frame_equal(base, orfa)

    def test_pct_acima_de_1_clampa(self):
        um = _simular(override={"2026-08-01": 1.0})
        acima = _simular(override={"2026-08-01": 3.0})
        pd.testing.assert_frame_equal(um, acima)
