"""
Testes do RepositorioUsuarios (auth_store.py) com fake do gateway em memória —
sem Supabase/rede.
"""

import pytest
from postgrest.exceptions import APIError

from auth_store import (
    EmailInvalido, RepositorioUsuarios, TAB_USUARIO, UsuarioJaExiste,
    email_valido, normalizar_email,
)


class RepoFake(RepositorioUsuarios):
    """Gateway em memória, com a unicidade do e-mail (PK) que o banco impõe."""

    def __init__(self, linhas=None):
        super().__init__(client=None)
        self.tabelas = {TAB_USUARIO: [dict(l) for l in (linhas or [])]}

    def _selecionar(self, tabela, filtros=None, colunas="*"):
        return [dict(r) for r in self.tabelas[tabela]
                if all(r.get(k) == v for k, v in (filtros or {}).items())]

    def _inserir(self, tabela, linhas):
        out = []
        for linha in (linhas if isinstance(linhas, list) else [linhas]):
            linha = dict(linha)
            if any(r["email"] == linha["email"] for r in self.tabelas[tabela]):
                raise APIError({"code": "23505", "message": "duplicate key"})
            self.tabelas[tabela].append(linha)
            out.append(dict(linha))
        return out

    def _atualizar(self, tabela, filtros, valores):
        out = []
        for r in self.tabelas[tabela]:
            if all(r.get(k) == v for k, v in filtros.items()):
                r.update(valores)
                out.append(dict(r))
        return out

    def _deletar(self, tabela, filtros):
        self.tabelas[tabela] = [
            r for r in self.tabelas[tabela]
            if not all(r.get(k) == v for k, v in filtros.items())
        ]


class RepoQuebrado(RepositorioUsuarios):
    """Gateway que estoura sempre — simula DDL ausente / schema fora do ar."""

    def __init__(self, code):
        super().__init__(client=None)
        self.code = code

    def _selecionar(self, tabela, filtros=None, colunas="*"):
        raise APIError({"code": self.code, "message": "boom"})

    def _atualizar(self, tabela, filtros, valores):
        raise APIError({"code": self.code, "message": "boom"})


def base():
    return [
        {"email": "chefe@x.com", "nome": "Chefe", "role": "admin", "ativo": True},
        {"email": "ana@x.com", "nome": "Ana", "role": "vendedor", "ativo": True},
    ]


class TestNormalizacao:
    def test_normalizar_email(self):
        assert normalizar_email("  ANA@X.com  ") == "ana@x.com"
        assert normalizar_email(None) == ""

    def test_email_valido(self):
        assert email_valido("ana@x.com")
        assert not email_valido("ana@x")
        assert not email_valido("ana")
        assert not email_valido("")


class TestLeitura:
    def test_mapa_por_email_normaliza_a_chave(self):
        repo = RepoFake([{"email": "Ana@X.com", "role": "vendedor", "ativo": True}])
        mapa, fonte_ok = repo.mapa_por_email()
        assert fonte_ok is True and "ana@x.com" in mapa

    def test_listar_ordena_por_email(self):
        repo = RepoFake(base())
        assert [u["email"] for u in repo.listar()] == ["ana@x.com", "chefe@x.com"]

    @pytest.mark.parametrize("code", ["42P01", "PGRST205", "PGRST106"])
    def test_tabela_ausente_devolve_fonte_ok_false(self, code):
        # Crítico: ({}, False), NUNCA ({}, True) — senão uma queda do banco
        # viraria "a allowlist está vazia" e ninguém seria admin.
        mapa, fonte_ok = RepoQuebrado(code).mapa_por_email()
        assert mapa == {} and fonte_ok is False

    def test_outro_erro_sobe(self):
        with pytest.raises(APIError):
            RepoQuebrado("42501").mapa_por_email()


class TestCriar:
    def test_cria_normalizado_e_ativo(self):
        repo = RepoFake(base())
        repo.criar("  NOVO@X.com ", "  Novo  ", "estoque", usuario="chefe@x.com")
        criado = repo.mapa_por_email()[0]["novo@x.com"]
        assert criado["role"] == "estoque" and criado["ativo"] is True
        assert criado["nome"] == "Novo" and criado["criado_por"] == "chefe@x.com"

    def test_email_invalido(self):
        with pytest.raises(EmailInvalido):
            RepoFake(base()).criar("sem-arroba", "X", "vendedor", usuario="chefe@x.com")

    def test_duplicado_vira_erro_legivel(self):
        with pytest.raises(UsuarioJaExiste):
            RepoFake(base()).criar("ana@x.com", "Ana", "vendedor", usuario="chefe@x.com")

    def test_role_invalido(self):
        with pytest.raises(ValueError):
            RepoFake(base()).criar("novo@x.com", "N", "deus", usuario="chefe@x.com")

    def test_nome_truncado(self):
        repo = RepoFake(base())
        repo.criar("novo@x.com", "A" * 200, "vendedor", usuario="chefe@x.com")
        assert len(repo.mapa_por_email()[0]["novo@x.com"]["nome"]) == 120


class TestSalvarLote:
    def test_grava_so_o_diff_com_auditoria(self):
        repo = RepoFake(base())
        n = repo.salvar_lote(
            [{"email": "ana@x.com", "nome": "Ana", "role": "supervisor", "ativo": False}],
            usuario="chefe@x.com")
        mapa = repo.mapa_por_email()[0]
        assert n == 1
        assert mapa["ana@x.com"]["role"] == "supervisor"
        assert mapa["ana@x.com"]["ativo"] is False
        assert mapa["ana@x.com"]["atualizado_por"] == "chefe@x.com"
        assert "atualizado_por" not in mapa["chefe@x.com"]   # não tocado

    def test_role_invalido_barrado_antes_do_banco(self):
        repo = RepoFake(base())
        with pytest.raises(ValueError):
            repo.salvar_lote([{"email": "ana@x.com", "nome": "Ana", "role": "deus",
                               "ativo": True}], usuario="chefe@x.com")
        assert repo.mapa_por_email()[0]["ana@x.com"]["role"] == "vendedor"

    def test_linha_sem_email_barrada(self):
        with pytest.raises(EmailInvalido):
            RepoFake(base()).salvar_lote(
                [{"email": "", "nome": "X", "role": "vendedor", "ativo": True}],
                usuario="chefe@x.com")


class TestRemoverEAcesso:
    def test_remover(self):
        repo = RepoFake(base())
        repo.remover("ANA@x.com ")
        assert "ana@x.com" not in repo.mapa_por_email()[0]

    def test_registrar_acesso_carimba(self):
        repo = RepoFake(base())
        repo.registrar_acesso("ana@x.com")
        assert repo.mapa_por_email()[0]["ana@x.com"]["ultimo_acesso"]

    def test_registrar_acesso_engole_falha(self):
        # Telemetria nunca derruba um login.
        RepoQuebrado("42P01").registrar_acesso("ana@x.com")
