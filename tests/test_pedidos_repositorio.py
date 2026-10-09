"""
Testes do RepositorioPedidos (pedidos/repositorio.py) com fake do gateway.

O fake substitui _inserir/_atualizar/_selecionar/_deletar por dicts em memória
que simulam o comportamento relevante do Postgres: unique parcial da rodada
(23505), cascade dos deletes e retorno de representação. O que exige o banco
real (trigger, grants) fica na verificação manual.
"""

import httpx
import pandas as pd
import pytest
from postgrest.exceptions import APIError

from pedidos import estados as e
from pedidos import repositorio as rp
from pedidos.repositorio import (
    RepositorioPedidos, RodadaJaCongelada, TransicaoInvalida, PedidoNaoEditavel,
    ItemJaExiste, ItemNaoRemovivel, MigracaoPendente,
    TAB_RODADA, TAB_PEDIDO, TAB_ITEM, TAB_REVISAO,
)


class FakeAPIError(Exception):
    def __init__(self, code):
        super().__init__(f"APIError code={code}")
        self.code = code


class RepoFake(RepositorioPedidos):
    """Gateway em memória + log de chamadas + falha injetável."""

    def __init__(self):
        super().__init__(client=None)
        self.tabelas = {TAB_RODADA: [], TAB_PEDIDO: [], TAB_ITEM: [], TAB_REVISAO: []}
        self.chamadas = []          # [(op, tabela), ...] na ordem real
        self.leituras = []          # idem, só SELECTs (fora do log de escrita)
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
            if tabela == TAB_REVISAO:
                # unique (pedido_id, numero)
                for r in self.tabelas[tabela]:
                    if (r["pedido_id"], r["numero"]) == (linha["pedido_id"], linha["numero"]):
                        raise FakeAPIError("23505")
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
        self.leituras.append(("selecionar", tabela))
        return [dict(r) for r in self.tabelas[tabela]
                if all(r.get(k) == v for k, v in (filtros or {}).items())]

    def _selecionar_in(self, tabela, coluna, valores, colunas="*"):
        self.leituras.append(("selecionar_in", tabela))
        return [dict(r) for r in self.tabelas[tabela] if r.get(coluna) in valores]

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
         "titulo": "NEVES - CALÇAS - R08/2026", "criado_por": "tester",
         "itens": [item, {**item, "sku": "A-M", "quantidade_sugerida": 4,
                          "quantidade_final": 4}]},
        {"colegio": "NEVES", "super_categoria": "CAMISETAS",
         "titulo": "NEVES - CAMISETAS - R08/2026", "criado_por": "tester",
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

    def test_listar_pedidos_le_os_itens_em_lote(self):
        # 1 leitura dos pedidos + 1 dos itens da rodada inteira — nunca uma por
        # pedido (eram N+1 requests em série a cada rerun da tela)
        repo = RepoFake()
        res = repo.congelar_rodada(snapshot_min(), grupos_min())
        repo.leituras.clear()
        repo.listar_pedidos(res["id"])
        assert repo.leituras == [("selecionar", TAB_PEDIDO),
                                 ("selecionar_in", TAB_ITEM)]

    def test_listar_pedidos_zera_pedido_sem_item(self):
        repo = RepoFake()
        res = repo.congelar_rodada(snapshot_min(), grupos_min())
        df = repo.listar_pedidos(res["id"])
        vazio = df.iloc[0]["id"]
        repo.tabelas[TAB_ITEM] = [i for i in repo.tabelas[TAB_ITEM]
                                  if i["pedido_id"] != vazio]
        linha = repo.listar_pedidos(res["id"]).set_index("id").loc[vazio]
        assert (linha["n_itens"], linha["qtd_sugerida"], linha["qtd_final"]) == (0, 0, 0)
        assert linha["investimento_final"] == 0.0

    def test_listar_pedidos_sem_nenhum_item_na_rodada(self):
        repo = RepoFake()
        res = repo.congelar_rodada(snapshot_min(), grupos_min())
        repo.tabelas[TAB_ITEM] = []
        df = repo.listar_pedidos(res["id"])
        assert len(df) == 2 and df["n_itens"].tolist() == [0, 0]

    def test_listar_rodadas_sem_jsonb(self):
        repo = RepoFake()
        repo.congelar_rodada(snapshot_min(), grupos_min())
        df = repo.listar_rodadas()
        assert len(df) == 1 and df.iloc[0]["status"] == e.RODADA_ABERTA

    def test_obter_resultado_skus_devolve_a_rede_inteira(self):
        repo = RepoFake()
        snap = snapshot_min()
        snap["resultado_skus"] = [{"SKU": "A", "SugestaoProducao": 4},
                                  {"SKU": "B", "SugestaoProducao": 0}]
        rodada_id = repo.congelar_rodada(snap, grupos_min())["id"]
        assert [r["SKU"] for r in repo.obter_resultado_skus(rodada_id)] == ["A", "B"]

    def test_obter_resultado_skus_de_rodada_inexistente_e_lista_vazia(self):
        assert RepoFake().obter_resultado_skus("nao-existe") == []


# ---------------------------------------------------------------------------
# Inclusão / remoção manual de itens (DDL 007)
# ---------------------------------------------------------------------------
def item_manual(sku="Z-M", qtd=6):
    return {"sku": sku, "id_produto_bling": "900", "produto": "Prod Z Tamanho:M",
            "tamanho": "M", "categoria": "Camisa", "quantidade_final": qtd,
            "custo_unit": 7.5,
            # o que quem chama mandar aqui NÃO pode valer — o repositório impõe
            "quantidade_sugerida": 99, "memoria_sugerida": {"x": 1}, "origem": "SIMULACAO"}


class TestItensManuais:
    def _repo_com_pedido(self):
        repo = RepoFake()
        repo.congelar_rodada(snapshot_min(), grupos_min())
        pedido = next(p for p in repo.tabelas[TAB_PEDIDO] if p["super_categoria"] == "CALÇAS")
        return repo, pedido["id"]

    def test_inclui_como_manual_com_sugerido_zero_e_auditoria(self):
        repo, pid = self._repo_com_pedido()
        assert repo.adicionar_itens(pid, [item_manual()], "gestor@ak") == 1

        novo = next(i for i in repo.tabelas[TAB_ITEM] if i["sku"] == "Z-M")
        assert novo["pedido_id"] == pid
        assert novo["origem"] == e.ORIGEM_MANUAL
        assert novo["quantidade_sugerida"] == 0 and novo["memoria_sugerida"] == {}
        assert novo["quantidade_final"] == 6
        assert novo["adicionado_por"] == "gestor@ak" and novo["adicionado_em"]
        pedido = repo.obter_pedido(pid)
        assert pedido["atualizado_por"] == "gestor@ak"

    def test_sku_que_ja_esta_no_pedido_e_recusado_sem_inserir(self):
        repo, pid = self._repo_com_pedido()
        antes = len(repo.tabelas[TAB_ITEM])
        with pytest.raises(ItemJaExiste, match="A-PP"):
            repo.adicionar_itens(pid, [item_manual("Z-M"), item_manual("A-PP")], "u")
        assert len(repo.tabelas[TAB_ITEM]) == antes     # tudo ou nada

    def test_fora_de_rascunho_nao_inclui(self):
        repo, pid = self._repo_com_pedido()
        repo.transicionar_pedido(pid, e.RASCUNHO, e.PRONTO, "u")
        with pytest.raises(PedidoNaoEditavel):
            repo.adicionar_itens(pid, [item_manual()], "u")

    def test_quantidade_zero_ou_sem_id_bling_e_erro(self):
        repo, pid = self._repo_com_pedido()
        with pytest.raises(ValueError):
            repo.adicionar_itens(pid, [item_manual(qtd=0)], "u")
        with pytest.raises(ValueError):
            repo.adicionar_itens(pid, [{**item_manual(), "id_produto_bling": ""}], "u")

    def test_banco_sem_o_ddl_007_vira_mensagem_de_migracao(self):
        repo, pid = self._repo_com_pedido()
        original = repo._inserir

        def sem_coluna(tabela, linhas):
            if tabela == TAB_ITEM:
                raise FakeAPIError("PGRST204")
            return original(tabela, linhas)
        repo._inserir = sem_coluna
        with pytest.raises(MigracaoPendente, match="007"):
            repo.adicionar_itens(pid, [item_manual()], "u")

    def test_listar_itens_trata_item_antigo_como_simulacao(self):
        repo, pid = self._repo_com_pedido()
        repo.adicionar_itens(pid, [item_manual()], "u")
        origem = repo.listar_itens(pid).set_index("sku")["origem"]
        assert origem["Z-M"] == e.ORIGEM_MANUAL
        assert origem["A-PP"] == e.ORIGEM_SIMULACAO      # gravado sem a coluna

    def test_remove_so_item_manual(self):
        repo, pid = self._repo_com_pedido()
        repo.adicionar_itens(pid, [item_manual()], "u")
        ids = {i["sku"]: i["id"] for i in repo.tabelas[TAB_ITEM] if i["pedido_id"] == pid}

        with pytest.raises(ItemNaoRemovivel, match="A-PP"):
            repo.remover_itens_manuais(pid, [ids["Z-M"], ids["A-PP"]], "u")
        assert {"Z-M", "A-PP"} <= {i["sku"] for i in repo.tabelas[TAB_ITEM]}   # nada saiu

        assert repo.remover_itens_manuais(pid, [ids["Z-M"]], "u") == 1
        assert "Z-M" not in {i["sku"] for i in repo.tabelas[TAB_ITEM]}
        assert "A-PP" in {i["sku"] for i in repo.tabelas[TAB_ITEM]}

    def test_remover_item_de_outro_pedido_nao_remove_nada(self):
        repo, pid = self._repo_com_pedido()
        outro = next(p["id"] for p in repo.tabelas[TAB_PEDIDO] if p["id"] != pid)
        repo.adicionar_itens(outro, [item_manual()], "u")
        alheio = next(i["id"] for i in repo.tabelas[TAB_ITEM] if i["sku"] == "Z-M")
        assert repo.remover_itens_manuais(pid, [alheio], "u") == 0
        assert "Z-M" in {i["sku"] for i in repo.tabelas[TAB_ITEM]}


# =====================================================================
# Leitura REAL (client fake, não gateway fake): retry e leitura em lote
# =====================================================================
class _Query:
    """Builder PostgREST mínimo: guarda os filtros e delega o execute ao client."""

    def __init__(self, client, tabela):
        self._client, self.tabela = client, tabela
        self.eqs, self.in_valores, self.faixa, self.ordem = {}, None, None, None

    def select(self, colunas):
        return self

    def eq(self, col, val):
        self.eqs[col] = val
        return self

    def in_(self, col, valores):
        self.in_valores = (col, list(valores))
        return self

    def order(self, col):
        self.ordem = col
        return self

    def range(self, inicio, fim):
        self.faixa = (inicio, fim)
        return self

    def execute(self):
        return self._client.executar(self)


class _Resp:
    def __init__(self, data):
        self.data = data


class _Client:
    """Client fake com falhas programadas: `falhas` = exceções a levantar nas
    primeiras execuções, na ordem; depois responde de `linhas`."""

    def __init__(self, linhas=None, falhas=()):
        self.linhas = list(linhas or [])
        self.falhas = list(falhas)
        self.queries = []

    def from_(self, tabela):
        return _Query(self, tabela)

    def executar(self, q):
        self.queries.append(q)
        if self.falhas:
            raise self.falhas.pop(0)
        out = [r for r in self.linhas
               if all(r.get(k) == v for k, v in q.eqs.items())]
        if q.in_valores:
            col, valores = q.in_valores
            out = [r for r in out if r.get(col) in valores]
        if q.ordem:
            out = sorted(out, key=lambda r: r[q.ordem])
        if q.faixa:
            out = out[q.faixa[0]:q.faixa[1] + 1]
        return _Resp([dict(r) for r in out])


def _erro_gateway(status=502):
    """O APIError que o postgrest monta quando a resposta não é JSON (HTML do
    Cloudflare): `code` é o status HTTP, inteiro."""
    return APIError({"message": "JSON could not be generated", "code": status,
                     "hint": "Refer to full message for details",
                     "details": "<html>502 Bad Gateway</html>"})


@pytest.fixture
def sem_espera(monkeypatch):
    monkeypatch.setattr(rp, "_ESPERA_RETRY_S", 0)


@pytest.mark.usefixtures("sem_espera")
class TestRetryDeLeitura:
    def test_502_transitorio_e_reemitido(self):
        client = _Client([{"id": "p1", "rodada_id": "r"}],
                         falhas=[_erro_gateway(502), _erro_gateway(503)])
        repo = RepositorioPedidos(client)
        assert repo._selecionar(TAB_PEDIDO, {"rodada_id": "r"}) == [
            {"id": "p1", "rodada_id": "r"}]
        assert len(client.queries) == 3
        # cada tentativa remonta a query (não reusa o builder)
        assert len({id(q) for q in client.queries}) == 3

    def test_queda_de_rede_e_reemitida(self):
        client = _Client([{"id": "p1"}], falhas=[httpx.ReadTimeout("lento")])
        assert RepositorioPedidos(client)._selecionar(TAB_PEDIDO) == [{"id": "p1"}]

    def test_desiste_depois_das_tentativas(self):
        client = _Client(falhas=[_erro_gateway()] * 10)
        with pytest.raises(APIError):
            RepositorioPedidos(client)._selecionar(TAB_PEDIDO)
        assert len(client.queries) == rp._TENTATIVAS_LEITURA

    @pytest.mark.parametrize("code", ["23505", "PGRST204", "42501", "42P01"])
    def test_erro_de_verdade_sobe_na_hora(self, code):
        # SQLSTATE/PGRST não é transitório: repetir só atrasaria o erro
        erro = APIError({"message": "x", "code": code, "hint": None, "details": None})
        client = _Client(falhas=[erro])
        with pytest.raises(APIError):
            RepositorioPedidos(client)._selecionar(TAB_PEDIDO)
        assert len(client.queries) == 1

    def test_escrita_nao_tem_retry(self):
        # um 502 pode chegar DEPOIS de o banco gravar — repetir duplicaria
        class _ClientEscrita(_Client):
            def from_(self, tabela):
                q = super().from_(tabela)
                q.insert = lambda linhas: q
                return q
        client = _ClientEscrita(falhas=[_erro_gateway()])
        with pytest.raises(APIError):
            RepositorioPedidos(client)._inserir(TAB_ITEM, [{"sku": "A"}])
        assert len(client.queries) == 1


class TestSelecionarIn:
    def _itens(self, n_pedidos, por_pedido):
        return [{"id": f"i{p:03d}-{k:03d}", "pedido_id": f"p{p:03d}"}
                for p in range(n_pedidos) for k in range(por_pedido)]

    def test_uma_leitura_para_varios_ids(self):
        client = _Client(self._itens(5, 3))
        linhas = RepositorioPedidos(client)._selecionar_in(
            TAB_ITEM, "pedido_id", ["p000", "p002"])
        assert {r["pedido_id"] for r in linhas} == {"p000", "p002"}
        assert len(linhas) == 6 and len(client.queries) == 1
        assert client.queries[0].ordem == "id"        # paginação estável

    def test_pagina_alem_do_corte_de_1000(self, monkeypatch):
        # o Supabase corta em max-rows SEM avisar: sem paginar, sumiriam itens
        monkeypatch.setattr(rp, "_PAGINA_LEITURA", 4)
        client = _Client(self._itens(2, 5))
        linhas = RepositorioPedidos(client)._selecionar_in(
            TAB_ITEM, "pedido_id", ["p000", "p001"])
        assert len(linhas) == 10 and len({r["id"] for r in linhas}) == 10
        assert [q.faixa for q in client.queries] == [(0, 3), (4, 7), (8, 11)]

    def test_fatia_a_lista_de_ids(self, monkeypatch):
        # o filtro IN vai na URL — lista longa demais estoura o limite
        monkeypatch.setattr(rp, "_CHUNK_IN", 2)
        client = _Client(self._itens(5, 1))
        linhas = RepositorioPedidos(client)._selecionar_in(
            TAB_ITEM, "pedido_id", [f"p{p:03d}" for p in range(5)])
        assert len(linhas) == 5
        assert [len(q.in_valores[1]) for q in client.queries] == [2, 2, 1]

    def test_lista_vazia_nao_toca_a_rede(self):
        client = _Client(self._itens(1, 1))
        assert RepositorioPedidos(client)._selecionar_in(TAB_ITEM, "pedido_id", []) == []
        assert client.queries == []


# ---------------------------------------------------------------------------
# Pós-emissão: revisões, abrir e descartar alteração (DDL 008)
# ---------------------------------------------------------------------------
def pedido_emitido(repo, com_olist=True, com_revisao=True):
    """Congela, e põe o 1º pedido como já emitido nos ERPs. Retorna o id."""
    res = repo.congelar_rodada(snapshot_min(), grupos_min())
    pid = repo.listar_pedidos(res["id"]).iloc[0]["id"]
    linha = next(p for p in repo.tabelas[TAB_PEDIDO] if p["id"] == pid)
    linha.update({"status": e.EMITIDO if com_olist else e.COMPRA_EMITIDA,
                  "bling_id": "555", "bling_numero": "78",
                  "olist_id": "900" if com_olist else None,
                  "olist_numero": "12" if com_olist else None})
    if com_revisao:
        repo.registrar_revisao(pid, e.REVISAO_EMISSAO, "t", motivo="Emissão",
                               erps_ok=("bling", "olist") if com_olist else ("bling",),
                               concluida=True)
    return pid


def _qtd(repo, pid):
    return {i["sku"]: int(i["quantidade_final"])
            for i in repo.tabelas[TAB_ITEM] if i["pedido_id"] == pid}


class TestRevisoes:
    def test_registrar_numera_em_sequencia_e_retrata_os_itens(self):
        repo = RepoFake()
        pid = pedido_emitido(repo)
        rev2 = repo.registrar_revisao(pid, e.REVISAO_ALTERACAO, "diogo", motivo="  turma menor ")

        assert rev2["numero"] == 2 and rev2["motivo"] == "turma menor"
        assert rev2["concluida_em"] is None and rev2["bling_ok_em"] is None
        assert {i["sku"]: i["quantidade"] for i in rev2["itens"]} == {"A-PP": 10, "A-M": 4}
        assert [r["numero"] for r in repo.listar_revisoes(pid)] == [1, 2]

    def test_linha_de_base_ignora_a_revisao_ainda_nao_concluida(self):
        repo = RepoFake()
        pid = pedido_emitido(repo)
        rev2 = repo.registrar_revisao(pid, e.REVISAO_ALTERACAO, "diogo")
        assert repo.obter_linha_de_base(pid)["numero"] == 1

        repo.confirmar_revisao(rev2["id"], "olist")
        assert repo.obter_linha_de_base(pid)["numero"] == 1      # parcial não é base
        repo.confirmar_revisao(rev2["id"], "bling")
        repo.confirmar_revisao(rev2["id"], concluir=True)
        assert repo.obter_linha_de_base(pid)["numero"] == 2

    def test_remover_revisao_so_tira_a_que_nenhum_erp_confirmou(self):
        repo = RepoFake()
        pid = pedido_emitido(repo)
        orfa = repo.registrar_revisao(pid, e.REVISAO_ALTERACAO, "diogo")
        repo.remover_revisao(orfa["id"])
        assert [r["numero"] for r in repo.listar_revisoes(pid)] == [1]

        parcial = repo.registrar_revisao(pid, e.REVISAO_ALTERACAO, "diogo")
        repo.confirmar_revisao(parcial["id"], "olist")
        repo.remover_revisao(parcial["id"])                      # é o registro do parcial
        assert [r["numero"] for r in repo.listar_revisoes(pid)] == [1, 2]

    def test_banco_sem_o_ddl_008_vira_mensagem_de_migracao(self):
        class SemTabela(RepoFake):
            def _selecionar(self, tabela, filtros=None, colunas="*"):
                if tabela == TAB_REVISAO:
                    raise FakeAPIError("PGRST205")
                return super()._selecionar(tabela, filtros, colunas)

        repo = SemTabela()
        pid = pedido_emitido(repo, com_revisao=False)
        with pytest.raises(MigracaoPendente, match="008"):
            repo.abrir_alteracao(pid, "diogo")
        assert repo.obter_pedido(pid)["status"] == e.EMITIDO


class TestAbrirAlteracao:
    def test_emitido_vira_em_alteracao_e_fica_editavel(self):
        repo = RepoFake()
        pid = pedido_emitido(repo)
        assert repo.abrir_alteracao(pid, "diogo") is True
        assert repo.obter_pedido(pid)["status"] == e.EM_ALTERACAO

        item = next(i for i in repo.tabelas[TAB_ITEM] if i["sku"] == "A-PP")
        assert repo.atualizar_quantidades(
            pid, [{"id": item["id"], "quantidade_final": 6}], "diogo") == 1

    def test_pedido_emitido_sem_revisao_ganha_a_linha_de_base(self):
        """Emitido antes do DDL 008: os itens travados SÃO o que foi emitido."""
        repo = RepoFake()
        pid = pedido_emitido(repo, com_olist=False, com_revisao=False)
        repo.abrir_alteracao(pid, "diogo")

        base = repo.obter_linha_de_base(pid)
        assert base["numero"] == 1 and base["tipo"] == e.REVISAO_EMISSAO
        assert base["bling_ok_em"] and not base["olist_ok_em"]   # só a compra existia
        assert {i["sku"]: i["quantidade"] for i in base["itens"]} == {"A-PP": 10, "A-M": 4}

    @pytest.mark.parametrize("status", [e.RASCUNHO, e.PRONTO, e.COMPRA_EMITINDO,
                                        e.EM_ALTERACAO, e.CANCELADO])
    def test_so_pedido_emitido_abre_alteracao(self, status):
        repo = RepoFake()
        pid = pedido_emitido(repo)
        next(p for p in repo.tabelas[TAB_PEDIDO] if p["id"] == pid)["status"] = status
        with pytest.raises(TransicaoInvalida):
            repo.abrir_alteracao(pid, "diogo")

    def test_emitido_fora_de_alteracao_continua_travado(self):
        repo = RepoFake()
        pid = pedido_emitido(repo)
        item = repo.tabelas[TAB_ITEM][0]
        with pytest.raises(PedidoNaoEditavel, match="abra uma alteração"):
            repo.atualizar_quantidades(pid, [{"id": item["id"], "quantidade_final": 1}], "t")


class TestDescartarAlteracao:
    def _em_alteracao(self):
        repo = RepoFake()
        pid = pedido_emitido(repo)
        repo.abrir_alteracao(pid, "diogo")
        return repo, pid

    def test_devolve_as_quantidades_e_tira_o_que_foi_incluido(self):
        repo, pid = self._em_alteracao()
        item = next(i for i in repo.tabelas[TAB_ITEM] if i["sku"] == "A-PP")
        repo.atualizar_quantidades(pid, [{"id": item["id"], "quantidade_final": 0}], "diogo")
        repo.adicionar_itens(pid, [item_manual("Z-M", 6)], "diogo")
        assert _qtd(repo, pid) == {"A-PP": 0, "A-M": 4, "Z-M": 6}

        assert repo.descartar_alteracao(pid, "diogo") is True
        assert _qtd(repo, pid) == {"A-PP": 10, "A-M": 4}
        assert repo.obter_pedido(pid)["status"] == e.EMITIDO

    def test_volta_a_compra_emitida_quando_nao_ha_venda(self):
        repo = RepoFake()
        pid = pedido_emitido(repo, com_olist=False)
        repo.abrir_alteracao(pid, "diogo")
        repo.descartar_alteracao(pid, "diogo")
        assert repo.obter_pedido(pid)["status"] == e.COMPRA_EMITIDA

    def test_envio_parcial_proibe_descartar(self):
        """Um ERP já recebeu a versão nova: descartar só restauraria o nosso lado."""
        repo, pid = self._em_alteracao()
        parcial = repo.registrar_revisao(pid, e.REVISAO_ALTERACAO, "diogo")
        repo.confirmar_revisao(parcial["id"], "olist")
        with pytest.raises(TransicaoInvalida, match="já recebeu"):
            repo.descartar_alteracao(pid, "diogo")
        assert repo.obter_pedido(pid)["status"] == e.EM_ALTERACAO

    def test_fora_de_alteracao_nao_ha_o_que_descartar(self):
        repo = RepoFake()
        pid = pedido_emitido(repo)
        with pytest.raises(TransicaoInvalida):
            repo.descartar_alteracao(pid, "diogo")

    def test_manual_ja_emitido_nao_se_remove_so_o_incluido_agora(self):
        repo = RepoFake()
        res = repo.congelar_rodada(snapshot_min(), grupos_min())
        pid = repo.listar_pedidos(res["id"]).iloc[0]["id"]
        repo.adicionar_itens(pid, [item_manual("Y-G", 3)], "diogo")      # manual, no rascunho
        next(p for p in repo.tabelas[TAB_PEDIDO] if p["id"] == pid).update(
            {"status": e.EMITIDO, "bling_id": "555", "olist_id": "900"})
        repo.abrir_alteracao(pid, "diogo")                               # Y-G entra na base
        repo.adicionar_itens(pid, [item_manual("Z-M", 6)], "diogo")      # incluído agora

        ids = {i["sku"]: i["id"] for i in repo.tabelas[TAB_ITEM] if i["pedido_id"] == pid}
        with pytest.raises(ItemNaoRemovivel, match="já emitido"):
            repo.remover_itens_manuais(pid, [ids["Y-G"]], "diogo")
        assert repo.remover_itens_manuais(pid, [ids["Z-M"]], "diogo") == 1
