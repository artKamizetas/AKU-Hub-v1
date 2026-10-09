"""
Página: Pedidos de Compra (Admin Only)

Rodadas congeladas do Simulador de Produção → pedidos de compra em rascunho
por Colégio × Super Categoria → revisão/edição de quantidades → PRONTO →
emissão em DOIS momentos: compra no Bling (AK Uniformes), depois venda no
Olist (Art Kamizetas). Depois de emitido o pedido ainda pode ser ALTERADO ou
CANCELADO — sobrepondo os dois ERPs, enquanto estiver em aberto neles.
Conexão/chaves das integrações ficam na aba Integrações de Configurações.

Três níveis: rodadas congeladas → pedidos da rodada → itens do pedido.
Leituras do schema `app` sem st.cache_data (cache confundiria o pós-escrita)
— exceto o resultado por SKU do snapshot, que é imutável (ver
_resultado_skus_rodada). O que se repete entre reruns do FRAGMENT (itens do
pedido aberto, prontidão do Olist) é lido uma vez por rerun de app (ver _memo).
"""

import streamlit as st
from auth import exigir_admin

import pandas as pd

from pedidos import builder, catalogo, estados, emissor, grade, revisoes
from pedidos.repositorio import (
    obter_repositorio, TransicaoInvalida, PedidoNaoEditavel,
    ItemJaExiste, ItemNaoRemovivel, MigracaoPendente,
)
from pedidos.integracoes.repositorio import obter_repositorio_integracoes
from ui_carga import carregar_com_feedback
from ui_tabelas import (
    FILA, MEMORIA, EDITOR, exibir, padrao_tabela, destacar, num, brl,
    col_sku, col_produto, col_colegio, col_tamanho, col_texto, col_pecas, col_moeda,
)

# Gate de admin (login + role numa chamada). `usuario` é o e-mail: alimenta as
# colunas de auditoria de toda escrita desta página.
_nome, usuario, role = exigir_admin()


BADGES_RODADA = {
    estados.RODADA_CONGELANDO: "⚠️ Incompleta",
    estados.RODADA_ABERTA: "📂 Aberta",
    estados.RODADA_CANCELADA: "🚫 Cancelada",
}


def _flash(nivel: str, texto: str):
    """Mensagem que sobrevive ao st.rerun() pós-ação."""
    st.session_state["pc_pagina_msg"] = (nivel, texto)


def _fmt_brl(x: float) -> str:
    return brl(x, 2)


def _memo(chave, ler):
    """
    Leitura do Supabase feita UMA vez por rerun de app e reaproveitada nos
    reruns do fragment. Digitar uma célula, trocar Lista/Grade ou abrir um
    toggle rerroda o fragment: sem isto cada tecla relia os itens do pedido (e,
    em pedido Pronto, a integração do Olist) — ~0,3 s por ida ao banco.

    Não é cache entre escritas: a bolsa é zerada no topo da página, que roda
    em todo st.rerun() de app. Falha não é guardada (a exceção sobe e a
    próxima execução tenta de novo).
    """
    bolsa = st.session_state.setdefault("pc_memo", {})
    if chave not in bolsa:
        bolsa[chave] = ler()
    return bolsa[chave]


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


# Mesmo vocabulário da Sugestão por SKU do Simulador — é a mesma conta.
_MEMORIA_CONFIG = {
    "SKU": col_sku(),
    "Situação": col_texto("Situação"),
    "Vendas hist. (alta)": col_pecas("Vendas Alta", casas=1),
    "Demanda período": col_pecas("Demanda do período", casas=1, largura=None),
    "…alta": col_pecas("· na alta", casas=1),
    "…baixa": col_pecas("· na baixa", casas=1),
    "Estoque segurança": col_pecas("Segurança", casas=1),
    "Estoque-alvo": col_pecas("Alvo", casas=1),
    "Estoque rede": col_pecas("Est. Rede", casas=1),
    "Backlog": col_pecas("Backlog", casas=1),
    "Projetado na chegada": col_pecas("Est. Projetado", casas=1, largura=None),
    "Nível serviço": col_texto("NS", largura="small"),
    "Qtd sugerida": col_pecas("Sugerida (pçs)", largura=None),
}

_SITUACAO_NO_PEDIDO = "No pedido"


@st.cache_data(ttl=3600, max_entries=4, show_spinner="Lendo o snapshot da rodada…")
def _resultado_skus_rodada(rodada_id: str) -> list:
    """
    Resultado por SKU do snapshot (rede inteira). É a ÚNICA leitura do schema
    `app` cacheada nesta página: o snapshot é imutável, então não existe
    pós-escrita para o cache confundir — e sem ele cada rerun do fragment com o
    toggle ligado baixaria o jsonb inteiro de novo.
    """
    return obter_repositorio().obter_resultado_skus(rodada_id)


def _linha_memoria(sku: str, tamanho, mem: dict, qtd_sugerida: int,
                   situacao: str = None) -> dict:
    linha = {"SKU": sku}
    if situacao is not None:
        linha["Situação"] = situacao
    for chave, rotulo in _MEMORIA_COLS.items():
        valor = mem.get(chave)
        linha[rotulo] = round(valor, 1) if isinstance(valor, (int, float)) else None
    ns = mem.get("nivel_servico")   # já em pontos percentuais (99, 92…)
    linha["Nível serviço"] = f"{ns:.0f}%" if isinstance(ns, (int, float)) else "—"
    linha["Qtd sugerida"] = int(qtd_sugerida)
    # ordem da grade (família, depois tamanho pela regra da confecção)
    linha["_ordem"] = (grade.familia(sku),
                       grade.chave_tamanho(grade.tamanho_efetivo(tamanho, sku)))
    return linha


def _memoria_sugestao(itens: pd.DataFrame, pedido_sel) -> None:
    """
    Painel read-only 'por que essa quantidade': os drivers congelados da
    sugestão (memoria_sugerida por item), ao lado do SKU e da qtd sugerida —
    sem carregar o resultado_skus pesado da rodada. Degrada com aviso quando
    a rodada foi congelada antes desta memória existir (jsonb vazio).

    Atrás de um toggle, completa a FAMÍLIA com o que ficou fora do pedido: os
    SKUs do mesmo Colégio × SuperCategoria com sugestão 0 só existem no
    snapshot da rodada (não viram item), e é ele que é lido nesse caso.
    """
    with st.expander("Por que essas quantidades? (memória de cálculo)",
                     icon=":material/calculate:"):
        if "memoria_sugerida" not in itens.columns:
            st.info("Rodada congelada antes desta versão — memória indisponível.")
            return

        st.caption(
            "Política **order-up-to**: `Demanda período + Estoque segurança = "
            "Estoque-alvo`; subtrai-se o estoque projetado na chegada "
            "(`Estoque rede − Backlog`, consumido até a rodada chegar) → **Qtd "
            "sugerida**. Valores congelados no momento do cálculo."
        )
        ver_fora = st.toggle(
            "Mostrar também o que ficou fora do pedido",
            key=f"mem_fora_{pedido_sel['id']}",
            help="Os outros tamanhos e modelos deste Colégio × Super Categoria "
                 "que o cálculo zerou (cobertos pelo estoque ou sem demanda). "
                 "Lidos do snapshot da rodada.",
        )

        linhas = []
        for _, it in itens.iterrows():
            mem = it["memoria_sugerida"] or {}
            if not isinstance(mem, dict) or not mem:
                continue
            linhas.append(_linha_memoria(
                it["sku"], it["tamanho"], mem, it["quantidade_sugerida"],
                _SITUACAO_NO_PEDIDO if ver_fora else None))

        if ver_fora:
            grupo = builder.memoria_do_grupo(
                _resultado_skus_rodada(pedido_sel["rodada_id"]),
                pedido_sel["colegio"], pedido_sel["super_categoria"])
            ja_listados = {linha["SKU"] for linha in linhas}
            manuais = set(itens.loc[itens["origem"] == estados.ORIGEM_MANUAL, "sku"])
            fora = [g for g in grupo
                    if g["sku"] not in ja_listados and g["sku"] not in manuais]
            for g in grupo:
                if g["sku"] in ja_listados:
                    continue
                # item incluído à mão: o snapshot explica por que o motor o zerou
                situacao = (f"Incluído à mão · {g['motivo'].lower()}"
                            if g["sku"] in manuais else g["motivo"])
                linhas.append(_linha_memoria(
                    g["sku"], g["tamanho"], g["memoria"],
                    g["quantidade_sugerida"], situacao))

            rotulo_grupo = f"{pedido_sel['colegio']} · {pedido_sel['super_categoria']}"
            if not grupo:
                st.info("O snapshot desta rodada não traz o resultado por SKU — "
                        "só é possível mostrar os itens do pedido.")
            elif not fora:
                st.caption(f"Nenhum outro SKU de **{rotulo_grupo}** nesta rodada — "
                           "tudo o que o cálculo avaliou está no pedido.")
            else:
                n_coberto = sum(g["motivo"] == builder.MOTIVO_COBERTO for g in fora)
                n_sem = sum(g["motivo"] == builder.MOTIVO_SEM_DEMANDA for g in fora)
                st.caption(
                    f"**{len(fora)}** SKU(s) de **{rotulo_grupo}** ficaram fora do "
                    f"pedido: **{n_coberto}** coberto(s) pelo estoque · "
                    f"**{n_sem}** sem demanda no período."
                )

        if not linhas:
            st.info(
                "Esta rodada foi congelada antes da memória de cálculo por item "
                "existir — abra o snapshot da rodada para conferir."
            )
            return

        linhas.sort(key=lambda linha: linha["_ordem"])
        exibir(pd.DataFrame(linhas).drop(columns="_ordem"), MEMORIA, _MEMORIA_CONFIG)


# =================================================================
# Itens do pedido — duas visões da MESMA tabela + inclusão manual
# =================================================================
VISAO_LISTA, VISAO_GRADE = "Lista", "Grade por tamanho"
_ROTULO_ORIGEM = {estados.ORIGEM_SIMULACAO: "Simulação", estados.ORIGEM_MANUAL: "Manual"}


def _rev() -> int:
    """
    Revisão dos editores: entra na `key` de todo data_editor de itens. Subir a
    revisão descarta as edições pendentes — obrigatório depois de incluir ou
    remover item, porque o editor guarda a edição por POSIÇÃO da linha e, com a
    tabela mudando de tamanho, ela reapareceria em outro SKU.
    """
    return st.session_state.get("pc_editor_rev", 0)


def _nova_rev() -> None:
    st.session_state["pc_editor_rev"] = _rev() + 1


def _chave_editor(visao: str, pedido_id: str) -> str:
    prefixo = "grade" if visao == VISAO_GRADE else "editor"
    return f"{prefixo}_{pedido_id}_{_rev()}"


def _editor_sujo(chave: str) -> bool:
    estado = st.session_state.get(chave)
    return bool(isinstance(estado, dict) and estado.get("edited_rows"))


def _edicoes_pendentes(visao: str, pedido_id: str) -> dict:
    """
    O `edited_rows` do editor ANTES de ele ser desenhado: o destaque do que foi
    alterado é pintado na própria tabela, então precisa saber o que foi digitado
    antes de ela existir neste rerun.
    """
    estado = st.session_state.get(_chave_editor(visao, pedido_id))
    return (estado.get("edited_rows") or {}) if isinstance(estado, dict) else {}


def _ao_trocar_visao(pedido_id: str) -> None:
    """
    Callback do seletor Lista/Grade. As duas visões são editores diferentes:
    trocar com quantidade digitada e não salva a perderia em silêncio. Aqui a
    troca é DESFEITA e a tela avisa — quem decide descartar é o usuário.
    """
    anterior = st.session_state.get("pc_visao_ativa", VISAO_LISTA)
    nova = st.session_state.get("pc_visao")
    if nova is None:   # clicar na opção já marcada a desmarca — mantém a atual
        st.session_state["pc_visao"] = anterior
        return
    if nova != anterior and _editor_sujo(_chave_editor(anterior, pedido_id)):
        st.session_state["pc_visao"] = anterior
        st.session_state["pc_visao_bloqueada"] = nova
        return
    st.session_state["pc_visao_ativa"] = nova


def _descartar_e_trocar(visao: str) -> None:
    _nova_rev()
    st.session_state["pc_visao"] = visao
    st.session_state["pc_visao_ativa"] = visao


# --- Navegação entre pedidos (seletor + anterior/próximo) ---
# Mesma regra da troca de visão: mudar de pedido com quantidade digitada e não
# salva a perderia em silêncio (o editor do pedido anterior sai da tela e o
# Streamlit descarta o estado dele). A troca é DESFEITA e a tela avisa.
def _pedido_sujo(pedido_id) -> bool:
    visao = st.session_state.get("pc_visao_ativa", VISAO_LISTA)
    return bool(pedido_id) and _editor_sujo(_chave_editor(visao, pedido_id))


def _ao_trocar_pedido(chave_sel: str) -> None:
    """Callback do selectbox de pedido."""
    anterior = st.session_state.get("pc_pedido_ativo")
    novo = st.session_state.get(chave_sel)
    if anterior and novo != anterior and _pedido_sujo(anterior):
        st.session_state[chave_sel] = anterior
        st.session_state["pc_pedido_bloqueado"] = novo


def _ir_para_pedido(chave_sel: str, destino: str) -> None:
    """Callback dos botões anterior/próximo."""
    if _pedido_sujo(st.session_state.get(chave_sel)):
        st.session_state["pc_pedido_bloqueado"] = destino
        return
    st.session_state[chave_sel] = destino


def _descartar_e_ir(chave_sel: str, destino: str) -> None:
    _nova_rev()
    st.session_state[chave_sel] = destino


def _assinatura_filtro(rodada_id: str) -> str:
    """Texto estável do filtro em vigor (entra na key da tabela de lote)."""
    partes = [st.session_state.get(f"pc_f_{campo}_{rodada_id}") or []
              for campo in ("col", "sup", "sta")]
    return "|".join(",".join(sorted(map(str, parte))) for parte in partes)


def _filtrar_pedidos(pedidos: pd.DataFrame, rodada_id: str) -> pd.DataFrame:
    """
    Filtros de Colégio, Super Categoria e Status — valem para os DOIS modos
    (a fila de um pedido por vez e a tabela de lote). Vazio = sem filtro.
    As chaves levam a rodada: trocar de rodada não herda o filtro da anterior.
    """
    contagem = pedidos["status"].value_counts()
    presentes = [s for s in estados.ROTULOS_BADGE if s in contagem.index]
    f_col, f_sup, f_sta = st.columns([2, 2, 3], vertical_alignment="top")
    colegios = f_col.multiselect(
        "Colégio", sorted(pedidos["colegio"].dropna().unique()),
        placeholder="Todos os colégios", key=f"pc_f_col_{rodada_id}")
    supers = f_sup.multiselect(
        "Super categoria", sorted(pedidos["super_categoria"].dropna().unique()),
        placeholder="Todas as categorias", key=f"pc_f_sup_{rodada_id}")
    status = f_sta.pills(
        "Status", presentes, selection_mode="multi",
        format_func=lambda s: f"{estados.ROTULOS_BADGE[s]} ({contagem[s]})",
        key=f"pc_f_sta_{rodada_id}")

    filtrado = pedidos
    if colegios:
        filtrado = filtrado[filtrado["colegio"].isin(colegios)]
    if supers:
        filtrado = filtrado[filtrado["super_categoria"].isin(supers)]
    if status:
        filtrado = filtrado[filtrado["status"].isin(status)]
    return filtrado.reset_index(drop=True)


def _catalogo() -> pd.DataFrame:
    """Produtos ativos para a inclusão manual (carga do loader: cache de 1h)."""
    dados, _ = carregar_com_feedback()
    return catalogo.montar_catalogo(dados["produtos"], dados["detalhes"])


def _adicionar_produto(pedido_sel, itens: pd.DataFrame, pendente: bool) -> None:
    """
    Inclui no rascunho um produto que a simulação não trouxe: escolhe o
    produto e digita a grade de tamanhos inteira de uma vez. Atrás de um
    toggle para o catálogo só ser carregado quando alguém vai mesmo incluir.
    """
    pedido_id = pedido_sel["id"]
    if not st.toggle(":material/add: Adicionar produto que não veio da simulação",
                     key=f"add_on_{pedido_id}"):
        return

    with st.container(border=True):
        cat = _catalogo()
        todos = st.checkbox(
            "Mostrar produtos de outros colégios e categorias",
            key=f"add_todos_{pedido_id}",
            help=f"Por padrão aparecem só os produtos de {pedido_sel['colegio']} · "
                 f"{pedido_sel['super_categoria']} — é o que o título do pedido "
                 "promete no Bling.",
        )
        base = cat if todos else catalogo.filtrar_escopo(
            cat, pedido_sel["colegio"], pedido_sel["super_categoria"])
        familias = catalogo.listar_familias(base)
        if len(familias) == 0:
            st.info(f"Nenhum produto ativo em {pedido_sel['colegio']} · "
                    f"{pedido_sel['super_categoria']}. Marque a opção acima para "
                    "buscar em todo o catálogo.")
            return

        nomes = dict(zip(familias["familia"], familias["produto_pai"]))
        fam = st.selectbox(
            "Produto", options=list(nomes), index=None,
            format_func=lambda f: f"{f} — {nomes[f]}",
            placeholder="Busque pelo SKU ou pelo nome do produto",
            key=f"add_fam_{pedido_id}_{int(todos)}",
        )
        if fam is None:
            return

        membros = catalogo.tamanhos_da_familia(cat, fam)
        no_pedido = set(itens["sku"])
        livres = membros[~membros["sku"].isin(no_pedido)]
        ja = membros[membros["sku"].isin(no_pedido)]
        if len(ja):
            st.caption("Já no pedido (ajuste na tabela acima): "
                       + ", ".join(ja["tamanho_grade"]))
        if len(livres) == 0:
            st.info("Todos os tamanhos deste produto já estão no pedido — "
                    "ajuste as quantidades na tabela acima.")
            return

        fora = membros.iloc[0]
        if (fora["colegio"], fora["super_categoria"]) != (
                pedido_sel["colegio"], pedido_sel["super_categoria"]):
            st.warning(
                f"Este produto é de **{fora['colegio']} · {fora['super_categoria']}**. "
                f"O pedido continua saindo no Bling como `{pedido_sel['titulo']}`.")

        tamanhos = list(livres["tamanho_grade"])
        linha = pd.DataFrame([{t: None for t in tamanhos}]).astype("Int64")
        editada = st.data_editor(
            linha, key=f"add_grade_{pedido_id}_{fam}_{_rev()}",
            **padrao_tabela(EDITOR, 1), num_rows="fixed",
            column_config={t: st.column_config.NumberColumn(t, min_value=0, step=1)
                           for t in tamanhos},
        )
        qtds = {sku: grade._qtd(editada.iloc[0][t])
                for sku, t in zip(livres["sku"], tamanhos)}
        novos = catalogo.montar_itens_manuais(livres, qtds)
        pecas = sum(i["quantidade_final"] for i in novos)
        valor = sum(i["quantidade_final"] * i["custo_unit"] for i in novos)

        c_txt, c_btn = st.columns([3, 1], vertical_alignment="center")
        with c_txt:
            if pendente:
                st.caption(":orange[:material/warning:] Salve as quantidades editadas na tabela antes de "
                           "incluir — a inclusão recarrega o pedido.")
            elif novos:
                st.caption(f"{len(novos)} tamanho(s) · {pecas} peça(s) · {_fmt_brl(valor)} "
                           "— entram com **sugerido 0** e origem **Manual**.")
            else:
                st.caption("Digite a quantidade nos tamanhos que quer incluir.")
        with c_btn:
            if st.button("Adicionar ao pedido", width="stretch",
                         disabled=pendente or not novos, key=f"add_btn_{pedido_id}"):
                try:
                    n = repo.adicionar_itens(pedido_id, novos, usuario)
                    _nova_rev()
                    _flash("success", f"**{n}** item(ns) de `{fam}` incluído(s) no pedido.")
                except (PedidoNaoEditavel, ItemJaExiste) as exc:
                    _flash("warning", str(exc))
                except MigracaoPendente as exc:
                    _flash("error", str(exc))
                st.rerun()


def _remover_manuais(pedido_id: str, itens: pd.DataFrame, pendente: bool,
                     ja_emitidos=frozenset()) -> None:
    """
    Remoção de item incluído à mão. Item da simulação não aparece aqui: zera-se.
    Numa alteração pós-emissão, o manual que já foi aos ERPs (`ja_emitidos`,
    ids da linha de base) também só se zera — sai só o incluído agora.
    """
    manuais = itens[(itens["origem"] == estados.ORIGEM_MANUAL)
                    & ~itens["id"].isin(list(ja_emitidos))]
    if len(manuais) == 0:
        return
    with st.popover(f"Remover item manual ({len(manuais)})", icon=":material/delete:",
                    width="stretch"):
        st.caption("Só itens incluídos à mão saem do pedido. Os da simulação ficam "
                   "como registro — para não comprar, zere a quantidade final.")
        rotulo = {r["id"]: f"{r['sku']} · {int(r['quantidade_final'])} pç"
                  for _, r in manuais.iterrows()}
        alvo = st.multiselect("Itens incluídos à mão", options=list(rotulo),
                              format_func=rotulo.get, key=f"rem_sel_{pedido_id}_{_rev()}")
        if pendente:
            st.caption(":orange[:material/warning:] Salve as quantidades editadas antes de remover.")
        if st.button("Remover do pedido", disabled=pendente or not alvo,
                     key=f"rem_btn_{pedido_id}"):
            try:
                n = repo.remover_itens_manuais(pedido_id, alvo, usuario)
                _nova_rev()
                _flash("success", f"**{n}** item(ns) manual(is) removido(s).")
            except (PedidoNaoEditavel, ItemNaoRemovivel) as exc:
                _flash("warning", str(exc))
            st.rerun()


# =================================================================
# PÓS-EMISSÃO — alterar / cancelar um pedido que já está nos ERPs
# =================================================================
def _chave_divergencia(pedido_id: str) -> str:
    """Onde fica guardada a edição manual que o último envio encontrou no ERP."""
    return f"pc_div_{pedido_id}"


def _texto_situacao_erps(pedido: dict, erps: dict) -> str:
    """'Bling nº 433: Em aberto · Olist nº 1021: Aberta', com o ícone do veredito."""
    partes = []
    for erp, nome, aberto in (
            ("bling", "Bling", lambda e: e["situacao_valor"] == emissor.bling.SITUACAO_EM_ABERTO),
            ("olist", "Olist", lambda e: e["situacao"] == emissor.olist.SITUACAO_ABERTA)):
        lido = erps.get(erp)
        if lido is None:
            continue
        icone = (":green[:material/lock_open:]" if aberto(lido)
                 else ":orange[:material/lock:]")
        partes.append(f"{icone} {nome} nº {pedido.get(f'{erp}_numero') or '?'}: "
                      f"**{lido['situacao_rotulo']}**")
    return " · ".join(partes)


def _acoes_pos_emissao(pedido_sel) -> None:
    """
    Alterar / cancelar depois da emissão. O que LIBERA é o status nativo de
    cada ERP (compra Em aberto no Bling, venda Aberta no Olist) — por isso a
    1ª ação é ler os ERPs, sob demanda: são chamadas de API (o Olist tem
    limite de 60/min) e não cabem em todo rerun. O resultado mora na bolsa do
    _memo: vale até a próxima ação, quando a situação pode ter mudado. O envio
    confere de novo de qualquer forma.
    """
    pedido_id = pedido_sel["id"]
    pedido = pedido_sel.to_dict()
    bolsa = st.session_state.setdefault("pc_memo", {})
    chave = ("erps", pedido_id)

    st.caption("**Depois da emissão** — alterar ou cancelar só enquanto o pedido "
               "está em aberto nos ERPs.")
    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("Verificar nos ERPs", icon=":material/sync:", width="stretch",
                     key=f"verif_{pedido_id}"):
            try:
                with st.spinner("Lendo a situação no Bling e no Olist…"):
                    bolsa[chave] = {"erps": emissor.consultar_erps(
                        pedido, obter_repositorio_integracoes())}
            except Exception as exc:
                bolsa[chave] = {"erro": str(exc)}

    verificado = bolsa.get(chave) or {}
    erps = verificado.get("erps")
    impedimentos, acoes_canc, imp_canc = [], {}, []
    if erps:
        impedimentos = emissor.avaliar_abertura(pedido, erps["bling"], erps["olist"])
        acoes_canc, imp_canc = emissor.planejar_cancelamento(
            pedido, erps["bling"], erps["olist"])
    dica = None if erps else "Verifique a situação nos ERPs primeiro."

    with c2:
        if st.button("Alterar pedido", icon=":material/edit:", width="stretch",
                     disabled=not erps or bool(impedimentos), help=dica,
                     key=f"alterar_{pedido_id}"):
            try:
                ok = repo.abrir_alteracao(pedido_id, usuario)
                _nova_rev()
                _flash("success", "Alteração aberta — edite as quantidades e envie "
                                  "aos ERPs.") if ok else _flash(
                    "warning", "O pedido mudou de estado em outra sessão — recarregado.")
            except (MigracaoPendente, TransicaoInvalida) as exc:
                _flash("error", str(exc))
            st.rerun()
    with c3:
        with st.popover("Cancelar pedido emitido", icon=":material/block:",
                        width="stretch", disabled=not erps or bool(imp_canc), help=dica):
            st.caption(
                "Cancela a venda no Olist e a compra no Bling — muda a situação, os "
                "pedidos ficam lá como registro. **Não tem volta**: este grupo só "
                "volta a ser comprado numa próxima rodada.")
            faltas = []
            if acoes_canc.get("bling") == "cancelar":
                faltas = emissor.validar_pre_cancelamento(_memo(
                    "cfg_bling",
                    lambda: (obter_repositorio_integracoes().ler("bling") or {})
                    .get("config") or {}))
            for falta in faltas:
                st.warning(falta, icon=":material/warning:")
            ja_feito = [nome for erp, nome in (("bling", "Bling"), ("olist", "Olist"))
                        if acoes_canc.get(erp) == "feito"]
            if ja_feito:
                st.caption(f"Já está cancelado no {' e no '.join(ja_feito)} — "
                           "lá não será feito nada.")
            motivo = st.text_input("Motivo do cancelamento", key=f"canc_motivo_{pedido_id}")
            if st.button("Confirmar cancelamento nos ERPs", type="primary",
                         disabled=bool(faltas) or not motivo.strip(),
                         key=f"canc_emitido_{pedido_id}"):
                with st.status("Cancelando nos ERPs…", expanded=True) as _s:
                    try:
                        emissor.cancelar_emitido(
                            pedido_id, motivo, usuario, repo,
                            obter_repositorio_integracoes(), progresso=_s.write)
                        _s.update(label="Pedido cancelado nos ERPs", state="complete")
                        _flash("success", "Pedido cancelado no Olist e no Bling.")
                    except emissor.EmissaoFalhou as exc:
                        _s.update(label="Cancelamento não concluído", state="error")
                        _flash("error", f"Cancelamento não concluído: {exc}")
                st.rerun()

    if verificado.get("erro"):
        st.warning(f"Não foi possível ler os ERPs agora: {verificado['erro']}",
                   icon=":material/cloud_off:")
    elif erps:
        st.caption(_texto_situacao_erps(pedido, erps))
        for impedimento in impedimentos:
            st.warning(impedimento, icon=":material/lock:")


def _tabela_divergencias(divergencias: list, itens: pd.DataFrame) -> None:
    """
    Edição feita direto no ERP: o que nós enviamos × o que cada ERP tem agora ×
    como vai ficar depois do envio. Só as linhas que divergem; a célula do ERP
    que diverge vai pintada. Int64 (e não float) para a célula sem valor ficar
    vazia e o número não ganhar casas decimais no Styler.
    """
    por_sku = itens.drop_duplicates("sku").set_index("sku")
    linhas, marcas = [], []
    for pos, d in enumerate(divergencias):
        no_pedido = d["sku"] in por_sku.index
        linhas.append({
            "sku": d["sku"],
            "tamanho": str(por_sku.loc[d["sku"], "tamanho"]) if no_pedido else "",
            "enviado": d["enviado"], "bling": d["bling"], "olist": d["olist"],
            "vai_ficar": int(por_sku.loc[d["sku"], "quantidade_final"]) if no_pedido else 0,
        })
        marcas += [(pos, erp) for erp in ("bling", "olist") if d[f"diverge_{erp}"]]
    df = pd.DataFrame(linhas)
    df["tamanho"] = df["tamanho"].replace("nan", "")
    for coluna in ("enviado", "bling", "olist", "vai_ficar"):
        df[coluna] = pd.array(df[coluna], dtype="Int64")
    exibir(destacar(df, marcas), MEMORIA, {
        "sku": col_sku(),
        "tamanho": col_tamanho(),
        "enviado": col_pecas("Enviado por nós", largura=None,
                             ajuda="O que o AKU-Hub enviou por último ao ERP"),
        "bling": col_pecas("No Bling", largura=None),
        "olist": col_pecas("No Olist", largura=None),
        "vai_ficar": col_pecas("Vai ficar", largura=None,
                               ajuda="A quantidade desta tela — é a que substitui a do ERP"),
    })
    st.caption("0 no ERP = a linha foi removida lá · enviado 0 = incluída direto no ERP · "
               "vai ficar 0 = sai do pedido no envio.")


def _secao_enviar_alteracao(pedido_sel, itens: pd.DataFrame, historico: list,
                            pendente: bool) -> None:
    """
    Conferência + envio da alteração. Mostra SÓ o que muda em relação ao que
    está nos ERPs (a linha de base) — é o que o gestor assina ao enviar.
    """
    pedido_id = pedido_sel["id"]
    base = revisoes.linha_de_base(historico)
    parcial = revisoes.envio_parcial(historico)
    diferencas = revisoes.diferencas(itens, base)

    st.markdown("**Enviar alteração aos ERPs**")
    if not diferencas and not parcial:
        st.caption("Nenhuma quantidade difere do que está emitido — edite a tabela "
                   "acima ou descarte a alteração.")
        return

    if diferencas:
        df = pd.DataFrame(diferencas)
        df["delta"] = df["nova"] - df["emitida"]
        df["delta_valor"] = df["delta"] * df["custo_unit"]
        df["tamanho"] = df["tamanho"].replace("nan", "")
        exibir(
            df[["sku", "produto", "tamanho", "emitida", "nova", "delta", "delta_valor"]],
            MEMORIA,
            {
                "sku": col_sku(),
                "produto": col_produto(),
                "tamanho": col_tamanho(),
                "emitida": col_pecas("Emitida (pçs)", largura=None,
                                     ajuda="O que está nos ERPs agora"),
                "nova": col_pecas("Nova (pçs)", largura=None),
                "delta": st.column_config.NumberColumn("Δ pçs", format="%+d", width="small"),
                "delta_valor": col_moeda("Δ valor", casas=2),
            })
        _dq, _dv = int(df["delta"].sum()), float(df["delta_valor"].sum())
        st.caption(f"**{len(df)}** item(ns) mudam · **{_dq:+d}** peças · "
                   f"**{'+' if _dv >= 0 else '−'}{_fmt_brl(abs(_dv))}** no pedido.")

    divergencias = st.session_state.get(_chave_divergencia(pedido_id))
    sobrepor = False
    if divergencias:
        st.warning(
            "O pedido foi **alterado direto no ERP** depois do nosso último envio. "
            "Enviar agora substitui o que está lá pelo que está nesta tela.",
            icon=":material/warning:")
        _tabela_divergencias(divergencias, itens)
        sobrepor = st.checkbox("Sobrepor o que foi alterado direto no ERP",
                               key=f"pc_sobrepor_{pedido_id}")

    motivo = st.text_input("Motivo da alteração", key=f"alt_motivo_{pedido_id}",
                           placeholder="Ex.: colégio reduziu a turma do 6º ano")
    if pendente:
        impede = "Salve as quantidades editadas antes de enviar."
    elif not motivo.strip():
        impede = "Informe o motivo da alteração."
    elif divergencias and not sobrepor:
        impede = "Confirme a sobreposição do que foi alterado direto no ERP."
    else:
        impede = None

    if st.button("Enviar alteração", icon=":material/send:", disabled=bool(impede),
                 help=impede, type="secondary" if pendente else "primary",
                 key=f"enviar_alt_{pedido_id}"):
        with st.status("Enviando alteração aos ERPs…", expanded=True) as _s:
            try:
                res = emissor.enviar_alteracao(
                    pedido_id, motivo, usuario, repo, obter_repositorio_integracoes(),
                    sobrepor=sobrepor, progresso=_s.write)
                st.session_state.pop(_chave_divergencia(pedido_id), None)
                _nova_rev()
                _s.update(label="Alteração enviada", state="complete")
                _alertas = (" Avisos do Bling: " + " · ".join(res["alertas"])
                            if res.get("alertas") else "")
                _flash("warning" if _alertas else "success",
                       "Alteração enviada ao Olist e ao Bling." + _alertas)
            except emissor.DivergenciaNoErp as exc:
                st.session_state[_chave_divergencia(pedido_id)] = exc.divergencias
                _s.update(label="Pedido alterado direto no ERP — confira", state="error")
                _flash("warning", str(exc))
            except emissor.EmissaoFalhou as exc:
                _s.update(label="Alteração não enviada", state="error")
                _flash("error", f"Alteração não enviada: {exc}")
        st.rerun()


st.title(":material/receipt_long: Pedidos de Compra")
st.caption(
    "Rodadas congeladas do Simulador de Produção, divididas em pedidos por "
    "**Colégio × Super Categoria**. Edite as quantidades no rascunho, marque "
    "como Pronto e emita a compra no Bling e a venda no Olist."
)

_msg = st.session_state.pop("pc_pagina_msg", None)
if _msg:
    # Sucesso é confirmação passageira → toast (aparece onde o usuário estiver
    # na rolagem; a mensagem fixa nascia no topo, longe do botão clicado).
    # Aviso e erro pedem leitura e ação: continuam fixos.
    if _msg[0] == "success":
        st.toast(_msg[1], icon=":material/check_circle:")
    else:
        getattr(st, _msg[0])(_msg[1])

repo = obter_repositorio()

# Leituras reaproveitadas ENTRE reruns do fragment (ver _memo). Esta linha só
# roda em rerun de APP — que é o que toda ação de escrita dispara —, então o
# que foi gravado é sempre relido.
st.session_state["pc_memo"] = {}

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
    st.page_link("pages/3_Fabrica.py", label="Abrir Simulador de Produção", icon=":material/factory:")
    st.stop()

with st.container(border=True):
    st.subheader("Rodadas congeladas")

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
    # O que a tabela de rodadas mostrava, agora só da rodada escolhida.
    st.caption(
        f"Congelada em **{pd.to_datetime(rodada_sel['congelada_em']):%d/%m/%Y %H:%M}** "
        f"por {rodada_sel['congelada_por']} · crescimento aplicado: "
        f"**{'sim' if rodada_sel['ativo_crescimento'] else 'não'}**"
    )

    # Congelamento abortado (falha no meio da gravação) → só limpar
    if rodada_sel["status"] == estados.RODADA_CONGELANDO:
        st.warning(
            "Este congelamento ficou **incompleto** (falha no meio da gravação). "
            "Limpe-o e congele a rodada de novo no Simulador."
        )
        if st.button("Limpar congelamento incompleto", icon=":material/cleaning_services:"):
            try:
                repo.limpar_congelamento_abortado(rodada_id)
                _flash("success", "Congelamento incompleto removido.")
            except TransicaoInvalida as exc:
                _flash("error", str(exc))
            st.rerun()
        st.stop()

    # Snapshot p/ conferência (jsonb pesado — só carrega sob demanda)
    with st.expander("Snapshot da rodada (conferência)", icon=":material/search:"):
        if st.toggle("Carregar snapshot", key=f"snap_{rodada_id}"):
            rodada_full = repo.obter_rodada(rodada_id)
            st.caption(
                f"Referência do cálculo: {rodada_full['data_referencia']} · "
                f"congelada em {pd.Timestamp(rodada_full['congelada_em']):%d/%m/%Y %H:%M} "
                f"por {rodada_full['congelada_por']}"
            )
            df_snap = pd.DataFrame(rodada_full["resultado_skus"])
            st.download_button(
                "Baixar resultado por SKU (CSV)", icon=":material/download:",
                data=df_snap.to_csv(index=False, sep=";", decimal=",").encode("utf-8"),
                file_name=f"snapshot_rodada_{rodada_full['mes_disparo']:02d}"
                          f"{rodada_full['ano_disparo']}.csv",
                mime="text/csv",
            )
            st.json(rodada_full["config_snapshot"], expanded=False)

    if rodada_sel["status"] == estados.RODADA_ABERTA:
        with st.popover("Cancelar rodada congelada", icon=":material/block:"):
            st.caption(
                "Cancela a rodada e todos os pedidos em rascunho — a rodada "
                "cancelada fica registrada e libera um novo congelamento. Só é "
                "possível se nenhum pedido saiu de RASCUNHO."
            )
            if st.button("Confirmar cancelamento da rodada", type="primary"):
                try:
                    repo.cancelar_rodada(rodada_id, usuario)
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
    # Pedido em alteração (ou com envio em curso) continua emitido nos dois ERPs
    _emitidos = pedidos.apply(
        lambda p: p["status"] == estados.EMITIDO
        or ((p["status"] == estados.EM_ALTERACAO or estados.enviando_alteracao(p["status"]))
            and estados.estado_emitido(p) == estados.EMITIDO), axis=1)
    _k4.metric("Emitidos", int(_emitidos.sum()))
    _k5.metric("Investimento (final)", _fmt_brl(float(pedidos["investimento_final"].sum())))

# =================================================================
# PEDIDO INDIVIDUAL — inspecionar / editar UM pedido. Fragmento próprio:
# trocar o pedido no selectbox rerroda SÓ esta caixa, não a tabela de ação
# em lote do outro modo. Os botões de ação usam st.rerun() (escopo app) porque
# mudam o estado do pedido e precisam refletir na tabela de lote + no flash.
# (Roda dentro de _area_trabalho — o @st.fragment é o do pai, não aqui.)
# =================================================================
def _secao_pedido(pedidos_f: pd.DataFrame):
    # --- Escolha do pedido: o seletor É o título do pedido (colégio ·
    # categoria · totais · status). O h3 com o título do Bling e a legenda de
    # status repetiam a mesma informação duas vezes logo abaixo. ---
    por_id = pedidos_f.set_index("id", drop=False)
    ids = list(por_id.index)
    chave_sel = f"pc_pedido_{rodada_id}"
    if st.session_state.get(chave_sel) not in ids:
        # 1ª vez, ou o pedido saiu do filtro (ex: filtro "Rascunho" e o
        # pedido acabou de virar Pronto): cai no que ocupa a MESMA posição,
        # e a fila anda sozinha para o próximo.
        st.session_state[chave_sel] = ids[
            min(st.session_state.get("pc_pedido_pos", 0), len(ids) - 1)]

    def _rotulo_pedido(pid):
        p = por_id.loc[pid]
        # Colégio na frente: a lista é ordenada por ele, e com o status
        # primeiro (textos de tamanhos diferentes) cada linha começava numa
        # posição. Status no fim — para achar por status existem os filtros.
        return (f"{p['colegio']} · {p['super_categoria']} — "
                f"{num(p['qtd_final'])} pç · {brl(p['investimento_final'])} · "
                f"{estados.ROTULOS_BADGE.get(p['status'], p['status'])}")

    # Rótulo visível, como os filtros acima: sem borda entre eles, é o que
    # separa "filtrar" de "escolher". Por isso o alinhamento é pela base.
    _ant, _sel, _prox, _pos = st.columns([0.5, 10, 0.5, 1.4], vertical_alignment="bottom")
    pedido_id = _sel.selectbox(
        "Pedido", options=ids, format_func=_rotulo_pedido, key=chave_sel,
        on_change=_ao_trocar_pedido, args=(chave_sel,),
    )
    posicao = ids.index(pedido_id)
    _ant.button("", icon=":material/chevron_left:", help="Pedido anterior",
                key="pc_ped_ant", width="stretch", disabled=posicao == 0,
                on_click=_ir_para_pedido,
                args=(chave_sel, ids[max(posicao - 1, 0)]))
    _prox.button("", icon=":material/chevron_right:", help="Próximo pedido",
                 key="pc_ped_prox", width="stretch", disabled=posicao == len(ids) - 1,
                 on_click=_ir_para_pedido,
                 args=(chave_sel, ids[min(posicao + 1, len(ids) - 1)]))
    _pos.caption(f"{posicao + 1} de {len(ids)}")
    st.session_state["pc_pedido_ativo"] = pedido_id
    st.session_state["pc_pedido_pos"] = posicao

    _ped_bloqueado = st.session_state.pop("pc_pedido_bloqueado", None)
    if _ped_bloqueado in ids:
        _av, _bt = st.columns([3, 1], vertical_alignment="center")
        _av.warning("Há quantidades editadas e **não salvas** neste pedido. "
                    "Salve antes de trocar — ou descarte.")
        _bt.button("Descartar e trocar", width="stretch", key="pc_ped_descartar",
                   on_click=_descartar_e_ir, args=(chave_sel, _ped_bloqueado))

    pedido_sel = por_id.loc[pedido_id]
    itens = _memo(("itens", pedido_id), lambda: repo.listar_itens(pedido_id))
    status = pedido_sel["status"]
    pode_editar = estados.editavel(status)

    # --- Pós-emissão: numa alteração a referência deixa de ser a sugestão e
    # passa a ser a LINHA DE BASE (o que está nos ERPs). As revisões só são
    # lidas nos estados que as usam. ---
    em_alteracao = status == estados.EM_ALTERACAO
    historico, _erro_revisoes = [], None
    if (em_alteracao or estados.enviando_alteracao(status)
            or (status == estados.CANCELADO and pedido_sel.get("bling_id"))):
        try:
            historico = _memo(("revisoes", pedido_id),
                              lambda: repo.listar_revisoes(pedido_id))
        except MigracaoPendente as exc:
            _erro_revisoes = str(exc)
    base = revisoes.linha_de_base(historico)
    qtd_base = revisoes.quantidades(base) if em_alteracao else None

    # --- Totais (preenchidos depois do editor, de onde saem) + visão ---
    st.session_state.setdefault("pc_visao", VISAO_LISTA)
    with st.container(horizontal=True, vertical_alignment="center"):   # idem título
        # Contêiner (e não st.empty): ocupa a largura que sobra e mantém o
        # seletor de visão parado na direita enquanto o texto muda de tamanho.
        _resumo = st.container()
        visao = st.segmented_control(
            "Visualização dos itens", [VISAO_LISTA, VISAO_GRADE],
            key="pc_visao", on_change=_ao_trocar_visao, args=(pedido_id,),
            label_visibility="collapsed", width="content",
        ) or VISAO_LISTA
    st.session_state["pc_visao_ativa"] = visao

    _bloqueada = st.session_state.pop("pc_visao_bloqueada", None)
    if _bloqueada:
        _av, _bt = st.columns([3, 1], vertical_alignment="center")
        _av.warning("Há quantidades editadas e **não salvas** nesta visão. "
                    "Salve antes de trocar — ou descarte.")
        _bt.button("Descartar e trocar", width="stretch",
                   on_click=_descartar_e_trocar, args=(_bloqueada,))

    tem_manual = bool((itens["origem"] == estados.ORIGEM_MANUAL).any())
    _novas = []   # células da grade sem item que receberam quantidade

    if visao == VISAO_GRADE:
        df_grade, celulas = grade.montar_grade(itens)
        tamanhos = [c for c in df_grade.columns
                    if c not in (grade.COL_SKU, grade.COL_PRODUTO)]
        # Alterado vs sugerido: a célula do tamanho (só pinta com o pedido
        # travado — coluna editável ignora estilo) e o SKU da linha (sempre).
        _divergentes = grade.divergencias(itens, grade.quantidades_por_item(
            grade.aplicar_edicoes(df_grade, _edicoes_pendentes(VISAO_GRADE, pedido_id)),
            celulas), base=qtd_base)
        _celula_do_item = {item_id: celula for celula, item_id in celulas.items()}
        _linha_pos = {sku: pos for pos, sku in enumerate(df_grade[grade.COL_SKU])}
        _marcas = []
        for _d in _divergentes:
            _linha, _tam = _celula_do_item[_d["id"]]
            _marcas += [(_linha_pos[_linha], _tam), (_linha_pos[_linha], grade.COL_SKU)]
        editado = st.data_editor(
            destacar(df_grade, _marcas),
            key=_chave_editor(VISAO_GRADE, pedido_id),
            **padrao_tabela(EDITOR, len(df_grade), max_linhas=18), num_rows="fixed",
            disabled=(True if not pode_editar
                      else [grade.COL_SKU, grade.COL_PRODUTO]),
            column_config={
                grade.COL_SKU: st.column_config.TextColumn("SKU", pinned=True),
                grade.COL_PRODUTO: st.column_config.TextColumn("Produto", width="large"),
                **{t: st.column_config.NumberColumn(t, min_value=0, step=1, width="small")
                   for t in tamanhos},
            },
        )
        _por_item = grade.quantidades_por_item(editado, celulas)
        _qtd_final = itens["id"].map(_por_item).fillna(0).astype(int)
        _alteracoes, _novas = grade.diff_grade(editado, itens, celulas)

        # Legenda só do que não é óbvio na tabela (que os números são a
        # quantidade final já está no resumo acima dela).
        _legenda = []
        if pode_editar:
            _legenda.append("Célula vazia = tamanho fora do pedido; digitar "
                            "nela **inclui** o tamanho ao salvar.")
        if tem_manual:
            _legenda.append("Incluídos à mão: " + ", ".join(
                itens.loc[itens["origem"] == estados.ORIGEM_MANUAL, "sku"]) + ".")
        if _legenda:
            st.caption(" ".join(_legenda))
        # Com o pedido editável a célula do tamanho não aceita cor: o SKU em
        # âmbar diz QUAL linha mudou, e esta legenda diz qual tamanho e quanto.
        if pode_editar and _divergentes:
            st.caption(
                f":orange[:material/edit:] **{len(_divergentes)}** quantidade(s) "
                f"diferente(s) {'do emitido' if em_alteracao else 'da sugestão'}: "
                + " · ".join(f"{d['sku']} {d['sugerida']}→{d['final']}"
                             for d in _divergentes[:8])
                + (f" · +{len(_divergentes) - 8}" if len(_divergentes) > 8 else ""))
        if _novas:
            st.info(f"**{len(_novas)}** tamanho(s) novo(s) serão incluídos ao salvar: "
                    + ", ".join(f"{n['sku_pai']}-{n['tamanho']} ({n['quantidade']})"
                                for n in _novas[:8]) + ("…" if len(_novas) > 8 else ""))
    else:
        cols_fixas = ["sku", "produto", "tamanho", "categoria"]
        if tem_manual:   # a coluna só aparece quando distingue alguma coisa
            cols_fixas.append("origem")
        cols_travadas = cols_fixas + ["quantidade_sugerida"]
        df_editor = itens[cols_travadas + ["quantidade_final"]].copy()
        if em_alteracao:   # o que está nos ERPs, ao lado do que vai passar a estar
            df_editor.insert(
                len(cols_travadas), "quantidade_emitida",
                itens["id"].map(qtd_base).fillna(0).astype(int).values)
            cols_travadas = cols_travadas + ["quantidade_emitida"]
        # Produto sem detalhe no cadastro chega com o TEXTO "nan" — vazio lê melhor.
        for _c in ("tamanho", "categoria"):
            df_editor[_c] = df_editor[_c].fillna("").astype(str).replace("nan", "")
        if tem_manual:
            df_editor["origem"] = df_editor["origem"].map(_ROTULO_ORIGEM)
        # Alterado vs referência: Sugerida (ou Emitida, na alteração) sempre,
        # e Final só pinta com o pedido travado — coluna editável ignora estilo.
        _previsto = grade.aplicar_edicoes(
            df_editor, _edicoes_pendentes(VISAO_LISTA, pedido_id))
        _divergentes = grade.divergencias(itens, dict(zip(
            itens["id"], pd.to_numeric(_previsto["quantidade_final"], errors="coerce"))),
            base=qtd_base)
        _pos_do_item = {item_id: pos for pos, item_id in enumerate(itens["id"])}
        _col_referencia = "quantidade_emitida" if em_alteracao else "quantidade_sugerida"
        _marcas = [(_pos_do_item[d["id"]], col) for d in _divergentes
                   for col in (_col_referencia, "quantidade_final")]
        editado = st.data_editor(
            destacar(df_editor, _marcas),
            key=_chave_editor(VISAO_LISTA, pedido_id),
            **padrao_tabela(EDITOR, len(df_editor), max_linhas=18), num_rows="fixed",
            disabled=(True if not pode_editar else cols_travadas),
            column_config={
                "sku": col_sku(),
                "produto": col_texto("Produto"),
                "tamanho": col_tamanho(),
                "categoria": col_texto("Categoria"),
                "origem": st.column_config.TextColumn(
                    "Origem", help="Manual = incluído pelo gestor, fora da simulação"),
                "quantidade_sugerida": st.column_config.NumberColumn(
                    "Sugerida (pçs)", help="Congelada no snapshot — imutável (auditoria)"),
                "quantidade_emitida": st.column_config.NumberColumn(
                    "Emitida (pçs)", help="O que está nos ERPs agora (última versão enviada)"),
                "quantidade_final": st.column_config.NumberColumn(
                    "Nova (pçs)" if em_alteracao else "Final (pçs)", min_value=0, step=1,
                    help=("Quantidade que vai SUBSTITUIR a emitida ao enviar a alteração"
                          if em_alteracao
                          else "Quantidade que será emitida — editável no rascunho")),
            },
        )
        _qtd_final = pd.to_numeric(editado["quantidade_final"], errors="coerce").fillna(0)
        # Diff editor × banco: só linhas alteradas (ordem preservada —
        # num_rows="fixed" mantém o alinhamento posicional com itens)
        _alteracoes = [
            {"id": iid, "quantidade_final": int(qf)}
            for iid, qf, q0 in zip(itens["id"], _qtd_final, itens["quantidade_final"])
            if int(qf) != int(q0)
        ]

    # --- Totais ao vivo, no lugar reservado ACIMA da tabela: é o veredito
    # do pedido e ficava em métricas grandes no fim da tela, depois de três
    # blocos recolhíveis. ---
    _delta = int(_qtd_final.sum() - itens["quantidade_sugerida"].sum())
    _invest = float((_qtd_final.values * itens["custo_unit"].values).sum())
    _n_pendentes = len(_alteracoes) + len(_novas)
    _pendente = _n_pendentes > 0
    _texto_resumo = (
        f"**{int((_qtd_final > 0).sum())}** SKUs · "
        f"**{num(int(_qtd_final.sum()))}** peças "
        f":gray[({_delta:+d} vs sugerido)] · **{_fmt_brl(_invest)}**")
    if em_alteracao:
        _dq_emit = int(_qtd_final.sum()) - sum(qtd_base.values())
        _dv_emit = _invest - sum(
            int(i.get("quantidade") or 0) * float(i.get("custo_unit") or 0)
            for i in base.get("itens") or [])
        _texto_resumo += (f" · :orange[{_dq_emit:+d} peças · "
                          f"{'+' if _dv_emit >= 0 else '−'}{_fmt_brl(abs(_dv_emit))} "
                          "vs emitido]")
        # Dois "R$" na mesma linha: o markdown leria `$…$` como LaTeX. Escapar
        # só um não basta — o par fecha no cifrão escapado.
        _texto_resumo = _texto_resumo.replace("$", "\\$")
    if _pendente:
        _texto_resumo += (f" · :orange[:material/edit: {_n_pendentes} "
                          "alteração(ões) não salva(s)]")
    _resumo.markdown(_texto_resumo)

    # --- Estado do pedido + o que impede a próxima ação (logo acima dos botões) ---
    if status == estados.PRONTO:
        st.info("Pedido **Pronto** — reabra o rascunho para editar quantidades.")
    elif status == estados.COMPRA_EMITIDA:
        st.success(f"Compra emitida no Bling · nº **{pedido_sel.get('bling_numero','?')}** "
                   "— falta emitir a venda no Olist.", icon=":material/shopping_cart:")
    elif estados.emitindo(status):
        st.warning(
            "**Emissão interrompida.** Este pedido ficou travado durante uma "
            "emissão (falha entre criar no ERP e confirmar aqui). **Confira no ERP "
            "se o pedido foi criado** antes de destravar e tentar de novo.",
            icon=":material/hourglass_top:",
        )
    elif status == estados.EMITIDO:
        st.success(
            f"Emitido nos dois ERPs · compra Bling **{pedido_sel.get('bling_numero','?')}** "
            f"· venda Olist **{pedido_sel.get('olist_numero','?')}**.",
            icon=":material/mark_email_read:",
        )
    elif em_alteracao:
        st.info(
            "**Em alteração.** Os ERPs continuam com a versão emitida"
            + (f" (revisão {base['numero']})" if base else "")
            + " — as quantidades abaixo só passam a valer depois de **Enviar alteração**.",
            icon=":material/edit_note:")
        _parcial = revisoes.envio_parcial(historico)
        if _parcial:
            _recebeu = [nome for erp, nome in (("olist", "Olist"), ("bling", "Bling"))
                        if _parcial.get(f"{erp}_ok_em")]
            st.warning(
                f"**Envio anterior incompleto**: o {' e o '.join(_recebeu)} já recebeu a "
                f"revisão {_parcial['numero']} e o outro ERP não. Envie a alteração de "
                "novo para alinhar os dois — até lá não dá para descartar.",
                icon=":material/sync_problem:")
    elif estados.enviando_alteracao(status):
        _pend = revisoes.pendente(historico)
        _recebeu = [nome for erp, nome in (("olist", "Olist"), ("bling", "Bling"))
                    if _pend.get(f"{erp}_ok_em")]
        _oque = ("Envio da alteração" if status == estados.ALTERACAO_ENVIANDO
                 else "Cancelamento")
        st.warning(
            f"**{_oque} incompleto.** "
            + (f"O {' e o '.join(_recebeu)} já recebeu; " if _recebeu
               else "Nenhum ERP confirmou ainda; ")
            + "clique em **Concluir** para reenviar — repetir é seguro, não duplica nada.",
            icon=":material/hourglass_top:")
    elif status == estados.CANCELADO and historico \
            and historico[-1].get("tipo") == estados.REVISAO_CANCELAMENTO:
        _canc = historico[-1]
        st.caption(
            f":material/block: Cancelado nos ERPs em "
            f"{pd.Timestamp(_canc['criado_em']):%d/%m/%Y %H:%M} por {_canc['criado_por']}"
            + (f" — {_canc['motivo']}" if _canc.get("motivo") else "")
            + f" · compra Bling {pedido_sel.get('bling_numero') or '—'}"
            + f" · venda Olist {pedido_sel.get('olist_numero') or '—'}.")
    if _erro_revisoes:
        st.error(_erro_revisoes, icon=":material/database:")

    # --- Pré-validação do mapeamento SKU→id Olist (só COMPRA_EMITIDA) —
    # usada tanto no preview quanto no botão de emitir venda logo abaixo.
    # Lida uma vez por rerun de app (_memo): sem isso, cada interação com a
    # tela refazia a leitura da integração e do cache de produtos. ---
    _mapa, _erros_pre, _erro_map = {}, [], None
    if status == estados.COMPRA_EMITIDA:
        def _ler_olist():
            _repo_int = obter_repositorio_integracoes()
            _cfg = (_repo_int.ler("olist") or {}).get("config") or {}
            _skus = itens[itens["quantidade_final"] > 0]["sku"].tolist()
            _m, _ = emissor.resolver_ids_olist(_skus, _repo_int)
            return _cfg, _m

        _cfg_olist = {}
        try:
            with st.spinner("Verificando catálogo do Olist…"):
                _cfg_olist, _mapa = _memo(("olist", pedido_id), _ler_olist)
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
    if status == estados.PRONTO:
        for _a in _memo("prontidao_olist", lambda: emissor.checar_prontidao_olist(
                obter_repositorio_integracoes())):
            st.warning(_a, icon=":material/warning:")

    # --- Observações + CSV (só montados aqui; exibidos abaixo das ações) ---
    obs_bling = builder.montar_observacoes_bling(
        rodada_sel.to_dict(), pedido_sel.to_dict(), itens)
    df_csv = itens[["sku", "produto", "tamanho", "categoria", "origem",
                    "quantidade_sugerida", "quantidade_final", "custo_unit"]].copy()
    df_csv["origem"] = df_csv["origem"].map(_ROTULO_ORIGEM)
    df_csv["investimento"] = df_csv["quantidade_final"] * df_csv["custo_unit"]
    df_csv = df_csv.rename(columns={
        "sku": "SKU", "produto": "Produto", "tamanho": "Tam", "categoria": "Categoria",
        "origem": "Origem",
        "quantidade_sugerida": "Qtd Sugerida", "quantidade_final": "Qtd Final",
        "custo_unit": "Custo Unit (R$)", "investimento": "Investimento (R$)",
    })
    cabecalho = "".join(f"# {linha}\n" for linha in obs_bling.split("\n")) + "\n"
    csv_pedido = (cabecalho + df_csv.to_csv(index=False, sep=";", decimal=",")).encode("utf-8")

    def _botao_csv():
        st.download_button(
            "Baixar CSV do pedido", icon=":material/download:",
            data=csv_pedido,
            file_name=f"pedido_{pedido_sel['colegio']}_{pedido_sel['super_categoria']}"
                      f"_{rodada_sel['mes_disparo']:02d}{rodada_sel['ano_disparo']}.csv",
            mime="text/csv", width="stretch", key=f"csv_{pedido_id}",
        )

    def _botao_salvar():
        """Grava o que foi digitado na tabela — no rascunho e na alteração."""
        if st.button(
                f"Salvar alterações ({_n_pendentes})" if _pendente
                else "Salvar alterações",
                icon=":material/save:", width="stretch", disabled=not _pendente,
                type="primary" if _pendente else "secondary",
                key=f"salvar_{pedido_id}"):
            # Células novas da grade só viram item se o tamanho existe
            # no cadastro ativo — o que não existe volta como aviso.
            _incluir, _faltantes = [], []
            if _novas:
                _incluir, _faltantes = catalogo.resolver_celulas(_catalogo(), _novas)
            _aviso = (" Não incluído(s) — tamanho sem cadastro ativo no Bling: "
                      + ", ".join(_faltantes) + "." if _faltantes else "")
            if not _alteracoes and not _incluir:
                _flash("warning" if _faltantes else "info",
                       "Nenhuma quantidade alterada." + _aviso)
            else:
                try:
                    with st.spinner(f"Salvando {_n_pendentes} alteração(ões)…"):
                        n = (repo.atualizar_quantidades(pedido_id, _alteracoes, usuario)
                             if _alteracoes else 0)
                        m = (repo.adicionar_itens(pedido_id, _incluir, usuario)
                             if _incluir else 0)
                    _nova_rev()
                    _partes = ([f"{n} item(ns) atualizado(s)"] if n else []) + (
                        [f"{m} tamanho(s) incluído(s)"] if m else [])
                    _flash("warning" if _faltantes else "success",
                           " · ".join(_partes) + "." + _aviso)
                except (PedidoNaoEditavel, ItemJaExiste) as exc:
                    _flash("warning", str(exc))
                except MigracaoPendente as exc:
                    _flash("error", str(exc))
            st.rerun()

    # --- AÇÕES: coladas na tabela. Ficavam no fim da tela, depois de três
    # blocos recolhíveis — o Salvar nascia fora da área visível. ---
    if status == estados.RASCUNHO:
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            # Uma ação primária por vez: com edição pendente é o Salvar;
            # sem pendência ele apaga e o destaque passa ao "Marcar como Pronto".
            _botao_salvar()
        with c2:
            # Bloqueado com edição pendente: o Pronto NÃO grava a tabela, e
            # a quantidade digitada se perdia sem aviso.
            if st.button("Marcar como Pronto", icon=":material/check_circle:",
                         width="stretch", disabled=_pendente,
                         type="secondary" if _pendente else "primary",
                         help="Salve as alterações antes de marcar como Pronto."
                         if _pendente else None):
                ok = repo.transicionar_pedido(
                    pedido_id, estados.RASCUNHO, estados.PRONTO, usuario)
                _flash("success", "Pedido marcado como **Pronto**.") if ok else _flash(
                    "warning", "O pedido mudou de estado em outra sessão — recarregado.")
                st.rerun()
        with c3:
            with st.popover("Cancelar pedido", icon=":material/block:", width="stretch"):
                st.caption("O pedido cancelado sai do fluxo (fica registrado p/ auditoria).")
                if st.button("Confirmar cancelamento", key=f"cancel_{pedido_id}"):
                    ok = repo.transicionar_pedido(
                        pedido_id, estados.RASCUNHO, estados.CANCELADO, usuario)
                    _flash("success", "Pedido cancelado.") if ok else _flash(
                        "warning", "O pedido mudou de estado em outra sessão — recarregado.")
                    st.rerun()
        with c4:
            _botao_csv()

    elif status == estados.PRONTO:
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            if st.button("Emitir compra (Bling)", icon=":material/send:", type="primary",
                         width="stretch",
                         key=f"emit_compra_{pedido_id}"):
                with st.status("Emitindo compra no Bling…", expanded=True) as _s:
                    try:
                        res = emissor.emitir_compra_bling(
                            pedido_id, usuario, repo, obter_repositorio_integracoes())
                        _s.update(label=f"Compra emitida · nº {res['bling_numero']}",
                                  state="complete")
                        _flash("success",
                               f"Compra emitida no Bling · nº **{res['bling_numero']}**.")
                    except emissor.EmissaoFalhou as exc:
                        _s.update(label="Falha na emissão da compra", state="error")
                        _flash("error", f"Emissão da compra falhou: {exc}")
                st.rerun()
        with c2:
            if st.button("Reabrir rascunho", icon=":material/undo:", width="stretch",
                         key=f"reabrir_{pedido_id}"):
                ok = repo.transicionar_pedido(
                    pedido_id, estados.PRONTO, estados.RASCUNHO, usuario)
                _flash("success", "Pedido reaberto para edição.") if ok else _flash(
                    "warning", "O pedido mudou de estado em outra sessão — recarregado.")
                st.rerun()
        with c3:
            with st.popover("Cancelar pedido", icon=":material/block:", width="stretch"):
                if st.button("Confirmar cancelamento", key=f"cancelp_{pedido_id}"):
                    ok = repo.transicionar_pedido(
                        pedido_id, estados.PRONTO, estados.CANCELADO, usuario)
                    _flash("success", "Pedido cancelado.") if ok else _flash(
                        "warning", "O pedido mudou de estado em outra sessão — recarregado.")
                    st.rerun()
        with c4:
            _botao_csv()

    elif status == estados.COMPRA_EMITIDA:
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Emitir venda (Olist)", icon=":material/send:", type="primary",
                         width="stretch",
                         disabled=bool(_erro_map or _erros_pre),
                         key=f"emit_venda_{pedido_id}"):
                with st.status("Emitindo venda no Olist…", expanded=True) as _s:
                    try:
                        res = emissor.emitir_venda_olist(
                            pedido_id, usuario, repo, obter_repositorio_integracoes(),
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
        _acoes_pos_emissao(pedido_sel)

    elif status == estados.EMITIDO:
        c1, _ = st.columns([1, 3])
        with c1:
            _botao_csv()
        _acoes_pos_emissao(pedido_sel)

    elif em_alteracao:
        c1, c2, c3 = st.columns(3)
        with c1:
            # Uma ação primária por vez: com edição pendente é o Salvar; sem
            # pendência o destaque passa ao "Enviar alteração", logo abaixo.
            _botao_salvar()
        with c2:
            _parcial = bool(revisoes.envio_parcial(historico))
            with st.popover("Descartar alteração", icon=":material/undo:", width="stretch",
                            disabled=_parcial,
                            help="Um dos ERPs já recebeu esta alteração — envie de "
                                 "novo para alinhar os dois." if _parcial else None):
                st.caption("As quantidades voltam ao que está nos ERPs e o que foi "
                           "incluído nesta alteração sai do pedido. Nada é enviado.")
                if st.button("Confirmar descarte", key=f"descartar_alt_{pedido_id}"):
                    try:
                        ok = repo.descartar_alteracao(pedido_id, usuario)
                        st.session_state.pop(_chave_divergencia(pedido_id), None)
                        _nova_rev()
                        _flash("success", "Alteração descartada — o pedido voltou ao "
                                          "que está emitido.") if ok else _flash(
                            "warning", "O pedido mudou de estado em outra sessão — recarregado.")
                    except (TransicaoInvalida, MigracaoPendente) as exc:
                        _flash("warning", str(exc))
                    st.rerun()
        with c3:
            _botao_csv()
        _secao_enviar_alteracao(pedido_sel, itens, historico, _pendente)

    elif estados.enviando_alteracao(status):
        _eh_alteracao = status == estados.ALTERACAO_ENVIANDO
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("Concluir envio" if _eh_alteracao else "Concluir cancelamento",
                         icon=":material/send:", type="primary", width="stretch",
                         key=f"concluir_{pedido_id}"):
                with st.status("Reenviando aos ERPs…", expanded=True) as _s:
                    try:
                        _fn = (emissor.enviar_alteracao if _eh_alteracao
                               else emissor.cancelar_emitido)
                        _fn(pedido_id, "", usuario, repo,
                            obter_repositorio_integracoes(), progresso=_s.write)
                        _nova_rev()
                        _s.update(label="Concluído", state="complete")
                        _flash("success", "Alteração enviada ao Olist e ao Bling."
                               if _eh_alteracao else "Pedido cancelado no Olist e no Bling.")
                    except emissor.EmissaoFalhou as exc:
                        _s.update(label="Não concluído", state="error")
                        _flash("error", f"Não concluído: {exc}")
                st.rerun()
        with c2:
            with st.popover("Voltar a editar" if _eh_alteracao else "Destravar",
                            icon=":material/lock_open:", width="stretch"):
                st.caption(
                    "Volta o pedido a ficar editável. Se um ERP já recebeu a "
                    "alteração, ela continua lá: a tela avisa e pede um novo envio "
                    "para alinhar os dois." if _eh_alteracao else
                    "Volta o pedido ao estado emitido. O que já foi cancelado num "
                    "ERP continua cancelado lá — conclua o cancelamento quando o "
                    "outro ERP permitir.")
                if st.button("Confirmar", key=f"destr_pos_{pedido_id}"):
                    ok = emissor.destravar(pedido_id, usuario, repo,
                                           obter_repositorio_integracoes())
                    _nova_rev()
                    _flash("success", "Pedido destravado.") if ok else _flash(
                        "warning", "O estado mudou em outra sessão — recarregado.")
                    st.rerun()
        with c3:
            _botao_csv()

    elif estados.emitindo(status):
        c1, c2 = st.columns(2)
        with c1:
            with st.popover("Destravar", icon=":material/lock_open:", width="stretch"):
                st.caption("Volta o pedido ao estado anterior. Confirme antes que o "
                           "pedido NÃO foi criado no ERP (senão vira duplicata).")
                if st.button("Confirmar destravamento", key=f"destr_{pedido_id}"):
                    ok = emissor.destravar(pedido_id, usuario, repo,
                                           obter_repositorio_integracoes())
                    _flash("success", "Pedido destravado.") if ok else _flash(
                        "warning", "O estado mudou em outra sessão — recarregado.")
                    st.rerun()
        with c2:
            _botao_csv()

    else:  # SINCRONIZADO / CANCELADO — sem ações, só o CSV
        c1, _ = st.columns([1, 3])
        with c1:
            _botao_csv()

    # --- Material de apoio: conferência e ajustes fora da grade, abaixo
    # das ações (quem só revisa quantidades não precisa passar por ele) ---
    # --- Inclusão/remoção manual de itens (no rascunho e na alteração) ---
    if pode_editar:
        _c_add, _c_rem = st.columns([3, 1], vertical_alignment="top")
        with _c_rem:
            _remover_manuais(pedido_id, itens, _pendente, ja_emitidos=set(qtd_base or {}))
        with _c_add:
            _adicionar_produto(pedido_sel, itens, _pendente)

    _memoria_sugestao(itens, pedido_sel)

    # --- Preview dos payloads de emissão (verificação humana, sem escrita).
    # Atrás de um interruptor: o corpo de um expander roda mesmo fechado,
    # e o preview relê pedido, itens, rodada e as duas integrações. ---
    if status in (estados.PRONTO, estados.COMPRA_EMITIDA):
        with st.expander("Preview dos payloads de emissão", icon=":material/search:"):
            st.caption("O JSON exato que será enviado aos ERPs — confira antes de emitir.")
            if st.toggle("Montar o preview", key=f"prev_{pedido_id}"):
                try:
                    _prev = _memo(
                        ("preview", pedido_id),
                        lambda: emissor.preview_payloads(
                            pedido_id, repo, obter_repositorio_integracoes(),
                            mapa_sku=_mapa))
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
    with st.expander("Observações para o Bling (padronizadas)",
                     icon=":material/description:"):
        st.caption(
            "Bloco que a emissão automática envia no campo **Observações** do "
            "pedido de compra. Nas **Observações internas** vai só o título "
            f"(`{pedido_sel['titulo']}`) — é o que aparece na listagem e na "
            "busca do Bling. Enquanto a emissão é manual, copie daqui (ou do "
            "cabeçalho do CSV)."
        )
        st.code(obs_bling, language=None)

    st.caption(
        f"Título no Bling: {pedido_sel['titulo']} · criado em "
        f"{pd.Timestamp(pedido_sel['criado_em']):%d/%m/%Y %H:%M} "
        f"por {pedido_sel['criado_por']}")


# =================================================================
# AÇÃO EM LOTE — seleção múltipla na tabela + botões (aprovar / reabrir /
# cancelar / emitir compra Bling / emitir venda Olist)
# =================================================================
def _secao_lote(pedidos_f: pd.DataFrame, assinatura_filtro: str):
    """
    Roda dentro de _area_trabalho (@st.fragment): clicar num checkbox da tabela
    dispara um rerun SÓ dessa área — não re-executa as leituras do topo
    (listar_rodadas/listar_pedidos, sem cache). Some com o 'piscar/carregar' a
    cada clique. Os botões de ação chamam st.rerun() (escopo app) para refletir
    a mudança na página inteira + a mensagem de resultado.
    """
    view_ped = pedidos_f.copy()
    view_ped["Status"] = view_ped["status"].map(estados.ROTULOS_BADGE)
    view_ped["Δ"] = view_ped["qtd_final"] - view_ped["qtd_sugerida"]
    # bling_numero/olist_numero podem não existir se o DDL 003 não foi aplicado
    for _col in ("bling_numero", "olist_numero"):
        if _col not in view_ped.columns:
            view_ped[_col] = ""
    st.caption(
        "Marque as linhas (☑) e use os botões abaixo para agir em vários pedidos "
        "de uma vez. O cabeçalho da coluna de seleção marca/desmarca tudo."
    )
    # Sem a coluna Título: dentro de uma rodada ela só repete Colégio +
    # Super Categoria (o título é "COLÉGIO - SUPERCAT - Rmm/aaaa").
    evento = exibir(
        view_ped[["colegio", "super_categoria", "Status", "n_itens",
                  "qtd_sugerida", "qtd_final", "Δ", "investimento_final",
                  "bling_numero", "olist_numero"]],
        FILA,
        {
            "colegio": col_colegio(),
            "super_categoria": col_texto("Super Categoria"),
            "n_itens": col_pecas("Itens"),
            "qtd_sugerida": col_pecas("Sugerida (pçs)", largura=None),
            "qtd_final": col_pecas("Final (pçs)", largura=None),
            "Δ": st.column_config.NumberColumn(
                "Δ", format="%+d", width="small",
                help="Final − Sugerida: o quanto a revisão mexeu na sugestão."),
            "investimento_final": col_moeda("Investimento", casas=2),
            "bling_numero": col_texto("Nº Bling", largura="small"),
            "olist_numero": col_texto("Nº Olist", largura="small"),
        },
        on_select="rerun", selection_mode="multi-row",
        # O filtro entra na key: a seleção é por POSIÇÃO da linha, e com a
        # tabela mudando de conteúdo ela apontaria para outros pedidos.
        key=f"sel_pedidos_{rodada_id}_{assinatura_filtro}",
    )

    # Posições selecionadas → pedidos (view_ped preserva a ordem de `pedidos_f`)
    _rows = [r for r in (getattr(evento.selection, "rows", []) if evento else [])
             if r < len(pedidos_f)]
    sel = pedidos_f.iloc[_rows] if _rows else pedidos_f.iloc[0:0]

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
        _s2.metric("Peças finais", num(int(sel['qtd_final'].sum())))
        _s3.metric("Investimento", _fmt_brl(float(sel['investimento_final'].sum())))

        b1, b2, b3, b4, b5 = st.columns(5)

        # Aprovar: RASCUNHO → PRONTO
        with b1:
            if st.button(f"Aprovar ({len(_rasc)})", icon=":material/check_circle:", type="primary",
                         disabled=_rasc.empty, width="stretch",
                         help="Marca os selecionados em Rascunho como Pronto"):
                suc, fal = _executar_lote(
                    _rasc, lambda r: (
                        repo.transicionar_pedido(
                            r["id"], estados.RASCUNHO, estados.PRONTO, usuario),
                        "o estado mudou em outra sessão"))
                _flash_resumo_lote("Aprovação", suc, fal)
                st.rerun()

        # Reabrir: PRONTO → RASCUNHO
        with b2:
            if st.button(f"Reabrir ({len(_pronto)})", icon=":material/undo:",
                         disabled=_pronto.empty, width="stretch",
                         help="Volta os selecionados em Pronto para Rascunho (edição)"):
                suc, fal = _executar_lote(
                    _pronto, lambda r: (
                        repo.transicionar_pedido(
                            r["id"], estados.PRONTO, estados.RASCUNHO, usuario),
                        "o estado mudou em outra sessão"))
                _flash_resumo_lote("Reabertura", suc, fal)
                st.rerun()

        # Cancelar: RASCUNHO/PRONTO → CANCELADO (com confirmação)
        with b3:
            with st.popover(f"Cancelar ({len(_cancelaveis)})", icon=":material/block:", width="stretch",
                            disabled=_cancelaveis.empty):
                st.caption("Cancela os selecionados em Rascunho/Pronto (fica registrado "
                           "p/ auditoria). Pedidos já emitidos são ignorados.")
                if st.button("Confirmar cancelamento", type="primary",
                             key="cancel_lote"):
                    suc, fal = _executar_lote(
                        _cancelaveis, lambda r: (
                            repo.transicionar_pedido(
                                r["id"], r["status"], estados.CANCELADO, usuario),
                            "o estado mudou em outra sessão"))
                    _flash_resumo_lote("Cancelamento", suc, fal)
                    st.rerun()

        # Emitir compra (Bling): PRONTO → Bling (com confirmação + total)
        with b4:
            with st.popover(f"Compra Bling ({len(_pronto)})", icon=":material/send:", width="stretch",
                            disabled=_pronto.empty):
                st.caption(
                    f"Emite **{len(_pronto)}** pedido(s) de compra REAIS no Bling · "
                    f"total **{_fmt_brl(float(_pronto['investimento_final'].sum()))}**. "
                    "Se um falhar, os demais seguem e o erro é reportado."
                )
                # Um lote de compras sem o Olist pronto vira um lote de
                # pedidos órfãos em COMPRA_EMITIDA — avisa antes.
                for _a in _memo("prontidao_olist", lambda: emissor.checar_prontidao_olist(
                        obter_repositorio_integracoes())):
                    st.warning(_a, icon=":material/warning:")
                if st.button("Confirmar emissão das compras", type="primary",
                             key="emit_compra_lote"):
                    with st.status(f"Emitindo {len(_pronto)} compra(s) no Bling…",
                                   expanded=True) as _s:
                        suc, fal = _executar_lote(
                            _pronto, lambda r: (
                                bool(emissor.emitir_compra_bling(
                                    r["id"], usuario, repo,
                                    obter_repositorio_integracoes())), ""))
                        _s.update(
                            label=f"Compras processadas: {suc} ok, {len(fal)} com problema",
                            state="error" if fal else "complete")
                    _flash_resumo_lote("Emissão de compra (Bling)", suc, fal)
                    st.rerun()

        # Emitir venda (Olist): COMPRA_EMITIDA → Olist (com confirmação + total)
        with b5:
            with st.popover(f"Venda Olist ({len(_compra)})", icon=":material/send:", width="stretch",
                            disabled=_compra.empty):
                st.caption(
                    f"Emite **{len(_compra)}** pedido(s) de venda no Olist (só os que "
                    "já têm a compra emitida). O mapeamento SKU→Olist é feito UMA vez "
                    "para o lote inteiro; falha em um não interrompe os outros."
                )
                if st.button("Confirmar emissão das vendas", type="primary",
                             key="emit_venda_lote"):
                    with st.status(f"Emitindo {len(_compra)} venda(s) no Olist…",
                                   expanded=True) as _s:
                        # Mapeia SKU→Olist UMA vez p/ todo o lote (cache +
                        # família + fallback) — passar mapa_sku=None por
                        # pedido refazia a busca N vezes e estourava o rate
                        # limit (429). Superset é seguro: cada emissão só
                        # consulta os SKUs do próprio pedido.
                        try:
                            _ri = obter_repositorio_integracoes()
                            _skus = sorted({
                                s for pid in _compra["id"]
                                for s in repo.listar_itens(pid).pipe(
                                    lambda d: d[d["quantidade_final"] > 0]["sku"])
                            })
                            _s.write(f"Mapeando {len(_skus)} SKU(s) no Olist…")
                            _mapa_lote, _ = emissor.resolver_ids_olist(_skus, _ri)
                        except Exception as _exc:
                            _mapa_lote = None
                            _s.write(f":orange[:material/warning:] Pré-mapeamento falhou ({_exc}); "
                                     "cada pedido mapeia sozinho.")
                        suc, fal = _executar_lote(
                            _compra, lambda r: (
                                bool(emissor.emitir_venda_olist(
                                    r["id"], usuario, repo,
                                    obter_repositorio_integracoes(),
                                    mapa_sku=_mapa_lote)), ""))
                        _s.update(
                            label=f"Vendas processadas: {suc} ok, {len(fal)} com problema",
                            state="error" if fal else "complete")
                    _flash_resumo_lote("Emissão de venda (Olist)", suc, fal)
                    st.rerun()


# Rótulos do seletor de modo: o mesmo texto é opção e é comparado abaixo.
MODO_EDITAR = ":material/edit: Editar um pedido"
MODO_LOTE = ":material/checklist: Ação em lote"


# Dois modos de trabalho (editar UM pedido × agir em VÁRIOS) num seletor de modo,
# NÃO em st.tabs: fragment-que-rerroda dentro de st.tabs quebra o show/hide e vaza
# o conteúdo das duas ("ghost tabs inside st.fragment", #9158/#9313 do Streamlit).
#
# O seletor, os FILTROS (Colégio / Super Categoria / Status, comuns aos dois
# modos) e as duas seções vivem TODOS dentro de _area_trabalho (@st.fragment):
# trocar de modo (ou de filtro, de pedido, ou marcar linhas no lote) rerroda SÓ esta área —
# NÃO re-executa as leituras do topo (listar_rodadas/listar_pedidos), que são sem
# cache e eram o que travava a troca de modo. Só as AÇÕES (salvar/emitir/cancelar)
# chamam st.rerun() de app inteiro, porque aí o dado mudou e o resumo + as tabelas
# do topo precisam refletir. As seções chamadas aqui NÃO são fragments próprias
# (seria aninhamento desnecessário) — o fragment é este pai.
@st.fragment
def _area_trabalho():
    # UMA caixa titulada, no molde das outras seções da página: modo e filtros
    # ficavam soltos acima de um retângulo sem título, e não se via que os três
    # eram a mesma seção. Sem caixa dentro de caixa — as seções não têm borda.
    with st.container(border=True):
        # Linha flexível, não colunas: o título ocupa o que sobra e o seletor
        # fica do tamanho do próprio texto. Em coluna de fração fixa ele
        # quebrava em duas linhas empilhadas quando a janela estreitava.
        with st.container(horizontal=True, vertical_alignment="center"):
            st.subheader("Pedidos da rodada")
            _modo = st.segmented_control(
                "Modo de trabalho",
                [MODO_EDITAR, MODO_LOTE],
                default=MODO_EDITAR,
                label_visibility="collapsed",
                key="pc_modo", width="content",
            )
        pedidos_f = _filtrar_pedidos(pedidos, rodada_id)
        if len(pedidos_f) == 0:
            st.info("Nenhum pedido desta rodada com esses filtros — limpe um deles.",
                    icon=":material/filter_alt_off:")
            return
        if len(pedidos_f) < len(pedidos):
            st.caption(f"Mostrando **{len(pedidos_f)}** de {len(pedidos)} pedidos da rodada.")

        if _modo == MODO_LOTE:
            _secao_lote(pedidos_f, _assinatura_filtro(rodada_id))
        else:
            _secao_pedido(pedidos_f)


_area_trabalho()
