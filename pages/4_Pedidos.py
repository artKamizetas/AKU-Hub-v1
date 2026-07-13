"""
Página: Pedidos de Compra (Admin Only)

Rodadas congeladas do Simulador de Produção → pedidos de compra em rascunho
por Colégio × Super Categoria → revisão/edição de quantidades → PRONTO.
A emissão no Bling é fase futura; até lá, o CSV do pedido (com as observações
internas padronizadas no cabeçalho) é a ponte manual para digitar no Bling.

Três níveis: rodadas congeladas → pedidos da rodada → itens do pedido.
Leituras do schema `app` sem st.cache_data (tabelas pequenas; cache
confundiria o pós-escrita).
"""

import streamlit as st
from auth import exigir_login
exigir_login()

import pandas as pd

from pedidos import builder, estados
from pedidos.repositorio import (
    obter_repositorio, TransicaoInvalida, PedidoNaoEditavel,
)

# Gate explícito de role (mesmo padrão de 5_Configuracoes.py)
username = st.session_state.get("username", "")
auth_config = dict(st.secrets.get("auth_config", {}))
usernames = auth_config.get("credentials", {}).get("usernames", {})
role = usernames.get(username, {}).get("role", "")

if role != "admin":
    st.error("⛔ Acesso negado. Apenas administradores podem acessar esta página.")
    st.stop()


BADGES_RODADA = {
    estados.RODADA_CONGELANDO: "⚠️ Incompleta",
    estados.RODADA_ABERTA: "📂 Aberta",
    estados.RODADA_CANCELADA: "🚫 Cancelada",
}


def _flash(nivel: str, texto: str):
    """Mensagem que sobrevive ao st.rerun() pós-ação."""
    st.session_state["pc_pagina_msg"] = (nivel, texto)


def _fmt_brl(x: float) -> str:
    return f"R$ {x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


st.title("🧾 Pedidos de Compra")
st.caption(
    "Rodadas congeladas do Simulador de Produção, divididas em pedidos por "
    "**Colégio × Super Categoria**. Edite as quantidades no rascunho e marque "
    "como Pronto; a emissão automática no Bling é a próxima fase."
)

_msg = st.session_state.pop("pc_pagina_msg", None)
if _msg:
    getattr(st, _msg[0])(_msg[1])

repo = obter_repositorio()

# =================================================================
# NÍVEL 1 — Rodadas congeladas
# =================================================================
try:
    rodadas = repo.listar_rodadas()
except Exception as exc:
    st.error(
        f"Não foi possível ler o schema `app` do Supabase: {exc}\n\n"
        "Verifique se o DDL (docs/sql/001_app_pedidos.sql) foi aplicado e o "
        "schema `app` está exposto na Data API."
    )
    st.stop()

if len(rodadas) == 0:
    st.info("Nenhuma rodada congelada ainda. Congele uma rodada no Simulador de Produção.")
    st.page_link("pages/3_Fabrica.py", label="Abrir Simulador de Produção", icon="🏭")
    st.stop()

with st.container(border=True):
    st.subheader("Rodadas congeladas")

    view_rodadas = rodadas.copy()
    view_rodadas["Status"] = view_rodadas["status"].map(BADGES_RODADA)
    view_rodadas["Congelada em"] = pd.to_datetime(
        view_rodadas["congelada_em"]).dt.strftime("%d/%m/%Y %H:%M")
    view_rodadas["Crescimento"] = view_rodadas["ativo_crescimento"].map(
        {True: "sim", False: "não"})
    st.dataframe(
        view_rodadas[["janela_label", "Status", "Congelada em", "congelada_por", "Crescimento"]]
        .rename(columns={"janela_label": "Janela", "congelada_por": "Por"}),
        width="stretch", hide_index=True,
    )

    idx_rodada = st.selectbox(
        "Rodada",
        options=list(range(len(rodadas))),
        format_func=lambda i: (
            f"{BADGES_RODADA.get(rodadas.iloc[i]['status'], rodadas.iloc[i]['status'])} · "
            f"{rodadas.iloc[i]['janela_label']}"
        ),
    )
    rodada_sel = rodadas.iloc[idx_rodada]
    rodada_id = rodada_sel["id"]

    # Congelamento abortado (falha no meio da gravação) → só limpar
    if rodada_sel["status"] == estados.RODADA_CONGELANDO:
        st.warning(
            "Este congelamento ficou **incompleto** (falha no meio da gravação). "
            "Limpe-o e congele a rodada de novo no Simulador."
        )
        if st.button("🧹 Limpar congelamento incompleto"):
            try:
                repo.limpar_congelamento_abortado(rodada_id)
                _flash("success", "Congelamento incompleto removido.")
            except TransicaoInvalida as exc:
                _flash("error", str(exc))
            st.rerun()
        st.stop()

    # Snapshot p/ conferência (jsonb pesado — só carrega sob demanda)
    with st.expander("🔍 Snapshot da rodada (conferência)"):
        if st.toggle("Carregar snapshot", key=f"snap_{rodada_id}"):
            rodada_full = repo.obter_rodada(rodada_id)
            st.caption(
                f"Referência do cálculo: {rodada_full['data_referencia']} · "
                f"congelada em {pd.Timestamp(rodada_full['congelada_em']):%d/%m/%Y %H:%M} "
                f"por {rodada_full['congelada_por']}"
            )
            df_snap = pd.DataFrame(rodada_full["resultado_skus"])
            st.download_button(
                "⬇️ Baixar resultado por SKU (CSV)",
                data=df_snap.to_csv(index=False, sep=";", decimal=",").encode("utf-8"),
                file_name=f"snapshot_rodada_{rodada_full['mes_disparo']:02d}"
                          f"{rodada_full['ano_disparo']}.csv",
                mime="text/csv",
            )
            st.json(rodada_full["config_snapshot"], expanded=False)

    if rodada_sel["status"] == estados.RODADA_ABERTA:
        with st.popover("🚫 Cancelar rodada congelada"):
            st.caption(
                "Cancela a rodada e todos os pedidos em rascunho — a rodada "
                "cancelada fica registrada e libera um novo congelamento. Só é "
                "possível se nenhum pedido saiu de RASCUNHO."
            )
            if st.button("Confirmar cancelamento da rodada", type="primary"):
                try:
                    repo.cancelar_rodada(rodada_id, username)
                    _flash("success", "Rodada cancelada — pode congelar de novo no Simulador.")
                except TransicaoInvalida as exc:
                    _flash("error", str(exc))
                st.rerun()

# =================================================================
# NÍVEL 2 — Pedidos da rodada selecionada
# =================================================================
pedidos = repo.listar_pedidos(rodada_id)
if len(pedidos) == 0:
    st.info("Rodada sem pedidos.")
    st.stop()

with st.container(border=True):
    st.subheader("Pedidos da rodada")

    view_ped = pedidos.copy()
    view_ped["Status"] = view_ped["status"].map(estados.ROTULOS_BADGE)
    view_ped["Δ"] = view_ped["qtd_final"] - view_ped["qtd_sugerida"]
    st.dataframe(
        view_ped[["titulo", "colegio", "super_categoria", "Status", "n_itens",
                  "qtd_sugerida", "qtd_final", "Δ", "investimento_final"]]
        .rename(columns={
            "titulo": "Título", "colegio": "Colégio", "super_categoria": "Super Categoria",
            "n_itens": "Itens", "qtd_sugerida": "Qtd Sugerida", "qtd_final": "Qtd Final",
            "investimento_final": "Investimento (R$)",
        }),
        width="stretch", hide_index=True,
        column_config={
            "Investimento (R$)": st.column_config.NumberColumn(format="R$ %.2f"),
        },
    )

    idx_pedido = st.selectbox(
        "Pedido",
        options=list(range(len(pedidos))),
        format_func=lambda i: (
            f"{estados.ROTULOS_BADGE.get(pedidos.iloc[i]['status'], '')} · "
            f"{pedidos.iloc[i]['colegio']} · {pedidos.iloc[i]['super_categoria']}"
        ),
    )
    pedido_sel = pedidos.iloc[idx_pedido]
    pedido_id = pedido_sel["id"]

# =================================================================
# NÍVEL 3 — Itens do pedido
# =================================================================
itens = repo.listar_itens(pedido_id)
pode_editar = estados.editavel(pedido_sel["status"])

with st.container(border=True):
    st.subheader(pedido_sel["titulo"])
    st.caption(
        f"{estados.ROTULOS_BADGE.get(pedido_sel['status'], pedido_sel['status'])} · "
        f"criado em {pd.Timestamp(pedido_sel['criado_em']):%d/%m/%Y %H:%M} "
        f"por {pedido_sel['criado_por']}"
    )
    if not pode_editar and pedido_sel["status"] == estados.PRONTO:
        st.info("Pedido **Pronto** — reabra o rascunho para editar quantidades.")

    cols_editor = ["sku", "produto", "tamanho", "categoria",
                   "quantidade_sugerida", "quantidade_final"]
    df_editor = itens[cols_editor].copy()
    editado = st.data_editor(
        df_editor,
        key=f"editor_{pedido_id}",
        width="stretch", hide_index=True, num_rows="fixed",
        disabled=(True if not pode_editar
                  else ["sku", "produto", "tamanho", "categoria", "quantidade_sugerida"]),
        column_config={
            "sku": st.column_config.TextColumn("SKU"),
            "produto": st.column_config.TextColumn("Produto"),
            "tamanho": st.column_config.TextColumn("Tam."),
            "categoria": st.column_config.TextColumn("Categoria"),
            "quantidade_sugerida": st.column_config.NumberColumn(
                "Qtd Sugerida", help="Congelada no snapshot — imutável (auditoria)"),
            "quantidade_final": st.column_config.NumberColumn(
                "Qtd Final", min_value=0, step=1,
                help="Quantidade que será emitida — editável no rascunho"),
        },
    )

    # Totais recalculados do editor (feedback imediato antes de salvar)
    _qtd_final = pd.to_numeric(editado["quantidade_final"], errors="coerce").fillna(0)
    _delta = int(_qtd_final.sum() - itens["quantidade_sugerida"].sum())
    _invest = float((_qtd_final.values * itens["custo_unit"].values).sum())
    st.caption(
        f"**{int((_qtd_final > 0).sum())}** SKUs · "
        f"**{int(_qtd_final.sum()):,}** pares finais "
        f"(Δ {_delta:+d} vs sugerido) · **{_fmt_brl(_invest)}**"
    )

    # --- Ações por estado ---
    ac1, ac2, ac3 = st.columns([1.2, 1.2, 3])

    if pedido_sel["status"] == estados.RASCUNHO:
        with ac1:
            if st.button("💾 Salvar alterações", type="primary"):
                # Diff editor × banco: só linhas alteradas (ordem preservada —
                # num_rows="fixed" mantém o alinhamento posicional com itens)
                alteracoes = [
                    {"id": iid, "quantidade_final": int(qf)}
                    for iid, qf, q0 in zip(itens["id"], _qtd_final, itens["quantidade_final"])
                    if int(qf) != int(q0)
                ]
                if not alteracoes:
                    _flash("info", "Nenhuma quantidade alterada.")
                else:
                    try:
                        n = repo.atualizar_quantidades(pedido_id, alteracoes, username)
                        _flash("success", f"{n} item(ns) atualizado(s).")
                    except PedidoNaoEditavel as exc:
                        _flash("warning", str(exc))
                st.rerun()
        with ac2:
            if st.button("✅ Marcar como Pronto"):
                ok = repo.transicionar_pedido(
                    pedido_id, estados.RASCUNHO, estados.PRONTO, username)
                _flash("success", "Pedido marcado como **Pronto**.") if ok else _flash(
                    "warning", "O pedido mudou de estado em outra sessão — recarregado.")
                st.rerun()
        with ac3:
            with st.popover("🚫 Cancelar pedido"):
                st.caption("O pedido cancelado sai do fluxo (fica registrado p/ auditoria).")
                if st.button("Confirmar cancelamento", key=f"cancel_{pedido_id}"):
                    ok = repo.transicionar_pedido(
                        pedido_id, estados.RASCUNHO, estados.CANCELADO, username)
                    _flash("success", "Pedido cancelado.") if ok else _flash(
                        "warning", "O pedido mudou de estado em outra sessão — recarregado.")
                    st.rerun()

    elif pedido_sel["status"] == estados.PRONTO:
        with ac1:
            if st.button("↩️ Reabrir rascunho"):
                ok = repo.transicionar_pedido(
                    pedido_id, estados.PRONTO, estados.RASCUNHO, username)
                _flash("success", "Pedido reaberto para edição.") if ok else _flash(
                    "warning", "O pedido mudou de estado em outra sessão — recarregado.")
                st.rerun()
        with ac2:
            with st.popover("🚫 Cancelar pedido"):
                if st.button("Confirmar cancelamento", key=f"cancelp_{pedido_id}"):
                    ok = repo.transicionar_pedido(
                        pedido_id, estados.PRONTO, estados.CANCELADO, username)
                    _flash("success", "Pedido cancelado.") if ok else _flash(
                        "warning", "O pedido mudou de estado em outra sessão — recarregado.")
                    st.rerun()

    # --- Observações padronizadas p/ o Bling (sempre recompostas — refletem
    # as quantidades finais salvas, nunca texto pré-gravado) ---
    obs_bling = builder.montar_observacoes_bling(
        rodada_sel.to_dict(), pedido_sel.to_dict(), itens)
    with st.expander("📄 Observações para o Bling (padronizadas)"):
        st.caption(
            "Texto que a emissão automática enviará no campo de observações "
            "internas do pedido de compra. Enquanto a emissão é manual, copie "
            "daqui (ou do cabeçalho do CSV)."
        )
        st.code(obs_bling, language=None)

    # --- CSV do pedido (ponte manual p/ digitar no Bling) ---
    df_csv = itens[["sku", "produto", "tamanho", "categoria",
                    "quantidade_sugerida", "quantidade_final", "custo_unit"]].copy()
    df_csv["investimento"] = df_csv["quantidade_final"] * df_csv["custo_unit"]
    df_csv = df_csv.rename(columns={
        "sku": "SKU", "produto": "Produto", "tamanho": "Tam", "categoria": "Categoria",
        "quantidade_sugerida": "Qtd Sugerida", "quantidade_final": "Qtd Final",
        "custo_unit": "Custo Unit (R$)", "investimento": "Investimento (R$)",
    })
    cabecalho = "".join(f"# {linha}\n" for linha in obs_bling.split("\n")) + "\n"
    csv_pedido = (cabecalho + df_csv.to_csv(index=False, sep=";", decimal=",")).encode("utf-8")
    st.download_button(
        "⬇️ Baixar CSV do pedido",
        data=csv_pedido,
        file_name=f"pedido_{pedido_sel['colegio']}_{pedido_sel['super_categoria']}"
                  f"_{rodada_sel['mes_disparo']:02d}{rodada_sel['ano_disparo']}.csv",
        mime="text/csv",
    )
