"""
Testes dos utilitários puros de etl/loader.py.

limpar_id é crítico: IDs do Bling voltam do PostgREST como float64 (NULL vira
NaN) e um astype(str) ingênuo gera '123.0', que quebra os merges entre tabelas.
"""

import pandas as pd
import pytest

from etl.loader import limpar_id, converter_data_flexivel


# ---------------------------------------------------------------------------
# limpar_id
# ---------------------------------------------------------------------------
class TestLimparId:
    @pytest.mark.parametrize("entrada,esperado", [
        (203379922.0, "203379922"),      # float do pandas
        ("203379922.0", "203379922"),    # str com .0
        ("203379922", "203379922"),      # já limpo
        (123, "123"),                    # int
        ("  45.0  ", "45"),              # espaços + .0
    ])
    def test_normaliza(self, entrada, esperado):
        assert limpar_id(entrada) == esperado

    def test_nan_vira_string_vazia(self):
        assert limpar_id(float("nan")) == ""
        assert limpar_id(pd.NA) == ""
        assert limpar_id(None) == ""

    def test_nao_numerico_preservado(self):
        assert limpar_id("ABC-PP") == "ABC-PP"
        # decimal real (não termina em .0) não é truncado
        assert limpar_id("1.5") == "1.5"

    def test_resultado_sempre_string(self):
        assert isinstance(limpar_id(203379922.0), str)
        assert isinstance(limpar_id(123), str)


# ---------------------------------------------------------------------------
# converter_data_flexivel
# ---------------------------------------------------------------------------
class TestConverterData:
    def test_iso(self):
        assert converter_data_flexivel("2026-02-24") == pd.Timestamp("2026-02-24")

    def test_br(self):
        assert converter_data_flexivel("24/02/2026") == pd.Timestamp("2026-02-24")

    def test_br_dia_primeiro(self):
        # 03/12 é 3 de dezembro (BR), não 12 de março
        assert converter_data_flexivel("03/12/2026") == pd.Timestamp("2026-12-03")

    def test_vazio_ou_nan_vira_nat(self):
        assert converter_data_flexivel("") is pd.NaT
        assert converter_data_flexivel(float("nan")) is pd.NaT
        assert converter_data_flexivel(None) is pd.NaT

    def test_lixo_vira_nat(self):
        assert converter_data_flexivel("não é data") is pd.NaT
