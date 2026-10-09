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
        # título curto nas observações internas (busca/listagem); bloco no público
        assert corpo["observacoesInternas"] == "NEVES - CALÇAS - R08/2026"
        assert corpo["observacoes"].startswith("NEVES - CALÇAS - R08/2026")

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
        # mapa None → resolver: 1 GET no pai "A" + fallback exato A-M/A-PP,
        # todos vazios → nada casa → faltantes
        http = HttpFake([RespostaFake(200, {"itens": []}) for _ in range(3)])

        with pytest.raises(emissor.EmissaoFalhou, match="sem match"):
            emissor.emitir_venda_olist(pid, "diogo", repo_ped, repo_int,
                                       mapa_sku=None, http=http)
        assert repo_ped.obter_pedido(pid)["status"] == e.COMPRA_EMITIDA   # rollback


# ---------------------------------------------------------------------------
# resolver_ids_olist — cache → família → fallback exato
# ---------------------------------------------------------------------------
class TestResolverIdsOlist:
    def test_cache_quente_nao_toca_a_api(self):
        repo = _repo_int_conectado()
        repo.gravar_cache_produtos_olist({"A-PP": 1, "A-M": 2})
        http = HttpFake([])   # qualquer chamada estouraria (lista vazia)
        mapa, faltantes = emissor.resolver_ids_olist(["A-PP", "A-M"], repo, http)
        assert mapa == {"A-PP": 1, "A-M": 2} and faltantes == []
        assert http.chamadas == []

    def test_resolve_por_familia_grava_e_reusa_cache(self):
        repo = _repo_int_conectado()
        http = HttpFake([
            RespostaFake(200, {"itens": [{"id": 10, "sku": "A"}]}),        # pai
            RespostaFake(200, {"id": 10, "sku": "A", "variacoes": [        # grade
                {"id": 11, "sku": "A-PP"}, {"id": 12, "sku": "A-M"},
                {"id": 13, "sku": "A-G"}]}),
        ])
        mapa, faltantes = emissor.resolver_ids_olist(["A-PP", "A-M"], repo, http)
        assert mapa == {"A-PP": 11, "A-M": 12}   # devolve só o pedido
        assert faltantes == []

        # 2ª chamada: cache aquecido, inclusive o irmão A-G que nem foi pedido
        http2 = HttpFake([])
        mapa2, _ = emissor.resolver_ids_olist(["A-G"], repo, http2)
        assert mapa2 == {"A-G": 13} and http2.chamadas == []

    def test_fallback_exato_e_faltante(self):
        repo = _repo_int_conectado()
        http = HttpFake([
            RespostaFake(200, {"itens": []}),   # pai "Z" não existe
            RespostaFake(200, {"itens": []}),   # exato "Z-P" também não
        ])
        mapa, faltantes = emissor.resolver_ids_olist(["Z-P"], repo, http)
        assert mapa == {} and faltantes == ["Z-P"]


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

    def test_validacao_bling_exige_forma_pagamento(self):
        """Sem forma de pagamento o pedido sairia sem parcela/vencimento —
        barra ANTES do clique, não no erro do POST."""
        itens = pd.DataFrame({"sku": ["A"], "quantidade_final": [2],
                              "id_produto_bling": ["111"]})
        erros = emissor.validar_pre_emissao_bling(itens, {"fornecedor_id": "9"})
        assert any("forma de pagamento" in x for x in erros)
        assert emissor.validar_pre_emissao_bling(
            itens, {"fornecedor_id": "9", "forma_pagamento_id": "555"}) == []

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


class TestProntidaoOlist:
    """
    Guarda o aviso que faltava quando 21 compras saíram no Bling e a venda
    quebrou depois (token do Olist expirado + config vazia).
    """

    def test_tudo_pronto_nao_avisa(self):
        assert emissor.checar_prontidao_olist(_repo_int_conectado()) == []

    def test_token_morto_avisa(self):
        repo = _repo_int_conectado()
        vencido = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=2)).isoformat()
        repo.concluir_oauth("olist", "tok", "ref-morto", vencido, "t")
        # refresh recusado, como o "Token is not active" do Keycloak
        http = HttpFake([RespostaFake(400, {"error": "invalid_grant"})])
        avisos = emissor.checar_prontidao_olist(repo, http)
        assert any("token utilizável" in a for a in avisos)

    def test_config_vazia_avisa_os_tres_ids(self):
        repo = _repo_int_conectado()
        repo.salvar_config("olist", {}, "t")
        avisos = emissor.checar_prontidao_olist(repo)
        assert any("contato_id" in a and "vendedor_id" in a and "deposito_id" in a
                   for a in avisos)

    def test_nunca_levanta(self):
        class RepoQuebrado:
            def ler(self, _):
                raise RuntimeError("supabase fora")
        assert len(emissor.checar_prontidao_olist(RepoQuebrado())) == 1


# ===========================================================================
# PÓS-EMISSÃO — alterar e cancelar um pedido que já está nos ERPs
# ===========================================================================
from pedidos import revisoes                                          # noqa: E402
from tests.test_pedidos_repositorio import TAB_PEDIDO, TAB_ITEM       # noqa: E402
from tests.test_integracoes_payloads import bling_pedido              # noqa: E402

OK204 = RespostaFake(204)


def _grupos_pos():
    """Um pedido com dois SKUs de ids de produto DIFERENTES (como no cadastro real)."""
    base = {"produto": "Calça", "categoria": "Calça", "custo_unit": 50.0}
    return [{"colegio": "NEVES", "super_categoria": "CALÇAS",
             "titulo": "NEVES - CALÇAS - R08/2026", "criado_por": "tester",
             "itens": [
                 {**base, "sku": "CAL-P", "id_produto_bling": "111", "tamanho": "P",
                  "quantidade_sugerida": 10, "quantidade_final": 10},
                 {**base, "sku": "CAL-G", "id_produto_bling": "333", "tamanho": "G",
                  "quantidade_sugerida": 4, "quantidade_final": 4}]}]


def _emitido(com_olist=True):
    """(repo_ped, repo_int, pedido_id) com o pedido já emitido e com linha de base."""
    repo_ped, repo_int = RepoPedFake(), _repo_int_conectado()
    repo_int.salvar_config("bling", {"fornecedor_id": "987",
                                     "situacao_cancelado_id": "4403"}, "t")
    repo_int.gravar_cache_produtos_olist({"CAL-P": 1001, "CAL-G": 1003})
    res = repo_ped.congelar_rodada(snapshot_min(), _grupos_pos())
    pid = repo_ped.listar_pedidos(res["id"]).iloc[0]["id"]
    next(p for p in repo_ped.tabelas[TAB_PEDIDO] if p["id"] == pid).update({
        "status": e.EMITIDO if com_olist else e.COMPRA_EMITIDA,
        "bling_id": "555", "bling_numero": "78",
        "olist_id": "900" if com_olist else None,
        "olist_numero": "12" if com_olist else None})
    repo_ped.registrar_revisao(pid, e.REVISAO_EMISSAO, "t", motivo="Emissão",
                               erps_ok=("bling", "olist") if com_olist else ("bling",),
                               concluida=True)
    return repo_ped, repo_int, pid


def _em_alteracao(com_olist=True, cal_p=6):
    """Pedido emitido, com alteração aberta e CAL-P mudado de 10 para `cal_p`."""
    repo_ped, repo_int, pid = _emitido(com_olist)
    repo_ped.abrir_alteracao(pid, "diogo")
    item = next(i for i in repo_ped.tabelas[TAB_ITEM] if i["sku"] == "CAL-P")
    repo_ped.atualizar_quantidades(pid, [{"id": item["id"], "quantidade_final": cal_p}], "diogo")
    return repo_ped, repo_int, pid


def _get_bling(valor=0, **kw):
    return RespostaFake(200, {"data": bling_pedido(valor=valor, **kw)})


def _get_olist(situacao=0, itens=None):
    return RespostaFake(200, {"id": 900, "numeroPedido": 12, "situacao": situacao,
                              "itens": itens if itens is not None else [
        {"produto": {"id": 1001, "sku": "CAL-P"}, "quantidade": 10, "valorUnitario": 50.0},
        {"produto": {"id": 1003, "sku": "CAL-G"}, "quantidade": 4, "valorUnitario": 50.0}]})


PUT_BLING = RespostaFake(200, {"data": {"id": 555, "numero": 78}})


def _metodos(http):
    return [(m, url.rsplit("/v3", 1)[-1]) for m, url, _ in http.chamadas]


# ---------------------------------------------------------------------------
# O filtro: status nativo de cada ERP
# ---------------------------------------------------------------------------
class TestAvaliarAbertura:
    PEDIDO = {"bling_id": "555", "bling_numero": "78", "olist_id": "900", "olist_numero": "12"}
    BLING_ABERTO = {"situacao_valor": 0, "situacao_rotulo": "Em aberto"}
    OLIST_ABERTA = {"situacao": 0, "situacao_rotulo": "Aberta"}

    def test_aberto_nos_dois_libera(self):
        assert emissor.avaliar_abertura(self.PEDIDO, self.BLING_ABERTO, self.OLIST_ABERTA) == []

    def test_bling_em_andamento_bloqueia(self):
        imp = emissor.avaliar_abertura(
            self.PEDIDO, {"situacao_valor": 3, "situacao_rotulo": "Em andamento"},
            self.OLIST_ABERTA)
        assert len(imp) == 1 and "Bling" in imp[0] and "Em andamento" in imp[0]

    def test_olist_aprovada_bloqueia(self):
        """Aprovada = a fábrica já assumiu o pedido. Só 'Aberta' libera."""
        imp = emissor.avaliar_abertura(
            self.PEDIDO, self.BLING_ABERTO, {"situacao": 3, "situacao_rotulo": "Aprovada"})
        assert len(imp) == 1 and "Olist" in imp[0] and "Aprovada" in imp[0]

    def test_sem_venda_emitida_o_olist_nao_entra_na_conta(self):
        so_compra = {"bling_id": "555", "bling_numero": "78", "olist_id": None}
        assert emissor.avaliar_abertura(so_compra, self.BLING_ABERTO, None) == []

    def test_erp_que_nao_respondeu_bloqueia(self):
        assert len(emissor.avaliar_abertura(self.PEDIDO, None, None)) == 2


class TestCompararComErp:
    ENVIADO = [{"sku": "CAL-P", "id_produto_bling": "111", "quantidade": 10},
               {"sku": "CAL-G", "id_produto_bling": "333", "quantidade": 4}]

    def _bling(self, *itens):
        return {"itens": [{"id_produto": i, "sku": s, "quantidade": q} for i, s, q in itens]}

    def _olist(self, *itens):
        return {"itens": [{"id_produto": "", "sku": s, "quantidade": q} for s, q in itens]}

    def test_erp_igual_ao_enviado_nao_diverge(self):
        assert emissor.comparar_com_erp(
            self.ENVIADO, self.ENVIADO,
            self._bling(("111", "CAL-P", 10), ("333", "CAL-G", 4)),
            self._olist(("CAL-P", 10), ("CAL-G", 4))) == []

    def test_quantidade_mexida_so_no_bling(self):
        difs = emissor.comparar_com_erp(
            self.ENVIADO, self.ENVIADO,
            self._bling(("111", "CAL-P", 8), ("333", "CAL-G", 4)),
            self._olist(("CAL-P", 10), ("CAL-G", 4)))
        assert difs == [{"sku": "CAL-P", "enviado": 10, "bling": 8, "olist": 10,
                         "diverge_bling": True, "diverge_olist": False}]

    def test_bling_casa_pelo_id_do_produto_nao_pelo_texto_do_codigo(self):
        """O 'código do fornecedor' é texto livre no Bling — alguém pode tê-lo trocado."""
        assert emissor.comparar_com_erp(
            self.ENVIADO, None,
            self._bling(("111", "OUTRO-TEXTO", 10), ("333", "", 4)), None) == []

    def test_linha_removida_e_linha_incluida_direto_no_erp(self):
        difs = emissor.comparar_com_erp(
            None, self.ENVIADO, None, self._olist(("CAL-P", 10), ("MEIA-U", 2)))
        assert [(d["sku"], d["enviado"], d["olist"]) for d in difs] == [
            ("CAL-G", 4, 0),        # sumiu do ERP
            ("MEIA-U", 0, 2)]       # incluída lá
        assert all(d["bling"] is None for d in difs)     # Bling fora da comparação


# ---------------------------------------------------------------------------
# enviar_alteracao
# ---------------------------------------------------------------------------
class TestEnviarAlteracao:
    def test_fluxo_feliz_olist_primeiro_bling_depois(self):
        repo_ped, repo_int, pid = _em_alteracao()
        http = HttpFake([_get_bling(), _get_olist(), OK204, OK204, PUT_BLING])
        passos = []

        res = emissor.enviar_alteracao(pid, "turma menor", "diogo", repo_ped, repo_int,
                                       http=http, progresso=passos.append)

        assert _metodos(http) == [
            ("get", "/pedidos/compras/555"), ("get", "/pedidos/900"),
            ("put", "/pedidos/900/itens"), ("put", "/pedidos/900"),
            ("put", "/pedidos/compras/555")]
        assert res["revisao"] == 2
        assert repo_ped.obter_pedido(pid)["status"] == e.EMITIDO

        # Olist: a grade nova inteira
        itens_olist = http.chamadas[2][2]["itens"]
        assert [(i["infoAdicional"], i["quantidade"]) for i in itens_olist] == [
            ("CAL-G", 4), ("CAL-P", 6)]
        assert "observacoes" in http.chamadas[3][2]

        # Bling: mesmo pedido (número/data/vencimento), itens e valor novos
        corpo = http.chamadas[4][2]
        assert corpo["numero"] == 78 and corpo["data"] == "2026-08-01"
        assert sorted(i["quantidade"] for i in corpo["itens"]) == [4, 6]
        assert corpo["parcelas"] == [{"valor": 500.0, "dataVencimento": "2026-08-31",
                                      "formaPagamento": {"id": 555}}]

        # a revisão 2 virou a linha de base, com motivo e os dois carimbos
        base = repo_ped.obter_linha_de_base(pid)
        assert base["numero"] == 2 and base["motivo"] == "turma menor"
        assert base["olist_ok_em"] and base["bling_ok_em"] and base["concluida_em"]
        acoes = list(repo_int.listar_eventos()["acao"])
        assert "alterar_venda" in acoes and "alterar_compra" in acoes
        assert any("Olist" in p for p in passos) and any("Bling" in p for p in passos)

    def test_so_compra_emitida_altera_so_o_bling(self):
        repo_ped, repo_int, pid = _em_alteracao(com_olist=False)
        http = HttpFake([_get_bling(), PUT_BLING])
        emissor.enviar_alteracao(pid, "ajuste", "diogo", repo_ped, repo_int, http=http)
        assert _metodos(http) == [("get", "/pedidos/compras/555"),
                                  ("put", "/pedidos/compras/555")]
        assert repo_ped.obter_pedido(pid)["status"] == e.COMPRA_EMITIDA

    def test_motivo_e_obrigatorio_e_nem_trava_o_pedido(self):
        repo_ped, repo_int, pid = _em_alteracao()
        http = HttpFake([])
        with pytest.raises(emissor.EmissaoFalhou, match="motivo"):
            emissor.enviar_alteracao(pid, "  ", "diogo", repo_ped, repo_int, http=http)
        assert repo_ped.obter_pedido(pid)["status"] == e.EM_ALTERACAO

    def test_pedido_fora_de_alteracao_nao_envia(self):
        repo_ped, repo_int, pid = _emitido()
        with pytest.raises(emissor.EmissaoFalhou, match="Outra sessão"):
            emissor.enviar_alteracao(pid, "x", "diogo", repo_ped, repo_int, http=HttpFake([]))

    @pytest.mark.parametrize("bling_valor,olist_sit,quem", [
        (3, 0, "Bling"),       # compra Em andamento
        (1, 0, "Bling"),       # compra Atendida
        (0, 3, "Olist"),       # venda Aprovada
        (0, 1, "Olist"),       # venda Faturada
    ])
    def test_fora_de_em_aberto_nao_toca_erp_nenhum(self, bling_valor, olist_sit, quem):
        repo_ped, repo_int, pid = _em_alteracao()
        http = HttpFake([_get_bling(bling_valor), _get_olist(olist_sit)])

        with pytest.raises(emissor.EmissaoFalhou, match=quem):
            emissor.enviar_alteracao(pid, "x", "diogo", repo_ped, repo_int, http=http)

        assert [m for m, _ in _metodos(http)] == ["get", "get"]        # só leu
        assert repo_ped.obter_pedido(pid)["status"] == e.EM_ALTERACAO  # segue editável
        assert revisoes.pendente(repo_ped.listar_revisoes(pid)) == {}  # sem revisão órfã

    def test_edicao_manual_no_erp_vira_pergunta_e_nao_envia(self):
        repo_ped, repo_int, pid = _em_alteracao()
        mexido = bling_pedido()
        mexido["itens"][1]["quantidade"] = 9            # CAL-G: enviamos 4, está 9
        http = HttpFake([RespostaFake(200, {"data": mexido}), _get_olist()])

        with pytest.raises(emissor.DivergenciaNoErp) as info:
            emissor.enviar_alteracao(pid, "x", "diogo", repo_ped, repo_int, http=http)

        assert info.value.divergencias == [
            {"sku": "CAL-G", "enviado": 4, "bling": 9, "olist": 4,
             "diverge_bling": True, "diverge_olist": False}]
        assert [m for m, _ in _metodos(http)] == ["get", "get"]
        assert repo_ped.obter_pedido(pid)["status"] == e.EM_ALTERACAO
        assert len(repo_int.listar_eventos()) == 0        # é pergunta, não falha: sem evento

    def test_sobrepor_envia_por_cima_da_edicao_manual(self):
        repo_ped, repo_int, pid = _em_alteracao()
        mexido = bling_pedido()
        mexido["itens"][1]["quantidade"] = 9
        http = HttpFake([RespostaFake(200, {"data": mexido}), _get_olist(),
                         OK204, OK204, PUT_BLING])
        emissor.enviar_alteracao(pid, "x", "diogo", repo_ped, repo_int,
                                 sobrepor=True, http=http)
        corpo = http.chamadas[4][2]
        assert {i["codigoFornecedor"]: i["quantidade"] for i in corpo["itens"]} == {
            "CAL-P": 6, "CAL-G": 4}                      # vale o que está na nossa tela
        assert repo_ped.obter_pedido(pid)["status"] == e.EMITIDO

    def test_zerar_tudo_manda_cancelar(self):
        repo_ped, repo_int, pid = _em_alteracao(cal_p=0)
        item = next(i for i in repo_ped.tabelas[TAB_ITEM] if i["sku"] == "CAL-G")
        repo_ped.atualizar_quantidades(pid, [{"id": item["id"], "quantidade_final": 0}], "d")
        http = HttpFake([])
        with pytest.raises(emissor.EmissaoFalhou, match="Cancelar pedido emitido"):
            emissor.enviar_alteracao(pid, "x", "diogo", repo_ped, repo_int, http=http)
        assert http.chamadas == []
        assert repo_ped.obter_pedido(pid)["status"] == e.EM_ALTERACAO

    def test_sem_mudanca_nao_envia(self):
        repo_ped, repo_int, pid = _em_alteracao(cal_p=10)       # igual ao emitido
        with pytest.raises(emissor.EmissaoFalhou, match="Nenhuma quantidade difere"):
            emissor.enviar_alteracao(pid, "x", "diogo", repo_ped, repo_int, http=HttpFake([]))

    def test_recusa_do_olist_nao_muda_nada_em_lugar_nenhum(self):
        """Olist vai primeiro: se ELE recusa, o Bling nem é chamado."""
        repo_ped, repo_int, pid = _em_alteracao()
        http = HttpFake([_get_bling(), _get_olist(),
                         RespostaFake(400, {"mensagem": "Pedido não pode ser alterado"})])

        with pytest.raises(emissor.EmissaoFalhou, match="não pode ser alterado"):
            emissor.enviar_alteracao(pid, "x", "diogo", repo_ped, repo_int, http=http)

        assert ("put", "/pedidos/compras/555") not in _metodos(http)
        assert repo_ped.obter_pedido(pid)["status"] == e.EM_ALTERACAO
        assert [r["numero"] for r in repo_ped.listar_revisoes(pid)] == [1]   # revisão removida
        ev = repo_int.listar_eventos().iloc[0]
        assert ev["acao"] == "alterar_pedido" and bool(ev["sucesso"]) is False
        assert ev["plataforma"] == "olist" and ev["detalhe"]["tocou_erp"] is False

    def test_bling_falha_depois_do_olist_fica_travado_e_concluir_resolve(self):
        repo_ped, repo_int, pid = _em_alteracao()
        http = HttpFake([_get_bling(), _get_olist(), OK204, OK204,
                         RespostaFake(500, {"error": {"description": "Indisponível"}})])

        with pytest.raises(emissor.EmissaoFalhou, match="Indisponível"):
            emissor.enviar_alteracao(pid, "turma menor", "diogo", repo_ped, repo_int, http=http)

        # Olist já está na versão nova: NÃO volta a ser editável sozinho
        assert repo_ped.obter_pedido(pid)["status"] == e.ALTERACAO_ENVIANDO
        historico = repo_ped.listar_revisoes(pid)
        parcial = revisoes.envio_parcial(historico)
        assert parcial["numero"] == 2 and parcial["olist_ok_em"] and not parcial["bling_ok_em"]
        assert revisoes.linha_de_base(historico)["numero"] == 1

        # Concluir: reenvia a MESMA revisão (sem motivo novo, sem checar divergência)
        http2 = HttpFake([_get_bling(), _get_olist(itens=[
            {"produto": {"id": 1001, "sku": "CAL-P"}, "quantidade": 6, "valorUnitario": 50.0},
            {"produto": {"id": 1003, "sku": "CAL-G"}, "quantidade": 4, "valorUnitario": 50.0}]),
            OK204, OK204, PUT_BLING])
        res = emissor.enviar_alteracao(pid, "", "diogo", repo_ped, repo_int, http=http2)

        assert res["revisao"] == 2
        assert repo_ped.obter_pedido(pid)["status"] == e.EMITIDO
        base = repo_ped.obter_linha_de_base(pid)
        assert base["numero"] == 2 and base["motivo"] == "turma menor"

    def test_voltar_a_editar_depois_do_parcial_e_reenviar(self):
        repo_ped, repo_int, pid = _em_alteracao()
        http = HttpFake([_get_bling(), _get_olist(), OK204, OK204, RespostaFake(500, {})])
        with pytest.raises(emissor.EmissaoFalhou):
            emissor.enviar_alteracao(pid, "v1", "diogo", repo_ped, repo_int, http=http)

        assert emissor.destravar(pid, "diogo", repo_ped, repo_int) is True
        assert repo_ped.obter_pedido(pid)["status"] == e.EM_ALTERACAO
        with pytest.raises(Exception, match="já recebeu"):       # descartar: bloqueado
            repo_ped.descartar_alteracao(pid, "diogo")

        # Novo envio: o Olist é comparado com o que ELE recebeu (a revisão 2),
        # não com a linha de base — senão o nosso próprio envio pareceria edição manual.
        olist_na_v2 = _get_olist(itens=[
            {"produto": {"id": 1001, "sku": "CAL-P"}, "quantidade": 6, "valorUnitario": 50.0},
            {"produto": {"id": 1003, "sku": "CAL-G"}, "quantidade": 4, "valorUnitario": 50.0}])
        http2 = HttpFake([_get_bling(), olist_na_v2, OK204, OK204, PUT_BLING])
        res = emissor.enviar_alteracao(pid, "v2", "diogo", repo_ped, repo_int, http=http2)

        assert res["revisao"] == 3
        assert repo_ped.obter_pedido(pid)["status"] == e.EMITIDO
        assert revisoes.envio_parcial(repo_ped.listar_revisoes(pid)) == {}

    def test_concluir_quando_so_o_commit_do_estado_faltou(self):
        """ERPs já receberam e a revisão fechou; só o status ficou no lock."""
        repo_ped, repo_int, pid = _em_alteracao()
        http = HttpFake([_get_bling(), _get_olist(), OK204, OK204, PUT_BLING])
        emissor.enviar_alteracao(pid, "x", "diogo", repo_ped, repo_int, http=http)
        next(p for p in repo_ped.tabelas[TAB_PEDIDO] if p["id"] == pid)["status"] = \
            e.ALTERACAO_ENVIANDO

        http2 = HttpFake([])                                 # nenhuma chamada esperada
        emissor.enviar_alteracao(pid, "", "diogo", repo_ped, repo_int, http=http2)
        assert http2.chamadas == []
        assert repo_ped.obter_pedido(pid)["status"] == e.EMITIDO

    def test_sku_novo_sem_cadastro_no_olist_barra_antes_de_tocar(self):
        repo_ped, repo_int, pid = _em_alteracao()
        repo_ped.adicionar_itens(pid, [{
            "sku": "NOVO-M", "id_produto_bling": "777", "produto": "Novo", "tamanho": "M",
            "categoria": "Calça", "quantidade_final": 2, "custo_unit": 10.0}], "diogo")
        # GETs dos ERPs + a busca do SKU no catálogo do Olist (família e exato) sem achar
        http = HttpFake([_get_bling(), _get_olist()]
                        + [RespostaFake(200, {"itens": []})] * 4)
        with pytest.raises(emissor.EmissaoFalhou, match="NOVO-M"):
            emissor.enviar_alteracao(pid, "x", "diogo", repo_ped, repo_int, http=http,
                                     dormir=lambda _s: None)
        assert not [m for m, _ in _metodos(http) if m == "put"]
        assert repo_ped.obter_pedido(pid)["status"] == e.EM_ALTERACAO


class TestRevisaoNaEmissao:
    def test_emitir_compra_e_venda_deixam_a_linha_de_base(self):
        repo_ped, repo_int = RepoPedFake(), _repo_int_conectado()
        pid = _pedido_pronto(repo_ped)
        emissor.emitir_compra_bling(pid, "diogo", repo_ped, repo_int, HttpFake([RESP_BLING]))
        base = repo_ped.obter_linha_de_base(pid)
        assert base["numero"] == 1 and base["bling_ok_em"] and not base["olist_ok_em"]

        emissor.emitir_venda_olist(pid, "diogo", repo_ped, repo_int, mapa_sku=MAPA,
                                   http=HttpFake([RESP_OLIST]))
        base = repo_ped.obter_linha_de_base(pid)
        assert base["numero"] == 1 and base["olist_ok_em"]           # carimbou, não duplicou

    def test_falha_ao_gravar_a_revisao_nao_derruba_a_emissao(self):
        """O ERP já foi tocado — a linha de base se reconstrói depois."""
        class SemRevisao(RepoPedFake):
            def listar_revisoes(self, pedido_id):
                raise RuntimeError("tabela de revisões fora do ar")

        repo_ped, repo_int = SemRevisao(), _repo_int_conectado()
        pid = _pedido_pronto(repo_ped)
        res = emissor.emitir_compra_bling(pid, "diogo", repo_ped, repo_int,
                                          HttpFake([RESP_BLING]))
        assert res["bling_id"] == "555"
        assert repo_ped.obter_pedido(pid)["status"] == e.COMPRA_EMITIDA


# ---------------------------------------------------------------------------
# cancelar_emitido
# ---------------------------------------------------------------------------
class TestCancelarEmitido:
    def test_fluxo_feliz_cancela_olist_depois_bling(self):
        repo_ped, repo_int, pid = _emitido()
        http = HttpFake([_get_bling(), _get_olist(), OK204, OK204, _get_bling(2)])

        acoes = emissor.cancelar_emitido(pid, "colégio desistiu", "diogo",
                                         repo_ped, repo_int, http=http)

        assert acoes == {"bling": "cancelar", "olist": "cancelar"}
        assert _metodos(http) == [
            ("get", "/pedidos/compras/555"), ("get", "/pedidos/900"),
            ("put", "/pedidos/900/situacao"),
            ("patch", "/pedidos/compras/555/situacoes/4403"),
            ("get", "/pedidos/compras/555")]                 # confere que ficou cancelado
        assert http.chamadas[2][2] == {"situacao": 2}
        pedido = repo_ped.obter_pedido(pid)
        assert pedido["status"] == e.CANCELADO
        assert pedido["bling_id"] == "555" and pedido["olist_id"] == "900"   # trilha fica
        ultima = repo_ped.listar_revisoes(pid)[-1]
        assert ultima["tipo"] == e.REVISAO_CANCELAMENTO and ultima["concluida_em"]
        assert ultima["motivo"] == "colégio desistiu"
        assert repo_ped.obter_linha_de_base(pid)["numero"] == 1     # base segue a emissão

    def test_so_compra_emitida_cancela_so_o_bling(self):
        repo_ped, repo_int, pid = _emitido(com_olist=False)
        http = HttpFake([_get_bling(), OK204, _get_bling(2)])
        acoes = emissor.cancelar_emitido(pid, "x", "diogo", repo_ped, repo_int, http=http)
        assert acoes == {"bling": "cancelar", "olist": None}
        assert repo_ped.obter_pedido(pid)["status"] == e.CANCELADO

    def test_ja_cancelado_a_mao_num_erp_conta_como_feito(self):
        repo_ped, repo_int, pid = _emitido()
        http = HttpFake([_get_bling(), _get_olist(2), OK204, _get_bling(2)])
        acoes = emissor.cancelar_emitido(pid, "x", "diogo", repo_ped, repo_int, http=http)
        assert acoes == {"bling": "cancelar", "olist": "feito"}
        assert ("put", "/pedidos/900/situacao") not in _metodos(http)
        assert repo_ped.obter_pedido(pid)["status"] == e.CANCELADO

    @pytest.mark.parametrize("bling_valor,olist_sit,quem", [
        (3, 0, "Bling"), (1, 0, "Bling"), (0, 3, "Olist"), (0, 1, "Olist")])
    def test_fora_de_em_aberto_nao_cancela_nada(self, bling_valor, olist_sit, quem):
        repo_ped, repo_int, pid = _emitido()
        http = HttpFake([_get_bling(bling_valor), _get_olist(olist_sit)])
        with pytest.raises(emissor.EmissaoFalhou, match=quem):
            emissor.cancelar_emitido(pid, "x", "diogo", repo_ped, repo_int, http=http)
        assert [m for m, _ in _metodos(http)] == ["get", "get"]
        assert repo_ped.obter_pedido(pid)["status"] == e.EMITIDO        # rollback
        assert [r["numero"] for r in repo_ped.listar_revisoes(pid)] == [1]

    def test_sem_a_situacao_de_cancelado_configurada_nao_comeca(self):
        repo_ped, repo_int, pid = _emitido()
        repo_int.salvar_config("bling", {"fornecedor_id": "987"}, "t")
        http = HttpFake([_get_bling(), _get_olist()])
        with pytest.raises(emissor.EmissaoFalhou, match="situação de cancelado"):
            emissor.cancelar_emitido(pid, "x", "diogo", repo_ped, repo_int, http=http)
        assert [m for m, _ in _metodos(http)] == ["get", "get"]       # Olist intocado
        assert repo_ped.obter_pedido(pid)["status"] == e.EMITIDO

    def test_motivo_obrigatorio(self):
        repo_ped, repo_int, pid = _emitido()
        with pytest.raises(emissor.EmissaoFalhou, match="motivo"):
            emissor.cancelar_emitido(pid, "", "diogo", repo_ped, repo_int, http=HttpFake([]))
        assert repo_ped.obter_pedido(pid)["status"] == e.EMITIDO

    @pytest.mark.parametrize("status", [e.RASCUNHO, e.PRONTO, e.EM_ALTERACAO, e.CANCELADO])
    def test_so_pedido_emitido_em_repouso_cancela_nos_erps(self, status):
        repo_ped, repo_int, pid = _emitido()
        next(p for p in repo_ped.tabelas[TAB_PEDIDO] if p["id"] == pid)["status"] = status
        with pytest.raises(emissor.EmissaoFalhou, match="só pedido emitido"):
            emissor.cancelar_emitido(pid, "x", "diogo", repo_ped, repo_int, http=HttpFake([]))

    def test_bling_falha_depois_do_olist_fica_travado_e_concluir_resolve(self):
        repo_ped, repo_int, pid = _emitido()
        http = HttpFake([_get_bling(), _get_olist(), OK204,
                         RespostaFake(500, {"error": {"description": "Indisponível"}})])
        with pytest.raises(emissor.EmissaoFalhou, match="Indisponível"):
            emissor.cancelar_emitido(pid, "desistiu", "diogo", repo_ped, repo_int, http=http)
        assert repo_ped.obter_pedido(pid)["status"] == e.CANCELAMENTO_ENVIANDO

        # Concluir: o Olist já está cancelado (conta como feito), falta o Bling
        http2 = HttpFake([_get_bling(), _get_olist(2), OK204, _get_bling(2)])
        acoes = emissor.cancelar_emitido(pid, "", "diogo", repo_ped, repo_int, http=http2)
        assert acoes == {"bling": "cancelar", "olist": "feito"}
        assert repo_ped.obter_pedido(pid)["status"] == e.CANCELADO
        canceladas = [r for r in repo_ped.listar_revisoes(pid)
                      if r["tipo"] == e.REVISAO_CANCELAMENTO]
        assert len(canceladas) == 1 and canceladas[0]["motivo"] == "desistiu"

    def test_id_de_situacao_errado_trava_e_avisa(self):
        """O PATCH passou mas o pedido não ficou Cancelado: não vira CANCELADO aqui."""
        repo_ped, repo_int, pid = _emitido(com_olist=False)
        http = HttpFake([_get_bling(), OK204, _get_bling(3)])
        with pytest.raises(emissor.EmissaoFalhou, match="Em andamento"):
            emissor.cancelar_emitido(pid, "x", "diogo", repo_ped, repo_int, http=http)
        assert repo_ped.obter_pedido(pid)["status"] == e.CANCELAMENTO_ENVIANDO

        assert emissor.destravar(pid, "diogo", repo_ped, repo_int) is True
        assert repo_ped.obter_pedido(pid)["status"] == e.COMPRA_EMITIDA
