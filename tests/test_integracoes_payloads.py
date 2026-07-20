"""
Testes dos clientes Bling/Olist (pedidos/integracoes/{bling,olist}.py):
payload builders PUROS + camada HTTP com fake — sem rede.
"""

import pandas as pd
import pytest

from pedidos.integracoes import bling, olist


class RespostaFake:
    def __init__(self, status_code=200, corpo=None, text="", headers=None):
        self.status_code = status_code
        self._corpo = corpo if corpo is not None else {}
        self.text = text
        self.headers = headers or {}

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
    df = pd.DataFrame(linhas, columns=["sku", "id_produto_bling", "produto",
                                       "quantidade_final", "custo_unit"])
    # memória curada do congelamento (DDL 004) → descricaoDetalhada do item
    df["quantidade_sugerida"] = df["quantidade_final"]
    df["memoria_sugerida"] = [{"demanda_periodo": 30.0, "estoque_seguranca": 12.0,
                               "estoque_projetado": 0.0, "estoque_meta": 42.0}] * len(df)
    return df


PEDIDO = {"titulo": "NEVES - CALÇAS - R08/2026", "id": "ped-uuid"}
RODADA = {"data_chegada": "2026-08-29", "mes_disparo": 8, "ano_disparo": 2026}
CFG_BLING = {"fornecedor_id": "987", "forma_pagamento_id": "555",
             "prazo_pagamento_dias": 30}
OBS = "NEVES - CALÇAS - R08/2026\nRodada...\nItens...\nOrigem... ref: ped-uuid"


# ---------------------------------------------------------------------------
# Bling — montar_payload_compra (puro)
# ---------------------------------------------------------------------------
class TestPayloadCompra:
    def test_payload_completo(self):
        p = bling.montar_payload_compra(PEDIDO, itens_df(), RODADA, CFG_BLING, OBS,
                                        data_emissao="2026-08-01")
        assert p["fornecedor"] == {"id": 987}
        assert p["dataPrevista"] == "2026-08-29"
        assert p["observacoesInternas"] == PEDIDO["titulo"]   # título curto → busca/listagem
        assert p["observacoes"] == OBS                        # bloco completo
        assert len(p["itens"]) == 2                       # zero excluído
        assert p["itens"][0] == {
            "produto": {"id": 111}, "codigoFornecedor": "CAL-P", "unidade": "PÇ",
            "quantidade": 10, "valor": 50.0, "descricao": "Calça P",
            "descricaoDetalhada": ("Alvo 42 = demanda 30 + segurança 12 - "
                                   "projetado 0 → 10 pç | R08/2026")}

    def test_unidade_do_config_sobrescreve_default(self):
        p = bling.montar_payload_compra(PEDIDO, itens_df(), RODADA,
                                        dict(CFG_BLING, unidade_padrao="UN"), OBS)
        assert all(i["unidade"] == "UN" for i in p["itens"])

    def test_parcela_unica_no_prazo(self):
        """Vencimento = emissão + prazo; valor = total dos itens válidos."""
        p = bling.montar_payload_compra(PEDIDO, itens_df(), RODADA, CFG_BLING, OBS,
                                        data_emissao="2026-08-01")
        assert p["data"] == "2026-08-01"
        assert p["parcelas"] == [{"valor": 710.0,          # 10×50 + 4×52,50
                                  "dataVencimento": "2026-08-31",
                                  "formaPagamento": {"id": 555}}]

    def test_sem_forma_pagamento_omite_parcelas(self):
        """Payload segue montável (preview); quem barra o clique é a pré-validação."""
        p = bling.montar_payload_compra(PEDIDO, itens_df(), RODADA,
                                        {"fornecedor_id": "987"}, OBS)
        assert "parcelas" not in p

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
        assert p["observacoesInternas"] == PEDIDO["titulo"]   # espelha o Bling
        assert p["observacoes"] == OBS
        assert len(p["itens"]) == 2
        assert p["itens"][0] == {"produto": {"id": 1001, "tipo": "P"},
                                 "quantidade": 10, "valorUnitario": 50.0,
                                 "infoAdicional": "CAL-P"}

    def test_data_prevista_espelha_a_chegada_da_rodada(self):
        # Mesmo valor que vai no Bling — os dois ERPs não podem divergir.
        p = olist.montar_payload_venda(PEDIDO, itens_df(), RODADA, CFG_OLIST,
                                       MAPA, "PC-1", OBS)
        b = bling.montar_payload_compra(PEDIDO, itens_df(), RODADA,
                                        {"fornecedor_id": "1"}, OBS)
        assert p["dataPrevista"] == b["dataPrevista"]

    def test_sem_forma_recebimento_nao_manda_pagamento(self):
        p = olist.montar_payload_venda(PEDIDO, itens_df(), RODADA, CFG_OLIST,
                                       MAPA, "PC-1", OBS)
        assert "pagamento" not in p and "data" not in p

    def test_pagamento_parcela_unica(self):
        cfg = {**CFG_OLIST, "forma_recebimento_id": "5"}
        p = olist.montar_payload_venda(PEDIDO, itens_df(), RODADA, cfg, MAPA,
                                       "PC-1", OBS, prazo_dias=30,
                                       data_emissao="2026-07-20")
        assert p["data"] == "2026-07-20"
        pag = p["pagamento"]
        assert pag["formaRecebimento"] == {"id": 5}
        assert "meioPagamento" not in pag          # opcional, não configurado
        assert len(pag["parcelas"]) == 1
        parcela = pag["parcelas"][0]
        assert parcela["dias"] == 30
        assert parcela["data"] == "2026-08-19"     # emissão + 30
        assert parcela["valor"] == 10 * 50.0 + 4 * 52.5   # o zerado fica de fora
        assert parcela["formaRecebimento"] == {"id": 5}

    def test_vencimento_bate_com_o_do_bling(self):
        # Mesmo acordo, dois documentos: o prazo do Bling manda nos dois.
        cfg = {**CFG_OLIST, "forma_recebimento_id": "5"}
        prazo = CFG_BLING["prazo_pagamento_dias"]
        venda = olist.montar_payload_venda(PEDIDO, itens_df(), RODADA, cfg, MAPA,
                                           "PC-1", OBS, prazo_dias=prazo,
                                           data_emissao="2026-07-20")
        compra = bling.montar_payload_compra(PEDIDO, itens_df(), RODADA, CFG_BLING,
                                             OBS, data_emissao="2026-07-20")
        assert (venda["pagamento"]["parcelas"][0]["data"]
                == compra["parcelas"][0]["dataVencimento"])
        assert venda["data"] == compra["data"]

    def test_prazo_ausente_cai_no_default(self):
        cfg = {**CFG_OLIST, "forma_recebimento_id": "5"}
        p = olist.montar_payload_venda(PEDIDO, itens_df(), RODADA, cfg, MAPA,
                                       "PC-1", OBS, data_emissao="2026-07-20")
        assert p["pagamento"]["parcelas"][0]["dias"] == olist.PRAZO_PAGAMENTO_PADRAO

    def test_meio_pagamento_quando_configurado(self):
        cfg = {**CFG_OLIST, "forma_recebimento_id": "5", "meio_pagamento_id": "2"}
        p = olist.montar_payload_venda(PEDIDO, itens_df(), RODADA, cfg, MAPA,
                                       "PC-1", OBS, data_emissao="2026-07-20")
        assert p["pagamento"]["meioPagamento"] == {"id": 2}
        assert p["pagamento"]["parcelas"][0]["meioPagamento"] == {"id": 2}

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
    def test_busca_direcionada_por_codigo(self):
        # 1 GET por SKU distinto, filtrando por ?codigo= (não varre o catálogo).
        http = HttpFake([
            RespostaFake(200, {"itens": [{"id": 100, "sku": "CAL-P"}]}),
            RespostaFake(200, {"itens": [{"id": 101, "codigo": "CAL-G"}]}),  # aceita sku OU codigo
            RespostaFake(200, {"itens": []}),                                # sem match
        ])
        mapa, faltantes = olist.mapear_produtos_por_sku(
            "tok", ["CAL-P", "CAL-G", "NAO-EXISTE"], http)
        assert mapa == {"CAL-P": 100, "CAL-G": 101}
        assert faltantes == ["NAO-EXISTE"]
        assert len(http.chamadas) == 3
        assert http.chamadas[0][2] == {"codigo": "CAL-P", "limit": olist._PAGINA}

    def test_match_exato_ignora_parciais_do_filtro(self):
        # ?codigo=CAL-P pode trazer CAL-PP junto; só o código idêntico casa.
        http = HttpFake([RespostaFake(200, {"itens": [
            {"id": 5, "sku": "CAL-PP"}, {"id": 6, "sku": "CAL-P"}]})])
        mapa, faltantes = olist.mapear_produtos_por_sku("tok", ["CAL-P"], http)
        assert mapa == {"CAL-P": 6} and faltantes == []

    def test_sku_repetido_faz_um_unico_get(self):
        http = HttpFake([RespostaFake(200, {"itens": [{"id": 9, "sku": "X"}]})])
        mapa, _ = olist.mapear_produtos_por_sku("tok", ["X", "X", " X "], http)
        assert mapa == {"X": 9} and len(http.chamadas) == 1

    def test_retry_em_429_respeita_retry_after(self):
        esperas = []
        http = HttpFake([
            RespostaFake(429, {"mensagem": "rate limit"}, headers={"Retry-After": "2"}),
            RespostaFake(200, {"itens": [{"id": 7, "sku": "X"}]}),
        ])
        mapa, _ = olist.mapear_produtos_por_sku(
            "tok", ["X"], http, dormir=esperas.append)
        assert mapa == {"X": 7}
        assert esperas == [2]                 # esperou os 2s do cabeçalho e reemitiu
        assert len(http.chamadas) == 2

    def test_429_persistente_acaba_levantando(self):
        http = HttpFake([RespostaFake(429, {"mensagem": "rate limit"})
                         for _ in range(olist._TENTATIVAS_429)])
        with pytest.raises(olist.OlistFalhou, match="429"):
            olist.mapear_produtos_por_sku("tok", ["X"], http, dormir=lambda _s: None)

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
