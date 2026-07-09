"""
Página: Simulador de Produção
Unifica PCP (sugestão por SKU) e Planejamento (rodadas, sazonalidade, projeção).
"""

import streamlit as st
from auth import exigir_login
exigir_login()
import plotly.express as px
import plotly.graph_objects as go
import pandas as pd

from etl.fabrica import processar_fabrica
from etl.planejamento import calcular_sazonalidade, simular_rodadas
from etl.demanda import listar_rodadas_selecionaveis

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

if not dados["validacao"]["ok"]:
    st.error("Dados inválidos. Verifique a página principal.")
    st.stop()

# =================================================================
# PROCESSAMENTO FÁBRICA (cacheado)
# =================================================================

@st.cache_data
def _processar(_dados, _config, mes_disparo=None, ano_disparo=None, ativo_crescimento=None):
    return processar_fabrica(
        _dados, _config, mes_disparo=mes_disparo, ano_disparo=ano_disparo,
        ativo_crescimento=ativo_crescimento,
    )

# Mapa SKU → Tamanho (vem de detalhes, não está no processar_fabrica)
detalhes = dados["detalhes"]
_tam = detalhes[["ID_produto", "Tamanho"]].copy()
_tam = _tam.rename(columns={"ID_produto": "_id_prod"})
_prod_map = dados["produtos"][["ID", "codigo"]].copy()
_prod_map["ID"] = _prod_map["ID"].astype(str).str.strip()
_tam["_id_prod"] = _tam["_id_prod"].astype(str).str.strip()
_tam = _tam.merge(_prod_map, left_on="_id_prod", right_on="ID", how="left")
_tam = _tam.dropna(subset=["codigo"]).drop_duplicates(subset=["codigo"])
_tam_map = _tam.set_index("codigo")["Tamanho"].to_dict()

# =================================================================
# TÍTULO
# =================================================================
st.title("🏭 Simulador de Produção")
st.caption(
    "Configure rodadas de produção e simule cenários. "
    "A aba **Visão Geral** mostra projeção macro; a aba **Sugestão por SKU** detalha cada produto."
)

# =================================================================
# SAZONALIDADE (calculada uma vez)
# =================================================================
sazonalidade = calcular_sazonalidade(dados, config)

# =================================================================
# PARÂMETROS DA SIMULAÇÃO (sempre visível, fora das tabs)
# =================================================================
st.subheader("⚙️ Parâmetros da Simulação")

cfg_plan = config.get("planejamento", {})
cfg_fab = config.get("fabrica", {})

MESES_NOME = {
    1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril",
    5: "Maio", 6: "Junho", 7: "Julho", 8: "Agosto",
    9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro",
}

cfg_dem = config.get("demanda", {})

col1, col2, col3 = st.columns(3)

with col1:
    rodadas_default = cfg_plan.get("rodadas", [7, 10])
    rodadas_sel = st.multiselect(
        "Meses de disparo (cenário de rodadas)",
        options=list(range(1, 13)),
        default=rodadas_default,
        format_func=lambda x: MESES_NOME[x],
        help="Simula um calendário de rodadas alternativo. Cada rodada cobre da "
             "sua chegada até a próxima chegar (política order-up-to).",
    )

with col2:
    ativo_cresc = st.checkbox(
        "Aplicar crescimento planejado (colégio×grupo)",
        value=cfg_dem.get("aplicar_crescimento_fabrica", True),
        help="Liga/desliga o fator de crescimento cadastrado em Configurações → Colégios, "
             "pra comparar o pedido com e sem crescimento.",
    )
    st.caption(f"Nível de serviço: alta {cfg_dem.get('nivel_servico_alta', 99)}% · "
               f"baixa {cfg_dem.get('nivel_servico_baixa', 92)}%")

with col3:
    lt = cfg_plan.get("lead_time_semanas", 4)
    st.metric("Lead Time", f"{lt} semanas")
    janela_alta = cfg_dem.get("janela_alta", [12, 1, 2])
    st.caption("Alta: " + ", ".join(MESES_NOME[m][:3] for m in janela_alta))

tem_rodadas = len(rodadas_sel) > 0
rodadas_sorted = sorted(rodadas_sel)

st.caption(
    "📐 Dimensionamento **order-up-to**: cada rodada repõe até o nível-alvo "
    "(demanda até a próxima chegada + estoque de segurança pelo nível de serviço). "
    "O tamanho de cada rodada **emerge da projeção de estoque** — não há mais % manual."
)

st.divider()

# =================================================================
# TABS
# =================================================================
tab_geral, tab_sku = st.tabs(["📊 Visão Geral", "📋 Sugestão por SKU"])

# =================================================================
# TAB: VISÃO GERAL
# =================================================================
with tab_geral:
    if not tem_rodadas:
        st.warning("Selecione pelo menos 1 mês de disparo nos parâmetros acima para ver a simulação.")
    else:
        # Simulação (order-up-to bottom-up)
        sim = simular_rodadas(dados, config, sazonalidade,
                              rodadas_override=rodadas_sorted,
                              ativo_crescimento=ativo_cresc)

        # KPIs macro
        t = sim["totais"]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Demanda Anual", f"{t['demanda_anual']:,.0f} pçs")
        c2.metric("Produção Total", f"{t['producao_total']:,.0f} pçs")
        c3.metric("Investimento Total", f"R$ {t['investimento_total']:,.2f}")
        c4.metric("Custo Médio", f"R$ {t['custo_medio']:.2f}/pç")
        st.caption(f"Estoque líquido atual da rede (ponto de partida da projeção): "
                   f"{t.get('estoque_inicial', 0):,.0f} pçs")

        # Rodadas detalhadas
        st.subheader("🏭 Rodadas de Produção")
        rodadas_df = pd.DataFrame(sim["rodadas"])

        for _, r in rodadas_df.iterrows():
            with st.expander(f"Rodada {r['rodada']} — {r['nome_disparo']} → chega {r['nome_chegada']}/{r['ano_chegada']}"):
                cols = st.columns(4)
                cols[0].metric("Demanda no período", f"{r['demanda_periodo']:,.0f}")
                cols[1].metric("Produção", f"{r['producao']:,.0f}", f"{r['pct_anual']:.0f}% da demanda anual")
                cols[2].metric("Custo médio pond.", f"R$ {r['custo_medio_ponderado']:.2f}")
                cols[3].metric("Investimento", f"R$ {r['investimento']:,.2f}")

                # De onde vem a produção — abatimento do estoque explícito
                st.markdown(
                    f"**Como se chega na produção:**  \n"
                    f"Alvo (demanda {r['demanda_periodo']:,.0f} + segurança {r['seguranca']:,.0f}) = **{r['sem_estoque']:,.0f}** "
                    f"→ (−) estoque existente útil **{r['abate_estoque']:,.0f}** "
                    f"→ **produção {r['producao']:,.0f} pares**"
                )
                st.caption(
                    "O estoque é abatido SKU a SKU (item×tamanho) — estoque no tamanho errado não abate. "
                    "Por isso o abatimento é menor que o estoque total da rede."
                )

                detalhe_col = r["detalhe_por_colegio"]
                if len(detalhe_col) > 0:
                    st.caption("Produção por colégio nesta rodada:")
                    st.dataframe(detalhe_col, hide_index=True, use_container_width=True)

        # Demanda vs Produção
        st.subheader("📦 Demanda vs Produção por Mês")

        df_dem = sim["demanda_mensal"]
        df_est = sim["estoque_projetado"]

        fig_dem = go.Figure()

        fig_dem.add_trace(go.Bar(
            x=df_dem["NomeMes"],
            y=df_dem["Demanda"],
            name="Demanda",
            marker_color="#EF5350",
            text=df_dem["Demanda"].apply(lambda x: f"{x:,.0f}"),
            textposition="outside",
        ))

        fig_dem.add_trace(go.Bar(
            x=df_est["NomeMes"],
            y=df_est["Entrada"],
            name="Entrada (produção)",
            marker_color="#66BB6A",
            text=df_est["Entrada"].apply(lambda x: f"{x:,.0f}" if x > 0 else ""),
            textposition="outside",
        ))

        fig_dem.update_layout(
            barmode="group",
            height=380,
            yaxis_title="Peças",
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
            margin=dict(t=50),
        )
        st.plotly_chart(fig_dem, use_container_width=True)

        # Curva de estoque projetado
        st.subheader("📉 Projeção de Estoque ao Longo do Ano")

        fig_est = go.Figure()

        cores_est = ["#C62828" if e < 0 else "#388E3C" for e in df_est["EstoqueFinal"]]

        fig_est.add_trace(go.Scatter(
            x=df_est["NomeMes"],
            y=df_est["EstoqueFinal"],
            mode="lines+markers+text",
            name="Estoque Final",
            line=dict(color="#1565C0", width=3),
            marker=dict(size=10, color=cores_est, line=dict(color="#1565C0", width=2)),
            text=[f"{e:,.0f}" for e in df_est["EstoqueFinal"]],
            textposition="top center",
        ))

        fig_est.add_hline(y=0, line_dash="dash", line_color="red",
                          annotation_text="⚠️ Ruptura", annotation_position="bottom right")

        nomes_meses = list(df_est["NomeMes"])
        for r in sim["rodadas"]:
            nome = r["nome_disparo"]
            if nome in nomes_meses:
                idx = nomes_meses.index(nome)
                fig_est.add_shape(
                    type="line", x0=idx, x1=idx, y0=0, y1=1,
                    xref="x", yref="paper",
                    line=dict(color="orange", width=2, dash="dot"),
                )
                fig_est.add_annotation(
                    x=idx, y=1.05, xref="x", yref="paper",
                    text=f"Disparo R{r['rodada']}",
                    showarrow=False, font=dict(size=10, color="orange"),
                )

        fig_est.update_layout(
            height=400,
            yaxis_title="Peças em estoque",
            showlegend=False,
            margin=dict(t=50),
        )
        st.plotly_chart(fig_est, use_container_width=True)

        # Alerta de ruptura
        meses_ruptura = df_est[df_est["EstoqueFinal"] < 0]
        if len(meses_ruptura) > 0:
            nomes = ", ".join(meses_ruptura["NomeMes"].tolist())
            st.error(f"⚠️ **Ruptura projetada** nos meses: {nomes}. "
                     f"Considere antecipar/adicionar uma rodada ou elevar o nível de serviço.")
        else:
            st.success("✅ Sem ruptura projetada com esta configuração.")

        # Tabela mensal detalhada
        with st.expander("📋 Tabela Detalhada — Mês a Mês"):
            df_detalhe = df_est.merge(df_dem[["Mes", "Peso"]], on="Mes")
            df_detalhe = df_detalhe[["NomeMes", "Peso", "Entrada", "Demanda", "EstoqueFinal"]]
            df_detalhe = df_detalhe.rename(columns={
                "NomeMes": "Mês",
                "Peso": "Sazonalidade",
                "Entrada": "Entrada (pçs)",
                "Demanda": "Saída (pçs)",
                "EstoqueFinal": "Estoque Final",
            })

            def _cor_estoque(val):
                if isinstance(val, (int, float)):
                    if val < 0:
                        return "background-color: #FFCDD2; color: #B71C1C"
                    elif val == 0:
                        return "background-color: #FFF9C4"
                return ""

            styled = df_detalhe.style.map(_cor_estoque, subset=["Estoque Final"])
            st.dataframe(styled, use_container_width=True, hide_index=True)

# =================================================================
# TAB: SUGESTÃO POR SKU
# =================================================================
with tab_sku:
    # Seletor de rodada — qual pedido de abastecimento você está montando
    rodadas_opts = listar_rodadas_selecionaveis(config)

    if rodadas_opts:
        idx = st.selectbox(
            "Pedido de qual rodada?",
            options=list(range(len(rodadas_opts))),
            format_func=lambda i: rodadas_opts[i]["label"],
            help="Cada rodada cobre da sua chegada até a rodada seguinte chegar. "
                 "A primeira opção é a rodada mais próxima de ser disparada.",
        )
        sel = rodadas_opts[idx]
        df = _processar(dados, config, sel["mes_disparo"], sel["ano_disparo"], ativo_cresc)
    else:
        df = _processar(dados, config, None, None, ativo_cresc)

    df["Tamanho"] = df["SKU"].map(_tam_map).fillna("")

    # Janela de cobertura — responde "essa sugestão cobre até qual abastecimento?"
    janela_label = df["JanelaLabel"].iloc[0] if len(df) > 0 else ""
    st.info(f"📦 {janela_label} — a sugestão abaixo é o pedido dessa rodada "
            f"(descontando estoque atual e backlog).")

    # KPIs
    skus_produzir = df[df["SugestaoProducao"] > 0]
    total_pares = skus_produzir["SugestaoProducao"].sum()
    total_investimento = skus_produzir["InvestimentoFabril"].sum()
    backlog_total = df["Backlog"].sum()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("SKUs para Produzir", len(skus_produzir))
    c2.metric("Total de Pares", f"{total_pares:,.0f}")
    c3.metric("Investimento Fabril", f"R$ {total_investimento:,.2f}")
    c4.metric("Backlog (Em Carteira)", f"{backlog_total:,.0f} pçs")

    # Parâmetros do cálculo
    with st.expander("⚙️ Metodologia do Cálculo"):
        cfg = config["fabrica"]
        cfg_d = config.get("demanda", {})
        janela_alta_m = cfg_d.get("janela_alta", [12, 1, 2])
        st.write(f"**Rodada:** {janela_label}")
        st.markdown(
            "- **Demanda ancorada na alta**: vendas reais da última temporada de alta "
            f"({', '.join(MESES_NOME[m] for m in janela_alta_m)}) × crescimento; a baixa só adiciona a demanda de baixa (proporção da alta).\n"
            "- **Política order-up-to**: repõe até o **Estoque-Alvo** (= demanda do período + estoque de segurança), "
            "descontando o estoque projetado na chegada (projeção forward).\n"
            f"- **Estoque de segurança** pelo nível de serviço: alta **{cfg_d.get('nivel_servico_alta', 99)}%**, "
            f"baixa **{cfg_d.get('nivel_servico_baixa', 92)}%** · Variação da Demanda {cfg_d.get('variacao_demanda', 0.25)}.\n"
            f"- **Crescimento aplicado:** {'sim' if ativo_cresc else 'não (comparação)'} · "
            f"correção manual global: {cfg.get('correcao_manual', 0)}"
        )

    # Filtros
    col_f1, col_f2 = st.columns(2)

    with col_f1:
        mostrar = st.radio(
            "Exibir",
            ["Só com produção sugerida", "Todos os SKUs"],
            horizontal=True
        )

    with col_f2:
        colegios = df["Colegio"].dropna().astype(str)
        colegios = colegios[colegios.ne("") & colegios.ne("nan")]
        colegios_disp = ["Todos"] + sorted(colegios.unique().tolist())
        filtro_colegio = st.selectbox("Colégio", colegios_disp)

    col_f3, col_f4 = st.columns(2)

    with col_f3:
        cats = df["Categoria"].dropna().astype(str)
        cats = cats[cats.ne("") & cats.ne("nan")]
        categorias = ["Todas"] + sorted(cats.unique().tolist())
        filtro_cat = st.selectbox("Categoria", categorias)

    with col_f4:
        filtro_texto = st.text_input("🔍 Buscar SKU ou Produto", placeholder="Digite para filtrar...")

    df_filtrado = df.copy()
    if mostrar == "Só com produção sugerida":
        df_filtrado = df_filtrado[df_filtrado["SugestaoProducao"] > 0]
    if filtro_colegio != "Todos":
        df_filtrado = df_filtrado[df_filtrado["Colegio"] == filtro_colegio]
    if filtro_cat != "Todas":
        df_filtrado = df_filtrado[df_filtrado["Categoria"] == filtro_cat]
    if filtro_texto.strip():
        termo = filtro_texto.strip().lower()
        df_filtrado = df_filtrado[
            df_filtrado["SKU"].str.lower().str.contains(termo, na=False) |
            df_filtrado["Produto"].str.lower().str.contains(termo, na=False)
        ]

    # Tabela principal
    st.subheader("Sugestão de Produção")

    # Rótulo honesto: a "demanda do período" cobre da chegada desta rodada até
    # a próxima chegar — o cabeçalho diz o intervalo pra não convidar a
    # comparação errada com as vendas da alta (3 meses).
    _cobre = df_filtrado["JanelaLabel"].iloc[0].split("cobre ")[-1] if len(df_filtrado) else "—"
    st.info(
        f"📆 **Esta rodada cobre: {_cobre}** — a coluna *Demanda do Período* soma a demanda "
        f"desse intervalo inteiro (pico **e** baixa), por isso pode ser maior que *Vendas Alta* "
        f"(que são só os 3 meses de pico da última temporada)."
    )

    st.dataframe(
        df_filtrado[[
            "SKU", "Produto", "Tamanho", "Colegio", "Grupo", "Categoria",
            "VendasHist", "EstoqueRede", "Backlog",
            "DemandaProjetada", "DemandaPeriodoAlta", "DemandaPeriodoBaixa",
            "EstoqueSeguranca", "EstoqueMeta", "EstoqueProjetado",
            "SugestaoProducao", "NivelServico", "CustoUnit", "InvestimentoFabril"
        ]],
        use_container_width=True,
        hide_index=True,
        height=900,  # ~25 linhas visíveis antes de rolar
        column_config={
            "Tamanho": st.column_config.TextColumn("Tam."),
            "VendasHist": st.column_config.NumberColumn("Vendas Alta", help="Vendas da última temporada de alta (histórico cru, sem crescimento) — só os meses de pico"),
            "EstoqueRede": st.column_config.NumberColumn("Est. Rede", help="Saldo físico em todos os depósitos hoje"),
            "DemandaProjetada": st.column_config.NumberColumn(f"Demanda do Período ({_cobre})", format="%.1f", help="Demanda projetada da chegada desta rodada até a próxima chegar = coluna 'na alta' + 'na baixa'"),
            "DemandaPeriodoAlta": st.column_config.NumberColumn("· na alta", format="%.1f", help="Parcela da Demanda do Período que cai nos meses de pico (vendas reais × crescimento)"),
            "DemandaPeriodoBaixa": st.column_config.NumberColumn("· na baixa", format="%.1f", help="Parcela da Demanda do Período que cai nos meses de baixa (demanda de baixa espalhada)"),
            "EstoqueSeguranca": st.column_config.NumberColumn("Segurança", format="%.1f", help="Estoque de segurança pelo nível de serviço"),
            "EstoqueMeta": st.column_config.NumberColumn("Alvo (S)", help="Nível-alvo order-up-to = demanda do período + segurança"),
            "EstoqueProjetado": st.column_config.NumberColumn("Est. Projetado", format="%.1f", help="Estoque projetado na chegada da rodada (já desconta o consumo até lá e chegadas de rodadas anteriores). O estoque de HOJE está em Est. Rede"),
            "SugestaoProducao": st.column_config.NumberColumn("Sugestão (pares)", help="Pedido = Alvo − Est. Projetado, arredondado a par"),
            "NivelServico": st.column_config.NumberColumn("NS %", help="Nível de serviço aplicado (alta vs baixa)"),
            "CustoUnit": st.column_config.NumberColumn("Custo Unit (R$)", format="R$ %.2f"),
            "InvestimentoFabril": st.column_config.NumberColumn("Investimento (R$)", format="R$ %.2f"),
        },
    )

    st.caption(
        f"**{len(df_filtrado)}** SKUs | "
        f"**{df_filtrado['SugestaoProducao'].sum():,.0f}** pares totais | "
        f"**R$ {df_filtrado['InvestimentoFabril'].sum():,.2f}** investimento"
    )

    # Top 10 SKUs por investimento
    if len(skus_produzir) > 0:
        st.subheader("Top 10 SKUs — Maior Investimento Fabril")
        top10 = skus_produzir.nlargest(10, "InvestimentoFabril")

        fig_top = px.bar(
            top10, x="SKU", y="InvestimentoFabril",
            text=top10["InvestimentoFabril"].apply(lambda x: f"R$ {x:,.0f}"),
            color="SugestaoProducao",
            color_continuous_scale="Blues",
        )
        fig_top.update_layout(height=400, xaxis_tickangle=-45)
        fig_top.update_traces(textposition="outside")
        st.plotly_chart(fig_top, use_container_width=True)

    # Export CSV
    st.subheader("Exportar")

    # Monta tabela de exportação
    df_export = df_filtrado[[
        "SKU", "Produto", "Tamanho", "Colegio", "Grupo", "Categoria",
        "VendasHist", "EstoqueRede", "Backlog",
        "DemandaProjetada", "DemandaPeriodoAlta", "DemandaPeriodoBaixa",
        "EstoqueSeguranca", "EstoqueMeta", "EstoqueProjetado",
        "SugestaoProducao", "NivelServico", "CustoUnit", "InvestimentoFabril"
    ]].copy()

    df_export = df_export.rename(columns={
        "Produto": "Descrição",
        "Tamanho": "Tam",
        "Colegio": "Colégio",
        "VendasHist": "Vendas Alta",
        "EstoqueRede": "Estoque Rede",
        "DemandaProjetada": f"Demanda do Período ({_cobre})",
        "DemandaPeriodoAlta": "Demanda na alta",
        "DemandaPeriodoBaixa": "Demanda na baixa",
        "EstoqueSeguranca": "Estoque Segurança",
        "EstoqueMeta": "Alvo (S)",
        "EstoqueProjetado": "Estoque Projetado",
        "SugestaoProducao": "Sugestão (pares)",
        "NivelServico": "Nível Serviço (%)",
        "CustoUnit": "Custo Unit (R$)",
        "InvestimentoFabril": "Investimento (R$)",
    })

    # Formata valores monetários para PT-BR
    def _fmt_brl(x):
        return f"R$ {x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    df_export["Custo Unit (R$)"] = df_export["Custo Unit (R$)"].apply(_fmt_brl)
    df_export["Investimento (R$)"] = df_export["Investimento (R$)"].apply(_fmt_brl)

    # Monta cabeçalho com parâmetros da simulação
    cfg_d = config.get("demanda", {})
    cfg_p = config.get("planejamento", {})
    linhas_param = []
    linhas_param.append(f"# Sugestão de Produção (order-up-to)")
    linhas_param.append(f"# {janela_label}")
    linhas_param.append(f"# Período histórico: {cfg_p.get('periodo_historico_inicio')} a {cfg_p.get('periodo_historico_fim')}")
    linhas_param.append(f"# Janela de alta: {cfg_d.get('janela_alta')}")
    linhas_param.append(f"# Nível de serviço: alta {cfg_d.get('nivel_servico_alta', 99)}% / baixa {cfg_d.get('nivel_servico_baixa', 92)}% · Variação da Demanda {cfg_d.get('variacao_demanda', 0.25)}")
    linhas_param.append(f"# Crescimento aplicado: {'sim' if ativo_cresc else 'nao'}")
    linhas_param.append("")

    csv_header = "\n".join(linhas_param)
    csv_data = df_export.to_csv(index=False, sep=";", decimal=",")
    csv_completo = (csv_header + csv_data).encode("utf-8")

    st.download_button(
        label="⬇️ Baixar CSV (para importar no Excel/Sheets)",
        data=csv_completo,
        file_name="simulador_producao.csv",
        mime="text/csv",
    )

# =================================================================
# SAZONALIDADE (expander no final, fora das tabs)
# =================================================================
with st.expander("📈 Sazonalidade Mensal (Histórico)"):
    fig_saz = go.Figure()

    cores = ["#E8F5E9" if p >= 1.0 else "#FFEBEE" for p in sazonalidade["PesoNormalizado"]]
    fig_saz.add_trace(go.Bar(
        x=sazonalidade["NomeMes"],
        y=sazonalidade["PesoNormalizado"],
        marker_color=cores,
        marker_line_color=["#388E3C" if p >= 1.0 else "#C62828" for p in sazonalidade["PesoNormalizado"]],
        marker_line_width=1.5,
        text=[f"{p:.2f}x" for p in sazonalidade["PesoNormalizado"]],
        textposition="outside",
        hovertemplate="<b>%{x}</b><br>Peso: %{y:.2f}x<br>Vendas: %{customdata:.0f}<extra></extra>",
        customdata=sazonalidade["Vendas"],
    ))

    fig_saz.add_hline(y=1.0, line_dash="dash", line_color="gray",
                      annotation_text="Média (1.0x)", annotation_position="top right")

    fig_saz.update_layout(
        height=320,
        yaxis_title="Peso",
        showlegend=False,
        margin=dict(t=30),
    )
    st.plotly_chart(fig_saz, use_container_width=True)

    col_s1, col_s2 = st.columns(2)
    with col_s1:
        st.caption("🟢 Verde = acima da média (alta) | 🔴 Vermelho = abaixo da média (baixa)")
    with col_s2:
        periodo = config.get("planejamento", {})
        st.caption(f"Período: {periodo.get('periodo_historico_inicio', '?')} a {periodo.get('periodo_historico_fim', '?')}")
