"""
Testes das regras PURAS das revisões (pedidos/revisoes.py): linha de base,
envio parcial, o que cada ERP confirmou e o diff "emitido × novo".
"""

import pandas as pd

from pedidos import estados as e
from pedidos import revisoes


def rev(numero, tipo=e.REVISAO_ALTERACAO, itens=None, bling=False, olist=False,
        concluida=False):
    quando = "2026-08-01T10:00:00+00:00"
    return {"id": f"rev-{numero}", "numero": numero, "tipo": tipo,
            "itens": itens if itens is not None else [
                {"item_id": "i1", "sku": "A-P", "id_produto_bling": "101",
                 "quantidade": 10, "custo_unit": 5.0},
                {"item_id": "i2", "sku": "A-M", "id_produto_bling": "102",
                 "quantidade": 0, "custo_unit": 5.0}],
            "bling_ok_em": quando if bling else None,
            "olist_ok_em": quando if olist else None,
            "concluida_em": quando if concluida else None}


EMISSAO = rev(1, e.REVISAO_EMISSAO, bling=True, olist=True, concluida=True)


class TestLinhaDeBase:
    def test_sem_revisao_nao_ha_base(self):
        assert revisoes.linha_de_base([]) == {}
        assert revisoes.pendente([]) == {}

    def test_ultima_concluida_vence(self):
        rev2 = rev(2, bling=True, olist=True, concluida=True)
        assert revisoes.linha_de_base([EMISSAO, rev2])["numero"] == 2

    def test_pendente_nao_e_base(self):
        historico = [EMISSAO, rev(2, olist=True)]
        assert revisoes.linha_de_base(historico)["numero"] == 1
        assert revisoes.pendente(historico)["numero"] == 2

    def test_cancelamento_nunca_e_base(self):
        """O descarte e o diff precisam das quantidades emitidas, não do cancelamento."""
        cancelado = rev(2, e.REVISAO_CANCELAMENTO, bling=True, olist=True, concluida=True)
        assert revisoes.linha_de_base([EMISSAO, cancelado])["numero"] == 1

    def test_parcial_antiga_superada_por_uma_concluida_some(self):
        historico = [EMISSAO, rev(2, olist=True),
                     rev(3, bling=True, olist=True, concluida=True)]
        assert revisoes.pendente(historico) == {}
        assert revisoes.envio_parcial(historico) == {}
        assert revisoes.linha_de_base(historico)["numero"] == 3


class TestEnvioParcial:
    def test_pendente_sem_carimbo_nao_e_parcial(self):
        assert revisoes.envio_parcial([EMISSAO, rev(2)]) == {}

    def test_um_erp_confirmou_e_parcial(self):
        assert revisoes.envio_parcial([EMISSAO, rev(2, olist=True)])["numero"] == 2

    def test_confirmada_no_erp_olha_cada_lado_separado(self):
        """No envio parcial o Olist está na revisão 2 e o Bling ainda na 1."""
        historico = [EMISSAO, rev(2, olist=True)]
        assert revisoes.confirmada_no_erp(historico, "olist")["numero"] == 2
        assert revisoes.confirmada_no_erp(historico, "bling")["numero"] == 1

    def test_confirmada_no_erp_sem_venda_emitida(self):
        so_compra = rev(1, e.REVISAO_EMISSAO, bling=True, concluida=True)
        assert revisoes.confirmada_no_erp([so_compra], "olist") == {}


class TestRetratoEDiferencas:
    def itens(self):
        return pd.DataFrame([
            {"id": "i1", "sku": "A-P", "id_produto_bling": "101", "produto": "Camisa P",
             "tamanho": "P", "quantidade_final": 6, "custo_unit": 5.0},
            {"id": "i2", "sku": "A-M", "id_produto_bling": "102", "produto": "Camisa M",
             "tamanho": "M", "quantidade_final": 0, "custo_unit": 5.0},
            {"id": "i3", "sku": "A-G", "id_produto_bling": "103", "produto": "Camisa G",
             "tamanho": "G", "quantidade_final": 4, "custo_unit": 5.0},
        ])

    def test_retrato_leva_todos_os_itens_inclusive_zerados(self):
        retrato = revisoes.retrato_itens(self.itens())
        assert [(i["item_id"], i["quantidade"]) for i in retrato] == [
            ("i1", 6), ("i2", 0), ("i3", 4)]
        assert retrato[0]["id_produto_bling"] == "101" and retrato[0]["custo_unit"] == 5.0

    def test_itens_no_erp_so_o_que_tem_quantidade(self):
        assert revisoes.itens_no_erp(EMISSAO) == [
            {"sku": "A-P", "id_produto_bling": "101", "quantidade": 10}]

    def test_diferencas_so_o_que_muda(self):
        """A-P muda 10→6, A-M segue zerado (não é diferença), A-G é item novo."""
        difs = revisoes.diferencas(self.itens(), EMISSAO)
        assert [(d["sku"], d["emitida"], d["nova"]) for d in difs] == [
            ("A-P", 10, 6), ("A-G", 0, 4)]

    def test_sem_mudanca_nao_ha_diferenca(self):
        itens = self.itens().iloc[:2].copy()
        itens.loc[0, "quantidade_final"] = 10
        assert revisoes.diferencas(itens, EMISSAO) == []
