"""
Testes dos clientes Bling/Olist (pedidos/integracoes/{bling,olist}.py):
payload builders PUROS + camada HTTP com fake — sem rede.
"""

import pandas as pd
import pytest

from pedidos.integracoes import bling, olist


class RespostaFake:
    def __init__(self, status_code=200, corpo=None, text=""):
        self.status_code = status_code
        self._corpo = corpo if corpo is not None else {}
        self.text = text

    def json(self):
        return self._corpo


class HttpFake:
    """Devolve respostas programadas em sequência; registra chamadas."""

    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.chamadas = []   # [(metodo, url, params_ou_json)]

    def get(self, url, headers=None, params=None):
        self.chamadas.append(("get", url, dict(params or {})))
        return self.respostas.pop(0)

    def post(self, url, headers=None, json=None):
        self.chamadas.append(("post", url, dict(json or {})))
        return self.respostas.pop(0)


def itens_df(linhas=None):
    """Itens no shape de repo.listar_itens()."""
    if linhas is None:
        linhas = [
            ("CAL-P", "111", "Calça P", 10, 50.0),
            ("CAL-M", "222", "Calça M", 0, 50.0),    # zero → fora do payload
            ("CAL-G", "333", "Calça G", 4, 52.5),
        ]
    return pd.DataFrame(linhas, columns=["sku", "id_produto_bling", "produto",
                                         "quantidade_final", "custo_unit"])


PEDIDO = {"titulo": "AKU-PC · NEVES · CALÇAS · R08/2026", "id": "ped-uuid"}
RODADA = {"data_chegada": "2026-08-29"}
OBS = "AKU-PC · NEVES · CALÇAS · R08/2026\nRodada...\nItens...\nOrigem... ref: ped-uuid"


# ---------------------------------------------------------------------------
# Bling — montar_payload_compra (puro)
# ---------------------------------------------------------------------------
class TestPayloadCompra:
    def test_payload_completo(self):
        p = bling.montar_payload_compra(PEDIDO, itens_df(), RODADA,
                                        {"fornecedor_id": "987"}, OBS)
        assert p["fornecedor"] == {"id": 987}
        assert p["dataPrevista"] == "2026-08-29"
        assert p["observacoes"] == PEDIDO["titulo"]
        assert p["observacoesInternas"] == OBS
        assert len(p["itens"]) == 2                       # zero excluído
        assert p["itens"][0] == {"produto": {"id": 111}, "quantidade": 10,
                                 "valor": 50.0, "descricao": "Calça P"}

    def test_sem_fornecedor_levanta(self):
        with pytest.raises(ValueError, match="fornecedor_id"):
            bling.montar_payload_compra(PEDIDO, itens_df(), RODADA, {}, OBS)

    def test_item_sem_id_produto_levanta(self):
        df = itens_df([("X-P", "", "X", 5, 1.0)])
        with pytest.raises(ValueError, match="X-P"):
            bling.montar_payload_compra(PEDIDO, df, RODADA, {"fornecedor_id": "1"}, OBS)

    def test_tudo_zero_levanta(self):
        df = itens_df([("X-P", "1", "X", 0, 1.0)])
        with pytest.raises(ValueError, match="quantidade final"):
            bling.montar_payload_compra(PEDIDO, df, RODADA, {"fornecedor_id": "1"}, OBS)


# ---------------------------------------------------------------------------
# Olist — montar_payload_venda (puro)
# ---------------------------------------------------------------------------
CFG_OLIST = {"contato_id": "77", "vendedor_id": "88", "deposito_id": "99"}
MAPA = {"CAL-P": 1001, "CAL-G": 1003}


class TestPayloadVenda:
    def test_payload_completo(self):
        p = olist.montar_payload_venda(PEDIDO, itens_df(), RODADA, CFG_OLIST,
                                       MAPA, "PC-4521", OBS)
        assert p["idContato"] == 77
        assert p["vendedor"] == {"id": 88} and p["deposito"] == {"id": 99}
        assert p["situacao"] == 0                          # default Aberta
        assert p["numeroOrdemCompra"] == "PC-4521"         # amarração com o Bling
        assert p["observacoesInternas"] == OBS
        assert len(p["itens"]) == 2
        assert p["itens"][0] == {"produto": {"id": 1001}, "quantidade": 10,
                                 "valorUnitario": 50.0, "infoAdicional": "CAL-P"}

    def test_situacao_customizada(self):
        p = olist.montar_payload_venda(PEDIDO, itens_df(), RODADA,
                                       {**CFG_OLIST, "situacao": 3}, MAPA, "PC-1", OBS)
        assert p["situacao"] == 3

    def test_cfg_incompleta_lista_faltantes(self):
        with pytest.raises(ValueError, match="vendedor_id, deposito_id"):
            olist.montar_payload_venda(PEDIDO, itens_df(), RODADA,
                                       {"contato_id": "77"}, MAPA, "PC-1", OBS)

    def test_sem_numero_bling_levanta(self):
        with pytest.raises(ValueError, match="emita a compra primeiro"):
            olist.montar_payload_venda(PEDIDO, itens_df(), RODADA, CFG_OLIST,
                                       MAPA, "", OBS)

    def test_sku_fora_do_mapa_levanta(self):
        with pytest.raises(ValueError, match="CAL-G"):
            olist.montar_payload_venda(PEDIDO, itens_df(), RODADA, CFG_OLIST,
                                       {"CAL-P": 1001}, "PC-1", OBS)


# ---------------------------------------------------------------------------
# Olist — mapeamento SKU → id (HTTP fake paginado)
# ---------------------------------------------------------------------------
class TestMapearProdutos:
    def test_paginacao_e_match(self):
        pagina1 = [{"id": i, "sku": f"SKU-{i}"} for i in range(100)]
        pagina2 = [{"id": 100, "sku": "CAL-P"}, {"id": 101, "codigo": "CAL-G"}]
        http = HttpFake([RespostaFake(200, {"itens": pagina1}),
                         RespostaFake(200, {"itens": pagina2})])
        mapa, faltantes = olist.mapear_produtos_por_sku(
            "tok", ["CAL-P", "CAL-G", "NAO-EXISTE"], http)
        assert mapa == {"CAL-P": 100, "CAL-G": 101}        # aceita sku OU codigo
        assert faltantes == ["NAO-EXISTE"]
        assert len(http.chamadas) == 2                     # 2 páginas, 1 varredura
        assert http.chamadas[1][2]["offset"] == 100

    def test_erro_da_api_levanta(self):
        http = HttpFake([RespostaFake(401, {"mensagem": "token inválido"})])
        with pytest.raises(olist.OlistFalhou, match="401"):
            olist.mapear_produtos_por_sku("tok", ["X"], http)


# ---------------------------------------------------------------------------
# Camada HTTP fina (criar/testar)
# ---------------------------------------------------------------------------
class TestHttpBling:
    def test_criar_pedido_compra_extrai_id_e_numero(self):
        http = HttpFake([RespostaFake(200, {"data": {"id": 555, "numero": "78"}})])
        res = bling.criar_pedido_compra("tok", {"fornecedor": {"id": 1}}, http)
        assert res == {"bling_id": "555", "bling_numero": "78"}
        metodo, url, corpo = http.chamadas[0]
        assert metodo == "post" and url.endswith("/pedidos/compras")

    def test_erro_legivel(self):
        http = HttpFake([RespostaFake(
            400, {"error": {"description": "Fornecedor inválido"}})])
        with pytest.raises(bling.BlingFalhou, match="Fornecedor inválido"):
            bling.criar_pedido_compra("tok", {}, http)

    def test_testar_conexao(self):
        assert bling.testar_conexao("tok", HttpFake([RespostaFake(200)]))[0] is True
        ok, msg = bling.testar_conexao("tok", HttpFake([RespostaFake(401, text="x")]))
        assert ok is False and "401" in msg

    def test_pedido_exemplo_vazio(self):
        http = HttpFake([RespostaFake(200, {"data": []})])
        assert bling.obter_pedido_compra_exemplo("tok", http) == {}


class TestHttpOlist:
    def test_criar_pedido_venda_extrai_id_e_numero(self):
        http = HttpFake([RespostaFake(200, {"id": 900, "numeroPedido": "V-12"})])
        res = olist.criar_pedido_venda("tok", {"idContato": 1}, http)
        assert res == {"olist_id": "900", "olist_numero": "V-12"}

    def test_erro_legivel(self):
        http = HttpFake([RespostaFake(400, {"mensagem": "depósito inválido"})])
        with pytest.raises(olist.OlistFalhou, match="depósito inválido"):
            olist.criar_pedido_venda("tok", {}, http)

    def test_testar_conexao(self):
        assert olist.testar_conexao("tok", HttpFake([RespostaFake(200, {"itens": []})]))[0] is True
