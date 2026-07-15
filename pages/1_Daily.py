"""
Página: Dashboard Comercial (Daily)
Filtros: loja, colégio, período (presets + calendário).
Gráfico combinado. Performance por vendedor e por colégio.
"""

import streamlit as st
from auth import exigir_login
exigir_login()
import plotly.graph_objects as go
import pandas as pd
from datetime import datetime, timedelta, date
from etl.daily import processar_daily

from etl.loader import carregar_dados, carregar_config


def _carregar():
    config = carregar_config()   # yaml (defaults) + app.parametros (Supabase)
    dados = carregar_dados()
    return dados, config


with st.spinner("Carregando dados..."):
    dados, config = _carregar()

if not dados["validacao"]["ok"]:
    st.error("Dados inválidos. Verifique a página principal.")
    st.stop()

# =================================================================
# PROCESSAMENTO
# =================================================================
df_detalhado, df_metas = processar_daily(dados, config)

st.title("📈 Daily — Acompanhamento Comercial")

# =================================================================
# FILTROS — Linha 1: Loja + Colégio
# =================================================================

# Lojas ativas no Bling — compara por ID (mais confiável que nome)
# Situacao pode ser numérico (1) ou texto ("Ativo") dependendo da exportação
lojas_ativas_ids = set(
    str(row["ID"]).strip()
    for _, row in dados["lojas"].iterrows()
    if str(row.get("Situacao", "0")).strip() in ("1", "Ativo", "ativo", "ATIVO")
)

# Cruzar com config: só mostra lojas cujo loja_id está ativo no Bling
lojas_config_ativas = [
    l for l in config["depositos"]["lojas"]
    if str(l["loja_id"]) in lojas_ativas_ids
]

# Fallback: se ainda vazio (ex: todas inativas no Bling), mostra todas do config
if not lojas_config_ativas:
    lojas_config_ativas = list(config["depositos"]["lojas"])
nomes_lojas = [l["nome"] for l in lojas_config_ativas]

col_f1, col_f2 = st.columns(2)

with col_f1:
    lojas_selecionadas = []
    st.markdown("**Lojas**")
    for nome in nomes_lojas:
        if st.checkbox(nome, value=True, key=f"loja_{nome}"):
            lojas_selecionadas.append(nome)

if len(lojas_selecionadas) == 0:
    st.warning("Selecione pelo menos 1 loja.")
    st.stop()

# Filtra por lojas selecionadas
df_loja = df_detalhado[df_detalhado["LojaConfig"].isin(lojas_selecionadas)].copy()

with col_f2:
    colegios_disp = sorted(df_loja["Colegio"].dropna().unique().tolist())
    colegios_disp = [c for c in colegios_disp if c and c != "nan" and c != "Sem Colégio"]
    colegio_selecionado = st.selectbox("Colégio", ["Todos"] + colegios_disp)

if colegio_selecionado != "Todos":
    df_loja = df_loja[df_loja["Colegio"] == colegio_selecionado]

# =================================================================
# FILTROS — Linha 2: Período
# =================================================================
hoje = date.today()
primeiro_dia_mes = hoje.replace(day=1)

# Mês passado
if hoje.month == 1:
    primeiro_mes_passado = date(hoje.year - 1, 12, 1)
else:
    primeiro_mes_passado = date(hoje.year, hoje.month - 1, 1)
ultimo_mes_passado = primeiro_dia_mes - timedelta(days=1)

# Semana atual (segunda a hoje)
inicio_semana = hoje - timedelta(days=hoje.weekday())

PERIODOS = {
    "Este Mês": (primeiro_dia_mes, hoje),
    "Esta Semana": (inicio_semana, hoje),
    "Mês Passado": (primeiro_mes_passado, ultimo_mes_passado),
    "Últimos 30 dias": (hoje - timedelta(days=30), hoje),
    "Últimos 90 dias": (hoje - timedelta(days=90), hoje),
    "Personalizado": None,
}

col_p1, col_p2, col_p3 = st.columns([2, 1, 1])

with col_p1:
    periodo_nome = st.selectbox("Período", list(PERIODOS.keys()), index=0)

if periodo_nome == "Personalizado":
    with col_p2:
        data_inicio = st.date_input("Início", value=primeiro_dia_mes)
    with col_p3:
        data_fim = st.date_input("Fim", value=hoje)
else:
    data_inicio, data_fim = PERIODOS[periodo_nome]
    with col_p2:
        st.date_input("Início", value=data_inicio, disabled=True)
    with col_p3:
        st.date_input("Fim", value=data_fim, disabled=True)

# Converte para datetime para filtrar
dt_inicio = pd.Timestamp(data_inicio)
dt_fim = pd.Timestamp(data_fim) + pd.Timedelta(hours=23, minutes=59, seconds=59)

# =================================================================
# DADOS FILTRADOS POR PERÍODO
# =================================================================
situacoes_venda = config["daily"]["situacoes_venda"]

# Meta sempre do mês atual (não muda com filtro de período)
# Meta: agregar lojas selecionadas
meta_sel = df_metas[df_metas["Loja"].isin(lojas_selecionadas)]
if len(meta_sel) == 0:
    st.warning("Sem dados de meta para as lojas selecionadas.")
    st.stop()

# Agrega: soma tudo
meta_loja = meta_sel.select_dtypes(include="number").sum()
meta_loja["% Atingido"] = meta_loja["Vendido"] / meta_loja["Meta"] if meta_loja["Meta"] > 0 else 0
meta_loja["Falta Vender"] = meta_loja["Meta"] - meta_loja["Vendido"]
hoje_dt = datetime.now()
dia_atual_v = hoje_dt.day
dias_no_mes_v = pd.Timestamp(hoje_dt.year, hoje_dt.month, 1).days_in_month
meta_loja["Run Rate"] = (meta_loja["Vendido"] / dia_atual_v * dias_no_mes_v) if dia_atual_v > 0 else 0
meta_loja["% Projetado"] = meta_loja["Run Rate"] / meta_loja["Meta"] if meta_loja["Meta"] > 0 else 0

if meta_loja["% Atingido"] >= 1:
    meta_loja["Status"] = "🏆 Batida"
elif meta_loja["% Projetado"] >= 1:
    meta_loja["Status"] = "📈 No Ritmo"
elif meta_loja["% Projetado"] >= 0.8:
    meta_loja["Status"] = "🏃 Correndo"
else:
    meta_loja["Status"] = "⚠️ Abaixo"

label_lojas = " + ".join(lojas_selecionadas) if len(lojas_selecionadas) <= 2 else f"{len(lojas_selecionadas)} lojas"

# Pedidos no período (todos os status)
df_periodo = df_loja[(df_loja["Data"] >= dt_inicio) & (df_loja["Data"] <= dt_fim)]

# Vendas efetivas no período
df_vendas_periodo = df_periodo[df_periodo["id_situacao"].isin(situacoes_venda)]

# =================================================================
# CARDS KPI — Meta (linha 1, sempre mês atual)
# =================================================================
c1, c2, c3, c4, c5, c6 = st.columns(6)

with c1:
    st.metric(
        label="💰 Vendido no Mês",
        value=f"R$ {meta_loja['Vendido']:,.0f}",
    )

with c2:
    st.metric(
        label="👕 Peças no Mês",
        value=f"{meta_loja['Pecas']:,.0f}",
    )

with c3:
    st.metric(
        label="🎯 Meta",
        value=f"R$ {meta_loja['Meta']:,.0f}",
        delta=f"{meta_loja['% Atingido']:.1%} atingido",
    )

with c4:
    st.metric(
        label="📊 Projeção",
        value=f"R$ {meta_loja['Run Rate']:,.0f}",
        delta=f"{meta_loja['% Projetado']:.0%} da meta",
    )

with c5:
    st.metric(
        label="🏷️ Falta Vender",
        value=f"R$ {meta_loja['Falta Vender']:,.0f}",
        delta=meta_loja["Status"],
        delta_color="off",
    )

with c6:
    st.metric(
        label="🔖 Desconto no Mês",
        value=f"R$ {meta_loja['Desconto']:,.0f}",
    )

# =================================================================
# CARDS — Pedidos por Status (linha 2, do período selecionado)
# =================================================================
status_ids = config["daily"]["status_ids"]
status_map = {
    "Em aberto": status_ids["em_aberto"],
    "Pronto para Retirada": status_ids["pronto_retirada"],
    "Em andamento": status_ids["em_andamento"],
}

s1, s2, s3, s4 = st.columns(4)

with s1:
    n = len(df_periodo[df_periodo["id_situacao"] == status_map["Em aberto"]])
    st.metric("📋 Em Aberto", n)

with s2:
    n = len(df_periodo[df_periodo["id_situacao"] == status_map["Em andamento"]])
    st.metric("⏳ Em Andamento", n)

with s3:
    n = len(df_periodo[df_periodo["id_situacao"] == status_map["Pronto para Retirada"]])
    st.metric("📦 Pronto p/ Retirada", n)

with s4:
    n = len(df_periodo[df_periodo["id_situacao"].isin(situacoes_venda)])
    st.metric("✅ Atendidos", n)

# =================================================================
# GAUGE — Velocímetro (sempre mês atual)
# =================================================================
fig_gauge = go.Figure(go.Indicator(
    mode="gauge+number",
    value=meta_loja["% Atingido"] * 100,
    number={"suffix": "%", "font": {"size": 48}},
    title={"text": f"Atingimento — {label_lojas}", "font": {"size": 18}},
    gauge={
        "axis": {"range": [0, 120], "ticksuffix": "%"},
        "bar": {"color": "#2ecc71" if meta_loja["% Atingido"] >= 1 else "#3498db"},
        "steps": [
            {"range": [0, 80], "color": "#fadbd8"},
            {"range": [80, 100], "color": "#fdebd0"},
            {"range": [100, 120], "color": "#d5f5e3"},
        ],
        "threshold": {"line": {"color": "red", "width": 2}, "value": 100},
    },
))
fig_gauge.update_layout(height=260, margin=dict(t=60, b=10, l=30, r=30))
st.plotly_chart(fig_gauge, width="stretch")

# =================================================================
# GRÁFICO COMBINADO — Período selecionado
# =================================================================
st.subheader(f"Histórico de Vendas — {data_inicio.strftime('%d/%m')} a {data_fim.strftime('%d/%m/%Y')}")

if len(df_vendas_periodo) > 0:
    diario = (
        df_vendas_periodo
        .groupby(df_vendas_periodo["Data"].dt.date)
        .agg({"Valor": "sum", "Qtd Peças": "sum"})
        .reset_index()
    )
    diario.columns = ["Data", "Valor", "Pecas"]
    diario = diario.sort_values("Data")

    # Preenche dias sem venda
    todos_dias = pd.date_range(data_inicio, data_fim, freq="D")
    todos_dias_df = pd.DataFrame({"Data": todos_dias.date})
    diario = todos_dias_df.merge(diario, on="Data", how="left").fillna(0)

    fig = go.Figure()

    fig.add_trace(go.Bar(
        x=diario["Data"],
        y=diario["Valor"],
        name="Faturamento (R$)",
        marker_color="rgba(52, 152, 219, 0.7)",
        customdata=diario[["Pecas"]],
        hovertemplate=(
            "<b>%{x|%d/%m}</b><br>"
            "Faturamento: R$ %{y:,.2f}<br>"
            "Peças: %{customdata[0]:,.0f}"
            "<extra></extra>"
        ),
    ))

    fig.add_trace(go.Scatter(
        x=diario["Data"],
        y=diario["Pecas"],
        name="Peças Vendidas",
        mode="lines+markers",
        line=dict(color="#2ecc71", width=2),
        marker=dict(size=5),
        yaxis="y2",
        hovertemplate=(
            "<b>%{x|%d/%m}</b><br>"
            "Peças: %{y:,.0f}"
            "<extra></extra>"
        ),
    ))

    fig.update_layout(
        height=380,
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="center", x=0.5),
        yaxis=dict(title="Faturamento (R$)", side="left"),
        yaxis2=dict(title="Peças", side="right", overlaying="y"),
        margin=dict(t=40, b=40),
    )

    st.plotly_chart(fig, width="stretch")
else:
    st.info("Sem vendas no período selecionado.")

# =================================================================
# TABELA — Performance por Vendedor (período selecionado)
# =================================================================
st.subheader("Performance por Vendedor")

if len(df_vendas_periodo) > 0:
    perf_v = (
        df_vendas_periodo
        .groupby("Vendedor")
        .agg(
            Valor=("Valor", "sum"),
            Pecas=("Qtd Peças", "sum"),
            Pedidos=("Pedido", "nunique"),
        )
        .reset_index()
    )

    perf_v["Ticket Médio"] = perf_v["Valor"] / perf_v["Pedidos"]
    perf_v["PA"] = perf_v["Pecas"] / perf_v["Pedidos"]
    perf_v = perf_v.sort_values("Valor", ascending=False).reset_index(drop=True)

    perf_v = perf_v.rename(columns={
        "Valor": "Valor Vendido (R$)",
        "Pecas": "Peças Vendidas",
        "Pedidos": "Qtd Pedidos",
        "Ticket Médio": "Ticket Médio (R$)",
        "PA": "Peças/Atendimento",
    })

    st.dataframe(
        perf_v,
        width="stretch",
        hide_index=True,
        column_config={
            "Valor Vendido (R$)": st.column_config.NumberColumn(format="R$ %.2f"),
            "Ticket Médio (R$)": st.column_config.NumberColumn(format="R$ %.2f"),
            "Peças/Atendimento": st.column_config.NumberColumn(format="%.1f"),
        },
    )

    st.caption(
        f"**{len(perf_v)}** vendedores | "
        f"**{perf_v['Qtd Pedidos'].sum()}** pedidos | "
        f"**R$ {perf_v['Valor Vendido (R$)'].sum():,.2f}** total"
    )
else:
    st.info("Sem vendas no período selecionado.")

# =================================================================
# TABELA — Performance por Colégio (período selecionado)
# =================================================================
st.subheader("Performance por Colégio")

if len(df_vendas_periodo) > 0:
    perf_c = (
        df_vendas_periodo
        .groupby("Colegio")
        .agg(
            Valor=("Valor", "sum"),
            Pecas=("Qtd Peças", "sum"),
            Pedidos=("Pedido", "nunique"),
        )
        .reset_index()
    )

    perf_c["Ticket Médio"] = perf_c["Valor"] / perf_c["Pedidos"]
    perf_c["PA"] = perf_c["Pecas"] / perf_c["Pedidos"]
    perf_c = perf_c.sort_values("Valor", ascending=False).reset_index(drop=True)

    perf_c = perf_c.rename(columns={
        "Colegio": "Colégio",
        "Valor": "Valor Vendido (R$)",
        "Pecas": "Peças Vendidas",
        "Pedidos": "Qtd Pedidos",
        "Ticket Médio": "Ticket Médio (R$)",
        "PA": "Peças/Atendimento",
    })

    st.dataframe(
        perf_c,
        width="stretch",
        hide_index=True,
        column_config={
            "Valor Vendido (R$)": st.column_config.NumberColumn(format="R$ %.2f"),
            "Ticket Médio (R$)": st.column_config.NumberColumn(format="R$ %.2f"),
            "Peças/Atendimento": st.column_config.NumberColumn(format="%.1f"),
        },
    )

    st.caption(
        f"**{len(perf_c)}** colégios | "
        f"**{perf_c['Qtd Pedidos'].sum()}** pedidos | "
        f"**R$ {perf_c['Valor Vendido (R$)'].sum():,.2f}** total"
    )
else:
    st.info("Sem vendas no período selecionado.")
