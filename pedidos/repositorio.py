"""
repositorio.py — ÚNICA porta de escrita/leitura do schema `app` do Supabase.

Primeiro (e único) caminho de escrita do dashboard: pedidos de compra e
rodadas congeladas. Nenhum outro módulo/página deve tocar o schema `app`
diretamente — sempre via RepositorioPedidos.

Consistência sem transação (PostgREST não dá transação multi-request):
  - ordem de inserts + estado CONGELANDO + unique parcial como guarda
    (ver congelar_rodada);
  - transições de estado via compare-and-swap (UPDATE ... WHERE status=de);
  - trava real de edição de itens é o trigger no banco (Streamlit reroda o
    script a cada clique — a UI não é confiável como trava).

Client PostgREST próprio (_conn_app), espelho do loader._conn_supabase mas
apontando para o schema `app` (st.secrets["supabase"]["schema_app"], default
"app"). Requer o schema exposto na Data API (Settings → API → Exposed
schemas) e o DDL de docs/sql/001_app_pedidos.sql aplicado.
"""

import pandas as pd
import streamlit as st

from pedidos import estados


# Nomes das tabelas no schema `app`
TAB_RODADA = "rodada_congelada"
TAB_PEDIDO = "pedido_compra"
TAB_ITEM = "pedido_compra_item"

_CHUNK_ITENS = 500   # lote máximo de itens por request de insert

# Colunas "leves" da rodada (sem os jsonb — payload pequeno p/ listagem)
_COLS_RODADA_LEVE = (
    "id,mes_disparo,ano_disparo,data_disparo,data_chegada,data_chegada_seguinte,"
    "rodada_numero,janela_label,data_referencia,congelada_em,congelada_por,"
    "ativo_crescimento,status,observacao"
)


class RodadaJaCongelada(Exception):
    """Já existe congelamento vivo (não-cancelado) para esta rodada mês×ano."""


class TransicaoInvalida(Exception):
    """Transição de estado não permitida pela máquina de estados."""


class PedidoNaoEditavel(Exception):
    """Tentativa de editar itens de pedido que não está em RASCUNHO."""


def _e_violacao_unique(exc: Exception) -> bool:
    """23505 = unique_violation do Postgres (via APIError do postgrest)."""
    return getattr(exc, "code", "") == "23505" or "23505" in str(exc)


def _agora_iso() -> str:
    return pd.Timestamp.now(tz="UTC").isoformat()


@st.cache_resource
def _conn_app():
    """
    Cliente PostgREST do schema `app` (cacheado por sessão). Espelho de
    loader._conn_supabase — mesmas credenciais (service_key ignora RLS),
    HTTP/1.1 forçado (http2 tem race em uso concorrente por threads).
    """
    import httpx
    from postgrest import SyncPostgrestClient

    cfg = st.secrets["supabase"]
    key = cfg["service_key"]
    schema = cfg.get("schema_app", "app")
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    http_client = httpx.Client(
        http2=False,
        headers=headers,
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    )
    return SyncPostgrestClient(
        f"{cfg['url'].rstrip('/')}/rest/v1",
        schema=schema,
        headers=headers,
        http_client=http_client,
    )


def obter_repositorio() -> "RepositorioPedidos":
    """Fábrica usada pelas páginas."""
    return RepositorioPedidos(_conn_app())


class RepositorioPedidos:
    """Porta de acesso ao schema `app`. Client injetado — testável com fake."""

    def __init__(self, client):
        self._client = client

    # -----------------------------------------------------------------
    # Gateway interno (o que os testes falsificam)
    # -----------------------------------------------------------------
    def _inserir(self, tabela: str, linhas) -> list:
        resp = self._client.from_(tabela).insert(linhas).execute()
        return resp.data or []

    def _atualizar(self, tabela: str, filtros: dict, valores: dict) -> list:
        q = self._client.from_(tabela).update(valores)
        for col, val in filtros.items():
            q = q.eq(col, val)
        resp = q.execute()
        return resp.data or []

    def _selecionar(self, tabela: str, filtros: dict = None, colunas: str = "*") -> list:
        q = self._client.from_(tabela).select(colunas)
        for col, val in (filtros or {}).items():
            q = q.eq(col, val)
        resp = q.execute()
        return resp.data or []

    def _deletar(self, tabela: str, filtros: dict) -> None:
        q = self._client.from_(tabela).delete()
        for col, val in filtros.items():
            q = q.eq(col, val)
        q.execute()

    # -----------------------------------------------------------------
    # Congelamento
    # -----------------------------------------------------------------
    def congelar_rodada(self, snapshot: dict, grupos: list) -> dict:
        """
        Congela a rodada e cria os pedidos rascunho, atômico-o-suficiente:

          1. INSERT rodada (CONGELANDO) — unique parcial (ano×mês vivo) é a
             trava anti duplo-clique: 23505 → RodadaJaCongelada
          2. INSERT pedidos em lote
          3. INSERT itens em lotes de _CHUNK_ITENS
          4. verificação de contagens (insert devolve representação)
          5. UPDATE rodada → ABERTA via CAS (commit lógico)

        Falha em 2-4: DELETE compensatório da rodada (cascade limpa filhos) e
        re-raise. Se o próprio delete falhar, sobra uma rodada CONGELANDO —
        a UI a exibe como incompleta com botão de limpeza
        (limpar_congelamento_abortado). Nenhum estado intermediário é jamais
        tratado como rodada válida.
        """
        if not grupos:
            raise ValueError("Nenhum grupo de pedido para congelar (nada com sugestão > 0).")

        criadas = None
        try:
            criadas = self._inserir(
                TAB_RODADA, [{**snapshot, "status": estados.RODADA_CONGELANDO}])
        except Exception as exc:
            if _e_violacao_unique(exc):
                raise RodadaJaCongelada(
                    f"A rodada {snapshot.get('mes_disparo'):02d}/{snapshot.get('ano_disparo')} "
                    "já tem um congelamento ativo. Cancele-o na página Pedidos de "
                    "Compra antes de congelar de novo."
                ) from exc
            raise
        rodada = criadas[0]
        rodada_id = rodada["id"]

        try:
            # 2. Pedidos em lote
            payload_pedidos = [{
                "rodada_id": rodada_id,
                "colegio": g["colegio"],
                "super_categoria": g["super_categoria"],
                "titulo": g["titulo"],
                "criado_por": g["criado_por"],
            } for g in grupos]
            pedidos_criados = self._inserir(TAB_PEDIDO, payload_pedidos)
            if len(pedidos_criados) != len(grupos):
                raise RuntimeError(
                    f"Criação parcial de pedidos: {len(pedidos_criados)}/{len(grupos)}.")

            # 3. Itens em lotes, com o pedido_id de cada grupo
            id_por_grupo = {(p["colegio"], p["super_categoria"]): p["id"]
                            for p in pedidos_criados}
            payload_itens = []
            for g in grupos:
                pid = id_por_grupo[(g["colegio"], g["super_categoria"])]
                for item in g["itens"]:
                    payload_itens.append({**item, "pedido_id": pid})

            inseridos = 0
            for i in range(0, len(payload_itens), _CHUNK_ITENS):
                lote = payload_itens[i:i + _CHUNK_ITENS]
                inseridos += len(self._inserir(TAB_ITEM, lote))

            # 4. Verificação
            if inseridos != len(payload_itens):
                raise RuntimeError(
                    f"Criação parcial de itens: {inseridos}/{len(payload_itens)}.")
        except Exception:
            # Compensação: remove a rodada (cascade limpa pedidos/itens).
            # Se falhar, fica CONGELANDO → limpável pela UI.
            try:
                self._deletar(TAB_RODADA, {"id": rodada_id})
            except Exception:
                pass
            raise

        # 5. Commit lógico (CAS)
        confirmadas = self._atualizar(
            TAB_RODADA,
            {"id": rodada_id, "status": estados.RODADA_CONGELANDO},
            {"status": estados.RODADA_ABERTA},
        )
        if not confirmadas:
            raise RuntimeError(
                "Falha ao confirmar o congelamento (rodada não estava mais em "
                "CONGELANDO) — verifique a página Pedidos de Compra.")

        return {**rodada, "status": estados.RODADA_ABERTA,
                "n_pedidos": len(grupos), "n_itens": len(payload_itens)}

    def limpar_congelamento_abortado(self, rodada_id: str) -> None:
        """Remove uma rodada que ficou presa em CONGELANDO (falha no meio)."""
        linhas = self._selecionar(TAB_RODADA, {"id": rodada_id}, colunas="id,status")
        if not linhas:
            return
        if linhas[0]["status"] != estados.RODADA_CONGELANDO:
            raise TransicaoInvalida(
                f"Rodada {rodada_id} está {linhas[0]['status']}, não CONGELANDO — "
                "só congelamentos abortados podem ser limpos.")
        self._deletar(TAB_RODADA, {"id": rodada_id})

    # -----------------------------------------------------------------
    # Leitura
    # -----------------------------------------------------------------
    def listar_rodadas(self) -> pd.DataFrame:
        """Rodadas congeladas SEM os jsonb (payload leve p/ listagem)."""
        linhas = self._selecionar(TAB_RODADA, colunas=_COLS_RODADA_LEVE)
        df = pd.DataFrame(linhas)
        if len(df):
            df = df.sort_values("congelada_em", ascending=False).reset_index(drop=True)
        return df

    def obter_rodada(self, rodada_id: str) -> dict:
        """Rodada completa (com config_snapshot e resultado_skus) p/ conferência."""
        linhas = self._selecionar(TAB_RODADA, {"id": rodada_id})
        return linhas[0] if linhas else {}

    def listar_pedidos(self, rodada_id: str) -> pd.DataFrame:
        """
        Pedidos da rodada + agregados dos itens (n_itens, qtd_sugerida,
        qtd_final, investimento_final = Σ final × custo).
        """
        pedidos = self._selecionar(TAB_PEDIDO, {"rodada_id": rodada_id})
        df = pd.DataFrame(pedidos)
        if len(df) == 0:
            return df

        agregados = []
        for pid in df["id"]:
            itens = self.listar_itens(pid)
            if len(itens):
                agregados.append({
                    "id": pid,
                    "n_itens": len(itens),
                    "qtd_sugerida": int(itens["quantidade_sugerida"].sum()),
                    "qtd_final": int(itens["quantidade_final"].sum()),
                    "investimento_final": float(
                        (itens["quantidade_final"] * itens["custo_unit"]).sum()),
                })
            else:
                agregados.append({"id": pid, "n_itens": 0, "qtd_sugerida": 0,
                                  "qtd_final": 0, "investimento_final": 0.0})
        df = df.merge(pd.DataFrame(agregados), on="id", how="left")
        return df.sort_values(["colegio", "super_categoria"]).reset_index(drop=True)

    def listar_itens(self, pedido_id: str) -> pd.DataFrame:
        itens = self._selecionar(TAB_ITEM, {"pedido_id": pedido_id})
        df = pd.DataFrame(itens)
        if len(df):
            df["custo_unit"] = pd.to_numeric(df["custo_unit"], errors="coerce").fillna(0)
            df = df.sort_values("sku").reset_index(drop=True)
        return df

    # -----------------------------------------------------------------
    # Escrita operacional
    # -----------------------------------------------------------------
    def atualizar_quantidades(self, pedido_id: str, alteracoes: list, usuario: str) -> int:
        """
        Aplica edições de quantidade_final (alteracoes = [{"id", "quantidade_final"}],
        já diffadas pela UI — só linhas alteradas). Guarda app-level aqui +
        trigger no banco como trava real. Retorna nº de itens atualizados.
        """
        pedido = self._selecionar(TAB_PEDIDO, {"id": pedido_id}, colunas="id,status")
        if not pedido:
            raise PedidoNaoEditavel(f"Pedido {pedido_id} não encontrado.")
        if not estados.editavel(pedido[0]["status"]):
            raise PedidoNaoEditavel(
                f"Pedido está {pedido[0]['status']} — reabra o rascunho para editar.")

        agora = _agora_iso()
        atualizados = 0
        for alt in alteracoes:
            linhas = self._atualizar(
                TAB_ITEM,
                {"id": alt["id"], "pedido_id": pedido_id},   # pedido_id: não vaza p/ outro pedido
                {"quantidade_final": int(alt["quantidade_final"]),
                 "atualizado_em": agora, "atualizado_por": usuario},
            )
            atualizados += len(linhas)

        if atualizados:
            self._atualizar(TAB_PEDIDO, {"id": pedido_id},
                            {"atualizado_em": agora, "atualizado_por": usuario})
        return atualizados

    def transicionar_pedido(self, pedido_id: str, de: str, para: str, usuario: str) -> bool:
        """
        Transição de estado via compare-and-swap: UPDATE ... WHERE status=de.
        Retorna False se a corrida foi perdida (outra sessão mudou o estado) —
        a UI mostra aviso e recarrega. TransicaoInvalida se a máquina não permite.
        """
        if not estados.pode_transicionar(de, para):
            raise TransicaoInvalida(f"Transição {de} → {para} não é permitida.")

        agora = _agora_iso()
        valores = {"status": para, "atualizado_em": agora, "atualizado_por": usuario}
        if para == estados.PRONTO:
            valores.update({"pronto_em": agora, "pronto_por": usuario})
        elif para == estados.RASCUNHO:   # reabrir limpa o carimbo de pronto
            valores.update({"pronto_em": None, "pronto_por": None})

        linhas = self._atualizar(TAB_PEDIDO, {"id": pedido_id, "status": de}, valores)
        return len(linhas) > 0

    def cancelar_rodada(self, rodada_id: str, usuario: str) -> None:
        """
        Cancela a rodada congelada e seus pedidos — só se nenhum pedido passou
        de RASCUNHO (pedidos PRONTO precisam ser reabertos/cancelados antes).
        A linha CANCELADA fica para auditoria e libera novo congelamento.
        """
        pedidos = self._selecionar(TAB_PEDIDO, {"rodada_id": rodada_id},
                                   colunas="id,status")
        travados = [p for p in pedidos
                    if p["status"] not in (estados.RASCUNHO, estados.CANCELADO)]
        if travados:
            raise TransicaoInvalida(
                f"{len(travados)} pedido(s) já saíram de RASCUNHO — reabra ou "
                "cancele cada um antes de cancelar a rodada.")

        agora = _agora_iso()
        self._atualizar(TAB_PEDIDO,
                        {"rodada_id": rodada_id, "status": estados.RASCUNHO},
                        {"status": estados.CANCELADO,
                         "atualizado_em": agora, "atualizado_por": usuario})
        self._atualizar(TAB_RODADA, {"id": rodada_id},
                        {"status": estados.RODADA_CANCELADA})
