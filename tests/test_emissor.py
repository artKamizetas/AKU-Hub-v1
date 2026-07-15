"""
Testes do emissor (pedidos/emissor.py) — fakes de repositórios e HTTP,
sem rede/Supabase. Cobre: fluxo feliz dos dois momentos, CAS perdido,
rollback pré-POST, sem-rollback pós-POST + destravar, idempotência e
pré-validação do Olist.
"""

import pandas as pd
import pytest

from pedidos import emissor, estados as e
from tests.test_pedidos_repositorio import RepoFake as RepoPedFake, snapshot_min, grupos_min
from tests.test_integracoes_repositorio import RepoFake as RepoIntFake
from tests.test_integracoes_payloads import HttpFake, RespostaFake


def _repo_int_conectado():
    """Bling e Olist conectados (token válido 1h) e com config de negócio."""
    repo = RepoIntFake()
    expira = (pd.Timestamp.now(tz="UTC") + pd.Timedelta(hours=1)).isoformat()
    for plat in ("bling", "olist"):
        repo.salvar_chaves(plat, "cid", "sec", "https://app/configuracoes", "t")
        repo.concluir_oauth(plat, f"tok-{plat}", f"ref-{plat}", expira, "t")
    repo.salvar_config("bling", {"fornecedor_id": "987"}, "t")
    repo.salvar_config("olist", {"contato_id": "77", "vendedor_id": "88",
                                 "deposito_id": "99"}, "t")
    return repo


def _pedido_pronto(repo_ped):
    """Congela rodada fake e deixa o 1º pedido em PRONTO. Retorna pedido_id."""
    res = repo_ped.congelar_rodada(snapshot_min(), grupos_min())
    pid = repo_ped.listar_pedidos(res["id"]).iloc[0]["id"]
    repo_ped.transicionar_pedido(pid, e.RASCUNHO, e.PRONTO, "t")
    return pid


RESP_BLING = RespostaFake(200, {"data": {"id": 555, "numero": "PC-78"}})
RESP_OLIST = RespostaFake(200, {"id": 900, "numeroPedido": "V-12"})


# ---------------------------------------------------------------------------
# emitir_compra_bling
# ---------------------------------------------------------------------------
class TestEmitirCompra:
    def test_fluxo_feliz(self):
        repo_ped, repo_int = RepoPedFake(), _repo_int_conectado()
        pid = _pedido_pronto(repo_ped)
        http = HttpFake([RESP_BLING])

        res = emissor.emitir_compra_bling(pid, "diogo", repo_ped, repo_int, http)

        assert res == {"bling_id": "555", "bling_numero": "PC-78"}
        pedido = repo_ped.obter_pedido(pid)
        assert pedido["status"] == e.COMPRA_EMITIDA
        assert pedido["bling_id"] == "555" and pedido["bling_numero"] == "PC-78"
        ev = repo_int.listar_eventos().iloc[0]
        assert ev["acao"] == "emitir_compra" and bool(ev["sucesso"]) is True
        # payload enviado: itens do pedido, fornecedor da config
        metodo, url, corpo = http.chamadas[0]
        assert corpo["fornecedor"] == {"id": 987}
        assert corpo["observacoes"].startswith("AKU-PC")

    def test_cas_perdido_nao_toca_erp(self):
        repo_ped, repo_int = RepoPedFake(), _repo_int_conectado()
        res = repo_ped.congelar_rodada(snapshot_min(), grupos_min())
        pid = repo_ped.listar_pedidos(res["id"]).iloc[0]["id"]   # ainda RASCUNHO
        http = HttpFake([RESP_BLING])

        with pytest.raises(emissor.EmissaoFalhou, match="Outra sessão"):
            emissor.emitir_compra_bling(pid, "diogo", repo_ped, repo_int, http)
        assert http.chamadas == []
        assert repo_ped.obter_pedido(pid)["status"] == e.RASCUNHO

    def test_falha_pre_post_faz_rollback(self):
        repo_ped, repo_int = RepoPedFake(), _repo_int_conectado()
        repo_int.salvar_config("bling", {}, "t")   # sem fornecedor_id → falha no payload
        pid = _pedido_pronto(repo_ped)
        http = HttpFake([RESP_BLING])

        with pytest.raises(emissor.EmissaoFalhou, match="fornecedor_id"):
            emissor.emitir_compra_bling(pid, "diogo", repo_ped, repo_int, http)
        assert http.chamadas == []                                   # ERP intocado
        assert repo_ped.obter_pedido(pid)["status"] == e.PRONTO      # rollback
        ev = repo_int.listar_eventos().iloc[0]
        assert bool(ev["sucesso"]) is False and ev["detalhe"]["pos_post"] is False

    def test_erro_do_erp_faz_rollback(self):
        repo_ped, repo_int = RepoPedFake(), _repo_int_conectado()
        pid = _pedido_pronto(repo_ped)
        http = HttpFake([RespostaFake(400, {"error": {"description": "Fornecedor inválido"}})])

        with pytest.raises(emissor.EmissaoFalhou, match="Fornecedor inválido"):
            emissor.emitir_compra_bling(pid, "diogo", repo_ped, repo_int, http)
        assert repo_ped.obter_pedido(pid)["status"] == e.PRONTO

    def test_falha_pos_post_nao_faz_rollback(self):
        """Gravação do id falhou APÓS criar no Bling → fica travado em
        COMPRA_EMITINDO (destravar manual, conferir no ERP)."""
        class RepoQuebrado(RepoPedFake):
            def registrar_ids_emissao(self, *a, **kw):
                raise RuntimeError("supabase caiu")

        repo_ped, repo_int = RepoQuebrado(), _repo_int_conectado()
        pid = _pedido_pronto(repo_ped)
        http = HttpFake([RESP_BLING])

        with pytest.raises(emissor.EmissaoFalhou, match="supabase caiu"):
            emissor.emitir_compra_bling(pid, "diogo", repo_ped, repo_int, http)
        assert repo_ped.obter_pedido(pid)["status"] == e.COMPRA_EMITINDO   # travado
        ev = repo_int.listar_eventos().iloc[0]
        assert ev["detalhe"]["pos_post"] is True

        # destravar → volta a PRONTO + evento
        assert emissor.destravar(pid, "diogo", repo_ped, repo_int) is True
        assert repo_ped.obter_pedido(pid)["status"] == e.PRONTO
        assert repo_int.listar_eventos().iloc[0]["acao"] == "destravar"

    def test_idempotencia_bling_id_ja_gravado(self):
        """Reemissão após falha de commit: id já existe → não re-POSTa."""
        repo_ped, repo_int = RepoPedFake(), _repo_int_conectado()
        pid = _pedido_pronto(repo_ped)
        repo_ped.registrar_ids_emissao(pid, {"bling_id": "555", "bling_numero": "PC-78"}, "t")
        http = HttpFake([])   # qualquer chamada estouraria (lista vazia)

        res = emissor.emitir_compra_bling(pid, "diogo", repo_ped, repo_int, http)
        assert res["bling_id"] == "555"
        assert http.chamadas == []
        assert repo_ped.obter_pedido(pid)["status"] == e.COMPRA_EMITIDA


# ---------------------------------------------------------------------------
# emitir_venda_olist
# ---------------------------------------------------------------------------
MAPA = {"A-PP": 1, "A-M": 2, "B-PP": 3}


class TestEmitirVenda:
    def _pedido_compra_emitida(self, repo_ped, repo_int):
        pid = _pedido_pronto(repo_ped)
        emissor.emitir_compra_bling(pid, "t", repo_ped, repo_int, HttpFake([RESP_BLING]))
        return pid

    def test_fluxo_feliz_com_mapa_injetado(self):
        repo_ped, repo_int = RepoPedFake(), _repo_int_conectado()
        pid = self._pedido_compra_emitida(repo_ped, repo_int)
        http = HttpFake([RESP_OLIST])

        res = emissor.emitir_venda_olist(pid, "diogo", repo_ped, repo_int,
                                         mapa_sku=MAPA, http=http)
        assert res == {"olist_id": "900", "olist_numero": "V-12"}
        pedido = repo_ped.obter_pedido(pid)
        assert pedido["status"] == e.EMITIDO
        assert pedido["olist_id"] == "900"
        # amarração: numeroOrdemCompra = nº do Bling
        metodo, url, corpo = http.chamadas[0]
        assert corpo["numeroOrdemCompra"] == "PC-78"
        assert corpo["idContato"] == 77

    def test_ordem_obrigatoria_bling_primeiro(self):
        repo_ped, repo_int = RepoPedFake(), _repo_int_conectado()
        pid = _pedido_pronto(repo_ped)   # PRONTO, compra NÃO emitida
        with pytest.raises(emissor.EmissaoFalhou, match="Outra sessão"):
            emissor.emitir_venda_olist(pid, "diogo", repo_ped, repo_int,
                                       mapa_sku=MAPA, http=HttpFake([]))
        assert repo_ped.obter_pedido(pid)["status"] == e.PRONTO

    def test_sku_faltante_no_catalogo_faz_rollback(self):
        repo_ped, repo_int = RepoPedFake(), _repo_int_conectado()
        pid = self._pedido_compra_emitida(repo_ped, repo_int)
        # mapa None → busca catálogo; catálogo só tem A-PP
        http = HttpFake([RespostaFake(200, {"itens": [{"id": 1, "sku": "A-PP"}]})])

        with pytest.raises(emissor.EmissaoFalhou, match="sem match"):
            emissor.emitir_venda_olist(pid, "diogo", repo_ped, repo_int,
                                       mapa_sku=None, http=http)
        assert repo_ped.obter_pedido(pid)["status"] == e.COMPRA_EMITIDA   # rollback


# ---------------------------------------------------------------------------
# validar_pre_emissao_olist / preview_payloads
# ---------------------------------------------------------------------------
class TestValidacaoEPreview:
    def test_validacao_cfg_e_faltantes(self):
        itens = pd.DataFrame({"sku": ["A", "B"], "quantidade_final": [2, 4]})
        erros = emissor.validar_pre_emissao_olist(itens, {}, {"A": 1})
        assert any("Config do Olist incompleta" in x for x in erros)
        assert any("B" in x for x in erros)

        cfg_ok = {"contato_id": "1", "vendedor_id": "2", "deposito_id": "3"}
        assert emissor.validar_pre_emissao_olist(itens, cfg_ok, {"A": 1, "B": 2}) == []

    def test_preview_compra_ok_venda_bloqueada_antes_do_bling(self):
        repo_ped, repo_int = RepoPedFake(), _repo_int_conectado()
        pid = _pedido_pronto(repo_ped)

        prev = emissor.preview_payloads(pid, repo_ped, repo_int, mapa_sku=MAPA)
        assert prev["compra"]["fornecedor"] == {"id": 987}    # payload real
        assert "emita a compra primeiro" in prev["venda"]["erro"]

    def test_preview_completo_pos_compra(self):
        repo_ped, repo_int = RepoPedFake(), _repo_int_conectado()
        pid = _pedido_pronto(repo_ped)
        emissor.emitir_compra_bling(pid, "t", repo_ped, repo_int, HttpFake([RESP_BLING]))

        prev = emissor.preview_payloads(pid, repo_ped, repo_int, mapa_sku=MAPA)
        assert prev["venda"]["numeroOrdemCompra"] == "PC-78"
