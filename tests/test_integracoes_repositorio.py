"""
Testes do RepositorioIntegracoes (pedidos/integracoes/repositorio.py) com
fake do gateway em memória — sem Supabase/rede.
"""

import pandas as pd
import pytest
from postgrest.exceptions import APIError

from pedidos.integracoes.repositorio import (
    RepositorioIntegracoes, TAB_INTEGRACAO, TAB_EVENTO, TAB_PRODUTO_CACHE,
)


class RepoFake(RepositorioIntegracoes):
    """Gateway em memória: as 2 linhas seed ('bling','olist') + eventos + cache."""

    def __init__(self):
        super().__init__(client=None)
        self.tabelas = {
            TAB_INTEGRACAO: [
                {"id": "bling", "config": {}, "oauth_state": None},
                {"id": "olist", "config": {}, "oauth_state": None},
            ],
            TAB_EVENTO: [],
            TAB_PRODUTO_CACHE: [],
        }
        self._seq = 0

    def _selecionar(self, tabela, filtros=None, colunas="*"):
        return [dict(r) for r in self.tabelas[tabela]
                if all(r.get(k) == v for k, v in (filtros or {}).items())]

    def _selecionar_in(self, tabela, coluna, valores, colunas="*"):
        alvo = set(valores or [])
        return [dict(r) for r in self.tabelas[tabela] if r.get(coluna) in alvo]

    def _atualizar(self, tabela, filtros, valores):
        out = []
        for r in self.tabelas[tabela]:
            if all(r.get(k) == v for k, v in filtros.items()):
                r.update(valores)
                out.append(dict(r))
        return out

    def _inserir(self, tabela, linhas):
        out = []
        for linha in (linhas if isinstance(linhas, list) else [linhas]):
            linha = dict(linha)
            self._seq += 1
            linha.setdefault("id", f"ev-{self._seq}")
            linha.setdefault("criado_em", pd.Timestamp.now(tz="UTC").isoformat())
            self.tabelas[tabela].append(linha)
            out.append(dict(linha))
        return out

    def _upsert(self, tabela, linhas):
        alvo = self.tabelas.setdefault(tabela, [])
        for linha in linhas:
            linha = dict(linha)
            existe = next((r for r in alvo if r.get("sku") == linha.get("sku")), None)
            if existe:
                existe.update(linha)
            else:
                alvo.append(linha)
        return [dict(r) for r in alvo]


class TestChavesEConfig:
    def test_salvar_e_ler_chaves(self):
        repo = RepoFake()
        repo.salvar_chaves("bling", "cid-123", "secreto", "https://app/configuracoes", "diogo")
        integ = repo.ler("bling")
        assert integ["client_id"] == "cid-123"
        assert integ["client_secret"] == "secreto"
        assert integ["redirect_uri"] == "https://app/configuracoes"
        assert integ["atualizado_por"] == "diogo"

    def test_secret_vazio_preserva_o_salvo(self):
        repo = RepoFake()
        repo.salvar_chaves("bling", "cid", "secreto-original", "https://x", "diogo")
        # UI reenvia com o campo password em branco → não apaga
        repo.salvar_chaves("bling", "cid-novo", "", "https://x", "diogo")
        integ = repo.ler("bling")
        assert integ["client_secret"] == "secreto-original"
        assert integ["client_id"] == "cid-novo"

    def test_salvar_config_substitui_jsonb(self):
        repo = RepoFake()
        repo.salvar_config("olist", {"contato_id": 1, "vendedor_id": 2}, "diogo")
        repo.salvar_config("olist", {"contato_id": 9}, "diogo")
        assert repo.ler("olist")["config"] == {"contato_id": 9}

    def test_ler_plataforma_inexistente_dict_vazio(self):
        assert RepoFake().ler("sap") == {}


class TestFluxoOAuth:
    def test_state_persiste_e_identifica_plataforma(self):
        repo = RepoFake()
        repo.salvar_state_oauth("olist", "state-abc", "diogo")
        achado = repo.buscar_por_state("state-abc")
        assert achado["id"] == "olist"

    def test_state_desconhecido_ou_vazio_none(self):
        repo = RepoFake()
        assert repo.buscar_por_state("nao-existe") is None
        assert repo.buscar_por_state("") is None
        assert repo.buscar_por_state(None) is None

    def test_concluir_oauth_grava_tokens_e_zera_state(self):
        repo = RepoFake()
        repo.salvar_state_oauth("bling", "state-x", "diogo")
        repo.concluir_oauth("bling", "acc-1", "ref-1", "2026-07-14T18:00:00+00:00", "diogo")
        integ = repo.ler("bling")
        assert integ["access_token"] == "acc-1"
        assert integ["refresh_token"] == "ref-1"
        assert integ["oauth_state"] is None
        assert integ["conectado_por"] == "diogo"
        assert repo.buscar_por_state("state-x") is None

    def test_atualizar_tokens_sem_refresh_preserva(self):
        repo = RepoFake()
        repo.concluir_oauth("bling", "acc-1", "ref-1", "2026-07-14T18:00:00+00:00", "diogo")
        # refresh vazio (plataforma não rotacionou) → mantém o anterior
        repo.atualizar_tokens("bling", "acc-2", "", "2026-07-15T00:00:00+00:00")
        integ = repo.ler("bling")
        assert integ["access_token"] == "acc-2"
        assert integ["refresh_token"] == "ref-1"

    def test_atualizar_tokens_com_rotacao(self):
        repo = RepoFake()
        repo.concluir_oauth("olist", "acc-1", "ref-1", "2026-07-14T18:00:00+00:00", "diogo")
        repo.atualizar_tokens("olist", "acc-2", "ref-2", "2026-07-15T00:00:00+00:00")
        assert repo.ler("olist")["refresh_token"] == "ref-2"


class TestEventos:
    def test_registra_e_lista_mais_recentes_primeiro(self):
        repo = RepoFake()
        repo.registrar_evento("bling", "testar_conexao", True, usuario="diogo")
        repo.registrar_evento("olist", "emitir_venda", False,
                              detalhe={"erro": "SKU sem match"}, pedido_id="ped-1",
                              usuario="diogo")
        df = repo.listar_eventos()
        assert len(df) == 2
        assert df.iloc[0]["acao"] == "emitir_venda"       # mais recente primeiro
        assert df.iloc[0]["sucesso"] == False              # noqa: E712 (valor do banco)
        assert df.iloc[0]["pedido_id"] == "ped-1"

    def test_limite(self):
        repo = RepoFake()
        for i in range(30):
            repo.registrar_evento("bling", "testar_conexao", True)
        assert len(repo.listar_eventos(limite=20)) == 20


class _QueryErro:
    """Query PostgREST que estoura no execute() — simula tabela ausente/erro."""

    def __init__(self, exc):
        self._exc = exc

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def execute(self):
        raise self._exc


class _ClientErro:
    """Client PostgREST fake cujo acesso estoura com um APIError de `code` —
    usado para exercitar o `_selecionar` REAL (o RepoFake o substitui)."""

    def __init__(self, code):
        self._exc = APIError({
            "message": 'relation "app.integracao" does not exist',
            "code": code, "hint": None, "details": None,
        })

    def from_(self, tabela):
        return _QueryErro(self._exc)


class TestDegradacaoSemDDL:
    """DDL 003 não aplicado (ou schema `app` não exposto): a LEITURA degrada p/
    vazio em vez de derrubar a página. Client fake (não gateway fake) para bater
    no `_selecionar` de verdade, onde vive o try/except."""

    @pytest.mark.parametrize("code", ["42P01", "PGRST205", "PGRST106"])
    def test_leitura_degrada_para_vazio(self, code):
        repo = RepositorioIntegracoes(_ClientErro(code))
        assert repo.ler("bling") == {}
        assert repo.buscar_por_state("qualquer") is None
        assert repo.listar_eventos().empty

    def test_outros_erros_de_api_sobem(self):
        # erro que NÃO é "tabela ausente" não pode ser engolido silenciosamente
        repo = RepositorioIntegracoes(_ClientErro("42501"))  # insufficient_privilege
        with pytest.raises(APIError):
            repo.ler("bling")
