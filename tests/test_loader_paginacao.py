"""
Testes da paginação paralela do loader (etl/loader.py).

A leitura do Supabase deixou de paginar em série dentro de cada tabela e passou
a montar uma FILA PLANA de páginas buscadas numa pool só (117 s → 11 s). Isso
trouxe três modos de falha novos, e é o que estes testes protegem:

1. `as_completed` NÃO preserva ordem — o montador precisa produzir o mesmo
   DataFrame independentemente da ordem de chegada dos lotes. Importa de
   verdade: 'Produtos_detalhes' faz drop_duplicates(keep="last"), então ordem
   instável mudaria QUAL linha sobrevive.
2. O filtro server-side precisa ser idêntico no count e nas páginas — se
   divergir, os offsets desalinham e linhas somem silenciosamente.
3. Com ~162 requests no lugar de ~9, um 5xx transitório passa a ser provável;
   cada página tem retry próprio.

Tudo aqui é sem rede: helpers puros + um fake do cliente PostgREST no espírito
dos RepoFake/HttpFake já usados na suíte.
"""

import pandas as pd
import pytest

from etl import loader
from etl.loader import (
    _PAGE_SIZE,
    FILTROS_NAO_NULOS,
    converter_data_flexivel,
    converter_serie_data,
    montar_dataframe,
    planejar_paginas,
)


# ---------------------------------------------------------------------------
# planejar_paginas — puro
# ---------------------------------------------------------------------------
class TestPlanejarPaginas:
    def test_tabela_grande_gera_uma_pagina_por_bloco(self):
        jobs = planejar_paginas({"itens": 165775}, tamanho=1000)
        assert len(jobs) == 166
        assert jobs[0] == ("itens", 0)
        assert jobs[1] == ("itens", 1000)
        assert jobs[-1] == ("itens", 165000)

    def test_multiplo_exato_nao_gera_job_vazio_extra(self):
        """2000 linhas = 2 páginas cheias. Um 3º job voltaria vazio — quem
        cuida de linhas novas depois do count é a drenagem de cauda."""
        assert planejar_paginas({"t": 2000}, tamanho=1000) == [("t", 0), ("t", 1000)]

    def test_tabela_vazia_ainda_gera_um_job(self):
        """A contagem pode estar velha; a leitura confirma o vazio."""
        assert planejar_paginas({"t": 0}, tamanho=1000) == [("t", 0)]

    def test_varias_tabelas(self):
        jobs = planejar_paginas({"a": 1500, "b": 10}, tamanho=1000)
        assert sorted(jobs) == [("a", 0), ("a", 1000), ("b", 0)]


# ---------------------------------------------------------------------------
# montar_dataframe — puro
# ---------------------------------------------------------------------------
def _lote(ids):
    return [{"id": i, "v": f"v{i}"} for i in ids]


class TestMontarDataframe:
    def test_ordem_de_chegada_nao_importa(self):
        """O teste que protege a troca de `for f in futuros` por `as_completed`."""
        na_ordem = montar_dataframe([_lote([1, 2]), _lote([3, 4]), _lote([5])])
        embaralhado = montar_dataframe([_lote([3, 4]), _lote([5]), _lote([1, 2])])
        pd.testing.assert_frame_equal(na_ordem, embaralhado)
        assert list(na_ordem["id"]) == [1, 2, 3, 4, 5]

    def test_remove_duplicatas_por_id(self):
        """Drift entre count e leitura pode repetir uma linha."""
        df = montar_dataframe([_lote([1, 2]), _lote([2, 3])])
        assert list(df["id"]) == [1, 2, 3]

    def test_sem_coluna_id_apenas_concatena(self):
        df = montar_dataframe([[{"a": 1}], [{"a": 2}]])
        assert len(df) == 2

    def test_lotes_vazios(self):
        assert montar_dataframe([[], []]).empty


# ---------------------------------------------------------------------------
# Fake do cliente PostgREST — sem rede
# ---------------------------------------------------------------------------
class _Resposta:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class _Query:
    """Reproduz o encadeamento usado pelo loader:
    from_().select().order().not_.is_().range()/limit().execute()"""

    def __init__(self, fake, tabela):
        self.fake, self.tabela = fake, tabela
        self.contando = False
        self.filtros = []
        self.faixa = None
        self.ordenado = False

    def select(self, *args, count=None):
        self.contando = count == "exact"
        return self

    def order(self, _col):
        self.ordenado = True
        return self

    @property
    def not_(self):
        return self

    def is_(self, col, _valor):
        self.filtros.append(col)
        return self

    def limit(self, _n):
        return self

    def range(self, ini, fim):
        self.faixa = (ini, fim)
        return self

    def execute(self):
        self.fake.registrar(self)
        linhas = self.fake.linhas_filtradas(self.tabela, self.filtros)
        if self.contando:
            return _Resposta([], count=len(linhas))
        ini, fim = self.faixa
        return _Resposta(linhas[ini:fim + 1])


class FakeClient:
    def __init__(self, tabelas, falhas=None):
        self.tabelas = tabelas
        self.falhas = falhas or {}   # (tabela, offset) → nº de falhas a simular
        self.queries = []
        self.chamadas = 0

    def from_(self, tabela):
        return _Query(self, tabela)

    def registrar(self, q):
        self.queries.append(q)
        self.chamadas += 1
        if q.faixa is not None:
            chave = (q.tabela, q.faixa[0])
            if self.falhas.get(chave, 0) > 0:
                self.falhas[chave] -= 1
                raise RuntimeError("500 transitório")

    def linhas_filtradas(self, tabela, filtros):
        linhas = self.tabelas[tabela]
        for col in filtros:
            linhas = [l for l in linhas if l.get(col) is not None]
        return linhas


@pytest.fixture
def cliente():
    return FakeClient({
        "itens": [{"id": i, "id_pedido_bling": None if i % 2 else i} for i in range(1, 11)],
        "lojas": [{"id": i} for i in range(1, 4)],
    })


# ---------------------------------------------------------------------------
# _contar / _ler_pagina — filtro simétrico e retry
# ---------------------------------------------------------------------------
class TestFiltroServerSide:
    def test_count_e_pagina_usam_o_mesmo_filtro(self, cliente, monkeypatch):
        """O modo de falha mais perigoso: filtrar só num dos dois lados faz o
        offset apontar para a linha errada e some com dados sem erro nenhum."""
        monkeypatch.setitem(FILTROS_NAO_NULOS, "itens", ("id_pedido_bling",))

        _, n = loader._contar(cliente, "itens")
        pagina = loader._ler_pagina(cliente, "itens", 0)

        assert n == 5                      # metade tem id_pedido_bling nulo
        assert len(pagina) == 5
        assert all(l["id_pedido_bling"] is not None for l in pagina)

        filtros_usados = {tuple(q.filtros) for q in cliente.queries}
        assert filtros_usados == {("id_pedido_bling",)}

    def test_tabela_sem_filtro_le_tudo(self, cliente):
        _, n = loader._contar(cliente, "lojas")
        assert n == 3
        assert cliente.queries[-1].filtros == []

    def test_pagina_sempre_ordenada_por_id(self, cliente):
        """Sem ORDER BY explícito o PostgREST não garante ordem estável entre
        requests — páginas paralelas poderiam duplicar ou pular linhas."""
        loader._ler_pagina(cliente, "lojas", 0)
        assert cliente.queries[-1].ordenado


class TestRetryDePagina:
    def test_reemite_e_tem_sucesso(self, cliente, monkeypatch):
        monkeypatch.setattr(loader.time, "sleep", lambda _s: None)
        cliente.falhas[("lojas", 0)] = 2          # falha 2×, acerta na 3ª
        assert len(loader._ler_pagina(cliente, "lojas", 0)) == 3
        assert cliente.chamadas == 3

    def test_desiste_apos_o_limite(self, cliente, monkeypatch):
        monkeypatch.setattr(loader.time, "sleep", lambda _s: None)
        cliente.falhas[("lojas", 0)] = 99
        with pytest.raises(RuntimeError, match="após 3 tentativas"):
            loader._ler_pagina(cliente, "lojas", 0)
        assert cliente.chamadas == loader._TENTATIVAS_PAGINA


# ---------------------------------------------------------------------------
# Drenagem de cauda
# ---------------------------------------------------------------------------
class TestDrenarCauda:
    def test_busca_linhas_criadas_apos_o_count(self, monkeypatch):
        """A pipeline externa escreve o tempo todo: se a última página planejada
        veio cheia, pode haver linhas depois dela."""
        monkeypatch.setattr(loader, "_PAGE_SIZE", 2)
        cliente = FakeClient({"t": [{"id": i} for i in range(1, 6)]})
        lotes = []
        loader._drenar_cauda(cliente, "t", lotes, ultimo_offset=2)
        assert [l["id"] for lote in lotes for l in lote] == [5]

    def test_para_quando_nao_ha_nada_adiante(self, monkeypatch):
        monkeypatch.setattr(loader, "_PAGE_SIZE", 2)
        cliente = FakeClient({"t": [{"id": i} for i in range(1, 5)]})
        lotes = []
        loader._drenar_cauda(cliente, "t", lotes, ultimo_offset=2)
        assert lotes == []


# ---------------------------------------------------------------------------
# Callback de progresso — testável sem Streamlit
# ---------------------------------------------------------------------------
def _parametros(fn):
    """Parâmetros da função por baixo do @st.cache_data."""
    import inspect
    return list(inspect.signature(getattr(fn, "__wrapped__", fn)).parameters)


class TestContratoDeProgresso:
    def test_progresso_e_opcional(self):
        """Scripts CLI e testes chamam sem callback — não pode virar obrigatório."""
        import inspect
        for fn in (loader.carregar_dados, loader._ler_supabase):
            alvo = getattr(fn, "__wrapped__", fn)
            assert inspect.signature(alvo).parameters["_progresso"].default is None

    def test_todo_parametro_fica_fora_do_hash(self):
        """Prefixo `_` é a convenção do Streamlit para NÃO entrar na cache key.
        Sem ele, cada rerun traz um callback novo → chave nova → o cache de 1 h
        nunca acerta, e a carga de 11 s viraria permanente em vez de horária."""
        for fn in (loader.carregar_dados, loader._ler_supabase):
            assert all(p.startswith("_") for p in _parametros(fn)), _parametros(fn)


# ---------------------------------------------------------------------------
# converter_serie_data — vetorização equivalente ao elemento a elemento
# ---------------------------------------------------------------------------
class TestConverterSerieData:
    def test_equivale_ao_elemento_a_elemento(self):
        s = pd.Series(["2026-02-24", "24/02/2026", "03/12/2026", None,
                       pd.NA, "não é data", "2026-13-99"])
        esperado = pd.Series(list(s.apply(converter_data_flexivel)))
        pd.testing.assert_series_equal(converter_serie_data(s), esperado)

    def test_iso_puro_caminho_rapido(self):
        s = pd.Series(["2026-01-01", "2026-06-15"])
        assert list(converter_serie_data(s)) == [
            pd.Timestamp("2026-01-01"), pd.Timestamp("2026-06-15")]

    def test_br_cai_no_fallback(self):
        """dayfirst: 03/12 é 3 de dezembro, não 12 de março."""
        assert converter_serie_data(pd.Series(["03/12/2026"]))[0] == pd.Timestamp("2026-12-03")

    def test_serie_toda_nula(self):
        assert converter_serie_data(pd.Series([None, pd.NA])).isna().all()
