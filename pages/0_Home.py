"""
Página: Home — Visão Geral
"""

import streamlit as st
from auth import exigir_login
exigir_login()
import yaml
from pathlib import Path
from etl.loader import carregar_dados


def _carregar():
    caminho_config = Path(__file__).parent.parent / "config.yaml"
    with open(caminho_config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    dados = carregar_dados()
    return dados, config


with st.spinner("Carregando dados..."):
    dados, config = _carregar()
val = dados["validacao"]

st.title("📊 Bling Dashboard")
st.caption("Inteligência de Estoque, PCP e Vendas")

if not val["ok"]:
    st.error("❌ Erro na validação dos dados")
    for erro in val["erros"]:
        st.write(f"• {erro}")
    st.stop()

st.success("✅ Dados carregados com sucesso")

# KPIs
col1, col2, col3, col4, col5 = st.columns(5)

with col1:
    st.metric("SKUs Ativos", len(dados["produtos"]))
with col2:
    estoque_total = dados["estoque"]["saldoFisico"].sum()
    st.metric("Peças em Estoque (Rede)", f"{estoque_total:,.0f}")
with col3:
    pecas_vendidas = dados["itens"]["Quantidade"].sum()
    st.metric("Peças Vendidas", f"{pecas_vendidas:,.0f}")
with col4:
    vendas_total = dados["pedidos"]["Total Venda"].sum()
    st.metric("Faturamento Total (R$)", f"R$ {vendas_total:,.2f}")
with col5:
    st.metric("Lojas Ativas", len(config["depositos"]["lojas"]))

# Depósitos
st.subheader("Depósitos Cadastrados")
st.dataframe(
    dados["depositos"][["ID", "descricao"]].rename(
        columns={"ID": "ID", "descricao": "Nome"}
    ),
    width="stretch", hide_index=True,
)

# Estoque por depósito
st.subheader("Estoque por Depósito")
est_dep = (
    dados["estoque"]
    .merge(dados["depositos"].rename(columns={"ID": "ID_deposito", "descricao": "Deposito"}),
           on="ID_deposito", how="left")
    .groupby("Deposito")["saldoFisico"]
    .sum()
    .reset_index()
    .rename(columns={"saldoFisico": "Total Peças"})
    .sort_values("Total Peças", ascending=False)
)
st.dataframe(est_dep, width="stretch", hide_index=True)

st.divider()
st.caption(f"Fonte: {config['fonte']['nome']}")

if st.button("🔄 Recarregar Dados", width="content"):
    st.cache_data.clear()
    st.rerun()
