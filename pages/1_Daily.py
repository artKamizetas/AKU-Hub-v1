"""
Página: Dashboard Comercial (Daily) — Metas Escalonadas

Dois regimes temporais, deliberadamente separados (antes se misturavam sem
aviso, e a leitura saía errada):
  · COMPETÊNCIA (mês/ano) rege o bloco de metas — permite reabrir meses fechados
  · PERÍODO rege a análise livre (histórico, status de pedidos, rankings)

Metas Prata/Ouro/Diamante por loja em Faturamento e PA; meta do vendedor é
rateada da loja. Regra em etl/metas.py, dados em etl/daily.py.
Spec: docs/requisitos/metas-escalonadas.md
"""

import streamlit as st
from auth import exigir_login
exigir_login()
import plotly.graph_objects as go
import pandas as pd
from datetime import timedelta, date

from etl.daily import processar_daily
from etl import metas
from etl.loader import fingerprint_config
from ui_carga import carregar_com_feedback, rodape_frescor


# =================================================================
# CONSTANTES VISUAIS
# =================================================================
# As três faixas são uma escala ORDINAL (prata < ouro < diamante), não
# identidades — por isso rampa neutra sequencial (padrão do bullet chart),
# não três matizes categóricas. A identidade do nível vem do emoji 🥈🥇💎
# (codificação secundária), nunca da cor sozinha.
FAIXA_PRATA = "rgba(148, 163, 184, 0.18)"
FAIXA_OURO = "rgba(148, 163, 184, 0.34)"
FAIXA_DIAMANTE = "rgba(148, 163, 184, 0.52)"
COR_REALIZADO = "#1976D2"      # primária do tema (.streamlit/config.toml)
COR_MARCADOR = "#D4A017"
CINZA_REF = "rgba(148, 163, 184, 0.75)"

EMOJI_NIVEL = {"Prata": "🥈", "Ouro": "🥇", "Diamante": "💎"}
MESES_NOME = {
    1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril", 5: "Maio", 6: "Junho",
    7: "Julho", 8: "Agosto", 9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro",
}


def _brl(v, casas=0) -> str:
    """Formata em Real com separador pt-BR (milhar '.', decimal ',')."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    s = f"{v:,.{casas}f}"
    return "R$ " + s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def _num(v, casas=0) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    s = f"{v:,.{casas}f}"
    return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def _badge_nivel(nivel) -> str:
    """'🥇 Ouro' ou '— Sem nível'. Nunca depende só de cor."""
    if not nivel or (isinstance(nivel, float) and pd.isna(nivel)):
        return "— Sem nível"
    return f"{EMOJI_NIVEL.get(nivel, '')} {nivel}".strip()


def _metas_da_linha(row, prefixo: str):
    """Reconstrói {prata,ouro,diamante} das colunas do DataFrame; None se a
    loja não tem meta na competência (NaN nas colunas numéricas)."""
    vals = {}
    for n in ("prata", "ouro", "diamante"):
        v = row.get(f"{prefixo} {n.capitalize()}")
        vals[n] = None if (v is None or pd.isna(v)) else float(v)
    return vals if vals.get("ouro") is not None else None


dados, config = carregar_com_feedback()

if not dados["validacao"]["ok"]:
    st.error("Dados inválidos. Verifique a página principal.")
    st.stop()

st.title("📈 Daily — Acompanhamento Comercial")

# =================================================================
# FILTROS
# =================================================================
hoje = date.today()

with st.container(border=True):
    col_c, col_l, col_col = st.columns([1.2, 1.4, 1])

    with col_c:
        # Competência: 24 meses para trás + o mês corrente (permite revisar
        # meses fechados, que a versão anterior travava em now()).
        opcoes_comp = []
        cursor = pd.Timestamp(hoje.year, hoje.month, 1)
        for i in range(24):
            d = cursor - pd.DateOffset(months=i)
            opcoes_comp.append(metas.chave_competencia(d.year, d.month))
        competencia = st.selectbox(
            "🎯 Competência (metas)", opcoes_comp, index=0,
            format_func=lambda c: f"{MESES_NOME[int(c[5:7])]}/{c[:4]}",
            help="Mês de referência das metas. Meses fechados podem ser revisados.",
        )

    # Lojas ativas no Bling — compara por ID (mais confiável que nome)
    lojas_ativas_ids = set(
        str(row["ID"]).strip()
        for _, row in dados["lojas"].iterrows()
        if str(row.get("Situacao", "0")).strip() in ("1", "Ativo", "ativo", "ATIVO")
    )
    lojas_config_ativas = [
        l for l in config["depositos"]["lojas"] if str(l["loja_id"]) in lojas_ativas_ids
    ]
    if not lojas_config_ativas:
        lojas_config_ativas = list(config["depositos"]["lojas"])
    nomes_lojas = [l["nome"] for l in lojas_config_ativas]

    with col_l:
        lojas_selecionadas = st.pills(
            "🏬 Lojas", nomes_lojas, selection_mode="multi",
            default=nomes_lojas, key="daily_lojas",
        )

    with col_col:
        # Colégios vêm do cadastro (já normalizado pelo de-para), não das
        # vendas — assim a lista não muda a cada troca de filtro.
        from etl.demanda import aplicar_alias_colegio
        _marcas = (
            aplicar_alias_colegio(dados["detalhes"], config)["Marca_sku"]
            .fillna("").astype(str).str.strip()
        )
        colegios_disp = sorted(
            c for c in _marcas.unique() if c and c.lower() != "nan" and c != "Sem Colégio"
        )
        colegio_selecionado = st.selectbox(
            "🏫 Colégio", ["Todos"] + colegios_disp, index=0, key="daily_colegio",
            help="Filtra a análise livre e a quebra por colégio. As metas são sempre da loja inteira.",
        )

if not lojas_selecionadas:
    st.warning("Selecione pelo menos 1 loja para ver os dados.")
    st.stop()

# =================================================================
# PROCESSAMENTO
# =================================================================
ano_c, mes_c = int(competencia[:4]), int(competencia[5:7])
rotulo_comp = f"{MESES_NOME[mes_c]}/{ano_c}"


@st.cache_data(show_spinner=False)
def _processar_daily(_dados, _config, competencia, fp_config):
    """Cacheado por competência: sem isso, trocar o mês — ou QUALQUER rerun de
    widget da página — recalculava 5 s do zero, em silêncio. `fp_config` sem
    underscore é o que carrega a versão do config para dentro da cache key."""
    return processar_daily(_dados, _config, competencia=competencia)


with st.spinner(f"Recalculando metas de {rotulo_comp}…", show_time=True):
    df_detalhado, df_metas_loja, df_metas_vendedor = _processar_daily(
        dados, config, competencia, fingerprint_config(config))

situacoes_venda = config["daily"]["situacoes_venda"]

df_loja_base = df_detalhado[df_detalhado["LojaConfig"].isin(lojas_selecionadas)].copy()
df_loja = df_loja_base
if colegio_selecionado != "Todos":
    df_loja = df_loja[df_loja["Colegio"] == colegio_selecionado]

# Metas: só as lojas selecionadas (a linha TOTAL do ETL cobre TODAS as lojas,
# então o agregado da seleção é recomposto aqui pelo mesmo motor puro).
metas_sel = df_metas_loja[df_metas_loja["Loja"].isin(lojas_selecionadas)].copy()
if len(metas_sel) == 0:
    st.warning("Sem dados de meta para as lojas selecionadas.")
    st.stop()

agg_fat = metas.agregar_faturamento([
    {"vendido": r["Vendido"], "metas": _metas_da_linha(r, "Meta")}
    for _, r in metas_sel.iterrows()
])
agg_pa = metas.agregar_pa([
    {"pecas": r["Pecas"], "pedidos": r["Pedidos"], "metas": _metas_da_linha(r, "PA Meta")}
    for _, r in metas_sel.iterrows()
])

dias_no_mes = pd.Timestamp(ano_c, mes_c, 1).days_in_month
eh_mes_corrente = (ano_c, mes_c) == (hoje.year, hoje.month)
dia_atual = hoje.day if eh_mes_corrente else dias_no_mes

resumo_fat = metas.resumo_faturamento(agg_fat["vendido"], agg_fat["metas"], dia_atual, dias_no_mes)
resumo_pa = metas.resumo_pa(agg_pa["pecas"], agg_pa["pedidos"], agg_pa["metas"])

label_lojas = " + ".join(lojas_selecionadas) if len(lojas_selecionadas) <= 2 else f"{len(lojas_selecionadas)} lojas"

# =================================================================
# BLOCO 1 — METAS (competência)
# =================================================================
st.subheader(f"🎯 Metas — {rotulo_comp}")

origens = set(metas_sel["Origem Meta"])
if "ausente" in origens:
    faltantes = metas_sel[metas_sel["Origem Meta"] == "ausente"]["Loja"].tolist()
    st.warning(
        f"⚠️ Sem meta cadastrada em {rotulo_comp} para: **{', '.join(faltantes)}**. "
        "Cadastre em *Configurações → Metas Mensais* — o realizado abaixo continua correto."
    )
elif "estimada" in origens:
    st.info(
        "ℹ️ Usando meta **estimada** do formato antigo (valor único como Ouro). "
        "Cadastre as metas do mês em *Configurações → Metas Mensais* para o número real."
    )

if not eh_mes_corrente:
    st.caption(f"📅 Mês fechado — a projeção é igual ao realizado (não há mais dias a percorrer).")

k1, k2, k3, k4 = st.columns(4)
with k1:
    st.metric("💰 Vendido no mês", _brl(resumo_fat["vendido"]),
              delta=f"{_num(agg_pa['pedidos'])} pedidos · {_num(agg_pa['pecas'])} peças",
              delta_color="off")
with k2:
    st.metric("🏅 Nível conquistado", _badge_nivel(resumo_fat["nivel"]),
              delta=f"PA {_num(resumo_pa['pa'], 2)} · {_badge_nivel(resumo_pa['nivel'])}",
              delta_color="off")
with k3:
    if resumo_fat["proximo_nivel"]:
        ritmo = resumo_fat["ritmo_necessario"]
        st.metric(f"🎯 Falta p/ {resumo_fat['proximo_nivel']}", _brl(resumo_fat["falta"]),
                  delta=(f"{_brl(ritmo)}/dia nos {dias_no_mes - dia_atual} dias restantes"
                         if ritmo else "sem dias restantes"),
                  delta_color="off")
    else:
        st.metric("🎯 Falta", "—",
                  delta="Diamante batido" if resumo_fat["nivel"] else "sem meta",
                  delta_color="off")
with k4:
    st.metric("📊 Projeção do mês", _brl(resumo_fat["run_rate"]),
              delta=f"fecharia em {_badge_nivel(resumo_fat['nivel_projetado'])}",
              delta_color="off")


def _bullet(fig, rotulo, valor, metas_d, indice, total, formato="brl"):
    """Uma linha de bullet chart: faixas ordinais no fundo, barra do realizado,
    marcador no próximo nível a conquistar."""
    if not metas_d or metas_d.get("ouro") is None:
        return
    p = metas_d.get("prata") or 0
    o = metas_d.get("ouro") or 0
    d = metas_d.get("diamante") or o
    eixo_max = max(d * 1.12, valor * 1.06, o * 1.2)
    cls = metas.classificar_nivel(valor, metas_d)
    marcador = cls["proximo_valor"] or d

    altura = 1 / total
    y0 = 1 - (indice + 1) * altura + altura * 0.18
    y1 = 1 - indice * altura - altura * 0.18

    fig.add_trace(go.Indicator(
        mode="number+gauge",
        value=valor,
        number=({"prefix": "R$ ", "valueformat": ",.0f"} if formato == "brl"
                else {"valueformat": ".2f"}),
        title={"text": f"<b>{rotulo}</b>", "font": {"size": 13}},
        gauge={
            "shape": "bullet",
            "axis": {"range": [0, eixo_max], "tickfont": {"size": 10}},
            "bgcolor": "rgba(0,0,0,0)",
            "borderwidth": 0,
            "steps": [
                {"range": [0, p], "color": FAIXA_PRATA},
                {"range": [p, o], "color": FAIXA_OURO},
                {"range": [o, d], "color": FAIXA_DIAMANTE},
            ],
            "bar": {"color": COR_REALIZADO, "thickness": 0.42},
            "threshold": {"line": {"color": COR_MARCADOR, "width": 3},
                          "thickness": 0.82, "value": marcador},
        },
        domain={"x": [0.28, 0.96], "y": [max(y0, 0), min(y1, 1)]},
    ))


mostrar_total = len(lojas_selecionadas) > 1

col_b1, col_b2 = st.columns(2)

with col_b1:
    st.markdown("**Faturamento**")
    n_fat = len(metas_sel) + (1 if mostrar_total else 0)
    fig_fat = go.Figure()
    for i, (_, r) in enumerate(metas_sel.iterrows()):
        _bullet(fig_fat, r["Loja"], r["Vendido"], _metas_da_linha(r, "Meta"), i, n_fat)
    if mostrar_total:
        _bullet(fig_fat, "TOTAL", resumo_fat["vendido"], agg_fat["metas"], n_fat - 1, n_fat)
    if len(fig_fat.data) == 0:
        st.info("Sem meta de faturamento cadastrada para o período.")
    else:
        fig_fat.update_layout(height=90 * n_fat + 30, margin=dict(t=25, b=20, l=10, r=20))
        st.plotly_chart(fig_fat, width="stretch")
        st.caption("Faixas = 🥈 Prata · 🥇 Ouro · 💎 Diamante (mais escuro = mais alto). "
                   "Barra azul = realizado · traço dourado = próximo nível.")

with col_b2:
    st.markdown("**PA — Peças por Atendimento**")
    n_pa = len(metas_sel) + (1 if mostrar_total else 0)
    fig_pa = go.Figure()
    for i, (_, r) in enumerate(metas_sel.iterrows()):
        _bullet(fig_pa, r["Loja"], r["PA"], _metas_da_linha(r, "PA Meta"), i, n_pa, formato="num")
    if mostrar_total:
        _bullet(fig_pa, "TOTAL", resumo_pa["pa"], agg_pa["metas"], n_pa - 1, n_pa, formato="num")
    if len(fig_pa.data) == 0:
        st.info("Sem meta de PA cadastrada para o período.")
    else:
        fig_pa.update_layout(height=90 * n_pa + 30, margin=dict(t=25, b=20, l=10, r=20))
        st.plotly_chart(fig_pa, width="stretch")
        st.caption("PA agregado é Σpeças ÷ Σpedidos (média ponderada), não a média dos PAs das lojas.")

# -----------------------------------------------------------------
# Ritmo do mês — acumulado realizado vs metas acumuladas
# -----------------------------------------------------------------
st.markdown("**📈 Ritmo do mês**")

vendas_comp = df_loja_base[
    (df_loja_base["Data"].dt.year == ano_c)
    & (df_loja_base["Data"].dt.month == mes_c)
    & (df_loja_base["id_situacao"].isin(situacoes_venda))
]

if agg_fat["metas"] and len(vendas_comp) > 0:
    diario_comp = (
        vendas_comp.groupby(vendas_comp["Data"].dt.day)["Valor"].sum()
        .reindex(range(1, dias_no_mes + 1), fill_value=0.0)
    )
    acumulado = diario_comp.cumsum()
    # Realizado só até hoje no mês corrente (senão a curva "cai" para o platô)
    dias_plot = list(range(1, dia_atual + 1))
    acumulado_plot = acumulado.loc[dias_plot]

    fig_ritmo = go.Figure()
    # Metas acumuladas lineares — referência, com rótulo direto (não só cor)
    for nome, chave, cor_dash in (
        ("💎 Diamante", "diamante", "rgba(148,163,184,0.95)"),
        ("🥇 Ouro", "ouro", "rgba(148,163,184,0.75)"),
        ("🥈 Prata", "prata", "rgba(148,163,184,0.55)"),
    ):
        alvo = agg_fat["metas"].get(chave)
        if not alvo:
            continue
        fig_ritmo.add_trace(go.Scatter(
            x=list(range(1, dias_no_mes + 1)),
            y=[alvo * dia / dias_no_mes for dia in range(1, dias_no_mes + 1)],
            name=nome, mode="lines",
            line=dict(color=cor_dash, width=2, dash="dash"),
            hovertemplate=f"{nome} até o dia %{{x}}<br>R$ %{{y:,.0f}}<extra></extra>",
        ))
    fig_ritmo.add_trace(go.Scatter(
        x=dias_plot, y=acumulado_plot.values,
        name="Realizado", mode="lines",
        line=dict(color=COR_REALIZADO, width=3),
        fill="tozeroy", fillcolor="rgba(25, 118, 210, 0.12)",
        hovertemplate="Dia %{x}<br>Acumulado: R$ %{y:,.0f}<extra></extra>",
    ))
    fig_ritmo.update_layout(
        height=320, hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="center", x=0.5),
        xaxis=dict(title="Dia do mês", dtick=2),
        yaxis=dict(title="Acumulado (R$)"),
        margin=dict(t=40, b=40),
    )
    st.plotly_chart(fig_ritmo, width="stretch")
    st.caption(
        "As linhas tracejadas são o ritmo constante necessário para cada nível. "
        "Acima da linha = no ritmo daquele nível."
    )
elif not agg_fat["metas"]:
    st.info("Cadastre as metas do mês para ver a curva de ritmo.")
else:
    st.info(f"Sem vendas registradas em {rotulo_comp}.")

# -----------------------------------------------------------------
# Detalhe por loja
# -----------------------------------------------------------------
with st.expander(f"📋 Detalhe por loja — {rotulo_comp}", expanded=False):
    det_loja = metas_sel.copy()
    det_loja["Nível"] = det_loja["Nivel"].map(lambda v: _badge_nivel(v))
    det_loja["PA Nível"] = det_loja["PA Nivel"].map(lambda v: _badge_nivel(v))
    cols = ["Loja", "Vendido", "Meta Prata", "Meta Ouro", "Meta Diamante", "Nível",
            "Falta Proximo", "Run Rate", "PA", "PA Meta Ouro", "PA Nível", "Pecas", "Pedidos"]
    st.dataframe(
        det_loja[cols],
        width="stretch", hide_index=True,
        column_config={
            "Vendido": st.column_config.NumberColumn("Vendido", format="R$ %.0f"),
            "Meta Prata": st.column_config.NumberColumn("🥈 Prata", format="R$ %.0f"),
            "Meta Ouro": st.column_config.NumberColumn("🥇 Ouro", format="R$ %.0f"),
            "Meta Diamante": st.column_config.NumberColumn("💎 Diamante", format="R$ %.0f"),
            "Falta Proximo": st.column_config.NumberColumn("Falta p/ próximo", format="R$ %.0f"),
            "Run Rate": st.column_config.NumberColumn("Projeção", format="R$ %.0f"),
            "PA": st.column_config.NumberColumn("PA", format="%.2f"),
            "PA Meta Ouro": st.column_config.NumberColumn("PA 🥇", format="%.2f"),
            "Pecas": st.column_config.NumberColumn("Peças", format="%d"),
            "Pedidos": st.column_config.NumberColumn("Pedidos", format="%d"),
        },
    )

# -----------------------------------------------------------------
# Histórico de atingimento — os 12 meses até a competência
# Barato: uma passada de groupby no df_detalhado (que já tem TODOS os
# pedidos), nunca um processar_daily por mês.
# -----------------------------------------------------------------
with st.expander("📅 Histórico de atingimento (12 meses)", expanded=False):
    _vendas_hist = df_loja[df_loja["id_situacao"].isin(situacoes_venda)]
    _realizado_hist = {}
    if len(_vendas_hist) > 0:
        _h = _vendas_hist.copy()
        _h["_comp"] = _h["Data"].dt.strftime("%Y-%m")
        for (_c, _l), _g in _h.groupby(["_comp", "LojaConfig"]):
            _realizado_hist[(_c, _l)] = {
                "vendido": float(_g["Valor"].sum()),
                "pecas": float(_g["Qtd Peças"].sum()),
                "pedidos": float(_g["Pedido"].nunique()),
            }

    _comps = metas.competencias_anteriores(competencia, 12)
    _hist = metas.historico_atingimento(config, lojas_selecionadas, _realizado_hist, _comps)

    _n_config = sum(1 for x in _hist if x["origem"] == "configurada")
    if _n_config == 0:
        st.info(
            "Nenhum mês desta janela tem meta **cadastrada** — as linhas viriam do "
            "valor legado (meta única, sem competência), que é a mesma em todos os "
            "meses e não serve de histórico. Cadastre em *Configurações → Metas "
            "Mensais* e este painel se preenche sozinho."
        )
    else:
        if _n_config < len(_hist):
            st.caption(
                f"ℹ️ {_n_config} de {len(_hist)} meses têm meta cadastrada; nos demais "
                "a linha vem do valor legado estimado — compare com ressalva."
            )
        _fig_h = go.Figure()
        _rot = [f"{MESES_NOME[int(c[5:7])][:3]}/{c[2:4]}" for c in _comps]

        # Barra em cor única + emoji do nível como rótulo: a identidade do
        # nível é a codificação secundária (emoji), nunca a cor sozinha —
        # mesma regra do bullet chart acima.
        _fig_h.add_trace(go.Bar(
            x=_rot,
            y=[x["vendido"] for x in _hist],
            name="Realizado",
            marker_color=COR_REALIZADO,
            text=[EMOJI_NIVEL.get(x["nivel"], "") for x in _hist],
            textposition="outside",
            customdata=[[_badge_nivel(x["nivel"]) if x["tem_meta"] else "sem meta cadastrada",
                         x["pa"], x["pedidos"]] for x in _hist],
            hovertemplate=("<b>%{x}</b><br>Faturamento: R$ %{y:,.0f}<br>"
                           "Nível: %{customdata[0]}<br>PA: %{customdata[1]:.2f}"
                           "<extra></extra>"),
        ))
        # Metas na rampa neutra sequencial (mais escuro = nível mais alto)
        for _n, _cor in (("prata", FAIXA_PRATA), ("ouro", FAIXA_OURO),
                         ("diamante", FAIXA_DIAMANTE)):
            _fig_h.add_trace(go.Scatter(
                x=_rot,
                y=[(x["metas"] or {}).get(_n) for x in _hist],
                name=_n.capitalize(), mode="lines",
                line=dict(color=CINZA_REF, width=1.5,
                          dash={"prata": "dot", "ouro": "dash", "diamante": "solid"}[_n]),
                opacity={"prata": 0.45, "ouro": 0.7, "diamante": 1.0}[_n],
                connectgaps=False,
                hovertemplate=f"{_n.capitalize()}: R$ %{{y:,.0f}}<extra></extra>",
            ))
        _fig_h.update_layout(
            height=300, margin=dict(t=30, b=30, l=10, r=10),
            hovermode="x unified", yaxis_title="Faturamento (R$)",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="center", x=0.5),
        )
        st.plotly_chart(_fig_h, width="stretch")

        _n_batidos = sum(1 for x in _hist if x["nivel"])
        _n_com_meta = sum(1 for x in _hist if x["tem_meta"])
        st.caption(
            f"**{_n_batidos}** de **{_n_com_meta}** mês(es) com meta cadastrada atingiram "
            "ao menos o Prata. Barra sem emoji = abaixo do Prata ou sem meta; "
            "falha na linha = mês sem meta cadastrada (lacuna, não zero)."
        )

# =================================================================
# BLOCO 2 — VENDEDORES (competência)
# =================================================================
st.subheader(f"👤 Vendedores — {rotulo_comp}")

vend_sel = df_metas_vendedor[df_metas_vendedor["Loja"].isin(lojas_selecionadas)].copy()
sem_atribuicao = df_metas_vendedor[
    (df_metas_vendedor["Loja"] == "Sem atribuição") & (df_metas_vendedor["Vendido"] > 0)
]

if len(vend_sel) == 0:
    st.info(
        "Nenhum vendedor atribuído às lojas selecionadas nesta competência. "
        "Configure em *Configurações → Metas Mensais → Vendedores por Loja*."
    )
else:
    vend_sel["% da Meta"] = vend_sel.apply(
        lambda r: (r["Vendido"] / r["Meta Ouro"] * 100) if pd.notna(r["Meta Ouro"]) and r["Meta Ouro"] else None,
        axis=1,
    )
    vend_sel["Nível"] = vend_sel["Nivel"].map(lambda v: _badge_nivel(v))
    vend_sel["PA Nível"] = vend_sel["PA Nivel"].map(lambda v: _badge_nivel(v))
    vend_sel = vend_sel.sort_values("Vendido", ascending=False)

    st.dataframe(
        vend_sel[["Vendedor", "Loja", "Vendido", "Meta Ouro", "% da Meta", "Nível",
                  "Falta Proximo", "PA", "PA Meta Ouro", "PA Nível",
                  "Pecas", "Pedidos", "Ticket Medio"]],
        width="stretch", hide_index=True,
        column_config={
            "Vendido": st.column_config.NumberColumn("Vendido", format="R$ %.0f"),
            "Meta Ouro": st.column_config.NumberColumn("Meta 🥇 Ouro", format="R$ %.0f",
                                                       help="Rateada da meta da loja pelo peso do vendedor."),
            "% da Meta": st.column_config.ProgressColumn("% da meta Ouro", format="%.0f%%",
                                                          min_value=0, max_value=120),
            "Falta Proximo": st.column_config.NumberColumn("Falta p/ próximo", format="R$ %.0f"),
            "PA": st.column_config.NumberColumn("PA", format="%.2f"),
            "PA Meta Ouro": st.column_config.NumberColumn("PA 🥇", format="%.2f"),
            "Pecas": st.column_config.NumberColumn("Peças", format="%d"),
            "Pedidos": st.column_config.NumberColumn("Pedidos", format="%d"),
            "Ticket Medio": st.column_config.NumberColumn("Ticket médio", format="R$ %.2f"),
        },
    )
    st.caption(
        f"**{len(vend_sel)}** vendedor(es) · meta individual = meta da loja × peso ÷ soma dos pesos. "
        "PA não se rateia: a meta de PA do vendedor é a mesma da loja."
    )

if len(sem_atribuicao) > 0:
    st.caption(
        f"⚠️ **{len(sem_atribuicao)}** vendedor(es) venderam em {rotulo_comp} sem atribuição de loja "
        f"({_brl(sem_atribuicao['Vendido'].sum())}) — não entram em nenhuma meta. "
        "Atribua em *Configurações → Vendedores por Loja*."
    )

# =================================================================
# BLOCO 3 — COLÉGIOS (competência)
# =================================================================
st.subheader(f"🏫 Colégios — {rotulo_comp}")

if len(vendas_comp) > 0:
    por_colegio = (
        vendas_comp.groupby("Colegio")
        .agg(Valor=("Valor", "sum"), Pecas=("Qtd Peças", "sum"), Pedidos=("ID_pedido", "nunique"))
        .reset_index()
    )
    total_col = por_colegio["Valor"].sum()
    por_colegio["Participacao"] = (por_colegio["Valor"] / total_col * 100) if total_col else 0
    por_colegio["PA"] = por_colegio["Pecas"] / por_colegio["Pedidos"].replace(0, pd.NA)
    por_colegio = por_colegio.sort_values("Valor", ascending=False)

    col_g, col_t = st.columns([1, 1])
    with col_g:
        top = por_colegio.head(10).sort_values("Valor")
        fig_col = go.Figure(go.Bar(
            x=top["Valor"], y=top["Colegio"], orientation="h",
            marker_color=COR_REALIZADO,
            hovertemplate="<b>%{y}</b><br>R$ %{x:,.0f}<extra></extra>",
        ))
        fig_col.update_layout(
            height=max(260, 32 * len(top)),
            xaxis=dict(title="Faturamento (R$)"),
            yaxis=dict(title=""),
            margin=dict(t=20, b=40, l=10, r=20),
        )
        st.plotly_chart(fig_col, width="stretch")
    with col_t:
        st.dataframe(
            por_colegio[["Colegio", "Valor", "Participacao", "Pecas", "Pedidos", "PA"]],
            width="stretch", hide_index=True, height=max(260, 32 * min(len(por_colegio), 10)),
            column_config={
                "Colegio": st.column_config.TextColumn("Colégio"),
                "Valor": st.column_config.NumberColumn("Faturamento", format="R$ %.0f"),
                "Participacao": st.column_config.ProgressColumn("Participação", format="%.1f%%",
                                                                 min_value=0, max_value=100),
                "Pecas": st.column_config.NumberColumn("Peças", format="%d"),
                "Pedidos": st.column_config.NumberColumn("Pedidos", format="%d"),
                "PA": st.column_config.NumberColumn("PA", format="%.2f"),
            },
        )
    st.caption("Colégio não tem meta própria — é o detalhamento do resultado da loja no mês.")
else:
    st.info(f"Sem vendas registradas em {rotulo_comp} para as lojas selecionadas.")

# =================================================================
# BLOCO 4 — ANÁLISE LIVRE (período)
# =================================================================
st.divider()
st.subheader("📊 Análise livre")
st.caption("Este bloco segue o **período** abaixo — independente da competência das metas acima.")

primeiro_dia_mes = hoje.replace(day=1)
if hoje.month == 1:
    primeiro_mes_passado = date(hoje.year - 1, 12, 1)
else:
    primeiro_mes_passado = date(hoje.year, hoje.month - 1, 1)
ultimo_mes_passado = primeiro_dia_mes - timedelta(days=1)
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
    periodo_nome = st.selectbox("Período", list(PERIODOS.keys()), index=0, key="daily_periodo")

if periodo_nome == "Personalizado":
    with col_p2:
        data_inicio = st.date_input("Início", value=primeiro_dia_mes, key="daily_dt_ini")
    with col_p3:
        data_fim = st.date_input("Fim", value=hoje, key="daily_dt_fim")
else:
    data_inicio, data_fim = PERIODOS[periodo_nome]
    with col_p2:
        st.caption(f"**De:** {data_inicio.strftime('%d/%m/%Y')}")
    with col_p3:
        st.caption(f"**Até:** {data_fim.strftime('%d/%m/%Y')}")

dt_inicio = pd.Timestamp(data_inicio)
dt_fim = pd.Timestamp(data_fim) + pd.Timedelta(hours=23, minutes=59, seconds=59)

df_periodo = df_loja[(df_loja["Data"] >= dt_inicio) & (df_loja["Data"] <= dt_fim)]
df_vendas_periodo = df_periodo[df_periodo["id_situacao"].isin(situacoes_venda)]

# --- Cards de status de pedido ---
status_ids = config["daily"]["status_ids"]
s1, s2, s3, s4 = st.columns(4)
with s1:
    st.metric("📋 Em Aberto", len(df_periodo[df_periodo["id_situacao"] == status_ids["em_aberto"]]))
with s2:
    st.metric("⏳ Em Andamento", len(df_periodo[df_periodo["id_situacao"] == status_ids["em_andamento"]]))
with s3:
    st.metric("📦 Pronto p/ Retirada", len(df_periodo[df_periodo["id_situacao"] == status_ids["pronto_retirada"]]))
with s4:
    st.metric("✅ Atendidos", len(df_vendas_periodo))

# --- Histórico diário ---
st.markdown(f"**Histórico de vendas — {data_inicio.strftime('%d/%m')} a {data_fim.strftime('%d/%m/%Y')}**")

if len(df_vendas_periodo) > 0:
    # Uma métrica por vez, num eixo só: faturamento e peças têm escalas
    # diferentes e o eixo duplo da versão anterior distorcia a comparação.
    metrica = st.segmented_control(
        "Métrica", ["Faturamento", "Peças"], default="Faturamento",
        key="daily_metrica_hist", label_visibility="collapsed",
    ) or "Faturamento"

    diario = (
        df_vendas_periodo
        .groupby(df_vendas_periodo["Data"].dt.date)
        .agg({"Valor": "sum", "Qtd Peças": "sum"})
        .reset_index()
    )
    diario.columns = ["Data", "Valor", "Pecas"]
    todos_dias = pd.DataFrame({"Data": pd.date_range(data_inicio, data_fim, freq="D").date})
    diario = todos_dias.merge(diario, on="Data", how="left").fillna(0).sort_values("Data")

    if metrica == "Faturamento":
        col_y, titulo_y, fmt = "Valor", "Faturamento (R$)", "R$ %{y:,.0f}"
    else:
        col_y, titulo_y, fmt = "Pecas", "Peças vendidas", "%{y:,.0f} peças"

    fig = go.Figure(go.Bar(
        x=diario["Data"], y=diario[col_y],
        marker_color=COR_REALIZADO,
        hovertemplate="<b>%{x|%d/%m}</b><br>" + fmt + "<extra></extra>",
    ))
    media = diario[col_y].mean()
    fig.add_hline(y=media, line=dict(color=CINZA_REF, width=2, dash="dash"),
                  annotation_text=f"média {_num(media, 0) if metrica != 'Faturamento' else _brl(media)}",
                  annotation_position="top left")
    fig.update_layout(
        height=340, yaxis=dict(title=titulo_y), xaxis=dict(title=""),
        margin=dict(t=30, b=40), showlegend=False,
    )
    st.plotly_chart(fig, width="stretch")
else:
    st.info("Sem vendas no período selecionado.")

# --- Rankings livres (detalhe secundário) ---
with st.expander("🔎 Rankings no período (vendedor e colégio)", expanded=False):
    if len(df_vendas_periodo) == 0:
        st.info("Sem vendas no período selecionado.")
    else:
        def _ranking(df, coluna, rotulo):
            perf = (
                df.groupby(coluna)
                .agg(Valor=("Valor", "sum"), Pecas=("Qtd Peças", "sum"), Pedidos=("Pedido", "nunique"))
                .reset_index()
            )
            perf["Ticket"] = perf["Valor"] / perf["Pedidos"]
            perf["PA"] = perf["Pecas"] / perf["Pedidos"]
            perf = perf.sort_values("Valor", ascending=False).reset_index(drop=True)
            st.dataframe(
                perf, width="stretch", hide_index=True,
                column_config={
                    coluna: st.column_config.TextColumn(rotulo),
                    "Valor": st.column_config.NumberColumn("Faturamento", format="R$ %.0f"),
                    "Pecas": st.column_config.NumberColumn("Peças", format="%d"),
                    "Pedidos": st.column_config.NumberColumn("Pedidos", format="%d"),
                    "Ticket": st.column_config.NumberColumn("Ticket médio", format="R$ %.2f"),
                    "PA": st.column_config.NumberColumn("PA", format="%.2f"),
                },
            )
            st.caption(
                f"**{len(perf)}** {rotulo.lower()}(s) · **{perf['Pedidos'].sum()}** pedidos · "
                f"**{_brl(perf['Valor'].sum())}** no período"
            )

        st.markdown("**Por vendedor**")
        _ranking(df_vendas_periodo, "Vendedor", "Vendedor")
        st.markdown("**Por colégio**")
        _ranking(df_vendas_periodo, "Colegio", "Colégio")

st.divider()
rodape_frescor(dados)
