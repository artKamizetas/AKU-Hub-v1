"""
Testes da lógica de calendário de rodadas de produção (etl/demanda.py).
Datas fixas (data_hoje injetável) → determinístico.
"""

import pandas as pd
import pytest

from etl import demanda as d


HOJE = pd.Timestamp("2026-07-15")


def cfg_explicito():
    return {"planejamento": {
        "rodadas_datas": ["2026-08-01", "2026-11-01", "2027-02-01"],
        "lead_time_semanas": 4,   # 28 dias
    }, "fabrica": {"cobertura_meses": 2}}


def cfg_vazio():
    return {"planejamento": {"rodadas_datas": [], "lead_time_semanas": 4},
            "fabrica": {"cobertura_meses": 2}}


# ---------------------------------------------------------------------------
# _candidatas_rodadas — chegada = disparo + lead time
# ---------------------------------------------------------------------------
class TestCandidatas:
    def test_datas_explicitas_chegada_em_dias(self):
        c = d._candidatas_rodadas(cfg_explicito(), HOJE)
        assert len(c) == 3
        # ordenadas por chegada, numeradas cronologicamente
        assert [x["numero"] for x in c] == [1, 2, 3]
        # 01/08 + 28 dias = 29/08
        assert c[0]["data_disparo"] == pd.Timestamp("2026-08-01")
        assert c[0]["data_chegada"] == pd.Timestamp("2026-08-29")

    def test_sem_rodadas_retorna_vazio(self):
        assert d._candidatas_rodadas(cfg_vazio(), HOJE) == []


# ---------------------------------------------------------------------------
# _sequencia_rodadas — descarta a última (sem "seguinte") e chegadas passadas
# ---------------------------------------------------------------------------
class TestSequencia:
    def test_ultima_sem_seguinte_e_descartada(self):
        seq = d._sequencia_rodadas(cfg_explicito(), HOJE)
        # 3 candidatas → 2 na sequência (a 3ª não tem chegada seguinte)
        assert len(seq) == 2
        assert seq[0]["data_chegada"] == pd.Timestamp("2026-08-29")
        assert seq[0]["data_chegada_seguinte"] == pd.Timestamp("2026-11-29")
        assert seq[1]["data_chegada_seguinte"] == pd.Timestamp("2027-03-01")

    def test_sem_rodadas_vazio(self):
        assert d._sequencia_rodadas(cfg_vazio(), HOJE) == []


# ---------------------------------------------------------------------------
# proxima_janela_cobertura — cobre até a rodada SEGUINTE à próxima
# ---------------------------------------------------------------------------
class TestProximaJanela:
    def test_com_rodadas(self):
        j = d.proxima_janela_cobertura(cfg_explicito(), HOJE)
        assert j["tem_rodadas"] is True
        assert j["modo"] == "rodadas"
        assert j["janela_inicio"] == HOJE
        # próxima chegada ≥ hoje é 29/08; a seguinte é 29/11 → janela vai até lá
        assert j["janela_fim"] == pd.Timestamp("2026-11-29")

    def test_fallback_fixo_sem_rodadas(self):
        j = d.proxima_janela_cobertura(cfg_vazio(), HOJE)
        assert j["tem_rodadas"] is False
        assert j["modo"] == "fallback_fixo"
        assert j["cobertura_meses"] == 2.0
        assert j["janela_fim"] == HOJE + pd.DateOffset(months=2)


# ---------------------------------------------------------------------------
# listar_rodadas_selecionaveis / montar_janela_rodada
# ---------------------------------------------------------------------------
class TestListarSelecionaveis:
    def test_uma_janela_por_rodada_futura(self):
        js = d.listar_rodadas_selecionaveis(cfg_explicito(), HOJE)
        # rodadas 1 e 2 são selecionáveis (a 3 não tem seguinte)
        assert len(js) == 2
        assert js[0]["numero"] == 1
        assert js[0]["janela_inicio"] == pd.Timestamp("2026-08-29")
        assert js[0]["janela_fim"] == pd.Timestamp("2026-11-29")

    def test_sem_rodadas_lista_vazia(self):
        assert d.listar_rodadas_selecionaveis(cfg_vazio(), HOJE) == []

    def test_montar_janela_por_disparo(self):
        j = d.montar_janela_rodada(cfg_explicito(), mes_disparo=8, ano_disparo=2026, data_hoje=HOJE)
        assert j["numero"] == 1
        assert j["janela_inicio"] == pd.Timestamp("2026-08-29")

    def test_montar_janela_inexistente_cai_no_fallback(self):
        j = d.montar_janela_rodada(cfg_explicito(), mes_disparo=1, ano_disparo=2000, data_hoje=HOJE)
        # rodada inexistente → proxima_janela_cobertura (modo rodadas)
        assert j["modo"] == "rodadas"
