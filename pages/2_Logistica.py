"""
Página: Logística — Reposição de Loja

Pergunta da tela: o que o CD separa HOJE para cada loja?
Estoque-alvo por loja × SKU (etl/reposicao.py) comparado com o estoque; daqui
sai o relatório de separação impresso. Metodologia em
docs/requisitos/reposicao-loja-v2.md.
"""

import streamlit as st
from auth import exigir_login
exigir_login()
import pandas as pd

from etl import logistica, relatorio_separacao, reposicao
from etl.loader import fingerprint_config
from ui_carga import carregar_com_feedback, rodape_frescor
from ui_tabelas import (
    FILA, MEMORIA, exibir, num,
    col_sku, col_produto, col_colegio, col_tamanho, col_texto, col_pecas, col_numero,
    col_pct, col_data,
)


dados, config = carregar_com_feedback()

if not dados["validacao"]["ok"]:
    st.error("Dados inválidos. Verifique a página principal.", icon=":material/error:")
    st.stop()


@st.cache_data(show_spinner=False)
def _processar(_dados, _config, fp_config, dia):
    # `fp_config` e `dia` SEM underscore de propósito: são o que ENTRA na cache
    # key (os `_` ficam fora). O config faz o resultado não ficar preso ao que
    # valia antes de um "Salvar"; o dia, porque o alvo olha o calendário para
    # frente — a fila de ontem não serve hoje.
    return logistica.processar_logistica(_dados, _config, data_hoje=dia)


hoje = pd.Timestamp.now().normalize()
# Sem spinner: leva ~0,5 s e o flash gera mais ruído do que confiança.
df = _processar(dados, config, fingerprint_config(config), hoje.strftime("%Y-%m-%d"))

params = reposicao.parametros(config)
nomes_lojas = [l["nome"] for l in config["depositos"]["lojas"]]
fase = reposicao.fase_atual(config, hoje)

st.title(":material/local_shipping: Logística — Reposição de Loja")
st.caption(
    f"**{'Alta temporada' if fase == reposicao.FASE_ALTA else 'Baixa temporada'}** · "
    + " · ".join(
        f"{n}: guarda {reposicao.janela_protecao_dias(params, fase, n)} dias de venda"
        for n in nomes_lojas)
    + f" · exposição mínima de {int(params['exposicao_minima'])} por tamanho · "
    f"{hoje.strftime('%d/%m/%Y')}"
)

if len(df) == 0:
    st.info(
        "Nenhum produto a acompanhar: confira o sortimento das lojas em "
        "Configurações → Reposição de Loja.", icon=":material/info:")
    rodape_frescor(dados)
    st.stop()

sem_cadastro = sorted(
    df.loc[df["SortimentoOrigem"] == reposicao.ORIGEM_VENDAS, "Loja"].unique())
if sem_cadastro:
    st.warning(
        f"**{', '.join(sem_cadastro)}** sem sortimento cadastrado: os colégios foram deduzidos "
        "das vendas dos últimos 12 meses. Cadastre em Configurações → Reposição de Loja.",
        icon=":material/warning:",
    )

# =================================================================
# RECORTE: loja + filtros
# =================================================================
loja = st.segmented_control(
    "Loja", ["Todas"] + nomes_lojas, default="Todas", key="log_loja") or "Todas"

col_f1, col_f2, col_f3 = st.columns([1, 1, 2])
with col_f1:
    colegios = sorted(c for c in df["Colegio"].dropna().astype(str).unique() if c and c != "nan")
    filtro_colegio = st.selectbox("Colégio", ["Todos"] + colegios)
with col_f2:
    cats = sorted(c for c in df["Categoria"].dropna().astype(str).unique() if c and c != "nan")
    filtro_cat = st.selectbox("Categoria", ["Todas"] + cats)
with col_f3:
    filtro_texto = st.text_input(
        ":material/search: Buscar SKU ou produto", placeholder="Digite para filtrar...")

df_f = df
if loja != "Todas":
    df_f = df_f[df_f["Loja"] == loja]
if filtro_colegio != "Todos":
    df_f = df_f[df_f["Colegio"] == filtro_colegio]
if filtro_cat != "Todas":
    df_f = df_f[df_f["Categoria"] == filtro_cat]
if filtro_texto.strip():
    termo = filtro_texto.strip().lower()
    df_f = df_f[
        df_f["SKU"].str.lower().str.contains(termo, na=False, regex=False)
        | df_f["Produto"].str.lower().str.contains(termo, na=False, regex=False)
    ]

# =================================================================
# VEREDITO
# =================================================================
separar = df_f[df_f["Separar"] > 0]
em_falta = df_f[df_f["Falta"] > 0]
recolher = df_f[df_f["Acao"] == logistica.ACAO_RECOLHER]
corrigir = df_f[df_f["Acao"] == logistica.ACAO_CORRIGIR]
limitados = df_f[df_f["LimitadoPorEspaco"]]

c1, c2, c3, c4 = st.columns(4)
c1.metric("Separar agora", f"{num(separar['Separar'].sum())} pçs", f"{num(len(separar))} SKUs",
          delta_color="off", delta_arrow="off", border=True)
c2.metric("Falta no CD", f"{num(em_falta['Falta'].sum())} pçs", f"{num(len(em_falta))} SKUs",
          delta_color="off", delta_arrow="off", border=True,
          help="O que a loja precisa e o CD não tem para mandar — assunto da produção.")
c3.metric("Recolher da loja", f"{num(recolher['Excesso'].sum())} pçs", f"{num(len(recolher))} SKUs",
          delta_color="off", delta_arrow="off", border=True,
          help="Estoque que a loja não vende no horizonte configurado, ou de colégio "
               "que ela não atende.")
c4.metric("Limitados por espaço", f"{num(len(limitados))} SKUs",
          f"{num(len(corrigir))} com saldo negativo", delta_color="off", delta_arrow="off", border=True,
          help="Alvo abaixo do ideal porque as gavetas da loja acabaram: dependem de "
               "reposição frequente.")

# =================================================================
# FILA DE TRABALHO — uma visão por vez
# =================================================================
VISOES = {
    "Separar": separar,
    "Falta no CD": em_falta,
    "Recolher": recolher,
    "Corrigir estoque": corrigir,
    "Tudo": df_f,
}
with st.container(border=True):
    with st.container(horizontal=True, vertical_alignment="bottom"):
        visao = st.segmented_control(
            "Fila", list(VISOES), default="Separar", key="log_visao",
            format_func=lambda v: f"{v} ({num(len(VISOES[v]))})") or "Separar"

    fila = VISOES[visao]
    tem_transito = bool(df_f["EmTransito"].notna().any())

    if visao == "Separar":
        with st.container(horizontal=True, vertical_alignment="center"):
            documento = relatorio_separacao.montar_html(separar, data=pd.Timestamp.now())
            if st.button("Imprimir relatório de separação", icon=":material/print:",
                         type="primary", disabled=len(separar) == 0):
                st.session_state["log_imprimir"] = True
            st.download_button(
                "Baixar", data=documento, icon=":material/download:",
                file_name=f"separacao_{hoje.strftime('%Y-%m-%d')}.html", mime="text/html",
                disabled=len(separar) == 0,
                help="O mesmo relatório em arquivo — abra no navegador e imprima.")
            st.caption("O relatório segue a loja e os filtros acima, agrupado por colégio e modelo.")
        if st.session_state.pop("log_imprimir", False):
            # Iframe invisível: o documento chama a impressão do navegador ao carregar.
            st.iframe(relatorio_separacao.montar_html(
                separar, data=pd.Timestamp.now(), imprimir_ao_abrir=True), height=1)

    if len(fila) == 0:
        vazio = {
            "Separar": "Nada a separar agora: as lojas estão no alvo ou o CD não tem saldo.",
            "Falta no CD": "O CD cobre tudo o que as lojas precisam.",
            "Recolher": "Nenhum excesso nas lojas.",
            "Corrigir estoque": "Nenhum saldo negativo.",
            "Tudo": "Nenhum SKU no recorte.",
        }[visao]
        st.info(vazio, icon=":material/check_circle:")
    else:
        # Ordem de leitura: identidade → DECISÃO → evidência.
        identidade = ["SKU", "Produto", "Tamanho", "Colegio", "Loja"]
        decisao = {
            "Separar": ["Separar", "Falta"],
            "Falta no CD": ["Falta", "Separar"],
            "Recolher": ["Excesso", "MotivoExcesso"],
            "Corrigir estoque": [],
            "Tudo": ["Acao", "Separar", "Falta", "Excesso"],
        }[visao]
        evidencia = ["EstoqueLoja", "Alvo", "EstoqueCentral"]
        if tem_transito and visao in ("Falta no CD", "Tudo"):
            evidencia += ["EmTransito", "ChegadaPrevista"]
        evidencia += ["GiroLoja"]
        # "Tudo" mantém a ordem da fila (ação mais urgente primeiro)
        ordem = {"Separar": ("Separar", False), "Falta no CD": ("Falta", False),
                 "Recolher": ("Excesso", False), "Corrigir estoque": ("EstoqueLoja", True)}
        if visao in ordem:
            fila = fila.sort_values(ordem[visao][0], ascending=ordem[visao][1], kind="stable")

        exibir(fila[identidade + decisao + evidencia], FILA, {
            "SKU": col_sku(),
            "Produto": col_produto(largura="large"),
            "Tamanho": col_tamanho(),
            "Colegio": col_colegio(),
            "Loja": col_texto("Loja", largura="small"),
            "Acao": col_texto("Ação"),
            "Separar": col_pecas("Separar (pçs)", largura=None,
                                 ajuda="O que o CD manda agora, já rateado entre as lojas."),
            "Falta": col_pecas("Falta (pçs)", largura=None,
                               ajuda="O que a loja precisa e o CD não tem."),
            "Excesso": col_pecas("Recolher (pçs)", largura=None),
            "MotivoExcesso": col_texto("Motivo"),
            "EstoqueLoja": col_pecas("Est. loja"),
            "Alvo": col_pecas("Alvo", ajuda="Quanto a loja deveria ter deste tamanho."),
            "EstoqueCentral": col_pecas("Est. CD"),
            "EmTransito": col_pecas("Em trânsito", ajuda="Já comprado, ainda não chegou ao CD."),
            "ChegadaPrevista": col_data("Chegada"),
            "GiroLoja": col_numero("Giro (pçs/dia)", casas=2, largura="small",
                                   ajuda="Venda recente da loja. Só indicador: não entra no alvo."),
        })
        if visao == "Corrigir estoque":
            st.caption(
                "Saldo negativo na loja ou no CD é erro de lançamento no Bling. Enquanto não "
                "for corrigido, o SKU fica sem sugestão.")

# =================================================================
# APOIO — espaço e memória de cálculo
# =================================================================
with st.expander("Gavetas — quem ocupa o espaço da loja", icon=":material/shelves:"):
    exposicao = int(params["exposicao_minima"])
    com_alvo = df_f[df_f["AlvoIdeal"] > 0].copy()
    com_alvo["_fundo_ideal"] = (com_alvo["AlvoIdeal"] - exposicao).clip(lower=0)
    com_alvo["_fundo"] = (com_alvo["Alvo"] - exposicao).clip(lower=0)
    por_modelo = (
        com_alvo.groupby(["Loja", "Modelo"], as_index=False)
        .agg(Colegio=("Colegio", "first"), SuperCategoria=("SuperCategoria", "first"),
             Tamanhos=("SKU", "count"), Gavetas=("Gavetas", "max"),
             FundoIdeal=("_fundo_ideal", "sum"), Fundo=("_fundo", "sum"))
    )
    por_modelo = por_modelo[por_modelo["FundoIdeal"] > 0].sort_values(
        ["Loja", "FundoIdeal"], ascending=[True, False])

    resumo = []
    for n in (nomes_lojas if loja == "Todas" else [loja]):
        cadastradas = reposicao.gavetas_da_loja(params, n)
        usadas = int(por_modelo.loc[por_modelo["Loja"] == n, "Gavetas"].sum())
        resumo.append(f"**{n}**: {usadas} de {cadastradas} gavetas" if cadastradas is not None
                      else f"**{n}**: sem limite cadastrado ({usadas} gavetas pedidas)")
    st.caption(
        " · ".join(resumo) + ". As gavetas vão para os modelos com mais peças além da arara; "
        "quem fica sem gaveta guarda só a exposição. Com filtro de colégio/categoria a "
        "contagem mostra só o recorte.")

    if len(por_modelo) == 0:
        st.info("Nenhum modelo precisa de mais do que a arara neste recorte.")
    else:
        exibir(por_modelo, MEMORIA, {
            "Loja": col_texto("Loja", largura="small"),
            "Modelo": col_sku("Modelo"),
            "Colegio": col_colegio(),
            "SuperCategoria": col_texto("Super categoria"),
            "Tamanhos": col_pecas("Tamanhos"),
            "Gavetas": col_pecas("Gavetas"),
            "FundoIdeal": col_pecas("Fundo ideal (pçs)", largura=None,
                                    ajuda="Peças além da arara que o modelo pediria sem limite de espaço."),
            "Fundo": col_pecas("Fundo no alvo (pçs)", largura=None,
                               ajuda="Peças além da arara que couberam nas gavetas concedidas."),
        })

with st.expander("Memória de cálculo — como o alvo foi formado", icon=":material/calculate:"):
    st.caption(
        "**Alvo ideal** = demanda da loja na janela + segurança, nunca abaixo da exposição. "
        "**Demanda da janela** = venda da última alta × crescimento × participação da loja, "
        "nos dias da janela a partir de hoje. **Segurança** = Fator de Serviço × "
        "√(demanda × PA). **Alvo** = o ideal que coube nas gavetas."
    )
    memoria = df_f[df_f["EmSortimento"]].copy()
    memoria["Participacao"] = memoria["Participacao"] * 100
    exibir(memoria.sort_values("AlvoIdeal", ascending=False)[[
        "SKU", "Produto", "Loja", "Alvo", "AlvoIdeal", "DemandaJanela", "JanelaDias",
        "Seguranca", "PA", "NivelServico", "Participacao", "TaxaCresc", "Gavetas",
    ]], MEMORIA, {
        "SKU": col_sku(),
        "Produto": col_produto(),
        "Loja": col_texto("Loja", largura="small"),
        "Alvo": col_pecas("Alvo"),
        "AlvoIdeal": col_pecas("Alvo ideal"),
        "DemandaJanela": col_numero("Demanda da janela", casas=2, largura="small"),
        "JanelaDias": col_pecas("Janela (dias)"),
        "Seguranca": col_numero("Segurança", casas=2, largura="small"),
        "PA": col_numero("PA (pçs/atend.)", casas=1, largura="small"),
        "NivelServico": col_pct("Nível de serviço"),
        "Participacao": col_pct("Participação da loja", casas=1,
                                ajuda="Fatia da loja na venda do SKU na última alta. "
                                      "A venda institucional não entra em loja nenhuma."),
        "TaxaCresc": col_numero("Crescimento (×)", casas=2, largura="small"),
        "Gavetas": col_pecas("Gavetas do modelo"),
    })

st.divider()
rodape_frescor(dados)
