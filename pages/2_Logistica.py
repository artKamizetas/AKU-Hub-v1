"""
Página: Dashboard Logístico (Reposição de Loja)
VM Dinâmico + Pulmão. Sugestão baseada em VM+Pulmão − Estoque.
"""

import streamlit as st
from auth import exigir_login
exigir_login()
import plotly.express as px
import pandas as pd
from etl.logistica import processar_logistica
from etl.vm_dinamico import calcular_vm_por_sku

from etl.loader import fingerprint_config
from ui_carga import carregar_com_feedback, rodape_frescor
from ui_tabelas import (
    FILA, MEMORIA, exibir, num,
    col_sku, col_produto, col_colegio, col_tamanho, col_texto, col_pecas, col_numero,
)


dados, config = carregar_com_feedback()

if not dados["validacao"]["ok"]:
    st.error("Dados inválidos. Verifique a página principal.")
    st.stop()


@st.cache_data(show_spinner=False)
def _processar(_dados, _config, fp_config):
    # `fp_config` SEM underscore de propósito: é o único argumento que ENTRA na
    # cache key (os `_` ficam fora). É ele que faz o resultado não ficar preso
    # ao config antigo depois de um "Salvar" em Configurações.
    vm_map = calcular_vm_por_sku(_dados, _config)
    return processar_logistica(_dados, _config, vm_map)


# Sem spinner: leva ~0,5 s e o flash gera mais ruído do que confiança.
df = _processar(dados, config, fingerprint_config(config))

st.title("📦 Logística — Reposição de Loja")

# Info sobre VM
g = config.get("vm", {})
st.caption(
    f"VM Dinâmico ativo — "
    f"Cobertura: {int(g.get('dias_cobertura', 15))}d | "
    f"Alta: {int(g.get('inicio_alta', 10))}-{int(g.get('fim_alta', 3))} | "
    f"Mult. PA: {g.get('mult_pa', 2.0)}x | "
    f"LT: {int(g.get('lead_time', 3))}d | "
    f"Colégios c/ ajuste manual: {len(config.get('colegios') or {})}"
)

# =================================================================
# FILTROS
# =================================================================
col_f1, col_f2, col_f3 = st.columns(3)

with col_f1:
    lojas = ["Todas"] + sorted(df["Loja"].unique().tolist())
    filtro_loja = st.selectbox("Loja", lojas)

with col_f2:
    ordem_acoes = ["🚫 Estoque Negativo", "🚨 Ruptura", "✨ Repor", "🔵 Sem Venda", "✅ OK"]
    acoes_existentes = [a for a in ordem_acoes if a in df["Acao"].values]
    filtro_acao = st.selectbox("Ação", ["Todas"] + acoes_existentes)

with col_f3:
    cats = df["Categoria"].dropna().astype(str)
    cats = cats[cats.ne("") & cats.ne("nan")]
    categorias = ["Todas"] + sorted(cats.unique().tolist())
    filtro_cat = st.selectbox("Categoria", categorias)

col_f4, col_f5 = st.columns(2)

with col_f4:
    colegios = df["Colegio"].dropna().astype(str)
    colegios = colegios[colegios.ne("") & colegios.ne("nan")]
    colegios_disp = ["Todos"] + sorted(colegios.unique().tolist())
    filtro_colegio = st.selectbox("Colégio", colegios_disp)

with col_f5:
    filtro_texto = st.text_input("🔍 Buscar SKU ou Produto", placeholder="Digite para filtrar...")

df_filtrado = df.copy()
if filtro_loja != "Todas":
    df_filtrado = df_filtrado[df_filtrado["Loja"] == filtro_loja]
if filtro_acao != "Todas":
    df_filtrado = df_filtrado[df_filtrado["Acao"] == filtro_acao]
if filtro_cat != "Todas":
    df_filtrado = df_filtrado[df_filtrado["Categoria"] == filtro_cat]
if filtro_colegio != "Todos":
    df_filtrado = df_filtrado[df_filtrado["Colegio"] == filtro_colegio]
if filtro_texto.strip():
    termo = filtro_texto.strip().lower()
    df_filtrado = df_filtrado[
        df_filtrado["SKU"].str.lower().str.contains(termo, na=False) |
        df_filtrado["Produto"].str.lower().str.contains(termo, na=False)
    ]

# =================================================================
# KPIs
# =================================================================
st.subheader("Resumo")

c1, c2, c3, c4 = st.columns(4)

repor = df_filtrado[df_filtrado["Acao"] == "✨ Repor"]
ruptura = df_filtrado[df_filtrado["Acao"] == "🚨 Ruptura"]
negativo = df_filtrado[df_filtrado["Acao"] == "🚫 Estoque Negativo"]
ok = df_filtrado[df_filtrado["Acao"] == "✅ OK"]

c1.metric("✨ Repor", f"{num(len(repor))} SKUs", f"{num(repor['SugestaoQtd'].sum())} pçs")
c2.metric("🚨 Ruptura", f"{num(len(ruptura))} SKUs")
c3.metric("🚫 Negativo", f"{num(len(negativo))} SKUs")
c4.metric("✅ OK", f"{num(len(ok))} SKUs")

# =================================================================
# TABELA PRINCIPAL
# =================================================================
st.subheader("Detalhamento por SKU")

# Ordem de leitura: identidade → DECISÃO (ação, sugestão) → evidência (estoques e
# alvo). A linha já chega ordenada por urgência (Negativo → Ruptura → Repor).
colunas_principais = [
    "SKU", "Produto", "Tamanho", "Colegio", "Loja",
    "Acao", "SugestaoQtd",
    "EstoqueLoja", "EstoqueCentral", "VM", "Pulmao", "Total", "Categoria",
]

# Sem fundo colorido na sugestão: pintava toda linha com sugestão > 0, então
# não destacava nada. O estado já está na coluna Ação.
exibir(df_filtrado[colunas_principais], FILA, {
    "SKU": col_sku(),
    "Produto": col_produto(largura="large"),
    "Tamanho": col_tamanho(),
    "Colegio": col_colegio(),
    "Loja": col_texto("Loja", largura="small"),
    "Acao": col_texto("Ação"),
    "SugestaoQtd": col_pecas("Sugestão (pçs)", largura=None),
    "EstoqueLoja": col_pecas("Est. Loja"),
    "EstoqueCentral": col_pecas("Est. CD"),
    "VM": col_pecas("VM"),
    "Pulmao": col_pecas("Pulmão"),
    "Total": col_pecas("VM+Pulmão"),
    "Categoria": col_texto("Categoria"),
})

st.caption(f"**{num(len(df_filtrado))}** SKUs exibidos")

# =================================================================
# DIAGNÓSTICO VM (expander)
# =================================================================
with st.expander("🔍 Diagnóstico VM Dinâmico — Detalhes do Cálculo"):
    st.caption(
        "Mostra como o VM e Pulmão foram calculados para cada SKU. "
        "**Pulmão** = Fator de Serviço × Desvio-Padrão × √lead time (absorve picos de demanda)."
    )

    colunas_diag = [
        "SKU", "Produto", "Colegio", "VM", "Pulmao", "Total", "FonteVM",
        "PA", "Sigma", "D_Alta", "TaxaCresc", "Correcao",
    ]

    df_diag = df_filtrado[colunas_diag].drop_duplicates(subset=["SKU"]).sort_values("Total", ascending=False)

    exibir(df_diag, MEMORIA, {
        "SKU": col_sku(),
        "Produto": col_produto(),
        "Colegio": col_colegio(),
        "VM": col_pecas("VM"),
        "Pulmao": col_pecas("Pulmão"),
        "Total": col_pecas("VM+Pulmão"),
        "FonteVM": col_texto("Fonte VM", largura="small"),
        "PA": col_numero("PA (pçs/atend)", casas=1, largura="small"),
        "Sigma": col_numero("Desvio-Padrão", casas=2, largura="small"),
        "D_Alta": col_numero("Demanda/Dia", casas=3, largura="small"),
        "TaxaCresc": col_numero("Taxa Cresc.", casas=2, largura="small"),
        "Correcao": col_numero("Correção", casas=2, largura="small"),
    })

    st.caption(
        "**Desvio-Padrão alto** = vendas irregulares → pulmão maior. "
        "**Desvio-Padrão ≈ 0** = vendas estáveis → pulmão mínimo."
    )

# =================================================================
# GRÁFICOS
# =================================================================

st.subheader("Distribuição por Ação")
dist_acao = df_filtrado["Acao"].value_counts().reset_index()
dist_acao.columns = ["Ação", "Quantidade"]

fig_acao = px.bar(
    dist_acao, x="Ação", y="Quantidade",
    color="Ação", text="Quantidade",
)
fig_acao.update_layout(showlegend=False, height=350)
fig_acao.update_traces(textposition="outside")
st.plotly_chart(fig_acao, width="stretch")

# Pareto por Categoria
if df_filtrado["Categoria"].notna().any() and df_filtrado["Categoria"].str.strip().ne("").any():
    st.subheader("Pareto — Sugestão de Reposição por Categoria")
    transf_cat = (
        df_filtrado[df_filtrado["Acao"] == "✨ Repor"]
        .groupby("SuperCategoria")["SugestaoQtd"]
        .sum()
        .sort_values(ascending=False)
        .reset_index()
    )
    transf_cat.columns = ["Super Categoria", "Quantidade"]

    if len(transf_cat) > 0:
        transf_cat["% Acumulado"] = (transf_cat["Quantidade"].cumsum() / transf_cat["Quantidade"].sum() * 100)

        fig_pareto = px.bar(
            transf_cat, x="Super Categoria", y="Quantidade", text="Quantidade",
        )
        fig_pareto.add_scatter(
            x=transf_cat["Super Categoria"], y=transf_cat["% Acumulado"],
            mode="lines+markers", name="% Acumulado", yaxis="y2",
        )
        fig_pareto.update_layout(
            yaxis2=dict(title="% Acumulado", overlaying="y", side="right", range=[0, 110]),
            showlegend=False, height=400,
        )
        st.plotly_chart(fig_pareto, width="stretch")

st.divider()
rodape_frescor(dados)
