"""
repositorio.py — Porta de acesso a app.integracao / app.integracao_evento.

Guarda credenciais OAuth2 (client_id/secret), tokens (access/refresh) e a
config de negócio de cada plataforma ('bling' | 'olist'), mais o log de
auditoria de toda interação com os ERPs.

Reusa o client PostgREST do schema `app` (pedidos.repositorio._conn_app) —
mesmo padrão do etl/config_store.py. Gateway fino (_selecionar/_atualizar/
_inserir) falsificável nos testes.

REGRA DE OURO: `detalhe` de evento NUNCA contém tokens/secrets.
"""

import pandas as pd
from postgrest.exceptions import APIError

from pedidos import estados  # noqa: F401  (constantes de plataforma futuras)


TAB_INTEGRACAO = "integracao"
TAB_EVENTO = "integracao_evento"
TAB_PRODUTO_CACHE = "olist_produto_cache"   # DDL 005 (opcional; degrada se ausente)

PLATAFORMAS_VALIDAS = ("bling", "olist")

# Códigos que significam "as tabelas do schema `app` da integração ainda não
# existem/estão expostas" (DDL 003 não aplicado). A LEITURA degrada para vazio em
# vez de derrubar a página inteira — a UI mostra "aplique o DDL". A ESCRITA segue
# estourando (não se grava numa tabela inexistente).
#   42P01    = Postgres: relation does not exist
#   PGRST205 = PostgREST: tabela ausente no cache de schema
#   PGRST106 = PostgREST: schema não exposto na Data API
_ERROS_TABELA_AUSENTE = {"42P01", "PGRST205", "PGRST106"}


class IntegracaoNaoConectada(Exception):
    """Plataforma sem refresh_token válido — precisa (re)conectar na UI."""


def _agora_iso() -> str:
    return pd.Timestamp.now(tz="UTC").isoformat()


def obter_repositorio_integracoes() -> "RepositorioIntegracoes":
    """Fábrica usada pelas páginas — reusa o client do schema `app`."""
    from pedidos.repositorio import _conn_app
    return RepositorioIntegracoes(_conn_app())


class RepositorioIntegracoes:
    """Porta de app.integracao/app.integracao_evento. Client injetado (testável)."""

    def __init__(self, client):
        self._client = client

    # -----------------------------------------------------------------
    # Gateway interno (o que os testes falsificam)
    # -----------------------------------------------------------------
    def _selecionar(self, tabela: str, filtros: dict = None, colunas: str = "*") -> list:
        q = self._client.from_(tabela).select(colunas)
        for col, val in (filtros or {}).items():
            q = q.eq(col, val)
        try:
            resp = q.execute()
        except APIError as e:
            # DDL 003 ainda não aplicado (ou schema `app` não exposto): a UI de
            # Integrações trata o vazio mostrando "aplique o DDL", sem crashar a
            # página. Qualquer outro erro sobe normalmente.
            if e.code in _ERROS_TABELA_AUSENTE:
                return []
            raise
        return resp.data or []

    def _atualizar(self, tabela: str, filtros: dict, valores: dict) -> list:
        q = self._client.from_(tabela).update(valores)
        for col, val in filtros.items():
            q = q.eq(col, val)
        resp = q.execute()
        return resp.data or []

    def _inserir(self, tabela: str, linhas) -> list:
        resp = self._client.from_(tabela).insert(linhas).execute()
        return resp.data or []

    def _selecionar_in(self, tabela: str, coluna: str, valores: list,
                       colunas: str = "*") -> list:
        """SELECT ... WHERE coluna IN (valores). Degrada p/ [] se tabela ausente."""
        if not valores:
            return []
        try:
            resp = (self._client.from_(tabela).select(colunas)
                    .in_(coluna, list(valores)).execute())
        except APIError as e:
            if e.code in _ERROS_TABELA_AUSENTE:
                return []
            raise
        return resp.data or []

    def _upsert(self, tabela: str, linhas: list) -> list:
        """Upsert por PK. Degrada p/ [] se a tabela ainda não existe (DDL ausente)."""
        if not linhas:
            return []
        try:
            resp = self._client.from_(tabela).upsert(linhas).execute()
        except APIError as e:
            if e.code in _ERROS_TABELA_AUSENTE:
                return []
            raise
        return resp.data or []

    # -----------------------------------------------------------------
    # Leitura
    # -----------------------------------------------------------------
    def ler(self, plataforma: str) -> dict:
        """Linha completa da plataforma ({} se DDL 003 não aplicado)."""
        linhas = self._selecionar(TAB_INTEGRACAO, {"id": plataforma})
        return linhas[0] if linhas else {}

    def buscar_por_state(self, state: str) -> dict:
        """
        Identifica a plataforma dona de um `state` OAuth pendente — usado pelo
        callback (?code&state) para saber de quem é o retorno. None = state
        desconhecido/expirado (fluxo reiniciado ou tentativa de CSRF).
        """
        if not state:
            return None
        linhas = self._selecionar(TAB_INTEGRACAO, {"oauth_state": state})
        return linhas[0] if linhas else None

    def listar_eventos(self, limite: int = 20) -> pd.DataFrame:
        """Últimos eventos de integração (auditoria, exibição na UI)."""
        linhas = self._selecionar(TAB_EVENTO)
        df = pd.DataFrame(linhas)
        if len(df):
            df = df.sort_values("criado_em", ascending=False).head(limite).reset_index(drop=True)
        return df

    # -----------------------------------------------------------------
    # Escrita
    # -----------------------------------------------------------------
    def salvar_chaves(self, plataforma: str, client_id: str, client_secret: str,
                      redirect_uri: str, usuario: str) -> None:
        """Grava as credenciais do app OAuth (client_secret vazio = manter o atual)."""
        valores = {
            "client_id": str(client_id).strip(),
            "redirect_uri": str(redirect_uri).strip(),
            "atualizado_em": _agora_iso(), "atualizado_por": usuario,
        }
        if str(client_secret).strip():   # campo password vazio não apaga o secret salvo
            valores["client_secret"] = str(client_secret).strip()
        self._atualizar(TAB_INTEGRACAO, {"id": plataforma}, valores)

    def salvar_config(self, plataforma: str, config: dict, usuario: str) -> None:
        """IDs de negócio (fornecedor/contato/vendedor/depósito...) — substitui o jsonb."""
        self._atualizar(TAB_INTEGRACAO, {"id": plataforma},
                        {"config": config, "atualizado_em": _agora_iso(),
                         "atualizado_por": usuario})

    def salvar_state_oauth(self, plataforma: str, state: str, usuario: str) -> None:
        """Persiste o state anti-CSRF (a sessão Streamlit morre no redirect OAuth)."""
        self._atualizar(TAB_INTEGRACAO, {"id": plataforma},
                        {"oauth_state": state, "atualizado_em": _agora_iso(),
                         "atualizado_por": usuario})

    def concluir_oauth(self, plataforma: str, access_token: str, refresh_token: str,
                       expira_em: str, usuario: str) -> None:
        """Fim do fluxo de autorização: grava tokens e zera o state pendente."""
        agora = _agora_iso()
        self._atualizar(TAB_INTEGRACAO, {"id": plataforma}, {
            "access_token": access_token, "refresh_token": refresh_token,
            "token_expira_em": expira_em, "oauth_state": None,
            "conectado_em": agora, "conectado_por": usuario,
            "atualizado_em": agora, "atualizado_por": usuario,
        })

    def atualizar_tokens(self, plataforma: str, access_token: str,
                         refresh_token: str, expira_em: str) -> None:
        """Pós-refresh silencioso (não mexe em conectado_em/por)."""
        valores = {"access_token": access_token, "token_expira_em": expira_em,
                   "atualizado_em": _agora_iso()}
        if refresh_token:   # plataformas podem rotacionar ou não o refresh token
            valores["refresh_token"] = refresh_token
        self._atualizar(TAB_INTEGRACAO, {"id": plataforma}, valores)

    def registrar_evento(self, plataforma: str, acao: str, sucesso: bool,
                         detalhe: dict = None, pedido_id: str = None,
                         usuario: str = None) -> None:
        """Auditoria. `detalhe` NUNCA deve conter tokens/secrets."""
        self._inserir(TAB_EVENTO, [{
            "plataforma": plataforma, "acao": acao, "sucesso": bool(sucesso),
            "detalhe": detalhe, "pedido_id": pedido_id, "criado_por": usuario,
        }])

    # -----------------------------------------------------------------
    # Cache SKU → id do Olist (DDL 005) — otimização da emissão da venda.
    # Ausente o DDL, tudo degrada: leitura vazia, escrita ignorada.
    # -----------------------------------------------------------------
    def ler_cache_produtos_olist(self, skus: list) -> dict:
        """{sku: olist_id} dos SKUs já resolvidos (só os presentes no cache)."""
        distintos = sorted({str(s).strip() for s in skus if str(s).strip()})
        linhas = self._selecionar_in(TAB_PRODUTO_CACHE, "sku", distintos,
                                      colunas="sku,olist_id")
        return {r["sku"]: int(r["olist_id"]) for r in linhas
                if r.get("olist_id") is not None}

    def gravar_cache_produtos_olist(self, mapa: dict) -> None:
        """Upsert de {sku: olist_id}. No-op silencioso se o DDL 005 não existe."""
        linhas = [{"sku": str(sku).strip(), "olist_id": int(oid),
                   "atualizado_em": _agora_iso()}
                  for sku, oid in (mapa or {}).items() if str(sku).strip()]
        self._upsert(TAB_PRODUTO_CACHE, linhas)
