"""
Testes do núcleo PURO de auth.py — resolução de acesso, mapa role→páginas e
validação da edição de usuários. Sem Streamlit, sem Supabase, sem rede.
"""

import pytest

import auth
from auth import (
    AUTORIZADO, INATIVO, INDISPONIVEL, NAO_AUTORIZADO,
    paginas_do_role, resolver_acesso, validar_edicao_usuarios,
)


def linha(email="ana@x.com", role="vendedor", ativo=True, nome="Ana"):
    return {"email": email, "role": role, "ativo": ativo, "nome": nome}


class TestResolverAcesso:
    def test_ativo_com_role_valido_autoriza(self):
        a = resolver_acesso("ana@x.com", "Ana", linha(), set(), True)
        assert (a.estado, a.role) == (AUTORIZADO, "vendedor")

    def test_inativo(self):
        a = resolver_acesso("ana@x.com", "Ana", linha(ativo=False), set(), True)
        assert a.estado == INATIVO and a.role == ""

    def test_email_fora_da_allowlist(self):
        a = resolver_acesso("estranho@x.com", "Estranho", None, set(), True)
        assert a.estado == NAO_AUTORIZADO and a.role == ""

    def test_email_vazio(self):
        assert resolver_acesso("", "", None, set(), True).estado == NAO_AUTORIZADO

    def test_fonte_indisponivel_falha_fechado(self):
        a = resolver_acesso("ana@x.com", "Ana", linha(), set(), False)
        assert a.estado == INDISPONIVEL and a.role == ""

    def test_role_invalido_no_banco_nao_passa(self):
        # Lixo de uma versão antiga ou edição manual: não vira acesso com
        # perfil indefinido.
        a = resolver_acesso("ana@x.com", "Ana", linha(role="pendente"), set(), True)
        assert a.estado == NAO_AUTORIZADO

    def test_break_glass_vence_fonte_indisponivel(self):
        a = resolver_acesso("chefe@x.com", "Chefe", None, {"chefe@x.com"}, False)
        assert (a.estado, a.role) == (AUTORIZADO, "admin")

    def test_break_glass_vence_ausencia_na_tabela(self):
        a = resolver_acesso("chefe@x.com", "Chefe", None, {"chefe@x.com"}, True)
        assert (a.estado, a.role) == (AUTORIZADO, "admin")

    def test_break_glass_normaliza_os_dois_lados(self):
        a = resolver_acesso("  Chefe@X.com ", "Chefe", None, {"CHEFE@x.com"}, True)
        assert a.estado == AUTORIZADO and a.email == "chefe@x.com"

    def test_email_normalizado_resolve_igual(self):
        a = resolver_acesso("  ANA@X.com ", "Ana", linha(), set(), True)
        assert a.estado == AUTORIZADO and a.email == "ana@x.com"


class TestPaginasDoRole:
    def test_admin_ve_tudo(self):
        assert paginas_do_role("admin") is None

    def test_role_desconhecido_devolve_tupla_vazia(self):
        # Regressão: o `.get()` antigo devolvia None para role fora do mapa —
        # e None significa "todas as páginas".
        for role in ("pendente", "", "typo", None):
            assert paginas_do_role(role) == ()

    def test_todo_role_valido_ve_ao_menos_uma_pagina(self):
        # Lista vazia faria st.navigation() levantar exceção.
        for role in auth.ROLES_VALIDOS:
            p = paginas_do_role(role)
            assert p is None or len(p) >= 1


class TestValidarEdicaoUsuarios:
    def setup_method(self):
        self.atuais = [linha("chefe@x.com", "admin"), linha("ana@x.com", "vendedor")]

    def test_edicao_legitima_passa(self):
        novas = [linha("chefe@x.com", "admin"), linha("ana@x.com", "supervisor")]
        assert validar_edicao_usuarios(novas, self.atuais, "chefe@x.com") == []

    def test_sem_nenhum_admin_ativo_falha(self):
        novas = [linha("chefe@x.com", "admin", ativo=False), linha("ana@x.com", "vendedor")]
        assert any("administrador ativo" in e
                   for e in validar_edicao_usuarios(novas, self.atuais, "outro@x.com"))

    def test_auto_rebaixamento_falha(self):
        novas = [linha("chefe@x.com", "vendedor"), linha("ana@x.com", "admin")]
        assert any("próprio acesso" in e
                   for e in validar_edicao_usuarios(novas, self.atuais, "chefe@x.com"))

    def test_auto_desativacao_falha(self):
        novas = [linha("chefe@x.com", "admin", ativo=False), linha("ana@x.com", "admin")]
        assert any("próprio acesso" in e
                   for e in validar_edicao_usuarios(novas, self.atuais, "chefe@x.com"))

    def test_auto_remocao_falha(self):
        novas = [linha("ana@x.com", "admin")]
        assert any("próprio acesso" in e
                   for e in validar_edicao_usuarios(novas, self.atuais, "chefe@x.com"))

    def test_role_invalido_falha(self):
        novas = [linha("chefe@x.com", "admin"), linha("ana@x.com", "deus")]
        assert any("Perfil inválido" in e
                   for e in validar_edicao_usuarios(novas, self.atuais, "chefe@x.com"))

    def test_email_duplicado_falha(self):
        novas = [linha("chefe@x.com", "admin"), linha("chefe@x.com", "vendedor")]
        assert any("repetido" in e
                   for e in validar_edicao_usuarios(novas, self.atuais, "chefe@x.com"))

    def test_linha_sem_email_falha(self):
        novas = [linha("chefe@x.com", "admin"), linha("", "vendedor")]
        assert any("sem e-mail" in e
                   for e in validar_edicao_usuarios(novas, self.atuais, "chefe@x.com"))

    def test_nome_longo_demais_falha(self):
        novas = [linha("chefe@x.com", "admin"), linha("ana@x.com", "vendedor", nome="A" * 121)]
        assert any("muito longo" in e
                   for e in validar_edicao_usuarios(novas, self.atuais, "chefe@x.com"))
