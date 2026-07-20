"""
Página: Pedidos de Compra (Admin Only)

Rodadas congeladas do Simulador de Produção → pedidos de compra em rascunho
por Colégio × Super Categoria → revisão/edição de quantidades → PRONTO →
emissão em DOIS momentos: compra no Bling (AK Uniformes), depois venda no
Olist (Art Kamizetas). Conexão/chaves das integrações ficam na aba
Integrações de Configurações.

Três níveis: rodadas congeladas → pedidos da rodada → itens do pedido.
Leituras do schema `app` sem st.cache_data (tabelas pequenas; cache
confundiria o pós-escrita).
"""

import streamlit as st
from auth import exigir_login
exigir_login()

import pandas as pd

from pedidos import builder, estados, emissor
from pedidos.repositorio import (
    obter_repositorio, TransicaoInvalida, PedidoNaoEditavel,
)
from pedidos.integracoes.repositorio import obter_repositorio_integracoes

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


def _executar_lote(pedidos_alvo, fn_por_pedido):
    """
    Roda `fn_por_pedido(row)` para cada pedido do subconjunto elegível, SEM
    parar no primeiro erro: cada falha vira uma linha de problema para o
    usuário tomar medida corretiva (requisito de emissão em lote). `fn_por_pedido`
    retorna (ok: bool, msg: str); exceções (ex: EmissaoFalhou) são capturadas
    aqui. Retorna (sucessos: int, falhas: list[str]).
    """
    sucessos, falhas = 0, []
    for _, row in pedidos_alvo.iterrows():
        rotulo = f"{row['colegio']} · {row['super_categoria']}"
        try:
            ok, msg = fn_por_pedido(row)
            if ok:
                sucessos += 1
            else:
                falhas.append(f"**{rotulo}** — {msg}")
        except Exception as exc:
            falhas.append(f"**{rotulo}** — {exc}")
    return sucessos, falhas


def _flash_resumo_lote(acao: str, sucessos: int, falhas: list):
    """Consolida o resultado de um lote numa mensagem que sobrevive ao rerun."""
    if not sucessos and not falhas:
        _flash("info", f"{acao}: nenhum pedido elegível na seleção.")
    elif sucessos and not falhas:
        _flash("success", f"{acao}: **{sucessos}** pedido(s) concluído(s).")
    else:
        corpo = "\n".join(f"- {f}" for f in falhas)
        if sucessos:
            _flash("warning",
                   f"{acao}: **{sucessos}** concluído(s), **{len(falhas)}** com "
                   f"problema — confira e trate:\n\n{corpo}")
        else:
            _flash("error",
                   f"{acao}: nenhum concluído. Problemas:\n\n{corpo}")


# Rótulos das colunas da memória de cálculo por SKU (ordem de exibição =
# a cadeia order-up-to: base → demanda → alvo → posição → sugestão)
_MEMORIA_COLS = {
    "vendas_hist": "Vendas hist. (alta)",
    "demanda_periodo": "Demanda período",
    "demanda_periodo_alta": "…alta",
    "demanda_periodo_baixa": "…baixa",
    "estoque_seguranca": "Estoque segurança",
    "estoque_meta": "Estoque-alvo",
    "estoque_rede": "Estoque rede",
    "backlog": "Backlog",
    "estoque_projetado": "Projetado na chegada",
}


def _memoria_sugestao(itens: pd.DataFrame) -> None:
    """
    Painel read-only 'por que essa quantidade': os drivers congelados da
    sugestão (memoria_sugerida por item), ao lado do SKU e da qtd sugerida —
    sem carregar o resultado_skus pesado da rodada. Degrada com aviso quando
    a rodada foi congelada antes desta memória existir (jsonb vazio)."""
    with st.expander("🧮 Por que essas quantidades? (memória de cálculo)"):
        if "memoria_sugerida" not in itens.columns:
            st.info("Rodada congelada antes desta versão — memória indisponível.")
            return
        linhas = []
        for _, it in itens.iterrows():
            mem = it["memoria_sugerida"] or {}
            if not isinstance(mem, dict) or not mem:
                continue
            linha = {"SKU": it["sku"]}
            for chave, rotulo in _MEMORIA_COLS.items():
                valor = mem.get(chave)
                linha[rotulo] = round(valor, 1) if isinstance(valor, (int, float)) else None
            ns = mem.get("nivel_servico")   # já em pontos percentuais (99, 92…)
            linha["Nível serviço"] = f"{ns:.0f}%" if isinstance(ns, (int, float)) else "—"
            linha["Qtd sugerida"] = int(it["quantidade_sugerida"])
            linhas.append(linha)

        if not linhas:
            st.info(
                "Esta rodada foi congelada antes da memória de cálculo por item "
                "existir — abra o snapshot da rodada para conferir."
            )
            return

        st.caption(
            "Política **order-up-to**: `Demanda período + Estoque segurança = "
            "Estoque-alvo`; subtrai-se o estoque projetado na chegada "
            "(`Estoque rede − Backlog`, consumido até a rodada chegar) → **Qtd "
            "sugerida**. Valores congelados no momento do cálculo."
        )
        st.dataframe(pd.DataFrame(linhas), width="stretch", hide_index=True)


st.title("🧾 Pedidos de Compra")
st.caption(
    "Rodadas congeladas do Simulador de Produção, divididas em pedidos por "
    "**Colégio × Super Categoria**. Edite as quantidades no rascunho, marque "
    "como Pronto e emita a compra no Bling e a venda no Olist."
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

# --- Resumo da rodada (veredito rápido, acima do seletor de modo) ---
with st.container(border=True):
    st.subheader("Resumo da rodada")
    _k1, _k2, _k3, _k4, _k5 = st.columns(5)
    _k1.metric("Pedidos", len(pedidos))
    _k2.metric("Rascunhos", int((pedidos["status"] == estados.RASCUNHO).sum()))
    _k3.metric("Prontos", int((pedidos["status"] == estados.PRONTO).sum()))
    _k4.metric("Emitidos", int((pedidos["status"] == estados.EMITIDO).sum()))
    _k5.metric("Investimento (final)", _fmt_brl(float(pedidos["investimento_final"].sum())))

# =================================================================
# PEDIDO INDIVIDUAL — inspecionar / editar UM pedido. Fragmento próprio:
# trocar o pedido no selectbox rerroda SÓ esta caixa, não a tabela de ação
# em lote do outro modo. Os botões de ação usam st.rerun() (escopo app) porque
# mudam o estado do pedido e precisam refletir na tabela de lote + no flash.
# (Roda dentro de _area_trabalho — o @st.fragment é o do pai, não aqui.)
# =================================================================
def _secao_pedido():
    with st.container(border=True):
        # --- Escolha do pedido: "Ver pedido" (h3) + selectbox ---
        st.subheader("🔍 Ver pedido")
        idx_pedido = st.selectbox(
            "Ver pedido",
            options=list(range(len(pedidos))),
            format_func=lambda i: (
                f"{estados.ROTULOS_BADGE.get(pedidos.iloc[i]['status'], '')} · "
                f"{pedidos.iloc[i]['colegio']} · {pedidos.iloc[i]['super_categoria']}"
            ),
            label_visibility="collapsed",
        )
        pedido_sel = pedidos.iloc[idx_pedido]
        pedido_id = pedido_sel["id"]
        itens = repo.listar_itens(pedido_id)
        pode_editar = estados.editavel(pedido_sel["status"])

        # --- Nome do pedido (h3) + status ---
        st.subheader(pedido_sel["titulo"])
        st.caption(
            f"{estados.ROTULOS_BADGE.get(pedido_sel['status'], pedido_sel['status'])} · "
            f"criado em {pd.Timestamp(pedido_sel['criado_em']):%d/%m/%Y %H:%M} "
            f"por {pedido_sel['criado_por']}"
        )
        if not pode_editar and pedido_sel["status"] == estados.PRONTO:
            st.info("Pedido **Pronto** — reabra o rascunho para editar quantidades.")

        # --- Itens (editor) ---
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
        # Totais recalculados do editor (exibidos mais abaixo, acima dos botões)
        _qtd_final = pd.to_numeric(editado["quantidade_final"], errors="coerce").fillna(0)
        _delta = int(_qtd_final.sum() - itens["quantidade_sugerida"].sum())
        _invest = float((_qtd_final.values * itens["custo_unit"].values).sum())

        _memoria_sugestao(itens)

        # --- Banners de estado (informativos) ---
        if pedido_sel["status"] == estados.COMPRA_EMITIDA:
            st.success(f"🛒 Compra emitida no Bling · nº **{pedido_sel.get('bling_numero','?')}** "
                       "— falta emitir a venda no Olist.")
        elif estados.emitindo(pedido_sel["status"]):
            st.warning(
                "⏳ **Emissão interrompida.** Este pedido ficou travado durante uma "
                "emissão (falha entre criar no ERP e confirmar aqui). **Confira no ERP "
                "se o pedido foi criado** antes de destravar e tentar de novo."
            )
        elif pedido_sel["status"] == estados.EMITIDO:
            st.success(
                f"📨 Emitido nos dois ERPs · compra Bling **{pedido_sel.get('bling_numero','?')}** "
                f"· venda Olist **{pedido_sel.get('olist_numero','?')}**."
            )

        # --- Pré-validação do mapeamento SKU→id Olist (só COMPRA_EMITIDA) —
        # usada tanto no preview quanto no botão de emitir venda lá embaixo ---
        _mapa, _erros_pre, _erro_map = {}, [], None
        if pedido_sel["status"] == estados.COMPRA_EMITIDA:
            try:
                from pedidos.integracoes import oauth as _oauth, olist as _olist
                _repo_int = obter_repositorio_integracoes()
                _cfg_olist = (_repo_int.ler("olist") or {}).get("config") or {}
                _token = _oauth.obter_access_token("olist", _repo_int)
                _skus = itens[itens["quantidade_final"] > 0]["sku"].tolist()
                with st.spinner("Verificando catálogo do Olist…"):
                    _mapa, _faltantes = _olist.mapear_produtos_por_sku(_token, _skus)
            except Exception as exc:
                _erro_map = str(exc)
            _erros_pre = emissor.validar_pre_emissao_olist(
                itens, _cfg_olist if not _erro_map else {}, _mapa)
            if _erro_map:
                st.warning(f"Não foi possível checar o catálogo do Olist agora: {_erro_map}")
            elif _erros_pre:
                for _e in _erros_pre:
                    st.warning(_e)

        # --- Olist pronto? Avisa ANTES da compra (que é irreversível daqui) ---
        if pedido_sel["status"] == estados.PRONTO:
            for _a in emissor.checar_prontidao_olist(obter_repositorio_integracoes()):
                st.warning(f"⚠️ {_a}")

        # --- Preview dos payloads de emissão (verificação humana, sem escrita) ---
        if pedido_sel["status"] in (estados.PRONTO, estados.COMPRA_EMITIDA):
            with st.expander("🔍 Preview dos payloads de emissão"):
                st.caption("O JSON exato que será enviado aos ERPs — confira antes de emitir.")
                try:
                    _prev = emissor.preview_payloads(
                        pedido_id, repo, obter_repositorio_integracoes(), mapa_sku=_mapa)
                    cpv, vpv = st.columns(2)
                    with cpv:
                        st.markdown("**Compra (Bling)**")
                        st.json(_prev["compra"], expanded=False)
                    with vpv:
                        st.markdown("**Venda (Olist)**")
                        st.json(_prev["venda"], expanded=False)
                except Exception as exc:
                    st.caption(f"Preview indisponível: {exc}")

        # --- Observações padronizadas p/ o Bling (sempre recompostas) ---
        obs_bling = builder.montar_observacoes_bling(
            rodada_sel.to_dict(), pedido_sel.to_dict(), itens)
        with st.expander("📄 Observações para o Bling (padronizadas)"):
            st.caption(
                "Bloco que a emissão automática envia no campo **Observações** do "
                "pedido de compra. Nas **Observações internas** vai só o título "
                f"(`{pedido_sel['titulo']}`) — é o que aparece na listagem e na "
                "busca do Bling. Enquanto a emissão é manual, copie daqui (ou do "
                "cabeçalho do CSV)."
            )
            st.code(obs_bling, language=None)

        # --- CSV do pedido (preparado aqui; botão renderizado na linha de ações) ---
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

        def _botao_csv():
            st.download_button(
                "⬇️ Baixar CSV do pedido",
                data=csv_pedido,
                file_name=f"pedido_{pedido_sel['colegio']}_{pedido_sel['super_categoria']}"
                          f"_{rodada_sel['mes_disparo']:02d}{rodada_sel['ano_disparo']}.csv",
                mime="text/csv", width="stretch", key=f"csv_{pedido_id}",
            )

        # =========================================================
        # Informação geral + AÇÕES (parte inferior, em colunas iguais)
        # =========================================================
        st.divider()
        _m1, _m2, _m3 = st.columns(3)
        _m1.metric("SKUs", int((_qtd_final > 0).sum()))
        _m2.metric("Pares finais", f"{int(_qtd_final.sum()):,}".replace(",", "."),
                   delta=f"{_delta:+d} vs sugerido", delta_color="off")
        _m3.metric("Investimento", _fmt_brl(_invest))

        status = pedido_sel["status"]
        if status == estados.RASCUNHO:
            c1, c2, c3, c4 = st.columns(4)
            with c1:
                if st.button("💾 Salvar alterações", type="primary", width="stretch"):
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
            with c2:
                if st.button("✅ Marcar como Pronto", width="stretch"):
                    ok = repo.transicionar_pedido(
                        pedido_id, estados.RASCUNHO, estados.PRONTO, username)
                    _flash("success", "Pedido marcado como **Pronto**.") if ok else _flash(
                        "warning", "O pedido mudou de estado em outra sessão — recarregado.")
                    st.rerun()
            with c3:
                with st.popover("🚫 Cancelar pedido", width="stretch"):
                    st.caption("O pedido cancelado sai do fluxo (fica registrado p/ auditoria).")
                    if st.button("Confirmar cancelamento", key=f"cancel_{pedido_id}"):
                        ok = repo.transicionar_pedido(
                            pedido_id, estados.RASCUNHO, estados.CANCELADO, username)
                        _flash("success", "Pedido cancelado.") if ok else _flash(
                            "warning", "O pedido mudou de estado em outra sessão — recarregado.")
                        st.rerun()
            with c4:
                _botao_csv()

        elif status == estados.PRONTO:
            c1, c2, c3, c4 = st.columns(4)
            with c1:
                if st.button("📤 Emitir compra (Bling)", type="primary", width="stretch",
                             key=f"emit_compra_{pedido_id}"):
                    with st.status("📤 Emitindo compra no Bling…", expanded=True) as _s:
                        try:
                            res = emissor.emitir_compra_bling(
                                pedido_id, username, repo, obter_repositorio_integracoes())
                            _s.update(label=f"Compra emitida · nº {res['bling_numero']}",
                                      state="complete")
                            _flash("success",
                                   f"Compra emitida no Bling · nº **{res['bling_numero']}**.")
                        except emissor.EmissaoFalhou as exc:
                            _s.update(label="Falha na emissão da compra", state="error")
                            _flash("error", f"Emissão da compra falhou: {exc}")
                    st.rerun()
            with c2:
                if st.button("↩️ Reabrir rascunho", width="stretch", key=f"reabrir_{pedido_id}"):
                    ok = repo.transicionar_pedido(
                        pedido_id, estados.PRONTO, estados.RASCUNHO, username)
                    _flash("success", "Pedido reaberto para edição.") if ok else _flash(
                        "warning", "O pedido mudou de estado em outra sessão — recarregado.")
                    st.rerun()
            with c3:
                with st.popover("🚫 Cancelar pedido", width="stretch"):
                    if st.button("Confirmar cancelamento", key=f"cancelp_{pedido_id}"):
                        ok = repo.transicionar_pedido(
                            pedido_id, estados.PRONTO, estados.CANCELADO, username)
                        _flash("success", "Pedido cancelado.") if ok else _flash(
                            "warning", "O pedido mudou de estado em outra sessão — recarregado.")
                        st.rerun()
            with c4:
                _botao_csv()

        elif status == estados.COMPRA_EMITIDA:
            c1, c2 = st.columns(2)
            with c1:
                if st.button("📤 Emitir venda (Olist)", type="primary", width="stretch",
                             disabled=bool(_erro_map or _erros_pre),
                             key=f"emit_venda_{pedido_id}"):
                    with st.status("📤 Emitindo venda no Olist…", expanded=True) as _s:
                        try:
                            res = emissor.emitir_venda_olist(
                                pedido_id, username, repo, obter_repositorio_integracoes(),
                                mapa_sku=_mapa)
                            _s.update(label=f"Venda emitida · nº {res['olist_numero']}",
                                      state="complete")
                            _flash("success",
                                   f"Venda emitida no Olist · nº **{res['olist_numero']}**.")
                        except emissor.EmissaoFalhou as exc:
                            _s.update(label="Falha na emissão da venda", state="error")
                            _flash("error", f"Emissão da venda falhou: {exc}")
                    st.rerun()
            with c2:
                _botao_csv()

        elif estados.emitindo(status):
            c1, c2 = st.columns(2)
            with c1:
                with st.popover("🔓 Destravar", width="stretch"):
                    st.caption("Volta o pedido ao estado anterior. Confirme antes que o "
                               "pedido NÃO foi criado no ERP (senão vira duplicata).")
                    if st.button("Confirmar destravamento", key=f"destr_{pedido_id}"):
                        ok = emissor.destravar(pedido_id, username, repo,
                                               obter_repositorio_integracoes())
                        _flash("success", "Pedido destravado.") if ok else _flash(
                            "warning", "O estado mudou em outra sessão — recarregado.")
                        st.rerun()
            with c2:
                _botao_csv()

        else:  # EMITIDO / SINCRONIZADO / CANCELADO — sem ações, só o CSV
            c1, _ = st.columns([1, 3])
            with c1:
                _botao_csv()


# =================================================================
# AÇÃO EM LOTE — seleção múltipla na tabela + botões (aprovar / reabrir /
# cancelar / emitir compra Bling / emitir venda Olist)
# =================================================================
def _secao_lote():
    """
    Roda dentro de _area_trabalho (@st.fragment): clicar num checkbox da tabela
    dispara um rerun SÓ dessa área — não re-executa as leituras do topo
    (listar_rodadas/listar_pedidos, sem cache). Some com o 'piscar/carregar' a
    cada clique. Os botões de ação chamam st.rerun() (escopo app) para refletir
    a mudança na página inteira + a mensagem de resultado.
    """
    with st.container(border=True):
        st.subheader("Pedidos da rodada — ação em lote")

        view_ped = pedidos.copy()
        view_ped["Status"] = view_ped["status"].map(estados.ROTULOS_BADGE)
        view_ped["Δ"] = view_ped["qtd_final"] - view_ped["qtd_sugerida"]
        # bling_numero/olist_numero podem não existir se o DDL 003 não foi aplicado
        for _col in ("bling_numero", "olist_numero"):
            if _col not in view_ped.columns:
                view_ped[_col] = ""
        st.caption(
            "Marque as linhas (☑) e use os botões abaixo para agir em vários pedidos "
            "de uma vez. Para ver/editar um pedido, troque para **✏️ Editar um pedido** "
            "no seletor de modo (topo). O cabeçalho da coluna de seleção marca/desmarca tudo."
        )
        evento = st.dataframe(
            view_ped[["titulo", "colegio", "super_categoria", "Status", "n_itens",
                      "qtd_sugerida", "qtd_final", "Δ", "investimento_final",
                      "bling_numero", "olist_numero"]]
            .rename(columns={
                "titulo": "Título", "colegio": "Colégio", "super_categoria": "Super Categoria",
                "n_itens": "Itens", "qtd_sugerida": "Qtd Sugerida", "qtd_final": "Qtd Final",
                "investimento_final": "Investimento (R$)",
                "bling_numero": "Nº Bling", "olist_numero": "Nº Olist",
            }),
            width="stretch", hide_index=True, height=560,
            on_select="rerun", selection_mode="multi-row",
            key=f"sel_pedidos_{rodada_id}",
            column_config={
                "Itens": st.column_config.NumberColumn(format="%d"),
                "Qtd Sugerida": st.column_config.NumberColumn(format="%d"),
                "Qtd Final": st.column_config.NumberColumn(format="%d"),
                "Δ": st.column_config.NumberColumn(format="%+d"),
                "Investimento (R$)": st.column_config.NumberColumn(format="R$ %.2f"),
            },
        )

        # Posições selecionadas → pedidos (view_ped preserva a ordem/índice de `pedidos`)
        _rows = list(getattr(evento.selection, "rows", []) if evento else [])
        sel = pedidos.iloc[_rows] if _rows else pedidos.iloc[0:0]

        # -----------------------------------------------------------------
        # Barra de AÇÕES EM LOTE (aparece com ≥1 selecionado; cada botão age só
        # nas linhas cujo estado permite aquela ação — seleção mista é ok)
        # -----------------------------------------------------------------
        if len(sel) >= 1:
            _rasc = sel[sel["status"] == estados.RASCUNHO]
            _pronto = sel[sel["status"] == estados.PRONTO]   # também os "reabríveis"
            _compra = sel[sel["status"] == estados.COMPRA_EMITIDA]
            _cancelaveis = sel[sel["status"].isin([estados.RASCUNHO, estados.PRONTO])]

            st.divider()
            _s1, _s2, _s3 = st.columns(3)
            _s1.metric("Selecionados", len(sel))
            _s2.metric("Pares finais", f"{int(sel['qtd_final'].sum()):,}".replace(",", "."))
            _s3.metric("Investimento", _fmt_brl(float(sel['investimento_final'].sum())))

            b1, b2, b3, b4, b5 = st.columns(5)

            # Aprovar: RASCUNHO → PRONTO
            with b1:
                if st.button(f"✅ Aprovar ({len(_rasc)})", type="primary",
                             disabled=_rasc.empty, width="stretch",
                             help="Marca os selecionados em Rascunho como Pronto"):
                    suc, fal = _executar_lote(
                        _rasc, lambda r: (
                            repo.transicionar_pedido(
                                r["id"], estados.RASCUNHO, estados.PRONTO, username),
                            "o estado mudou em outra sessão"))
                    _flash_resumo_lote("Aprovação", suc, fal)
                    st.rerun()

            # Reabrir: PRONTO → RASCUNHO
            with b2:
                if st.button(f"↩️ Reabrir ({len(_pronto)})",
                             disabled=_pronto.empty, width="stretch",
                             help="Volta os selecionados em Pronto para Rascunho (edição)"):
                    suc, fal = _executar_lote(
                        _pronto, lambda r: (
                            repo.transicionar_pedido(
                                r["id"], estados.PRONTO, estados.RASCUNHO, username),
                            "o estado mudou em outra sessão"))
                    _flash_resumo_lote("Reabertura", suc, fal)
                    st.rerun()

            # Cancelar: RASCUNHO/PRONTO → CANCELADO (com confirmação)
            with b3:
                with st.popover(f"🚫 Cancelar ({len(_cancelaveis)})", width="stretch",
                                disabled=_cancelaveis.empty):
                    st.caption("Cancela os selecionados em Rascunho/Pronto (fica registrado "
                               "p/ auditoria). Pedidos já emitidos são ignorados.")
                    if st.button("Confirmar cancelamento", type="primary",
                                 key="cancel_lote"):
                        suc, fal = _executar_lote(
                            _cancelaveis, lambda r: (
                                repo.transicionar_pedido(
                                    r["id"], r["status"], estados.CANCELADO, username),
                                "o estado mudou em outra sessão"))
                        _flash_resumo_lote("Cancelamento", suc, fal)
                        st.rerun()

            # Emitir compra (Bling): PRONTO → Bling (com confirmação + total)
            with b4:
                with st.popover(f"📤 Compra Bling ({len(_pronto)})", width="stretch",
                                disabled=_pronto.empty):
                    st.caption(
                        f"Emite **{len(_pronto)}** pedido(s) de compra REAIS no Bling · "
                        f"total **{_fmt_brl(float(_pronto['investimento_final'].sum()))}**. "
                        "Se um falhar, os demais seguem e o erro é reportado."
                    )
                    # Um lote de compras sem o Olist pronto vira um lote de
                    # pedidos órfãos em COMPRA_EMITIDA — avisa antes.
                    for _a in emissor.checar_prontidao_olist(
                            obter_repositorio_integracoes()):
                        st.warning(f"⚠️ {_a}")
                    if st.button("Confirmar emissão das compras", type="primary",
                                 key="emit_compra_lote"):
                        with st.status(f"📤 Emitindo {len(_pronto)} compra(s) no Bling…",
                                       expanded=True) as _s:
                            suc, fal = _executar_lote(
                                _pronto, lambda r: (
                                    bool(emissor.emitir_compra_bling(
                                        r["id"], username, repo,
                                        obter_repositorio_integracoes())), ""))
                            _s.update(
                                label=f"Compras processadas: {suc} ok, {len(fal)} com problema",
                                state="error" if fal else "complete")
                        _flash_resumo_lote("Emissão de compra (Bling)", suc, fal)
                        st.rerun()

            # Emitir venda (Olist): COMPRA_EMITIDA → Olist (com confirmação + total)
            with b5:
                with st.popover(f"📤 Venda Olist ({len(_compra)})", width="stretch",
                                disabled=_compra.empty):
                    st.caption(
                        f"Emite **{len(_compra)}** pedido(s) de venda no Olist (só os que "
                        "já têm a compra emitida). O mapeamento SKU→Olist é feito UMA vez "
                        "para o lote inteiro; falha em um não interrompe os outros."
                    )
                    if st.button("Confirmar emissão das vendas", type="primary",
                                 key="emit_venda_lote"):
                        with st.status(f"📤 Emitindo {len(_compra)} venda(s) no Olist…",
                                       expanded=True) as _s:
                            # Mapeia SKU→Olist UMA vez p/ todo o lote — passar
                            # mapa_sku=None por pedido refazia a busca N vezes e
                            # estourava o rate limit (429). Superset é seguro:
                            # cada emissão só consulta os SKUs do próprio pedido.
                            from pedidos.integracoes import (oauth as _oauth,
                                                             olist as _olist)
                            try:
                                _ri = obter_repositorio_integracoes()
                                _tok = _oauth.obter_access_token("olist", _ri)
                                _skus = sorted({
                                    s for pid in _compra["id"]
                                    for s in repo.listar_itens(pid).pipe(
                                        lambda d: d[d["quantidade_final"] > 0]["sku"])
                                })
                                _s.write(f"Mapeando {len(_skus)} SKU(s) no Olist…")
                                _mapa_lote, _ = _olist.mapear_produtos_por_sku(_tok, _skus)
                            except Exception as _exc:
                                _mapa_lote = None
                                _s.write(f"⚠️ Pré-mapeamento falhou ({_exc}); "
                                         "cada pedido mapeia sozinho.")
                            suc, fal = _executar_lote(
                                _compra, lambda r: (
                                    bool(emissor.emitir_venda_olist(
                                        r["id"], username, repo,
                                        obter_repositorio_integracoes(),
                                        mapa_sku=_mapa_lote)), ""))
                            _s.update(
                                label=f"Vendas processadas: {suc} ok, {len(fal)} com problema",
                                state="error" if fal else "complete")
                        _flash_resumo_lote("Emissão de venda (Olist)", suc, fal)
                        st.rerun()


# Dois modos de trabalho (editar UM pedido × agir em VÁRIOS) num seletor de modo,
# NÃO em st.tabs: fragment-que-rerroda dentro de st.tabs quebra o show/hide e vaza
# o conteúdo das duas ("ghost tabs inside st.fragment", #9158/#9313 do Streamlit).
#
# O seletor + as duas seções vivem TODOS dentro de _area_trabalho (@st.fragment):
# trocar de modo (ou de pedido, ou marcar linhas no lote) rerroda SÓ esta área —
# NÃO re-executa as leituras do topo (listar_rodadas/listar_pedidos), que são sem
# cache e eram o que travava a troca de modo. Só as AÇÕES (salvar/emitir/cancelar)
# chamam st.rerun() de app inteiro, porque aí o dado mudou e o resumo + as tabelas
# do topo precisam refletir. As seções chamadas aqui NÃO são fragments próprias
# (seria aninhamento desnecessário) — o fragment é este pai.
@st.fragment
def _area_trabalho():
    _modo = st.segmented_control(
        "Modo de trabalho",
        ["✏️ Editar um pedido", "⚙️ Ação em lote"],
        default="✏️ Editar um pedido",
        label_visibility="collapsed",
        key="pc_modo",
    )
    if _modo == "⚙️ Ação em lote":
        _secao_lote()
    else:
        _secao_pedido()


_area_trabalho()
