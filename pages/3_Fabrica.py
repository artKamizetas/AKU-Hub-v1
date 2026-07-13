"""
Página: Simulador de Produção
Unifica PCP (sugestão por SKU) e Planejamento (rodadas, sazonalidade, projeção).

Duas altitudes da mesma metodologia order-up-to:
  • Visão Geral (estratégico): planeja o ano — cenário de rodadas, projeção de estoque.
  • Sugestão por SKU (tático): monta o pedido de UMA rodada.
O topo guarda só o que é transversal às duas; cada aba tem seu contexto.
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
from pedidos import builder as pedidos_builder
from pedidos.repositorio import obter_repositorio, RodadaJaCongelada

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

MESES_NOME = {
    1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril",
    5: "Maio", 6: "Junho", 7: "Julho", 8: "Agosto",
    9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro",
}

# Paleta — série ≠ semântico. Azul/verde (colorblind-safe) para as séries;
# vermelho reservado a alerta (ruptura). Não depende só de verde/vermelho.
COR_DEMANDA = "#4C78A8"    # azul — saída (demanda)
COR_PRODUCAO = "#59A14F"   # verde — entrada (produção)
COR_ESTOQUE = "#3E7CB1"    # azul — linha de estoque projetado
COR_ALERTA = "#D64550"     # vermelho — SÓ ruptura/negativo
COR_ALTA = "#4C78A8"       # azul — meses acima da média
COR_BAIXA = "#AEB6BD"      # cinza — meses abaixo da média

cfg_plan = config.get("planejamento", {})
cfg_fab = config.get("fabrica", {})
cfg_dem = config.get("demanda", {})

# =================================================================
# TÍTULO
# =================================================================
st.title("🏭 Simulador de Produção")
st.caption(
    "**Visão Geral** planeja o ano (cenário de rodadas + projeção de estoque). "
    "**Sugestão por SKU** monta o pedido de uma rodada específica."
)

# Sazonalidade (calculada uma vez — usada na projeção e no gráfico de contexto)
sazonalidade = calcular_sazonalidade(dados, config)

# =================================================================
# CONTEXTO GLOBAL (transversal às duas abas)
# =================================================================
gc1, gc2, gc3 = st.columns([1.4, 1, 1])

with gc1:
    ativo_cresc = st.toggle(
        "Aplicar crescimento planejado (colégio×grupo)",
        value=cfg_dem.get("aplicar_crescimento_fabrica", True),
        help="Liga/desliga o fator de crescimento cadastrado em Configurações → Colégios. "
             "Vale para as DUAS abas — serve para comparar o pedido com e sem crescimento.",
    )
with gc2:
    lt = cfg_plan.get("lead_time_semanas", 4)
    st.metric("Lead Time", f"{lt} semanas", help="Tempo entre disparar e receber a rodada")
with gc3:
    st.metric(
        "Nível de serviço",
        f"{cfg_dem.get('nivel_servico_alta', 99)}% / {cfg_dem.get('nivel_servico_baixa', 92)}%",
        help="Alta / baixa temporada — define o estoque de segurança",
    )

janela_alta = cfg_dem.get("janela_alta", [12, 1, 2])
st.caption(
    "📐 Dimensionamento **order-up-to**: cada rodada repõe até o nível-alvo "
    "(demanda até a próxima chegada + estoque de segurança). O tamanho **emerge da "
    "projeção de estoque** — sem % manual.  ·  Alta: "
    + ", ".join(MESES_NOME[m][:3] for m in janela_alta)
)

st.divider()

# =================================================================
# QUADRO 1: VISÃO GERAL (estratégico) — tudo delimitado num container
# =================================================================
with st.container(border=True):
    st.subheader("📊 Visão Geral — planejamento anual")
    # --- Contexto: sazonalidade (formato do ano) ---
    with st.expander("📈 Sazonalidade mensal (formato histórico do ano)"):
        fig_saz = go.Figure()
        cores_saz = [COR_ALTA if p >= 1.0 else COR_BAIXA for p in sazonalidade["PesoNormalizado"]]
        fig_saz.add_trace(go.Bar(
            x=sazonalidade["NomeMes"],
            y=sazonalidade["PesoNormalizado"],
            marker_color=cores_saz,
            text=[f"{p:.2f}x" for p in sazonalidade["PesoNormalizado"]],
            textposition="outside",
            hovertemplate="<b>%{x}</b><br>Peso: %{y:.2f}x<br>Vendas: %{customdata:.0f}<extra></extra>",
            customdata=sazonalidade["Vendas"],
        ))
        fig_saz.add_hline(y=1.0, line_dash="dash", line_color="gray",
                          annotation_text="Média (1.0x)", annotation_position="top right")
        fig_saz.update_layout(height=300, yaxis_title="Peso", showlegend=False, margin=dict(t=30))
        st.plotly_chart(fig_saz, width="stretch")
        st.caption(
            f"🔵 Azul = acima da média (alta) · ⚪ Cinza = abaixo (baixa)  ·  "
            f"Período: {cfg_plan.get('periodo_historico_inicio', '?')} a {cfg_plan.get('periodo_historico_fim', '?')}"
        )

    # --- Calendário de rodadas (definido em Configurações → Produção) ---
    datas_rodadas = sorted(str(d) for d in (cfg_plan.get("rodadas_datas") or []))
    tem_rodadas = len(datas_rodadas) >= 2

    if datas_rodadas:
        rotulos = ", ".join(pd.Timestamp(d).strftime("%b/%y") for d in datas_rodadas)
        st.caption(f"📅 **Calendário de rodadas:** {rotulos}  ·  edite em "
                   "*Configurações → Produção → calendário de rodadas*.")

    if not tem_rodadas:
        st.warning(
            "Calendário de rodadas não configurado (são necessárias 2+ datas — a última "
            "só fecha o intervalo da penúltima). Defina em "
            "**Configurações → Produção → calendário de rodadas**."
        )
    else:
        sim = simular_rodadas(dados, config, sazonalidade, ativo_crescimento=ativo_cresc)

        # --- KPIs macro: dois grupos (deste ano × total dos anos planejados) ---
        # Agregados por ANO DE CHEGADA das rodadas — cada rodada contribui pro
        # seu ano. Os intervalos order-up-to são consecutivos e não se sobrepõem,
        # então somar demanda_periodo não duplica (cobre a linha do tempo inteira).
        rodadas = sim["rodadas"]
        ano_atual = pd.Timestamp.now().year
        anos_plano = sorted({r["ano_chegada"] for r in rodadas})

        def _kpis(rods):
            dem = sum(r["demanda_periodo"] for r in rods)
            prod = sum(r["producao"] for r in rods)
            inv = sum(r["investimento"] for r in rods)
            custo = (inv / prod) if prod else 0.0
            cob = (prod / dem * 100) if dem else 0.0
            return dem, prod, inv, custo, cob

        def _bloco_kpis(rods):
            dem, prod, inv, custo, cob = _kpis(rods)
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("Demanda", f"{dem:,.0f} pçs")
            k2.metric("Produção", f"{prod:,.0f} pçs",
                      delta=f"cobre {cob:.0f}% da demanda", delta_color="off")
            k3.metric("Investimento", f"R$ {inv:,.2f}")
            k4.metric("Custo Médio", f"R$ {custo:.2f}/pç")

        rodadas_ano = [r for r in rodadas if r["ano_chegada"] == ano_atual]

        st.markdown(f"**📅 Deste ano ({ano_atual})** — rodadas que chegam em {ano_atual}")
        if rodadas_ano:
            _bloco_kpis(rodadas_ano)
        else:
            st.caption(f"Nenhuma rodada chega em {ano_atual}.")

        span = f"{anos_plano[0]}" if len(anos_plano) <= 1 else f"{anos_plano[0]}–{anos_plano[-1]}"
        st.markdown(f"**📊 Total planejado ({span})** — somatório de todas as {len(rodadas)} rodadas")
        _bloco_kpis(rodadas)

        st.caption(f"Estoque líquido atual da rede (ponto de partida da projeção): "
                   f"{sim['totais'].get('estoque_inicial', 0):,.0f} pçs")

        # --- Resumo das rodadas (comparação de relance) ANTES do detalhe ---
        st.subheader("🏭 Rodadas de Produção")
        rodadas_df = pd.DataFrame(sim["rodadas"])

        resumo = pd.DataFrame([{
            "Rodada": r["rodada"],
            "Disparo → Chegada": f"{r['nome_disparo']} → {r['nome_chegada']}/{r['ano_chegada']}",
            "Demanda": r["demanda_periodo"],
            "Produção": r["producao"],
            "% anual": r["pct_anual"],
            "Investimento": r["investimento"],
        } for _, r in rodadas_df.iterrows()])
        st.dataframe(
            resumo, hide_index=True, width="stretch",
            column_config={
                "Rodada": st.column_config.NumberColumn("R", width="small"),
                "Demanda": st.column_config.NumberColumn("Demanda (pçs)", format="%.0f"),
                "Produção": st.column_config.NumberColumn("Produção (pçs)", format="%.0f"),
                "% anual": st.column_config.NumberColumn("% da demanda anual", format="%.0f%%"),
                "Investimento": st.column_config.NumberColumn("Investimento", format="R$ %.2f"),
            },
        )

        st.caption("Abra cada rodada para ver **como se chega no número** (alvo − estoque útil).")
        for _, r in rodadas_df.iterrows():
            with st.expander(f"Rodada {r['rodada']} — {r['nome_disparo']} → chega {r['nome_chegada']}/{r['ano_chegada']}"):
                cols = st.columns(4)
                cols[0].metric("Demanda no período", f"{r['demanda_periodo']:,.0f}")
                cols[1].metric("Produção", f"{r['producao']:,.0f}", f"{r['pct_anual']:.0f}% da demanda anual",
                               delta_color="off")
                cols[2].metric("Custo médio pond.", f"R$ {r['custo_medio_ponderado']:.2f}")
                cols[3].metric("Investimento", f"R$ {r['investimento']:,.2f}")

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
                    st.dataframe(detalhe_col, hide_index=True, width="stretch")

        # --- Demanda vs Produção ---
        # Janela real de 12 meses a partir de hoje, datada (Mês/Ano) — as duas
        # séries vêm do MESMO df_est, então nada mistura anos. Rodadas com chegada
        # além dos 12 meses ficam na tabela de rodadas acima.
        st.subheader("📦 Demanda vs Produção — próximos 12 meses")
        df_dem = sim["demanda_mensal"]
        df_est = sim["estoque_projetado"]

        rodadas_fora = [
            r for r in sim["rodadas"]
            if pd.Timestamp(year=r["ano_chegada"], month=r["mes_chegada"], day=1)
            > df_est["Data"].max()
        ]

        fig_dem = go.Figure()
        fig_dem.add_trace(go.Bar(
            x=df_est["Rotulo"], y=df_est["Demanda"], name="Demanda",
            marker_color=COR_DEMANDA,
            text=df_est["Demanda"].apply(lambda x: f"{x:,.0f}"), textposition="outside",
        ))
        fig_dem.add_trace(go.Bar(
            x=df_est["Rotulo"], y=df_est["Entrada"], name="Entrada (produção)",
            marker_color=COR_PRODUCAO,
            text=df_est["Entrada"].apply(lambda x: f"{x:,.0f}" if x > 0 else ""), textposition="outside",
        ))
        fig_dem.update_layout(
            barmode="group", height=380, yaxis_title="Peças",
            legend=dict(orientation="h", yanchor="bottom", y=1.02), margin=dict(t=50),
        )
        st.plotly_chart(fig_dem, width="stretch")
        if rodadas_fora:
            nomes_fora = ", ".join(f"R{r['rodada']} ({r['nome_chegada'][:3]}/{r['ano_chegada']})"
                                   for r in rodadas_fora)
            st.caption(f"⏭️ Janela de 12 meses a partir de hoje. Rodada(s) além do horizonte "
                       f"— **{nomes_fora}** — não aparecem aqui; veja a tabela **🏭 Rodadas de Produção** acima.")

        # --- Curva de estoque projetado ---
        st.subheader("📉 Projeção de Estoque — próximos 12 meses")
        fig_est = go.Figure()
        cores_est = [COR_ALERTA if e < 0 else COR_PRODUCAO for e in df_est["EstoqueFinal"]]
        fig_est.add_trace(go.Scatter(
            x=df_est["Rotulo"], y=df_est["EstoqueFinal"],
            mode="lines+markers+text", name="Estoque Final",
            line=dict(color=COR_ESTOQUE, width=3),
            marker=dict(size=10, color=cores_est, line=dict(color=COR_ESTOQUE, width=2)),
            text=[f"{e:,.0f}" for e in df_est["EstoqueFinal"]], textposition="top center",
        ))
        fig_est.add_hline(y=0, line_dash="dash", line_color=COR_ALERTA,
                          annotation_text="⚠️ Ruptura", annotation_position="bottom right")

        rotulos_meses = list(df_est["Rotulo"])
        for r in sim["rodadas"]:
            rotulo_disp = f"{r['nome_disparo'][:3]}/{str(r['ano_disparo'])[2:]}"
            if rotulo_disp in rotulos_meses:
                idx = rotulos_meses.index(rotulo_disp)
                fig_est.add_shape(type="line", x0=idx, x1=idx, y0=0, y1=1,
                                  xref="x", yref="paper",
                                  line=dict(color="orange", width=2, dash="dot"))
                fig_est.add_annotation(x=idx, y=1.05, xref="x", yref="paper",
                                       text=f"Disparo R{r['rodada']}", showarrow=False,
                                       font=dict(size=10, color="orange"))
        fig_est.update_layout(height=400, yaxis_title="Peças em estoque",
                              showlegend=False, margin=dict(t=50))
        st.plotly_chart(fig_est, width="stretch")

        meses_ruptura = df_est[df_est["EstoqueFinal"] < 0]
        if len(meses_ruptura) > 0:
            nomes = ", ".join(meses_ruptura["Rotulo"].tolist())
            st.error(f"⚠️ **Ruptura projetada** em: {nomes}. "
                     f"Considere antecipar/adicionar uma rodada ou elevar o nível de serviço.")
        else:
            st.success("✅ Sem ruptura projetada com esta configuração.")

        with st.expander("📋 Tabela Detalhada — Mês a Mês"):
            df_detalhe = df_est.merge(df_dem[["Mes", "Peso"]], on="Mes")
            df_detalhe = df_detalhe[["Rotulo", "Peso", "Entrada", "Demanda", "EstoqueFinal"]]
            df_detalhe = df_detalhe.rename(columns={
                "Rotulo": "Mês", "Peso": "Sazonalidade",
                "Entrada": "Entrada (pçs)", "Demanda": "Saída (pçs)",
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
            st.dataframe(styled, width="stretch", hide_index=True)

# =================================================================
# QUADRO 2: SUGESTÃO POR SKU (tático)
# =================================================================
# Colunas: enxuta (decisão) por padrão; memória de cálculo (auditoria) sob toggle.
COLS_LEAN = [
    "SKU", "Produto", "Tamanho", "Colegio", "EstoqueRede", "Backlog",
    "DemandaProjetada", "EstoqueMeta", "EstoqueProjetado",
    "SugestaoProducao", "InvestimentoFabril",
]
COLS_FULL = [
    "SKU", "Produto", "Tamanho", "Colegio", "Grupo", "Categoria",
    "VendasHist", "EstoqueRede", "Backlog",
    "DemandaProjetada", "DemandaPeriodoAlta", "DemandaPeriodoBaixa",
    "EstoqueSeguranca", "EstoqueMeta", "EstoqueProjetado",
    "SugestaoProducao", "NivelServico", "CustoUnit", "InvestimentoFabril",
]

with st.container(border=True):
    st.subheader("📋 Sugestão por SKU — pedido de uma rodada")
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

    janela_label = df["JanelaLabel"].iloc[0] if len(df) > 0 else ""
    _cobre = janela_label.split("cobre ")[-1] if "cobre " in janela_label else "—"

    # --- Cartão de contexto único da rodada (consolida as 3 mensagens antigas) ---
    with st.container(border=True):
        cc1, cc2 = st.columns([5, 1])
        with cc1:
            st.markdown(f"**📦 {janela_label}**")
            st.caption(
                f"A sugestão é o **pedido desta rodada** (descontando estoque atual e backlog). "
                f"A coluna *Demanda do Período* soma o intervalo inteiro (pico **e** baixa: {_cobre}) — "
                f"por isso pode superar *Vendas Alta*, que são só os 3 meses de pico."
            )
        with cc2:
            with st.popover("ⓘ Metodologia", width="stretch"):
                cfg_c = config["fabrica"]
                janela_alta_m = cfg_dem.get("janela_alta", [12, 1, 2])
                st.markdown(
                    "- **Demanda ancorada na alta**: vendas reais da última temporada de alta "
                    f"({', '.join(MESES_NOME[m] for m in janela_alta_m)}) × crescimento; a baixa só adiciona a demanda de baixa.\n"
                    "- **Política order-up-to**: repõe até o **Estoque-Alvo** (demanda do período + segurança), "
                    "descontando o estoque projetado na chegada.\n"
                    f"- **Segurança** pelo nível de serviço: alta **{cfg_dem.get('nivel_servico_alta', 99)}%**, "
                    f"baixa **{cfg_dem.get('nivel_servico_baixa', 92)}%** · Variação {cfg_dem.get('variacao_demanda', 0.25)}.\n"
                    f"- **Crescimento aplicado:** {'sim' if ativo_cresc else 'não (comparação)'} · "
                    f"correção manual global: {cfg_c.get('correcao_manual', 0)}"
                )

    # --- KPIs ---
    skus_produzir = df[df["SugestaoProducao"] > 0]
    total_pares = skus_produzir["SugestaoProducao"].sum()
    total_investimento = skus_produzir["InvestimentoFabril"].sum()
    backlog_total = df["Backlog"].sum()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("SKUs para Produzir", len(skus_produzir))
    c2.metric("Total de Pares", f"{total_pares:,.0f}")
    c3.metric("Investimento Fabril", f"R$ {total_investimento:,.2f}")
    c4.metric("Backlog (Em Carteira)", f"{backlog_total:,.0f} pçs",
              help="Já embutido no Est. Projetado — consome estoque antes de repor")

    # --- Filtros em uma linha ---
    f1, f2, f3, f4 = st.columns([2, 1, 1, 1.3])
    with f1:
        filtro_texto = st.text_input("🔍 Buscar SKU ou Produto", placeholder="Digite para filtrar...")
    with f2:
        colegios = df["Colegio"].dropna().astype(str)
        colegios = colegios[colegios.ne("") & colegios.ne("nan")]
        filtro_colegio = st.multiselect(
            "Colégio", sorted(colegios.unique().tolist()),
            placeholder="Todos", help="Selecione um ou mais colégios (vazio = todos)",
        )
    with f3:
        cats = df["Categoria"].dropna().astype(str)
        cats = cats[cats.ne("") & cats.ne("nan")]
        filtro_cat = st.selectbox("Categoria", ["Todas"] + sorted(cats.unique().tolist()))
    with f4:
        exibir = st.segmented_control(
            "Exibir", ["Com produção", "Todos"], default="Com produção",
            help="'Com produção' mostra só SKUs com sugestão > 0",
        )

    df_filtrado = df.copy()
    if exibir != "Todos":
        df_filtrado = df_filtrado[df_filtrado["SugestaoProducao"] > 0]
    if filtro_colegio:
        df_filtrado = df_filtrado[df_filtrado["Colegio"].isin(filtro_colegio)]
    if filtro_cat != "Todas":
        df_filtrado = df_filtrado[df_filtrado["Categoria"] == filtro_cat]
    if filtro_texto.strip():
        termo = filtro_texto.strip().lower()
        df_filtrado = df_filtrado[
            df_filtrado["SKU"].str.lower().str.contains(termo, na=False) |
            df_filtrado["Produto"].str.lower().str.contains(termo, na=False)
        ]

    # --- Tabela: enxuta por padrão, memória de cálculo sob demanda ---
    th1, th2 = st.columns([3, 2])
    with th1:
        st.subheader("Sugestão de Produção")
    with th2:
        ver_memoria = st.toggle(
            "🧮 Mostrar memória de cálculo",
            help="Revela as colunas de auditoria (Vendas Alta, alta/baixa, Segurança, NS, Custo) — "
                 "como cada número foi obtido.",
        )

    colunas_visiveis = COLS_FULL if ver_memoria else COLS_LEAN
    _column_config = {
        "Tamanho": st.column_config.TextColumn("Tam."),
        "VendasHist": st.column_config.NumberColumn("Vendas Alta", help="Vendas da última temporada de alta (histórico cru, sem crescimento) — só os meses de pico"),
        "EstoqueRede": st.column_config.NumberColumn("Est. Rede", help="Saldo físico em todos os depósitos hoje"),
        "DemandaProjetada": st.column_config.NumberColumn(f"Demanda do Período ({_cobre})", format="%.1f", help="Demanda projetada da chegada desta rodada até a próxima chegar = 'na alta' + 'na baixa'"),
        "DemandaPeriodoAlta": st.column_config.NumberColumn("· na alta", format="%.1f", help="Parcela da Demanda do Período nos meses de pico (vendas reais × crescimento)"),
        "DemandaPeriodoBaixa": st.column_config.NumberColumn("· na baixa", format="%.1f", help="Parcela da Demanda do Período nos meses de baixa (demanda de baixa espalhada)"),
        "EstoqueSeguranca": st.column_config.NumberColumn("Segurança", format="%.1f", help="Estoque de segurança pelo nível de serviço"),
        "EstoqueMeta": st.column_config.NumberColumn("Alvo (S)", help="Nível-alvo order-up-to = demanda do período + segurança"),
        "EstoqueProjetado": st.column_config.NumberColumn("Est. Projetado", format="%.1f", help="Estoque projetado na chegada da rodada (já desconta consumo e chegadas anteriores). O de HOJE está em Est. Rede"),
        "SugestaoProducao": st.column_config.NumberColumn("Sugestão (pares)", help="Pedido = Alvo − Est. Projetado, arredondado a par"),
        "NivelServico": st.column_config.NumberColumn("NS %", help="Nível de serviço aplicado (alta vs baixa)"),
        "CustoUnit": st.column_config.NumberColumn("Custo Unit (R$)", format="R$ %.2f"),
        "InvestimentoFabril": st.column_config.NumberColumn("Investimento (R$)", format="R$ %.2f"),
    }

    st.dataframe(
        df_filtrado[colunas_visiveis],
        width="stretch", hide_index=True,
        height=640,
        column_config=_column_config,
    )
    st.caption(
        f"**{len(df_filtrado)}** SKUs | "
        f"**{df_filtrado['SugestaoProducao'].sum():,.0f}** pares totais | "
        f"**R$ {df_filtrado['InvestimentoFabril'].sum():,.2f}** investimento"
    )

    # --- Top 10 por investimento (barra única — o comprimento já é o investimento) ---
    if len(skus_produzir) > 0:
        st.subheader("Top 10 SKUs — Maior Investimento Fabril")
        top10 = skus_produzir.nlargest(10, "InvestimentoFabril")
        fig_top = px.bar(
            top10, x="SKU", y="InvestimentoFabril",
            text=top10["InvestimentoFabril"].apply(lambda x: f"R$ {x:,.0f}"),
            hover_data={"SugestaoProducao": True, "Colegio": True},
        )
        fig_top.update_traces(marker_color=COR_DEMANDA, textposition="outside")
        fig_top.update_layout(height=400, xaxis_tickangle=-45,
                              yaxis_title="Investimento (R$)", margin=dict(t=30))
        st.plotly_chart(fig_top, width="stretch")

    # --- Exportar (CSV sempre com a memória de cálculo completa) ---
    st.subheader("Exportar")
    df_export = df_filtrado[COLS_FULL].copy()
    df_export = df_export.rename(columns={
        "Produto": "Descrição", "Tamanho": "Tam", "Colegio": "Colégio",
        "VendasHist": "Vendas Alta", "EstoqueRede": "Estoque Rede",
        "DemandaProjetada": f"Demanda do Período ({_cobre})",
        "DemandaPeriodoAlta": "Demanda na alta", "DemandaPeriodoBaixa": "Demanda na baixa",
        "EstoqueSeguranca": "Estoque Segurança", "EstoqueMeta": "Alvo (S)",
        "EstoqueProjetado": "Estoque Projetado", "SugestaoProducao": "Sugestão (pares)",
        "NivelServico": "Nível Serviço (%)", "CustoUnit": "Custo Unit (R$)",
        "InvestimentoFabril": "Investimento (R$)",
    })

    def _fmt_brl(x):
        return f"R$ {x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    df_export["Custo Unit (R$)"] = df_export["Custo Unit (R$)"].apply(_fmt_brl)
    df_export["Investimento (R$)"] = df_export["Investimento (R$)"].apply(_fmt_brl)

    linhas_param = [
        "# Sugestão de Produção (order-up-to)",
        f"# {janela_label}",
        f"# Período histórico: {cfg_plan.get('periodo_historico_inicio')} a {cfg_plan.get('periodo_historico_fim')}",
        f"# Janela de alta: {cfg_dem.get('janela_alta')}",
        f"# Nível de serviço: alta {cfg_dem.get('nivel_servico_alta', 99)}% / baixa {cfg_dem.get('nivel_servico_baixa', 92)}% · Variação {cfg_dem.get('variacao_demanda', 0.25)}",
        f"# Crescimento aplicado: {'sim' if ativo_cresc else 'nao'}",
        "",
    ]
    csv_completo = ("\n".join(linhas_param) + df_export.to_csv(index=False, sep=";", decimal=",")).encode("utf-8")

    st.download_button(
        label="⬇️ Baixar CSV (para importar no Excel/Sheets)",
        data=csv_completo, file_name="simulador_producao.csv", mime="text/csv",
    )

    # --- Congelar rodada → Pedidos de Compra (admin only) ---
    # Gate explícito de role (mesmo padrão de 5_Configuracoes.py) — defesa em
    # profundidade além do filtro de páginas por role no app.py.
    _usuario = st.session_state.get("username", "")
    _role = (dict(st.secrets.get("auth_config", {}))
             .get("credentials", {}).get("usernames", {})
             .get(_usuario, {}).get("role", ""))

    if _role == "admin" and rodadas_opts:
        st.divider()
        st.subheader("🧊 Congelar rodada → Pedidos de Compra")
        st.caption(
            "Tira um snapshot imutável do cálculo desta rodada e gera pedidos de "
            "compra em **rascunho**, agrupados por Colégio × Super Categoria. "
            "Para refazer, cancele a rodada congelada na página Pedidos de Compra."
        )
        with st.popover("Congelar rodada e gerar pedidos"):
            st.markdown(f"**{janela_label}**")
            st.caption(
                "O cálculo será refeito agora com os dados vivos e congelado "
                "como está — edições posteriores acontecem nos rascunhos."
            )
            if st.button("✅ Confirmar congelamento", type="primary"):
                with st.spinner("Congelando rodada e gerando pedidos..."):
                    # Recalcula FRESCO (não usa o _processar cacheado: o cache
                    # pode ter ancorado now() horas atrás) e sobre o df
                    # COMPLETO — nunca o df_filtrado da tela.
                    df_fresco = processar_fabrica(
                        dados, config,
                        mes_disparo=sel["mes_disparo"], ano_disparo=sel["ano_disparo"],
                        ativo_crescimento=ativo_cresc,
                    )
                    erros = pedidos_builder.validar_pre_congelamento(df_fresco)
                    if erros:
                        st.session_state["pc_congelar_msg"] = ("error", " · ".join(erros))
                    else:
                        try:
                            snapshot = pedidos_builder.montar_snapshot_rodada(
                                df_fresco, config, sel["mes_disparo"], sel["ano_disparo"],
                                ativo_crescimento=bool(ativo_cresc), usuario=_usuario,
                            )
                            grupos = pedidos_builder.agrupar_pedidos(
                                df_fresco, dados["produtos"], _usuario,
                                sel["mes_disparo"], sel["ano_disparo"],
                            )
                            res = obter_repositorio().congelar_rodada(snapshot, grupos)
                            st.session_state["pc_congelar_msg"] = ("success", (
                                f"Rodada congelada: **{res['n_pedidos']} pedidos** "
                                f"({res['n_itens']} itens) gerados em rascunho."
                            ))
                        except RodadaJaCongelada as exc:
                            st.session_state["pc_congelar_msg"] = ("warning", str(exc))
                        except ValueError as exc:
                            st.session_state["pc_congelar_msg"] = ("error", str(exc))
                        except Exception as exc:
                            st.session_state["pc_congelar_msg"] = (
                                "error", f"Falha ao congelar a rodada: {exc}")

        # Resultado exibido FORA do popover (que fecha no rerun do clique)
        _msg = st.session_state.pop("pc_congelar_msg", None)
        if _msg:
            nivel, texto = _msg
            getattr(st, nivel)(texto)
            if nivel in ("success", "warning"):
                st.page_link("pages/4_Pedidos.py", label="Abrir Pedidos de Compra", icon="🧾")
