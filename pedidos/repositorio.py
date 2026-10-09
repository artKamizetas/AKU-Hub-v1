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

import time

import pandas as pd
import streamlit as st

from pedidos import estados, revisoes
from pedidos.estados import ORIGEM_MANUAL, ORIGEM_SIMULACAO


# Nomes das tabelas no schema `app`
TAB_RODADA = "rodada_congelada"
TAB_PEDIDO = "pedido_compra"
TAB_ITEM = "pedido_compra_item"
TAB_REVISAO = "pedido_compra_revisao"   # DDL 008

_CHUNK_ITENS = 500   # lote máximo de itens por request de insert

# Leitura em lote (WHERE col IN (...)): ids por request (o filtro vai na URL —
# 100 uuids ≈ 3,7 KB) e tamanho da página (o Supabase corta em 1.000, max-rows)
_CHUNK_IN = 100
_PAGINA_LEITURA = 1000

# Retry de LEITURA em falha transitória (5xx do gateway, queda de rede)
_TENTATIVAS_LEITURA = 3
_ESPERA_RETRY_S = 0.5

# Só o que os agregados de listar_pedidos usam (sem o jsonb memoria_sugerida)
_COLS_ITEM_AGREGADO = "id,pedido_id,quantidade_sugerida,quantidade_final,custo_unit"

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
    """Tentativa de editar itens de pedido fora de RASCUNHO / EM_ALTERACAO."""


class ItemJaExiste(Exception):
    """O SKU já é um item deste pedido (unique (pedido_id, sku))."""


class ItemNaoRemovivel(Exception):
    """Só item incluído manualmente pode ser removido — o da simulação zera-se."""


class MigracaoPendente(Exception):
    """A coluna/trava que a operação exige ainda não existe no banco (DDL não aplicado)."""


def _e_violacao_unique(exc: Exception) -> bool:
    """23505 = unique_violation do Postgres (via APIError do postgrest)."""
    return getattr(exc, "code", "") == "23505" or "23505" in str(exc)


def _e_coluna_ausente(exc: Exception) -> bool:
    """PGRST204 = coluna fora do schema cache do PostgREST (DDL não aplicado)."""
    return getattr(exc, "code", "") == "PGRST204" or "PGRST204" in str(exc)


def _e_tabela_ausente(exc: Exception) -> bool:
    """PGRST205 / 42P01 = tabela fora do schema (DDL não aplicado)."""
    codigo = str(getattr(exc, "code", "") or "")
    return codigo in ("PGRST205", "42P01") or "PGRST205" in str(exc) or "42P01" in str(exc)


def _e_check_violado(exc: Exception) -> bool:
    """23514 = check_violation (ex.: status novo antes do DDL que o libera)."""
    return str(getattr(exc, "code", "") or "") == "23514" or "23514" in str(exc)


_MSG_DDL_008 = ("Alterar ou cancelar pedido emitido exige a migração "
                "docs/sql/008_app_alteracao_pos_emissao.sql — rode "
                "`python scripts/migrar.py aplicar`.")


def _e_falha_transitoria(exc: Exception) -> bool:
    """
    5xx HTTP do gateway na frente do PostgREST (502/503/504 do Cloudflare) ou
    queda de rede. Erro de verdade do Postgres/PostgREST tem `code` SQLSTATE ou
    PGRST (ex: '23505', 'PGRST204'), nunca um 5xx — esses sobem na hora.
    """
    import httpx

    if isinstance(exc, httpx.TransportError):
        return True
    try:
        return 500 <= int(getattr(exc, "code", None)) <= 599
    except (TypeError, ValueError):
        return False


def _ler_com_retry(montar_query) -> list:
    """
    Executa uma LEITURA com retry em falha transitória. `montar_query` devolve a
    query pronta e é chamado a cada tentativa (não se reusa o builder).

    Só leitura: SELECT é idempotente. Escrita NÃO passa por aqui — um 502 pode
    chegar depois de o banco já ter gravado, e repetir duplicaria o insert ou
    faria o compare-and-swap parecer corrida perdida.
    """
    for tentativa in range(_TENTATIVAS_LEITURA):
        try:
            return montar_query().execute().data or []
        except Exception as exc:
            if (not _e_falha_transitoria(exc)
                    or tentativa == _TENTATIVAS_LEITURA - 1):
                raise
            time.sleep(_ESPERA_RETRY_S * (tentativa + 1))
    return []


# Colunas que a inclusão manual aceita de quem chama — o resto (pedido_id,
# origem, quantidade_sugerida, memória, auditoria) é imposto aqui
_COLS_ITEM_MANUAL = ("sku", "id_produto_bling", "produto", "tamanho", "categoria",
                     "quantidade_final", "custo_unit")


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
        def montar():
            q = self._client.from_(tabela).select(colunas)
            for col, val in (filtros or {}).items():
                q = q.eq(col, val)
            return q
        return _ler_com_retry(montar)

    def _selecionar_in(self, tabela: str, coluna: str, valores: list,
                       colunas: str = "*") -> list:
        """
        SELECT ... WHERE coluna IN (valores), em UMA leitura por lote de ids em
        vez de uma por id. Pagina com `.order("id")` porque o Supabase corta a
        resposta em 1.000 linhas (max-rows) SEM avisar — `colunas` precisa
        incluir `id`.
        """
        linhas = []
        valores = list(valores)
        for i in range(0, len(valores), _CHUNK_IN):
            lote = valores[i:i + _CHUNK_IN]
            inicio = 0
            while True:
                pagina = _ler_com_retry(
                    lambda: self._client.from_(tabela).select(colunas)
                    .in_(coluna, lote).order("id")
                    .range(inicio, inicio + _PAGINA_LEITURA - 1))
                linhas.extend(pagina)
                if len(pagina) < _PAGINA_LEITURA:
                    break
                inicio += _PAGINA_LEITURA
        return linhas

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

    def obter_resultado_skus(self, rodada_id: str) -> list:
        """
        Só o resultado por SKU do snapshot (sem o config_snapshot) — a rede
        inteira, inclusive os SKUs de sugestão 0 que não viraram item.
        """
        linhas = self._selecionar(TAB_RODADA, {"id": rodada_id},
                                  colunas="resultado_skus")
        return (linhas[0].get("resultado_skus") or []) if linhas else []

    def listar_pedidos(self, rodada_id: str) -> pd.DataFrame:
        """
        Pedidos da rodada + agregados dos itens (n_itens, qtd_sugerida,
        qtd_final, investimento_final = Σ final × custo).
        """
        pedidos = self._selecionar(TAB_PEDIDO, {"rodada_id": rodada_id})
        df = pd.DataFrame(pedidos)
        if len(df) == 0:
            return df

        # Itens da rodada INTEIRA de uma vez. Era uma leitura por pedido (70
        # requests em série numa rodada de 69 pedidos, a cada rerun da tela):
        # lento e, sem retry, um único 502 do gateway derrubava a página.
        itens = pd.DataFrame(
            self._selecionar_in(TAB_ITEM, "pedido_id", list(df["id"]),
                                colunas=_COLS_ITEM_AGREGADO),
            columns=_COLS_ITEM_AGREGADO.split(","))
        for col in ("quantidade_sugerida", "quantidade_final", "custo_unit"):
            itens[col] = pd.to_numeric(itens[col], errors="coerce").fillna(0)
        itens["investimento_final"] = itens["quantidade_final"] * itens["custo_unit"]
        agregados = itens.groupby("pedido_id").agg(
            n_itens=("id", "size"),
            qtd_sugerida=("quantidade_sugerida", "sum"),
            qtd_final=("quantidade_final", "sum"),
            investimento_final=("investimento_final", "sum"),
        )

        # Pedido sem item não aparece no groupby → zera (como antes)
        df = df.merge(agregados, left_on="id", right_index=True, how="left")
        for col in ("n_itens", "qtd_sugerida", "qtd_final"):
            df[col] = df[col].fillna(0).astype(int)
        df["investimento_final"] = df["investimento_final"].fillna(0.0).astype(float)
        return df.sort_values(["colegio", "super_categoria"]).reset_index(drop=True)

    def listar_itens(self, pedido_id: str) -> pd.DataFrame:
        itens = self._selecionar(TAB_ITEM, {"pedido_id": pedido_id})
        df = pd.DataFrame(itens)
        if len(df):
            df["custo_unit"] = pd.to_numeric(df["custo_unit"], errors="coerce").fillna(0)
            # Antes do DDL 007 a coluna não existe — todo item era da simulação
            if "origem" not in df.columns:
                df["origem"] = ORIGEM_SIMULACAO
            df["origem"] = df["origem"].fillna(ORIGEM_SIMULACAO)
            df = df.sort_values("sku").reset_index(drop=True)
        return df

    def obter_pedido(self, pedido_id: str) -> dict:
        """Linha única do pedido ({} se não existe) — usado pelo emissor."""
        linhas = self._selecionar(TAB_PEDIDO, {"id": pedido_id})
        return linhas[0] if linhas else {}

    def obter_rodada_leve(self, rodada_id: str) -> dict:
        """Rodada SEM os jsonb (payload de emissão só precisa das datas/metadados)."""
        linhas = self._selecionar(TAB_RODADA, {"id": rodada_id},
                                  colunas=_COLS_RODADA_LEVE)
        return linhas[0] if linhas else {}

    # -----------------------------------------------------------------
    # Escrita operacional
    # -----------------------------------------------------------------
    def atualizar_quantidades(self, pedido_id: str, alteracoes: list, usuario: str) -> int:
        """
        Aplica edições de quantidade_final (alteracoes = [{"id", "quantidade_final"}],
        já diffadas pela UI — só linhas alteradas). Guarda app-level aqui +
        trigger no banco como trava real. Retorna nº de itens atualizados.
        """
        self._exigir_editavel(pedido_id)

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

    def _exigir_editavel(self, pedido_id: str) -> str:
        """Barra a edição fora de RASCUNHO / EM_ALTERACAO. Devolve o status."""
        pedido = self._selecionar(TAB_PEDIDO, {"id": pedido_id}, colunas="id,status")
        if not pedido:
            raise PedidoNaoEditavel(f"Pedido {pedido_id} não encontrado.")
        status = pedido[0]["status"]
        if not estados.editavel(status):
            raise PedidoNaoEditavel(
                f"Pedido está {status} — "
                + ("abra uma alteração para editar." if estados.alteravel(status)
                   else "reabra o rascunho para editar."))
        return status

    def adicionar_itens(self, pedido_id: str, itens: list, usuario: str) -> int:
        """
        Inclui no RASCUNHO itens que a simulação não trouxe (itens = dicts de
        catalogo.montar_itens_manuais). Sempre origem MANUAL, quantidade_sugerida
        0 e memória vazia — impostos AQUI, não confiados a quem chama: é o que
        mantém a auditoria "o motor sugeriu × o gestor incluiu" honesta.

        Guardas app-level (rascunho, SKU repetido) dão a mensagem legível; a
        trava real é o banco: trigger de RASCUNHO no INSERT (DDL 007) e
        unique (pedido_id, sku) → 23505 → ItemJaExiste. Um único INSERT em
        lote: ou entra a grade inteira do produto, ou nada.
        """
        self._exigir_editavel(pedido_id)
        if not itens:
            return 0

        skus = [str(i["sku"]) for i in itens]
        existentes = {i["sku"] for i in self._selecionar(
            TAB_ITEM, {"pedido_id": pedido_id}, colunas="sku")}
        repetidos = sorted({s for s in skus if s in existentes or skus.count(s) > 1})
        if repetidos:
            raise ItemJaExiste(
                f"Já no pedido: {', '.join(repetidos[:8])}"
                + ("…" if len(repetidos) > 8 else "")
                + " — ajuste a quantidade na tabela em vez de incluir de novo.")

        agora = _agora_iso()
        payload = []
        for item in itens:
            if not str(item.get("id_produto_bling") or "").strip():
                raise ValueError(f"Item {item['sku']} sem id de produto do Bling.")
            if int(item["quantidade_final"]) <= 0:
                raise ValueError(f"Item {item['sku']} com quantidade zero — nada a incluir.")
            linha = {c: item[c] for c in _COLS_ITEM_MANUAL}
            linha.update({
                "pedido_id": pedido_id,
                "quantidade_final": int(item["quantidade_final"]),
                "quantidade_sugerida": 0,
                "memoria_sugerida": {},
                "origem": ORIGEM_MANUAL,
                "adicionado_por": usuario, "adicionado_em": agora,
                "atualizado_por": usuario, "atualizado_em": agora,
            })
            payload.append(linha)

        try:
            inseridos = self._inserir(TAB_ITEM, payload)
        except Exception as exc:
            if _e_violacao_unique(exc):
                raise ItemJaExiste(
                    "Um dos SKUs já está neste pedido (incluído em outra sessão) "
                    "— recarregue e ajuste a quantidade.") from exc
            if _e_coluna_ausente(exc):
                raise MigracaoPendente(
                    "A inclusão manual exige a migração docs/sql/007_app_item_manual.sql "
                    "— rode `python scripts/migrar.py aplicar`.") from exc
            raise

        self._atualizar(TAB_PEDIDO, {"id": pedido_id},
                        {"atualizado_em": agora, "atualizado_por": usuario})
        return len(inseridos)

    def remover_itens_manuais(self, pedido_id: str, item_ids: list, usuario: str) -> int:
        """
        Remove do RASCUNHO itens incluídos à mão. Item da simulação NÃO se
        remove (ItemNaoRemovivel): zera-se a quantidade_final, e a linha fica
        como prova do que o motor sugeriu. O filtro `origem` no DELETE repete a
        guarda no próprio comando; o trigger do DDL 007 é a trava real.

        Numa ALTERAÇÃO pós-emissão vale a mesma ideia para o item manual que já
        foi aos ERPs: a linha é o registro do que foi emitido — zera-se. Só sai
        o que foi incluído durante a própria alteração.
        """
        status = self._exigir_editavel(pedido_id)
        do_pedido = {i["id"]: i for i in self._selecionar(TAB_ITEM, {"pedido_id": pedido_id})}
        alvo = [do_pedido[i] for i in item_ids if i in do_pedido]
        da_simulacao = [i["sku"] for i in alvo
                        if i.get("origem", ORIGEM_SIMULACAO) != ORIGEM_MANUAL]
        if da_simulacao:
            raise ItemNaoRemovivel(
                f"{', '.join(da_simulacao[:8])}: item da simulação não se remove "
                "— zere a quantidade final.")
        if status == estados.EM_ALTERACAO:
            emitidos = set(revisoes.quantidades(self.obter_linha_de_base(pedido_id)))
            ja_emitidos = [i["sku"] for i in alvo if i["id"] in emitidos]
            if ja_emitidos:
                raise ItemNaoRemovivel(
                    f"{', '.join(ja_emitidos[:8])}: item já emitido nos ERPs não se "
                    "remove — zere a quantidade e envie a alteração.")

        for item in alvo:
            self._deletar(TAB_ITEM, {"id": item["id"], "pedido_id": pedido_id,
                                     "origem": ORIGEM_MANUAL})
        if alvo:
            self._atualizar(TAB_PEDIDO, {"id": pedido_id},
                            {"atualizado_em": _agora_iso(), "atualizado_por": usuario})
        return len(alvo)

    def registrar_ids_emissao(self, pedido_id: str, campos: dict, usuario: str) -> None:
        """
        Grava os identificadores devolvidos pelo ERP na emissão
        (bling_id/bling_numero ou olist_id/olist_numero). Whitelist de
        colunas — nada além dos ids de emissão passa por aqui.
        """
        permitidos = {"bling_id", "bling_numero", "olist_id", "olist_numero"}
        valores = {k: str(v) for k, v in campos.items() if k in permitidos}
        if not valores:
            return
        valores.update({"atualizado_em": _agora_iso(), "atualizado_por": usuario})
        self._atualizar(TAB_PEDIDO, {"id": pedido_id}, valores)

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

    # -----------------------------------------------------------------
    # Revisões (DDL 008) — o que foi aos ERPs, versão a versão
    # -----------------------------------------------------------------
    def listar_revisoes(self, pedido_id: str) -> list:
        """Revisões do pedido em ordem de `numero` (regras em pedidos/revisoes.py)."""
        try:
            linhas = self._selecionar(TAB_REVISAO, {"pedido_id": pedido_id})
        except Exception as exc:
            if _e_tabela_ausente(exc):
                raise MigracaoPendente(_MSG_DDL_008) from exc
            raise
        return sorted(linhas, key=lambda r: int(r.get("numero") or 0))

    def obter_linha_de_base(self, pedido_id: str) -> dict:
        """O que está nos ERPs agora: última revisão concluída ({} se não há)."""
        return revisoes.linha_de_base(self.listar_revisoes(pedido_id))

    def registrar_revisao(self, pedido_id: str, tipo: str, usuario: str,
                          motivo: str = "", erps_ok=(), concluida: bool = False) -> dict:
        """
        Grava uma revisão com o RETRATO ATUAL dos itens do pedido. `erps_ok` já
        carimba os ERPs que confirmaram (a emissão nasce com o Bling confirmado);
        `concluida` fecha a operação. Unique (pedido_id, numero) → corrida entre
        duas sessões vira 23505 e sobe (a 2ª não grava por cima da 1ª).
        """
        anteriores = self.listar_revisoes(pedido_id)
        agora = _agora_iso()
        linha = {
            "pedido_id": pedido_id,
            "numero": (int(anteriores[-1]["numero"]) + 1) if anteriores else 1,
            "tipo": tipo, "motivo": str(motivo or "").strip(),
            "itens": revisoes.retrato_itens(self._selecionar(TAB_ITEM, {"pedido_id": pedido_id})),
            "criado_em": agora, "criado_por": usuario,
            "bling_ok_em": agora if "bling" in erps_ok else None,
            "olist_ok_em": agora if "olist" in erps_ok else None,
            "concluida_em": agora if concluida else None,
        }
        inseridas = self._inserir(TAB_REVISAO, linha)
        return inseridas[0] if inseridas else linha

    def confirmar_revisao(self, revisao_id: str, erp: str = None,
                          concluir: bool = False) -> None:
        """Carimba o ERP que confirmou a revisão e/ou fecha a operação."""
        agora = _agora_iso()
        valores = {}
        if erp in revisoes.ERPS:
            valores[f"{erp}_ok_em"] = agora
        if concluir:
            valores["concluida_em"] = agora
        if valores:
            self._atualizar(TAB_REVISAO, {"id": revisao_id}, valores)

    def remover_revisao(self, revisao_id: str) -> None:
        """
        Apaga uma revisão que NÃO chegou a nenhum ERP (envio abortado antes de
        tocar neles) — senão ela ficaria como "pendente" para sempre e a
        próxima tentativa nasceria com um buraco no histórico. Revisão que
        algum ERP confirmou NÃO sai: ela é o registro do envio parcial.
        """
        linhas = self._selecionar(TAB_REVISAO, {"id": revisao_id})
        if not linhas or any(linhas[0].get(f"{erp}_ok_em") for erp in revisoes.ERPS):
            return
        self._deletar(TAB_REVISAO, {"id": revisao_id})

    # -----------------------------------------------------------------
    # Alteração pós-emissão
    # -----------------------------------------------------------------
    def abrir_alteracao(self, pedido_id: str, usuario: str) -> bool:
        """
        Estado emitido → EM_ALTERACAO (CAS). Quem decide se PODE é o status
        nativo dos ERPs — isso o emissor checa antes de chamar aqui.

        Pedido emitido sem revisão (emitido antes do DDL 008, ou cujo registro
        da revisão 1 falhou) ganha a nº 1 agora: fora de RASCUNHO/EM_ALTERACAO
        os itens estão travados desde a emissão, então SÃO o que foi emitido.
        Sem linha de base não haveria contra o que comparar nem para onde voltar.
        """
        pedido = self.obter_pedido(pedido_id)
        status = pedido.get("status", "")
        if not estados.alteravel(status):
            raise TransicaoInvalida(
                f"Pedido está {status or 'inexistente'} — só pedido emitido abre alteração.")
        if not self.obter_linha_de_base(pedido_id):
            self.registrar_revisao(
                pedido_id, estados.REVISAO_EMISSAO, usuario,
                motivo="Linha de base reconstruída ao abrir a 1ª alteração",
                erps_ok=("bling", "olist") if estados.estado_emitido(pedido) == estados.EMITIDO
                else ("bling",),
                concluida=True)
        try:
            return self.transicionar_pedido(pedido_id, status, estados.EM_ALTERACAO, usuario)
        except Exception as exc:
            if _e_check_violado(exc):
                raise MigracaoPendente(_MSG_DDL_008) from exc
            raise

    def descartar_alteracao(self, pedido_id: str, usuario: str) -> bool:
        """
        EM_ALTERACAO → estado emitido, devolvendo os itens à linha de base:
        quantidade de volta ao que está nos ERPs, e o que foi incluído durante
        a alteração sai. Os itens voltam ANTES da transição (fora de
        EM_ALTERACAO o trigger os trava) — se falhar no meio o pedido continua
        em alteração e o descarte pode ser repetido.

        Recusa com envio parcial: um ERP já recebeu a versão nova, e descartar
        só restauraria o nosso lado.
        """
        pedido = self.obter_pedido(pedido_id)
        if pedido.get("status") != estados.EM_ALTERACAO:
            raise TransicaoInvalida("Pedido não está em alteração — nada a descartar.")
        historico = self.listar_revisoes(pedido_id)
        if revisoes.envio_parcial(historico):
            raise TransicaoInvalida(
                "Um dos ERPs já recebeu esta alteração — envie de novo para "
                "alinhar os dois antes de descartar.")
        base = revisoes.linha_de_base(historico)
        if not base:
            raise TransicaoInvalida("Pedido sem linha de base — não há para onde voltar.")

        na_base = revisoes.quantidades(base)
        agora = _agora_iso()
        for item in self._selecionar(TAB_ITEM, {"pedido_id": pedido_id}):
            if item["id"] not in na_base:
                self._deletar(TAB_ITEM, {"id": item["id"], "pedido_id": pedido_id,
                                         "origem": ORIGEM_MANUAL})
            elif int(item["quantidade_final"]) != na_base[item["id"]]:
                self._atualizar(
                    TAB_ITEM, {"id": item["id"], "pedido_id": pedido_id},
                    {"quantidade_final": na_base[item["id"]],
                     "atualizado_em": agora, "atualizado_por": usuario})
        return self.transicionar_pedido(
            pedido_id, estados.EM_ALTERACAO, estados.estado_emitido(pedido), usuario)

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
