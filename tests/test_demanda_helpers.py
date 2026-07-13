"""Testes das funções puras de etl/demanda.py (sem I/O, valores exatos)."""

import math
import pandas as pd
import pytest

from etl import demanda as d


# ---------------------------------------------------------------------------
# _nivel_para_z — Fator de Serviço a partir do nível de serviço
# ---------------------------------------------------------------------------
class TestNivelParaZ:
    def test_percentual_conhecido(self):
        assert d._nivel_para_z(99) == 2.33
        assert d._nivel_para_z(92) == 1.41
        assert d._nivel_para_z(95) == 1.65

    def test_aceita_fracao(self):
        # 0.99 é tratado como 99%
        assert d._nivel_para_z(0.99) == 2.33
        assert d._nivel_para_z(0.92) == 1.41

    def test_nivel_desconhecido_usa_default(self):
        assert d._nivel_para_z(93) == 1.65
        assert d._nivel_para_z(50) == 1.65


# ---------------------------------------------------------------------------
# estoque_seguranca — Fator de Serviço × Variação × demanda / √meses
# ---------------------------------------------------------------------------
class TestEstoqueSeguranca:
    def _cfg(self):
        return {"demanda": {"nivel_servico_alta": 99, "nivel_servico_baixa": 92,
                            "variacao_demanda": 0.25}}

    def test_alta_um_mes(self):
        # 2.33 × 0.25 × 100 / √1
        assert d.estoque_seguranca(100, True, self._cfg(), 1.0) == pytest.approx(58.25)

    def test_baixa_um_mes(self):
        # 1.41 × 0.25 × 100
        assert d.estoque_seguranca(100, False, self._cfg(), 1.0) == pytest.approx(35.25)

    def test_raiz_do_tempo(self):
        # 9 meses divide por √9 = 3, não por 9 (pooling)
        esperado = 2.33 * 0.25 * 100 / 3
        assert d.estoque_seguranca(100, True, self._cfg(), 9.0) == pytest.approx(esperado)

    def test_demanda_negativa_zera(self):
        assert d.estoque_seguranca(-10, True, self._cfg(), 1.0) == 0.0

    def test_meses_minimo_um(self):
        # meses < 1 é elevado a 1 (não estoura o √)
        a = d.estoque_seguranca(100, True, self._cfg(), 0.3)
        b = d.estoque_seguranca(100, True, self._cfg(), 1.0)
        assert a == pytest.approx(b)


# ---------------------------------------------------------------------------
# _par_ceil — arredonda pra cima forçando par
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("entrada,esperado", [
    (0, 0), (1, 2), (2, 2), (2.1, 4), (3, 4), (4, 4),
    (0.5, 2), (-5, 0), (7.0, 8),
])
def test_par_ceil(entrada, esperado):
    assert d._par_ceil(entrada) == esperado
    assert d._par_ceil(entrada) % 2 == 0


# ---------------------------------------------------------------------------
# _ordenar_janela_cronologica — detecta a virada de ano pela maior lacuna
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("entrada,esperado", [
    ([12, 1, 2], [12, 1, 2]),
    ([1, 2, 12], [12, 1, 2]),
    ([2, 12, 1], [12, 1, 2]),
    ([6, 7, 8], [6, 7, 8]),
    ([11, 12, 1, 2], [11, 12, 1, 2]),
    ([5], [5]),
    ([], []),
])
def test_ordenar_janela_cronologica(entrada, esperado):
    assert d._ordenar_janela_cronologica(entrada) == esperado


# ---------------------------------------------------------------------------
# _ultima_temporada_alta — mapeia a janela p/ o (ano,mês) concreto da última alta
# ---------------------------------------------------------------------------
class TestUltimaTemporadaAlta:
    def _cfg(self):
        return {"demanda": {"janela_alta": [12, 1, 2]}}

    def test_meio_do_ano(self):
        # Jul/2026: última alta completa é Dez/2025–Fev/2026
        t = d._ultima_temporada_alta(self._cfg(), pd.Timestamp("2026-07-09"))
        assert t[12] == pd.Timestamp("2025-12-01")
        assert t[1] == pd.Timestamp("2026-01-01")
        assert t[2] == pd.Timestamp("2026-02-01")

    def test_durante_a_alta_pega_a_anterior(self):
        # Jan/2026: a alta atual (Fev/2026) ainda não terminou → usa 2024-2025
        t = d._ultima_temporada_alta(self._cfg(), pd.Timestamp("2026-01-15"))
        assert t[12] == pd.Timestamp("2024-12-01")
        assert t[1] == pd.Timestamp("2025-01-01")
        assert t[2] == pd.Timestamp("2025-02-01")


# ---------------------------------------------------------------------------
# taxa_crescimento_efetiva — cascata (manual do planejador sempre vence)
# ---------------------------------------------------------------------------
class TestTaxaCrescimentoEfetiva:
    def test_desligado_retorna_um(self):
        assert d.taxa_crescimento_efetiva("COL", {}, grupo="EME", ativo=False) == 1.0

    def test_manual_por_grupo_vence(self):
        cfg = {"colegios": {"COL": {
            "crescimento_grupos": {"EME": 1.5}, "taxa_crescimento": 1.2}}}
        assert d.taxa_crescimento_efetiva("COL", cfg, grupo="EME") == 1.5

    def test_manual_colegio_quando_grupo_ausente(self):
        cfg = {"colegios": {"COL": {
            "crescimento_grupos": {"EME": 1.5}, "taxa_crescimento": 1.2}}}
        assert d.taxa_crescimento_efetiva("COL", cfg, grupo="EF1") == 1.2

    def test_observado_por_segmento(self):
        cfg = {"colegios": {}, "grupo_segmento": {"EME": "Médio"}}
        obs = {"COL": {"__geral__": 1.1, "segmentos": {"Médio": 1.3}}}
        assert d.taxa_crescimento_efetiva("COL", cfg, grupo="EME", observado=obs) == 1.3

    def test_observado_geral_quando_segmento_ausente(self):
        cfg = {"colegios": {}, "grupo_segmento": {}}
        obs = {"COL": {"__geral__": 1.1, "segmentos": {}}}
        assert d.taxa_crescimento_efetiva("COL", cfg, grupo="ZZZ", observado=obs) == 1.1

    def test_fallback_global(self):
        cfg = {"fabrica": {"crescimento_pct": 10.0}}
        assert d.taxa_crescimento_efetiva("COL", cfg, grupo="EME") == pytest.approx(1.1)


# ---------------------------------------------------------------------------
# proporcao_baixa_efetiva — cascata SKU → colégio → global
# ---------------------------------------------------------------------------
class TestProporcaoBaixaEfetiva:
    def test_override_por_sku_vence(self):
        cfg = {"excecoes_sku": {"A-PP": {"proporcao_baixa": 0.7}},
               "colegios": {"COL": {"proporcao_baixa": 0.5}}}
        assert d.proporcao_baixa_efetiva("A-PP", "COL", cfg, 0.43) == 0.7

    def test_override_por_colegio(self):
        cfg = {"excecoes_sku": {}, "colegios": {"COL": {"proporcao_baixa": 0.5}}}
        assert d.proporcao_baixa_efetiva("A-PP", "COL", cfg, 0.43) == 0.5

    def test_fallback_global(self):
        assert d.proporcao_baixa_efetiva("A-PP", "COL", {}, 0.43) == 0.43


# ---------------------------------------------------------------------------
# mapa_grupo_segmento / segmento_do_grupo
# ---------------------------------------------------------------------------
class TestSegmento:
    def test_default_do_codigo(self):
        assert d.segmento_do_grupo("EME") == "Médio"
        assert d.segmento_do_grupo("EF1") == "Fundamental"

    def test_grupo_desconhecido_vira_outros(self):
        assert d.segmento_do_grupo("ZZZ") == "Outros"

    def test_config_sobrescreve_default(self):
        cfg = {"grupo_segmento": {"EME": "Ensino Médio"}}
        assert d.segmento_do_grupo("EME", cfg) == "Ensino Médio"
        # grupos não citados mantêm o default
        assert d.segmento_do_grupo("EF1", cfg) == "Fundamental"


# ---------------------------------------------------------------------------
# fracionar_janela_por_mes — quebra intervalo em (mês, fração de dias)
# ---------------------------------------------------------------------------
class TestFracionarJanela:
    def test_meses_inteiros(self):
        r = d.fracionar_janela_por_mes(pd.Timestamp("2026-01-01"), pd.Timestamp("2026-03-01"))
        assert r == [(1, 1.0), (2, 1.0)]

    def test_mes_parcial(self):
        # 15/jan a 01/fev = 17 dias de janeiro (31 dias)
        r = d.fracionar_janela_por_mes(pd.Timestamp("2026-01-15"), pd.Timestamp("2026-02-01"))
        assert len(r) == 1
        mes, frac = r[0]
        assert mes == 1
        assert frac == pytest.approx(17 / 31)

    def test_janela_vazia(self):
        r = d.fracionar_janela_por_mes(pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-01"))
        assert r == []

    def test_fracoes_somam_o_span_em_meses(self):
        ini, fim = pd.Timestamp("2026-01-10"), pd.Timestamp("2026-04-20")
        total = sum(frac for _, frac in d.fracionar_janela_por_mes(ini, fim))
        # ~3.3 meses; cada mês pesa por dias
        assert total == pytest.approx(3.34, abs=0.1)
