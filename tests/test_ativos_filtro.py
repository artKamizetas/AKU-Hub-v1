"""
Testes do filtro de produtos ATIVOS no motor de demanda (etl/demanda.py).

Um produto inativo — presente em `itens` (histórico de vendas) e `detalhes`
(categorização/colégio), mas AUSENTE de `produtos` (que o loader já filtra por
situacao == "A") — não pode influenciar nenhum agregado do Simulador de
Produção nem da Reposição de Loja. Espelha o caso real do colégio OVD:
descontinuado, ainda com histórico no Bling, mas fora do catálogo ativo.
"""

import pandas as pd
import pytest

from etl import demanda as d

from tests.conftest import ID_A, ID_B


ID_OVD = "999"   # produto INATIVO: existe em itens/detalhes, não em produtos


@pytest.fixture
def dados_com_inativo(dados, hoje):
    """
    A fixture `dados` + um produto inativo do colégio 'OVD' com vendas fortes
    na alta E na baixa. Como NÃO entra em `produtos`, o motor deve ignorá-lo
    por completo — os agregados têm de bater com os da fixture `dados` limpa.
    """
    linhas_ovd = []
    for ano in range(hoje.year - 6, hoje.year + 1):
        for mes in (12, 1, 2):
            linhas_ovd.append((ID_OVD, pd.Timestamp(year=ano, month=mes, day=15), 50, "PED-OVD-ALTA"))
    linhas_ovd.append((ID_OVD, pd.Timestamp(year=hoje.year - 1, month=6, day=10), 40, "PED-OVD-BAIXA"))
    itens_ovd = pd.DataFrame(linhas_ovd, columns=["ID_produto", "Data", "Quantidade", "ID_pedido"])

    det_ovd = pd.DataFrame({
        "ID_produto": [ID_OVD],
        "Marca_sku": ["OVD"],
        "Grupo": ["EME"],
        "categoria": ["Camisa"],
        "Super_categoria": ["Uniforme"],
    })

    novo = dict(dados)
    novo["itens"] = pd.concat([dados["itens"], itens_ovd], ignore_index=True)
    novo["detalhes"] = pd.concat([dados["detalhes"], det_ovd], ignore_index=True)
    # 'produtos' permanece só com A e B → OVD é "inativo"
    return novo


class TestRestringirAAtivos:
    def test_dropa_itens_e_detalhes_do_inativo(self, dados_com_inativo):
        r = d.restringir_a_ativos(dados_com_inativo)
        ativos = {ID_A, ID_B}
        assert set(r["itens"]["ID_produto"].astype(str)) <= ativos
        assert set(r["detalhes"]["ID_produto"].astype(str)) <= ativos
        assert ID_OVD not in set(r["itens"]["ID_produto"].astype(str))
        assert ID_OVD not in set(r["detalhes"]["ID_produto"].astype(str))

    def test_idempotente(self, dados_com_inativo):
        r1 = d.restringir_a_ativos(dados_com_inativo)
        r2 = d.restringir_a_ativos(r1)
        assert len(r1["itens"]) == len(r2["itens"])
        assert len(r1["detalhes"]) == len(r2["detalhes"])

    def test_preserva_ativos(self, dados, dados_com_inativo):
        r = d.restringir_a_ativos(dados_com_inativo)
        assert len(r["itens"]) == len(dados["itens"])
        assert len(r["detalhes"]) == len(dados["detalhes"])


class TestInativoNaoVazaNosAgregados:
    def test_colegio_ovd_fora_da_sazonalidade_por_colegio(self, dados_com_inativo, config):
        saz = d.calcular_sazonalidade_por_colegio(dados_com_inativo, config)
        assert "OVD" not in set(saz["Colegio"])

    def test_sazonalidade_empresa_igual_sem_inativo(self, dados, dados_com_inativo, config):
        base = d.calcular_sazonalidade_empresa(dados, config)
        com = d.calcular_sazonalidade_empresa(dados_com_inativo, config)
        pd.testing.assert_series_equal(base["PesoNormalizado"], com["PesoNormalizado"])

    def test_proporcao_baixa_igual_sem_inativo(self, dados, dados_com_inativo, config):
        assert (d.calcular_proporcao_baixa(dados_com_inativo, config)
                == d.calcular_proporcao_baixa(dados, config))

    def test_crescimento_observado_ignora_inativo(self, dados_com_inativo, config):
        cfg = {**config, "demanda": {**config["demanda"], "crescimento_observado_ativo": True}}
        obs = d.calcular_crescimento_observado(dados_com_inativo, cfg)
        assert "OVD" not in obs

    def test_demanda_por_sku_sem_linha_do_inativo(self, dados_com_inativo, config):
        dem = d.calcular_demanda_mensal_por_sku(dados_com_inativo, config)
        assert "OVD" not in set(dem["Colegio"])
        assert ID_OVD not in set(dem["ID_produto"].astype(str))
