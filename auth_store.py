"""
auth_store.py — Porta de acesso a app.usuario (DDL 006).

Allowlist do dashboard: quem entra (`ativo`) e o que vê (`role`). Substitui o
cadastro de pessoas que vivia em st.secrets["auth_config"] — o login em si é
o OIDC nativo do Streamlit (ver auth.py).

Reusa o client PostgREST do schema `app` (pedidos.repositorio._conn_app),
mesmo padrão do etl/config_store.py e do pedidos/integracoes/repositorio.py.
Gateway fino (_selecionar/_inserir/_atualizar/_deletar) falsificável nos testes.

REGRA DE OURO: a leitura devolve (mapa, fonte_ok). Um Supabase fora do ar
devolve ({}, False) — NUNCA ({}, True). Confundir "não consegui ler" com
"a tabela está vazia" transformaria uma queda do banco em "ninguém é admin".
"""

import re

import pandas as pd
from postgrest.exceptions import APIError


TAB_USUARIO = "usuario"

ROLES_VALIDOS = ("admin", "supervisor", "vendedor", "estoque")

MAX_NOME = 120

# Mesmos códigos tratados em pedidos/integracoes/repositorio.py: significam
# "DDL 006 ainda não aplicado / schema não exposto". Aqui NÃO viram lista
# vazia — viram fonte_ok=False (ver regra de ouro no topo).
#   42P01    = Postgres: relation does not exist
#   PGRST205 = PostgREST: tabela ausente no cache de schema
#   PGRST106 = PostgREST: schema não exposto na Data API
_ERROS_TABELA_AUSENTE = {"42P01", "PGRST205", "PGRST106"}

# Validação deliberadamente frouxa: quem valida e-mail de verdade é o Google
# no login. Isto só pega erro de digitação grosseiro no formulário de convite.
_RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class EmailInvalido(ValueError):
    """E-mail que não passa na validação do formulário de convite."""


class UsuarioJaExiste(ValueError):
    """Convite para um e-mail que já está na allowlist."""


def normalizar_email(valor) -> str:
    """Forma canônica do e-mail: sem espaços e minúsculo (o CHECK do DDL exige)."""
    return str(valor or "").strip().lower()


def email_valido(valor) -> bool:
    return bool(_RE_EMAIL.match(normalizar_email(valor)))


def _agora_iso() -> str:
    return pd.Timestamp.now(tz="UTC").isoformat()


def obter_repositorio_usuarios() -> "RepositorioUsuarios":
    """Fábrica usada por auth.py e pela aba Usuários — reusa o client do schema `app`."""
    from pedidos.repositorio import _conn_app
    return RepositorioUsuarios(_conn_app())


class RepositorioUsuarios:
    """Porta de app.usuario. Client injetado (testável sem Supabase)."""

    def __init__(self, client):
        self._client = client

    # -----------------------------------------------------------------
    # Gateway interno (o que os testes falsificam)
    # -----------------------------------------------------------------
    def _selecionar(self, tabela: str, filtros: dict = None, colunas: str = "*") -> list:
        q = self._client.from_(tabela).select(colunas)
        for col, val in (filtros or {}).items():
            q = q.eq(col, val)
        return q.execute().data or []

    def _inserir(self, tabela: str, linhas) -> list:
        return self._client.from_(tabela).insert(linhas).execute().data or []

    def _atualizar(self, tabela: str, filtros: dict, valores: dict) -> list:
        q = self._client.from_(tabela).update(valores)
        for col, val in filtros.items():
            q = q.eq(col, val)
        return q.execute().data or []

    def _deletar(self, tabela: str, filtros: dict) -> None:
        q = self._client.from_(tabela).delete()
        for col, val in filtros.items():
            q = q.eq(col, val)
        q.execute()

    # -----------------------------------------------------------------
    # Leitura
    # -----------------------------------------------------------------
    def mapa_por_email(self) -> tuple:
        """
        ({email: linha}, fonte_ok) — a allowlist inteira, chaveada pelo e-mail
        normalizado. São poucas linhas: auth.py cacheia isto por 5 min e resolve
        todo login em memória.

        fonte_ok=False significa "não consegui ler" (DDL ausente, schema fora
        do ar, PostgREST indisponível) e faz o login falhar FECHADO. Só a lista
        de break-glass do secrets entra sem consultar isto.
        """
        try:
            linhas = self._selecionar(TAB_USUARIO)
        except APIError as e:
            if e.code in _ERROS_TABELA_AUSENTE:
                return {}, False
            raise
        return {normalizar_email(l.get("email")): l for l in linhas}, True

    def listar(self) -> list:
        """Allowlist ordenada por e-mail, para a grade da aba Usuários."""
        linhas, _ = self.mapa_por_email()
        return sorted(linhas.values(), key=lambda l: normalizar_email(l.get("email")))

    # -----------------------------------------------------------------
    # Escrita (não degrada: não se grava em tabela inexistente)
    # -----------------------------------------------------------------
    def criar(self, email: str, nome: str, role: str, usuario: str) -> dict:
        """Convite do admin — a ÚNICA porta de entrada na allowlist."""
        email = normalizar_email(email)
        if not email_valido(email):
            raise EmailInvalido(f"E-mail inválido: {email or '(vazio)'}")
        if role not in ROLES_VALIDOS:
            raise ValueError(f"Perfil inválido: {role}")

        agora = _agora_iso()
        try:
            criadas = self._inserir(TAB_USUARIO, [{
                "email": email,
                "nome": str(nome or "").strip()[:MAX_NOME],
                "role": role,
                "ativo": True,
                "criado_por": usuario,
                "atualizado_em": agora,
                "atualizado_por": usuario,
            }])
        except Exception as exc:
            if getattr(exc, "code", "") == "23505" or "23505" in str(exc):
                raise UsuarioJaExiste(
                    f"{email} já está cadastrado. Edite o perfil na tabela abaixo."
                ) from exc
            raise
        return criadas[0] if criadas else {}

    def salvar_lote(self, alteracoes: list, usuario: str) -> int:
        """
        Grava só as linhas que MUDARAM (o diff vem da página). Um UPDATE por
        linha: são poucas, e assim `atualizado_por` fica legível — dois admins
        editando ao mesmo tempo só colidem nas linhas que ambos tocaram.

        Escreve apenas nome/role/ativo: e-mail é PK e o resto é do sistema.
        """
        agora = _agora_iso()
        gravadas = 0
        for linha in alteracoes:
            email = normalizar_email(linha.get("email"))
            role = linha.get("role")
            if not email:
                raise EmailInvalido("Linha sem e-mail.")
            if role not in ROLES_VALIDOS:
                raise ValueError(f"Perfil inválido para {email}: {role}")
            self._atualizar(TAB_USUARIO, {"email": email}, {
                "nome": str(linha.get("nome") or "").strip()[:MAX_NOME],
                "role": role,
                "ativo": bool(linha.get("ativo")),
                "atualizado_em": agora,
                "atualizado_por": usuario,
            })
            gravadas += 1
        return gravadas

    def remover(self, email: str) -> None:
        self._deletar(TAB_USUARIO, {"email": normalizar_email(email)})

    def registrar_acesso(self, email: str) -> None:
        """
        Carimba o último acesso (1x por sessão, chamado pelo auth.py).
        É telemetria: qualquer falha é engolida — nunca derruba um login.
        """
        try:
            self._atualizar(TAB_USUARIO, {"email": normalizar_email(email)},
                            {"ultimo_acesso": _agora_iso()})
        except Exception:
            pass
