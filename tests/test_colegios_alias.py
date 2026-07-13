"""
Testes da normalização configurável de colégios (Marca_sku cru → canônico).
Requisito: docs/requisitos/normalizacao-colegios.md (D1 = identidade + exceções).
"""

import pandas as pd
import pytest

from etl import demanda as d


# ---------------------------------------------------------------------------
# mapa_colegio / colegio_efetivo
# ---------------------------------------------------------------------------
class TestColegioEfetivo:
    def test_identidade_sem_mapa(self):
        # decisão D1=A: valor não listado é mantido como está
        assert d.colegio_efetivo("ADC", {}) == "ADC"
        assert d.colegio_efetivo("ADC", None) == "ADC"

    def test_ruido_vira_outros(self):
        cfg = {"colegios_alias": {"27": "Outros"}}
        assert d.colegio_efetivo("27", cfg) == "Outros"

    def test_strip_no_valor_cru(self):
        cfg = {"colegios_alias": {"27": "Outros"}}
        assert d.colegio_efetivo("  27 ", cfg) == "Outros"

    def test_vazio_e_none_seguem_vazio(self):
        # colégio "" cai no fallback da empresa lá no motor — não pode virar outra coisa
        cfg = {"colegios_alias": {"27": "Outros"}}
        assert d.colegio_efetivo("", cfg) == ""
        assert d.colegio_efetivo(None, cfg) == ""

    def test_mapa_faz_strip_de_chave_e_valor(self):
        cfg = {"colegios_alias": {" 27 ": " Outros "}}
        assert d.mapa_colegio(cfg) == {"27": "Outros"}

    def test_mapa_vazio_sem_config(self):
        assert d.mapa_colegio(None) == {}
        assert d.mapa_colegio({}) == {}


# ---------------------------------------------------------------------------
# aplicar_alias_colegio
# ---------------------------------------------------------------------------
class TestAplicarAlias:
    def _det(self):
        return pd.DataFrame({
            "ID_produto": ["1", "2", "3"],
            "Marca_sku": ["27", "31", "ADC"],
            "Grupo": ["EME", "EDF", "EME"],
        })

    def test_agrupa_e_preserva_raw(self):
        cfg = {"colegios_alias": {"27": "Outros", "31": "Outros"}}
        out = d.aplicar_alias_colegio(self._det(), cfg)
        assert list(out["Marca_sku"]) == ["Outros", "Outros", "ADC"]
        assert list(out["Marca_sku_raw"]) == ["27", "31", "ADC"]

    def test_nao_muta_o_original(self):
        det = self._det()
        cfg = {"colegios_alias": {"27": "Outros"}}
        d.aplicar_alias_colegio(det, cfg)
        assert list(det["Marca_sku"]) == ["27", "31", "ADC"]
        assert "Marca_sku_raw" not in det.columns

    def test_sem_mapa_e_noop(self):
        det = self._det()
        out = d.aplicar_alias_colegio(det, {})
        # identidade: sem de-para não adiciona coluna nem altera nada
        assert list(out["Marca_sku"]) == ["27", "31", "ADC"]
        assert "Marca_sku_raw" not in out.columns

    def test_sem_coluna_marca_sku_e_noop(self):
        det = pd.DataFrame({"ID_produto": ["1"], "Grupo": ["EME"]})
        out = d.aplicar_alias_colegio(det, {"colegios_alias": {"27": "Outros"}})
        assert "Marca_sku" not in out.columns


# ---------------------------------------------------------------------------
# Integração: o alias flui pelo motor de demanda
# ---------------------------------------------------------------------------
class TestIntegracaoMotor:
    def test_alias_renomeia_colegio_na_saida(self, dados, config):
        # ambos os SKUs da fixture são "COL"; renomeia para exibição
        config["colegios_alias"] = {"COL": "Escola Central"}
        dem = d.calcular_demanda_mensal_por_sku(dados, config)
        assert set(dem["Colegio"]) == {"Escola Central"}

    def test_sem_alias_mantem_cru(self, dados, config):
        dem = d.calcular_demanda_mensal_por_sku(dados, config)
        assert set(dem["Colegio"]) == {"COL"}


# ---------------------------------------------------------------------------
# parece_ruido — heurística de sugestão (não aplica nada)
# ---------------------------------------------------------------------------
class TestPareceRuido:
    @pytest.mark.parametrize("valor,esperado", [
        ("27", True), ("31", True), ("12/3", True),
        ("ADC", False), ("NEV", False), ("N1", False),
        ("", False), ("  ", False),
    ])
    def test_flag(self, valor, esperado):
        assert d.parece_ruido(valor) is esperado
