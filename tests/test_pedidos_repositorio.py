"""
Testes do RepositorioPedidos (pedidos/repositorio.py) com fake do gateway.

O fake substitui _inserir/_atualizar/_selecionar/_deletar por dicts em memória
que simulam o comportamento relevante do Postgres: unique parcial da rodada
(23505), cascade dos deletes e retorno de representação. O que exige o banco
real (trigger, grants) fica na verificação manual.
"""

import pandas as pd
import pytest

from pedidos import estados as e
from pedidos.repositorio import (
    RepositorioPedidos, RodadaJaCongelada, TransicaoInvalida, PedidoNaoEditavel,
    TAB_RODADA, TAB_PEDIDO, TAB_ITEM,
)


class FakeAPIError(Exception):
    def __init__(self, code):
        super().__init__(f"APIError code={code}")
        self.code = code


class RepoFake(RepositorioPedidos):
    """Gateway em memória + log de chamadas + falha injetável."""

    def __init__(self):
        super().__init__(client=None)
        self.tabelas = {TAB_RODADA: [], TAB_PEDIDO: [], TAB_ITEM: []}
        self.chamadas = []          # [(op, tabela), ...] na ordem real
        self.falhar_insert_em = None   # nome de tabela → _inserir levanta erro
        self._seq = 0

    def _novo_id(self):
        self._seq += 1
        return f"id-{self._seq}"

    def _inserir(self, tabela, linhas):
        self.chamadas.append(("inserir", tabela))
        if self.falhar_insert_em == tabela:
            raise RuntimeError(f"falha simulada em {tabela}")
        linhas = linhas if isinstance(linhas, list) else [linhas]
        out = []
        for linha in linhas:
            linha = dict(linha)
            linha.setdefault("id", self._novo_id())
            if tabela == TAB_RODADA:
                # unique parcial: (ano, mes) só pode ter 1 não-CANCELADA
                for r in self.tabelas[tabela]:
                    if ((r["ano_disparo"], r["mes_disparo"])
                            == (linha["ano_disparo"], linha["mes_disparo"])
                            and r["status"] != e.RODADA_CANCELADA):
                        raise FakeAPIError("23505")
                # default now() do banco
                linha.setdefault("congelada_em", pd.Timestamp.now(tz="UTC").isoformat())
            if tabela == TAB_PEDIDO:
                linha.setdefault("status", e.RASCUNHO)
            self.tabelas[tabela].append(linha)
            out.append(dict(linha))
        return out

    def _atualizar(self, tabela, filtros, valores):
        self.chamadas.append(("atualizar", tabela))
        out = []
        for r in self.tabelas[tabela]:
            if all(r.get(k) == v for k, v in filtros.items()):
                r.update(valores)
                out.append(dict(r))
        return out

    def _selecionar(self, tabela, filtros=None, colunas="*"):
        return [dict(r) for r in self.tabelas[tabela]
                if all(r.get(k) == v for k, v in (filtros or {}).items())]

    def _deletar(self, tabela, filtros):
        self.chamadas.append(("deletar", tabela))
        antes = self.tabelas[tabela]
        removidos = [r for r in antes if all(r.get(k) == v for k, v in filtros.items())]
        self.tabelas[tabela] = [r for r in antes if r not in removidos]
        if tabela == TAB_RODADA:   # simula ON DELETE CASCADE
            ids_rodada = {r["id"] for r in removidos}
            ped_removidos = {p["id"] for p in self.tabelas[TAB_PEDIDO]
                             if p["rodada_id"] in ids_rodada}
            self.tabelas[TAB_PEDIDO] = [p for p in self.tabelas[TAB_PEDIDO]
                                        if p["rodada_id"] not in ids_rodada]
            self.tabelas[TAB_ITEM] = [i for i in self.tabelas[TAB_ITEM]
                                      if i["pedido_id"] not in ped_removidos]


def snapshot_min(mes=8, ano=2026):
    return {
        "mes_disparo": mes, "ano_disparo": ano,
        "data_disparo": f"{ano}-{mes:02d}-01", "data_chegada": f"{ano}-{mes:02d}-29",
        "data_chegada_seguinte": f"{ano}-11-29", "rodada_numero": 1,
        "janela_label": "Rodada 1", "data_referencia": "2026-07-15",
        "congelada_por": "tester", "ativo_crescimento": False,
        "config_snapshot": {}, "resultado_skus": [],
    }


def grupos_min():
    item = {"sku": "A-PP", "id_produto_bling": "101", "produto": "Prod A",
            "tamanho": "PP", "categoria": "Camisa",
            "quantidade_sugerida": 10, "quantidade_final": 10, "custo_unit": 5.0}
    return [
        {"colegio": "NEVES", "super_categoria": "CALÇAS",
         "titulo": "AKU-PC · NEVES · CALÇAS · R08/2026", "criado_por": "tester",
         "itens": [item, {**item, "sku": "A-M", "quantidade_sugerida": 4,
                          "quantidade_final": 4}]},
        {"colegio": "NEVES", "super_categoria": "CAMISETAS",
         "titulo": "AKU-PC · NEVES · CAMISETAS · R08/2026", "criado_por": "tester",
         "itens": [{**item, "sku": "B-PP"}]},
    ]


# ---------------------------------------------------------------------------
# congelar_rodada
# ---------------------------------------------------------------------------
class TestCongelarRodada:
    def test_fluxo_feliz_ordem_e_estado_final(self):
        repo = RepoFake()
        res = repo.congelar_rodada(snapshot_min(), grupos_min())

        assert res["status"] == e.RODADA_ABERTA
        assert res["n_pedidos"] == 2 and res["n_itens"] == 3
        # ordem: rodada CONGELANDO → pedidos → itens → CAS p/ ABERTA
        assert repo.chamadas == [
            ("inserir", TAB_RODADA), ("inserir", TAB_PEDIDO),
            ("inserir", TAB_ITEM), ("atualizar", TAB_RODADA),
        ]
        assert repo.tabelas[TAB_RODADA][0]["status"] == e.RODADA_ABERTA
        assert all(p["status"] == e.RASCUNHO for p in repo.tabelas[TAB_PEDIDO])
        assert len(repo.tabelas[TAB_ITEM]) == 3

    def test_duplicado_levanta_sem_inserir_filhos(self):
        repo = RepoFake()
        repo.congelar_rodada(snapshot_min(), grupos_min())
        antes_ped = len(repo.tabelas[TAB_PEDIDO])

        with pytest.raises(RodadaJaCongelada):
            repo.congelar_rodada(snapshot_min(), grupos_min())
        assert len(repo.tabelas[TAB_PEDIDO]) == antes_ped
        assert len(repo.tabelas[TAB_RODADA]) == 1

    def test_cancelada_libera_novo_congelamento(self):
        repo = RepoFake()
        res = repo.congelar_rodada(snapshot_min(), grupos_min())
        repo.cancelar_rodada(res["id"], "tester")
        res2 = repo.congelar_rodada(snapshot_min(), grupos_min())
        assert res2["status"] == e.RODADA_ABERTA
        assert len(repo.tabelas[TAB_RODADA]) == 2   # cancelada fica p/ auditoria

    def test_falha_nos_itens_dispara_delete_compensatorio(self):
        repo = RepoFake()
        repo.falhar_insert_em = TAB_ITEM
        with pytest.raises(RuntimeError, match="falha simulada"):
            repo.congelar_rodada(snapshot_min(), grupos_min())
        # compensação removeu tudo (cascade)
        assert ("deletar", TAB_RODADA) in repo.chamadas
        assert repo.tabelas[TAB_RODADA] == []
        assert repo.tabelas[TAB_PEDIDO] == []

    def test_sem_grupos_levanta_valueerror(self):
        with pytest.raises(ValueError):
            RepoFake().congelar_rodada(snapshot_min(), [])


# ---------------------------------------------------------------------------
# limpar_congelamento_abortado
# ---------------------------------------------------------------------------
class TestLimparAbortado:
    def test_limpa_congelando(self):
        repo = RepoFake()
        repo.falhar_insert_em = TAB_PEDIDO
        with pytest.raises(RuntimeError):
            repo.congelar_rodada(snapshot_min(), grupos_min())
        # simula compensação que falhou: reinsere a rodada presa em CONGELANDO
        repo.falhar_insert_em = None
        repo._inserir(TAB_RODADA, [{**snapshot_min(), "status": e.RODADA_CONGELANDO}])
        rodada_id = repo.tabelas[TAB_RODADA][0]["id"]

        repo.limpar_congelamento_abortado(rodada_id)
        assert repo.tabelas[TAB_RODADA] == []

    def test_recusa_rodada_aberta(self):
        repo = RepoFake()
        res = repo.congelar_rodada(snapshot_min(), grupos_min())
        with pytest.raises(TransicaoInvalida):
            repo.limpar_congelamento_abortado(res["id"])


# ---------------------------------------------------------------------------
# transicionar_pedido — CAS
# ---------------------------------------------------------------------------
class TestTransicionar:
    def _pedido(self, repo):
        res = repo.congelar_rodada(snapshot_min(), grupos_min())
        return repo.listar_pedidos(res["id"]).iloc[0]["id"]

    def test_rascunho_para_pronto(self):
        repo = RepoFake()
        pid = self._pedido(repo)
        assert repo.transicionar_pedido(pid, e.RASCUNHO, e.PRONTO, "tester") is True
        ped = repo._selecionar(TAB_PEDIDO, {"id": pid})[0]
        assert ped["status"] == e.PRONTO
        assert ped["pronto_por"] == "tester"

    def test_corrida_perdida_retorna_false(self):
        repo = RepoFake()
        pid = self._pedido(repo)
        repo.transicionar_pedido(pid, e.RASCUNHO, e.PRONTO, "aba1")
        # aba2 ainda acha que está RASCUNHO → CAS não encontra → False
        assert repo.transicionar_pedido(pid, e.RASCUNHO, e.PRONTO, "aba2") is False

    def test_reabrir_limpa_carimbo_de_pronto(self):
        repo = RepoFake()
        pid = self._pedido(repo)
        repo.transicionar_pedido(pid, e.RASCUNHO, e.PRONTO, "tester")
        repo.transicionar_pedido(pid, e.PRONTO, e.RASCUNHO, "tester")
        ped = repo._selecionar(TAB_PEDIDO, {"id": pid})[0]
        assert ped["status"] == e.RASCUNHO
        assert ped["pronto_em"] is None and ped["pronto_por"] is None

    def test_transicao_proibida_levanta(self):
        repo = RepoFake()
        pid = self._pedido(repo)
        with pytest.raises(TransicaoInvalida):
            repo.transicionar_pedido(pid, e.RASCUNHO, e.EMITIDO, "tester")


# ---------------------------------------------------------------------------
# atualizar_quantidades
# ---------------------------------------------------------------------------
class TestAtualizarQuantidades:
    def test_atualiza_so_linhas_enviadas(self):
        repo = RepoFake()
        res = repo.congelar_rodada(snapshot_min(), grupos_min())
        pid = repo.listar_pedidos(res["id"]).iloc[0]["id"]
        itens = repo.listar_itens(pid)

        n = repo.atualizar_quantidades(
            pid, [{"id": itens.iloc[0]["id"], "quantidade_final": 99}], "tester")
        assert n == 1
        depois = repo.listar_itens(pid)
        alterado = depois[depois["id"] == itens.iloc[0]["id"]].iloc[0]
        assert alterado["quantidade_final"] == 99
        assert alterado["quantidade_sugerida"] == itens.iloc[0]["quantidade_sugerida"]
        # a outra linha ficou intacta
        intacto = depois[depois["id"] != itens.iloc[0]["id"]].iloc[0]
        assert intacto["quantidade_final"] == intacto["quantidade_sugerida"]

    def test_recusa_pedido_nao_rascunho(self):
        repo = RepoFake()
        res = repo.congelar_rodada(snapshot_min(), grupos_min())
        pid = repo.listar_pedidos(res["id"]).iloc[0]["id"]
        repo.transicionar_pedido(pid, e.RASCUNHO, e.PRONTO, "tester")
        with pytest.raises(PedidoNaoEditavel):
            repo.atualizar_quantidades(pid, [{"id": "x", "quantidade_final": 1}], "t")

    def test_item_de_outro_pedido_nao_vaza(self):
        repo = RepoFake()
        res = repo.congelar_rodada(snapshot_min(), grupos_min())
        pedidos = repo.listar_pedidos(res["id"])
        pid_a, pid_b = pedidos.iloc[0]["id"], pedidos.iloc[1]["id"]
        item_de_b = repo.listar_itens(pid_b).iloc[0]["id"]
        # tenta editar item do pedido B passando o pedido A → 0 atualizados
        assert repo.atualizar_quantidades(
            pid_a, [{"id": item_de_b, "quantidade_final": 1}], "t") == 0


# ---------------------------------------------------------------------------
# cancelar_rodada / listagens
# ---------------------------------------------------------------------------
class TestCancelarEListar:
    def test_cancela_rodada_e_pedidos(self):
        repo = RepoFake()
        res = repo.congelar_rodada(snapshot_min(), grupos_min())
        repo.cancelar_rodada(res["id"], "tester")
        assert repo._selecionar(TAB_RODADA, {"id": res["id"]})[0]["status"] == e.RODADA_CANCELADA
        assert all(p["status"] == e.CANCELADO
                   for p in repo._selecionar(TAB_PEDIDO, {"rodada_id": res["id"]}))

    def test_recusa_se_pedido_ja_pronto(self):
        repo = RepoFake()
        res = repo.congelar_rodada(snapshot_min(), grupos_min())
        pid = repo.listar_pedidos(res["id"]).iloc[0]["id"]
        repo.transicionar_pedido(pid, e.RASCUNHO, e.PRONTO, "tester")
        with pytest.raises(TransicaoInvalida):
            repo.cancelar_rodada(res["id"], "tester")

    def test_listar_pedidos_agrega_itens(self):
        repo = RepoFake()
        res = repo.congelar_rodada(snapshot_min(), grupos_min())
        df = repo.listar_pedidos(res["id"])
        assert len(df) == 2
        calcas = df[df["super_categoria"] == "CALÇAS"].iloc[0]
        assert calcas["n_itens"] == 2
        assert calcas["qtd_sugerida"] == calcas["qtd_final"] == 14
        assert calcas["investimento_final"] == pytest.approx(14 * 5.0)

    def test_listar_rodadas_sem_jsonb(self):
        repo = RepoFake()
        repo.congelar_rodada(snapshot_min(), grupos_min())
        df = repo.listar_rodadas()
        assert len(df) == 1 and df.iloc[0]["status"] == e.RODADA_ABERTA
