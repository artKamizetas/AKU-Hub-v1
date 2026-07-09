"""
Página: Configurações do Sistema (Admin Only)
Gerencia todos os parâmetros de produção via UI:
- Parâmetros gerais (metas, IDs, períodos)
- Exceções de SKU (upload/download CSV)
- Upload de dados (Excel)
- Sistema (cache, backup config)
"""

import streamlit as st
from auth import exigir_login
exigir_login()

from pathlib import Path
import yaml
from ruamel.yaml import YAML
from datetime import datetime, date
import pandas as pd
import io

from etl.loader import carregar_dados

# Pega as credenciais do session_state (setado em auth.py)
# Busca o role no secrets.toml baseado no username
username = st.session_state.get("username", "")
auth_config = dict(st.secrets.get("auth_config", {}))
usernames = auth_config.get("credentials", {}).get("usernames", {})
user_data = usernames.get(username, {})
role = user_data.get("role", "")

if role != "admin":
    st.error("⛔ Acesso negado. Apenas administradores podem acessar esta página.")
    st.stop()

MESES_NOME_CFG = {
    1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril", 5: "Maio", 6: "Junho",
    7: "Julho", 8: "Agosto", 9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro",
}

# =================================================================
# FUNÇÕES AUXILIARES
# =================================================================

def carregar_config():
    """Carrega config.yaml com ruamel.yaml para preservar comentários."""
    caminho_config = Path(__file__).parent.parent / "config.yaml"
    yaml_handler = YAML()
    yaml_handler.preserve_quotes = True
    yaml_handler.default_flow_style = False
    with open(caminho_config, "r", encoding="utf-8") as f:
        config = yaml_handler.load(f)
    return config, caminho_config, yaml_handler


def salvar_config(config, caminho_config, yaml_handler):
    """Salva config.yaml preservando comentários."""
    with open(caminho_config, "w", encoding="utf-8") as f:
        yaml_handler.dump(config, f)


def validar_config(config):
    """Valida estrutura mínima do config antes de salvar."""
    erros = []

    # Verificar seções obrigatórias
    obrigatorias = ["fonte", "depositos", "logistica", "daily", "fabrica", "planejamento", "vm", "demanda"]
    for secao in obrigatorias:
        if secao not in config:
            erros.append(f"Seção '{secao}' ausente")

    if "planejamento" in config:
        try:
            data_ini = datetime.fromisoformat(config["planejamento"]["periodo_historico_inicio"])
            data_fim = datetime.fromisoformat(config["planejamento"]["periodo_historico_fim"])
            if data_ini > data_fim:
                erros.append("Planejamento: periodo_historico_inicio > periodo_historico_fim")
        except (ValueError, KeyError) as e:
            erros.append(f"Planejamento: datas do período histórico inválidas ({e})")

        datas_rod = config["planejamento"].get("rodadas_datas") or []
        if len(datas_rod) == 1:
            erros.append(
                "Planejamento: o calendário explícito precisa de 2+ datas "
                "(a última só fecha o intervalo da penúltima)"
            )

    # Validar números positivos
    campos_positivos = [
        ("logistica.vm_padrao", ["logistica", "vm_padrao"]),
        ("logistica.dias_analise_giro", ["logistica", "dias_analise_giro"]),
        ("vm.dias_cobertura", ["vm", "dias_cobertura"]),
        ("vm.mult_pa", ["vm", "mult_pa"]),
        ("vm.vm_minimo", ["vm", "vm_minimo"]),
        ("vm.lead_time", ["vm", "lead_time"]),
        ("fabrica.crescimento_pct", ["fabrica", "crescimento_pct"]),
        ("fabrica.cobertura_meses", ["fabrica", "cobertura_meses"]),
        ("planejamento.lead_time_semanas", ["planejamento", "lead_time_semanas"]),
    ]

    for nome_campo, caminho in campos_positivos:
        try:
            valor = config
            for chave in caminho:
                valor = valor[chave]
            if valor < 0:
                erros.append(f"{nome_campo} não pode ser negativo")
        except (KeyError, TypeError):
            pass

    return erros


# =================================================================
# INTERFACE PRINCIPAL
# =================================================================

st.title("⚙️ Configurações do Sistema")
st.markdown("_Gerenciar parâmetros de produção, exceções de SKU e sistema._")

tab1, tab2, tab3 = st.tabs([
    "📋 Parâmetros Gerais",
    "📦 Exceções de SKU",
    "ℹ️ Sistema"
])

# =================================================================
# ABA 1 — PARÂMETROS GERAIS
# =================================================================

with tab1:
    config, caminho_config, yaml_handler = carregar_config()

    st.subheader("Parâmetros de Operação")

    cfg_vm = config.get("vm", {})
    cfg_dem = config.get("demanda", {})
    _ns_opts = [90, 92, 95, 97, 98, 99]
    _meses_opts = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]

    with st.form("form_parametros"):
        # =========================================================
        # 1. COMERCIAL (Daily)
        # =========================================================
        st.markdown("### 📈 Comercial")
        st.caption("Metas de faturamento do mês (Daily) e IDs de status de pedido do Bling.")
        col_c1, col_c2 = st.columns(2)
        with col_c1:
            meta_natal = st.number_input(
                "Meta Natal (R$)",
                value=float(config["daily"]["metas"].get("Natal", 150000)),
                min_value=0.0, step=1000.0,
            )
            meta_mossoró = st.number_input(
                "Meta Mossoró (R$)",
                value=float(config["daily"]["metas"].get("Mossoró", 100000)),
                min_value=0.0, step=1000.0,
            )
        with col_c2:
            status_aberto = st.number_input(
                "Status ID — Em Aberto",
                value=int(config["daily"]["status_ids"]["em_aberto"]),
            )
            status_andamento = st.number_input(
                "Status ID — Em Andamento",
                value=int(config["daily"]["status_ids"]["em_andamento"]),
            )
            status_pronto = st.number_input(
                "Status ID — Pronto para Retirada",
                value=int(config["daily"]["status_ids"]["pronto_retirada"]),
            )

        st.divider()

        # =========================================================
        # 2. REPOSIÇÃO DE LOJA (VM Dinâmico)
        # =========================================================
        st.markdown("### 📦 Reposição de Loja")
        st.caption(
            "VM (Visual Merchandising) calculado por SKU a partir das vendas reais da alta "
            "temporada, com pulmão de reposição por nível de serviço. Os campos de *fallback* "
            "só entram quando o SKU não tem giro suficiente para o cálculo dinâmico."
        )
        col_vm1, col_vm2, col_vm3 = st.columns(3)
        with col_vm1:
            vm_dias_cobertura = st.number_input(
                "Dias de cobertura",
                value=int(cfg_vm.get("dias_cobertura", 15)), min_value=1,
            )
            vm_mult_pa = st.number_input(
                "Multiplicador PA (piso do VM)",
                value=float(cfg_vm.get("mult_pa", 2.0)), min_value=0.1, step=0.1,
            )
            vm_minimo = st.number_input(
                "VM mínimo absoluto (unidades)",
                value=int(cfg_vm.get("vm_minimo", 2)), min_value=0,
            )
        with col_vm2:
            vm_inicio_alta = st.number_input(
                "Início alta temporada (mês)",
                value=int(cfg_vm.get("inicio_alta", 10)), min_value=1, max_value=12,
            )
            vm_fim_alta = st.number_input(
                "Fim alta temporada (mês)",
                value=int(cfg_vm.get("fim_alta", 3)), min_value=1, max_value=12,
            )
            vm_lead_time = st.number_input(
                "Lead time reposição (dias)",
                value=int(cfg_vm.get("lead_time", 3)), min_value=1,
            )
        with col_vm3:
            vm_nivel_servico = st.selectbox(
                "Nível de serviço padrão (%)",
                options=[90, 95, 97, 98, 99],
                index=[90, 95, 97, 98, 99].index(
                    round(cfg_vm.get("nivel_servico_default", 95))
                    if round(cfg_vm.get("nivel_servico_default", 95)) in [90, 95, 97, 98, 99]
                    else 95
                ),
            )
            vm_toggle_cresc = st.checkbox(
                "Aplicar crescimento (colégio × grupo)",
                value=cfg_vm.get("aplicar_crescimento", True),
            )

        st.markdown("**Fallback — SKU sem giro para o cálculo dinâmico**")
        col_fb1, col_fb2 = st.columns(2)
        with col_fb1:
            vm_padrao = st.number_input(
                "VM padrão fixo (unidades)",
                value=int(config["logistica"]["vm_padrao"]), min_value=0,
            )
        with col_fb2:
            dias_analise = st.number_input(
                "Dias de análise de giro",
                value=int(config["logistica"]["dias_analise_giro"]), min_value=1,
            )

        st.divider()

        # =========================================================
        # 3. PRODUÇÃO (Simulador — Demanda + Planejamento)
        # =========================================================
        st.markdown("### 🏭 Produção (Simulador)")
        st.caption(
            "Motor único de demanda ancorada na última temporada de ALTA × crescimento, com "
            "política order-up-to (estoque de segurança por nível de serviço). Base comum da "
            "Sugestão por SKU (tática) e da Visão Geral (rodadas anuais)."
        )

        st.markdown("**Demanda / Abastecimento**")
        col_d1, col_d2, col_d3 = st.columns(3)
        with col_d1:
            dem_ns_alta = st.selectbox(
                "Nível de serviço — ALTA (%)",
                options=_ns_opts,
                index=_ns_opts.index(round(cfg_dem.get("nivel_servico_alta", 99)))
                if round(cfg_dem.get("nivel_servico_alta", 99)) in _ns_opts else 5,
                help="Não pode faltar na alta → nível alto (99%).",
            )
            dem_ns_baixa = st.selectbox(
                "Nível de serviço — BAIXA (%)",
                options=_ns_opts,
                index=_ns_opts.index(round(cfg_dem.get("nivel_servico_baixa", 92)))
                if round(cfg_dem.get("nivel_servico_baixa", 92)) in _ns_opts else 1,
            )
        with col_d2:
            dem_cv = st.number_input(
                "Variação da Demanda — incerteza",
                value=float(cfg_dem.get("variacao_demanda", 0.25)),
                min_value=0.0, max_value=2.0, step=0.05,
                help="Multiplica o estoque de segurança. Maior = mais margem.",
            )
            dem_janela_alta = st.multiselect(
                "Meses da alta temporada (âncora)",
                options=_meses_opts,
                default=cfg_dem.get("janela_alta", [12, 1, 2]),
                format_func=lambda x: MESES_NOME_CFG[x],
                help="Ordem cronológica da temporada (ex: Dez, Jan, Fev).",
            )
        with col_d3:
            dem_toggle_fab = st.checkbox(
                "Aplicar crescimento na produção",
                value=cfg_dem.get("aplicar_crescimento_fabrica", True),
            )

        st.markdown("**Planejamento — calendário de rodadas**")
        st.caption(
            "**Calendário explícito (recomendado):** datas reais de disparo deste ano E do "
            "próximo — nada se repete automaticamente; permite rodada atrasada este ano e "
            "antecipada no próximo. A **última data só fecha o intervalo da penúltima** "
            "(inclua sempre a primeira rodada do ano seguinte). Deixe a tabela vazia para "
            "usar os meses fixos (que se repetem todo ano)."
        )
        _datas_atuais = config["planejamento"].get("rodadas_datas") or []
        df_rodadas_datas = pd.DataFrame({
            "data_disparo": [pd.Timestamp(str(d)).date() for d in _datas_atuais]  # str(): ruamel devolve DoubleQuotedScalarString
        }) if _datas_atuais else pd.DataFrame({"data_disparo": pd.Series(dtype="object")})
        df_rodadas_datas_edit = st.data_editor(
            df_rodadas_datas,
            column_config={
                "data_disparo": st.column_config.DateColumn(
                    "Data de disparo", format="DD/MM/YYYY", required=True,
                ),
            },
            num_rows="dynamic",
            hide_index=True,
            key="editor_rodadas_datas",
        )

        col_p1, col_p2, col_p3 = st.columns(3)
        with col_p1:
            rodadas = st.multiselect(
                "Meses fixos (fallback, repetem todo ano)",
                options=_meses_opts,
                default=config["planejamento"]["rodadas"],
                help="Usados apenas quando o calendário explícito acima está vazio.",
            )
            lead_time = st.number_input(
                "Lead time de produção (semanas)",
                value=int(config["planejamento"]["lead_time_semanas"]), min_value=1,
            )
        with col_p2:
            periodo_hist_ini = st.date_input(
                "Período histórico — Início",
                value=datetime.fromisoformat(config["planejamento"]["periodo_historico_inicio"]).date(),
                help="Janela de vendas passadas que ensina o FORMATO do ano (sazonalidade "
                     "e base dos SKUs que só vendem na baixa). Use 12+ meses, incluindo baixa. "
                     "O tamanho do pico NÃO vem daqui — vem das vendas reais da última alta.",
            )
        with col_p3:
            periodo_hist_fim = st.date_input(
                "Período histórico — Fim",
                value=datetime.fromisoformat(config["planejamento"]["periodo_historico_fim"]).date(),
            )

        st.markdown("**Fallback da Fábrica**")
        st.caption(
            "Crescimento é o fallback para colégios sem taxa própria (Configurações → Colégios); "
            "cobertura só entra quando nenhuma rodada está configurada acima; "
            "correção manual soma um ajuste fixo à demanda de todo SKU."
        )
        col_f1, col_f2, col_f3 = st.columns(3)
        with col_f1:
            crescimento = st.number_input(
                "Crescimento — fallback (%)",
                value=float(config["fabrica"]["crescimento_pct"]), min_value=0.0, step=0.5,
            )
        with col_f2:
            cobertura_meses = st.number_input(
                "Cobertura — fallback (meses)",
                value=int(config["fabrica"]["cobertura_meses"]), min_value=1,
            )
        with col_f3:
            correcao_manual = st.number_input(
                "Correção manual global (unidades)",
                value=int(config["fabrica"]["correcao_manual"]), step=1,
            )

        # Botão enviar
        submitted = st.form_submit_button("💾 Salvar Configurações", type="primary")

    if submitted:
        # Atualizar config
        config["daily"]["metas"]["Natal"] = meta_natal
        config["daily"]["metas"]["Mossoró"] = meta_mossoró
        config["daily"]["status_ids"]["em_aberto"] = status_aberto
        config["daily"]["status_ids"]["em_andamento"] = status_andamento
        config["daily"]["status_ids"]["pronto_retirada"] = status_pronto
        config["logistica"]["vm_padrao"] = vm_padrao
        config["logistica"]["dias_analise_giro"] = dias_analise
        config.setdefault("vm", {})
        config["vm"]["dias_cobertura"] = vm_dias_cobertura
        config["vm"]["inicio_alta"] = vm_inicio_alta
        config["vm"]["fim_alta"] = vm_fim_alta
        config["vm"]["mult_pa"] = vm_mult_pa
        config["vm"]["vm_minimo"] = vm_minimo
        config["vm"]["lead_time"] = vm_lead_time
        config["vm"]["nivel_servico_default"] = vm_nivel_servico
        config["vm"]["aplicar_crescimento"] = bool(vm_toggle_cresc)
        config.setdefault("demanda", {})
        config["demanda"]["nivel_servico_alta"] = dem_ns_alta
        config["demanda"]["nivel_servico_baixa"] = dem_ns_baixa
        config["demanda"]["variacao_demanda"] = dem_cv
        config["demanda"]["janela_alta"] = list(dem_janela_alta)
        config["demanda"]["aplicar_crescimento_fabrica"] = bool(dem_toggle_fab)
        config["fabrica"]["crescimento_pct"] = crescimento
        config["fabrica"]["cobertura_meses"] = cobertura_meses
        config["fabrica"]["correcao_manual"] = correcao_manual
        config["planejamento"]["rodadas"] = sorted(list(set(rodadas)))
        _datas_novas = sorted(
            pd.Timestamp(d).date().isoformat()
            for d in df_rodadas_datas_edit["data_disparo"].dropna()
        )
        config["planejamento"]["rodadas_datas"] = _datas_novas or None
        config["planejamento"]["lead_time_semanas"] = lead_time
        config["planejamento"]["periodo_historico_inicio"] = periodo_hist_ini.isoformat()
        config["planejamento"]["periodo_historico_fim"] = periodo_hist_fim.isoformat()

        # Validar
        erros = validar_config(config)
        if erros:
            st.error("❌ Erros encontrados:")
            for erro in erros:
                st.write(f"- {erro}")
        else:
            # Salvar
            salvar_config(config, caminho_config, yaml_handler)
            st.cache_data.clear()
            st.success("✅ Configurações salvas com sucesso!")
            st.info("💡 Cache limpo. Os dados serão recarregados na próxima visualização das páginas.")

    st.markdown("---")
    st.subheader("Parâmetros por Colégio")
    st.markdown(
        "Taxa de crescimento **base** e nível de serviço por colégio (usados por VM e Fábrica). "
        "Colégio sem linha usa taxa 1.0 e o nível de serviço padrão."
    )

    config, caminho_config, yaml_handler = carregar_config()
    cfg_colegios = config.get("colegios") or {}
    ns_default_atual = int(config.get("vm", {}).get("nivel_servico_default", 95))

    dados_colegios = carregar_dados()
    det_cfg = dados_colegios["detalhes"][["Marca_sku", "Grupo"]].copy()
    det_cfg["Colegio"] = det_cfg["Marca_sku"].fillna("").astype(str).str.strip()
    det_cfg["GrupoC"] = det_cfg["Grupo"].fillna("").astype(str).str.strip()
    det_cfg = det_cfg[(det_cfg["Colegio"] != "") & (det_cfg["Colegio"] != "nan")]

    colegios_disponiveis = sorted(
        c for c in det_cfg["Colegio"].unique() if c and c != "nan"
    )

    from etl.demanda import calcular_proporcao_baixa
    prop_global = round(float(calcular_proporcao_baixa(dados_colegios, config)), 3)
    st.caption(
        f"**Proporção da baixa** = quanto a baixa vende em relação à alta. Base **global {prop_global}** "
        "(medida, últimos 2 ciclos), pré-preenchida. Só mude num colégio que você sabe ter cauda "
        "diferente (ex: vende o ano todo). Célula igual ao global fica viva; só o que mudar vira override."
    )

    df_colegios = pd.DataFrame([
        {
            "colegio": c,
            "taxa_crescimento": float(cfg_colegios.get(c, {}).get("taxa_crescimento", 1.0)),
            "nivel_servico": int(cfg_colegios.get(c, {}).get("nivel_servico", ns_default_atual)),
            "proporcao_baixa": float(cfg_colegios.get(c, {}).get("proporcao_baixa", prop_global)),
        }
        for c in colegios_disponiveis
    ])

    df_colegios_editado = st.data_editor(
        df_colegios,
        column_config={
            "colegio": st.column_config.TextColumn("Colégio", disabled=True),
            "taxa_crescimento": st.column_config.NumberColumn("Taxa base", min_value=0.0, step=0.05),
            "nivel_servico": st.column_config.SelectboxColumn("Nível de serviço (%)", options=[90, 95, 97, 98, 99]),
            "proporcao_baixa": st.column_config.NumberColumn("Proporção baixa", min_value=0.0, step=0.05, format="%.3f",
                                                             help=f"Cauda da baixa vs alta. Global (default) = {prop_global}"),
        },
        hide_index=True,
        use_container_width=True,
        key="editor_colegios",
    )

    if st.button("💾 Salvar Colégios (taxa base)", key="btn_salvar_colegios", type="primary"):
        novo_colegios = dict(config.get("colegios") or {})
        for _, row in df_colegios_editado.iterrows():
            c = row["colegio"]
            entry = dict(novo_colegios.get(c, {}))     # preserva crescimento_grupos
            entry["taxa_crescimento"] = float(row["taxa_crescimento"])
            entry["nivel_servico"] = int(row["nivel_servico"])
            pb = float(row["proporcao_baixa"])
            if abs(pb - prop_global) > 1e-6:           # só grava override se difere do global
                entry["proporcao_baixa"] = round(pb, 4)
            else:
                entry.pop("proporcao_baixa", None)
            novo_colegios[c] = entry
        config["colegios"] = novo_colegios
        salvar_config(config, caminho_config, yaml_handler)
        st.cache_data.clear()
        st.success(f"✅ Taxa base de {len(df_colegios_editado)} colégio(s) salva!")

    # --- Matriz crescimento por (colégio × grupo/série) ---
    st.markdown("#### Crescimento por Colégio × Grupo (série)")
    st.markdown(
        "**Pré-preenchido com o crescimento medido dos dados** (coluna _Observado_ = alta-sobre-alta "
        "por colégio×segmento). Edite só onde você **sabe de algo que os dados não sabem** (expansão de "
        "turma futura, colégio novo). Célula deixada **igual ao observado fica viva** — re-mede sozinha "
        "a cada temporada; só o que você **mudar** vira override fixo. Vazio no Observado = amostra pequena."
    )
    from etl import demanda as _dem
    obs_cresc = _dem.calcular_crescimento_observado(dados_colegios, config)
    mapa_seg_cfg = _dem.mapa_grupo_segmento(config)

    def _obs_cel(colegio, grupo):
        o = obs_cresc.get(colegio, {})
        v = (o.get("segmentos") or {}).get(mapa_seg_cfg.get(grupo, "Outros"))
        return v if v is not None else o.get("__geral__")

    celulas = (
        det_cfg[det_cfg["GrupoC"].ne("") & det_cfg["GrupoC"].ne("nan")]
        .groupby(["Colegio", "GrupoC"]).size().reset_index(name="n_skus")
        .sort_values(["Colegio", "GrupoC"])
    )
    linhas_matriz = []
    for _, r in celulas.iterrows():
        col_, gr_ = r["Colegio"], r["GrupoC"]
        manual = (cfg_colegios.get(col_, {}).get("crescimento_grupos") or {}).get(gr_)
        ob = _obs_cel(col_, gr_)
        base = ob if ob is not None else 1.0
        linhas_matriz.append({
            "colegio": col_, "grupo": gr_, "segmento": mapa_seg_cfg.get(gr_, "Outros"),
            "skus": int(r["n_skus"]),
            "observado": round(ob, 3) if ob is not None else None,
            "taxa_crescimento": round(float(manual) if manual is not None else base, 3),
            "origem": "manual" if manual is not None else ("medido" if ob is not None else "padrão"),
        })
    df_matriz = pd.DataFrame(linhas_matriz)

    df_matriz_editado = st.data_editor(
        df_matriz,
        column_config={
            "colegio": st.column_config.TextColumn("Colégio", disabled=True),
            "grupo": st.column_config.TextColumn("Grupo", disabled=True),
            "segmento": st.column_config.TextColumn("Segmento", disabled=True),
            "skus": st.column_config.NumberColumn("SKUs", disabled=True),
            "observado": st.column_config.NumberColumn("Observado", disabled=True, format="%.3f",
                                                       help="Crescimento medido dos dados (colégio×segmento). Vazio = amostra insuficiente"),
            "taxa_crescimento": st.column_config.NumberColumn("Crescimento aplicado", min_value=0.0, step=0.05),
            "origem": st.column_config.TextColumn("Origem", disabled=True,
                                                  help="manual = você definiu · medido = dos dados · padrão = fallback global"),
        },
        hide_index=True,
        use_container_width=True,
        height=500,
        key="editor_matriz_grupo",
    )

    if st.button("💾 Salvar Crescimento por Grupo", key="btn_salvar_matriz", type="primary"):
        novo_colegios = dict(config.get("colegios") or {})
        # Grava override SÓ onde o usuário mudou vs o observado (senão fica vivo)
        grupos_por_col = {}
        for _, row in df_matriz_editado.iterrows():
            col_, gr_ = row["colegio"], row["grupo"]
            taxa = float(row["taxa_crescimento"])
            ob = _obs_cel(col_, gr_)
            base = ob if ob is not None else 1.0
            if abs(taxa - base) > 1e-6:
                grupos_por_col.setdefault(col_, {})[gr_] = round(taxa, 4)
        for c in colegios_disponiveis:
            entry = dict(novo_colegios.get(c, {}))
            if c in grupos_por_col:
                entry["crescimento_grupos"] = grupos_por_col[c]
            else:
                entry.pop("crescimento_grupos", None)
            novo_colegios[c] = entry
        config["colegios"] = novo_colegios
        salvar_config(config, caminho_config, yaml_handler)
        st.cache_data.clear()
        n = sum(len(v) for v in grupos_por_col.values())
        st.success(f"✅ {n} override(s) manual(is) salvos — o resto segue o observado (vivo).")

    # --- Agrupamento de Grupos em Segmentos (nível intermediário) ---
    st.markdown("---")
    st.subheader("Agrupamento de Grupos em Segmentos")
    st.markdown(
        "O **crescimento observado** é medido por _colégio × segmento_. O segmento é um nível "
        "intermediário que junta as siglas de Grupo (EF1·EF2·EFD → Fundamental, EDF → Ed. Física…) "
        "para dar células mais estáveis. Reagrupe aqui para testar outros cortes — afeta o cálculo "
        "de crescimento. Grupo sem segmento cai em _Outros_; você pode criar segmentos novos."
    )
    from etl.demanda import mapa_grupo_segmento
    mapa_atual = mapa_grupo_segmento(config)
    grupos_vol = (
        det_cfg[det_cfg["GrupoC"].ne("") & det_cfg["GrupoC"].ne("nan")]
        .groupby("GrupoC").size().reset_index(name="skus").sort_values("skus", ascending=False)
    )
    df_seg = pd.DataFrame([
        {"grupo": r["GrupoC"], "skus": int(r["skus"]),
         "segmento": mapa_atual.get(r["GrupoC"], "Outros")}
        for _, r in grupos_vol.iterrows()
    ])
    st.caption("Segmentos em uso: " + " · ".join(sorted(set(mapa_atual.values()))))
    df_seg_edit = st.data_editor(
        df_seg,
        column_config={
            "grupo": st.column_config.TextColumn("Grupo", disabled=True),
            "skus": st.column_config.NumberColumn("SKUs", disabled=True),
            "segmento": st.column_config.TextColumn("Segmento", help="Nome do balde — pode reutilizar ou criar novos"),
        },
        hide_index=True, use_container_width=True, height=500, key="editor_grupo_seg",
    )
    if st.button("💾 Salvar Agrupamento de Segmentos", key="btn_salvar_seg", type="primary"):
        novo_seg = dict(config.get("grupo_segmento") or {})
        for _, row in df_seg_edit.iterrows():
            g = str(row["grupo"]).strip()
            s = str(row["segmento"]).strip()
            if g and s:
                novo_seg[g] = s
        config["grupo_segmento"] = novo_seg
        salvar_config(config, caminho_config, yaml_handler)
        st.cache_data.clear()
        st.success(f"✅ Agrupamento salvo — {len(set(novo_seg.values()))} segmento(s).")

# =================================================================
# ABA 2 — EXCEÇÕES DE SKU
# =================================================================

with tab2:
    config, caminho_config, yaml_handler = carregar_config()

    st.subheader("Gerenciar Exceções de SKU")
    st.markdown(
        "Sobrescreve regras globais para produtos específicos. Colunas: "
        "**`vm_override`** (força o VM de prateleira na Reposição de Loja), "
        "**`correcao_manual`** (ajuste do SKU — unidades no PCP, fator no VM dinâmico) e "
        "**`proporcao_baixa`** (cauda da baixa vs alta do SKU — para gigantes de cauda curta como o NEV009; "
        "vazio = usa o global/colégio)."
    )

    col_down, col_up = st.columns(2)

    # Download template
    with col_down:
        st.markdown("#### 📥 Baixar Template")

        # Preparar dados atuais
        excecoes = config.get("excecoes_sku") or {}
        df_excecoes = pd.DataFrame([
            {
                "sku": sku,
                "vm_override": params.get("vm", "") if isinstance(params, dict) else "",
                "correcao_manual": params.get("correcao", "") if isinstance(params, dict) else "",
                "proporcao_baixa": params.get("proporcao_baixa", "") if isinstance(params, dict) else "",
            }
            for sku, params in excecoes.items()
        ])

        if len(df_excecoes) == 0:
            df_excecoes = pd.DataFrame({
                "sku": ["EXEMPLO-P", "EXEMPLO-M"],
                "vm_override": [5, 8],
                "correcao_manual": ["", 10],
                "proporcao_baixa": [0.15, ""],
            })
            csv_data = df_excecoes.to_csv(index=False)
            st.info("📝 Template padrão (nenhuma exceção cadastrada ainda)")
        else:
            csv_data = df_excecoes.to_csv(index=False)
            st.info(f"📝 {len(df_excecoes)} exceção(ões) cadastrada(s)")

        st.download_button(
            label="⬇️ Baixar CSV",
            data=csv_data,
            file_name="excecoes_sku.csv",
            mime="text/csv",
            type="primary",
        )

    # Upload de exceções
    with col_up:
        st.markdown("#### 📤 Fazer Upload")

        uploaded_file = st.file_uploader("Selecione arquivo CSV", type=["csv"])

        if uploaded_file is not None:
            try:
                df_novo = pd.read_csv(uploaded_file)

                # Validar colunas
                colunas_obrigatorias = ["sku"]
                if not all(col in df_novo.columns for col in colunas_obrigatorias):
                    st.error(f"❌ Colunas obrigatórias: {', '.join(colunas_obrigatorias)}")
                else:
                    st.dataframe(df_novo, use_container_width=True)

                    if st.button("✅ Aplicar Exceções", key="btn_aplicar_sku", type="primary"):
                        # Converter para dict
                        excecoes_novo = {}
                        for _, row in df_novo.iterrows():
                            sku = str(row["sku"]).strip()
                            params = {}

                            if pd.notna(row.get("vm_override")):
                                params["vm"] = int(row["vm_override"])
                            if pd.notna(row.get("correcao_manual")):
                                params["correcao"] = int(row["correcao_manual"])
                            if pd.notna(row.get("proporcao_baixa")):
                                params["proporcao_baixa"] = float(row["proporcao_baixa"])

                            if params:
                                excecoes_novo[sku] = params

                        # Salvar
                        config["excecoes_sku"] = excecoes_novo
                        salvar_config(config, caminho_config, yaml_handler)
                        st.cache_data.clear()
                        st.success(f"✅ {len(excecoes_novo)} exceção(ões) aplicada(s)!")

            except Exception as e:
                st.error(f"❌ Erro ao processar CSV: {e}")

# =================================================================
# ABA 3 — INFORMAÇÕES DO SISTEMA
# =================================================================

with tab3:
    st.subheader("Informações do Sistema")

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("#### 📊 Versões")
        st.write(f"**Python:** {__import__('sys').version.split()[0]}")
        st.write(f"**Streamlit:** {st.__version__}")
        st.write(f"**Pandas:** {pd.__version__}")

    with col2:
        st.markdown("#### 📊 Fonte de Dados")
        st.write("**Fonte:** Supabase — Bling ERP")
        st.write("**Cache:** Recarregado a cada 1 hora (ou ao clicar 🔄)")

        try:
            caminho_config = Path(__file__).parent.parent / "config.yaml"
            mod_time = datetime.fromtimestamp(caminho_config.stat().st_mtime)
            st.write(f"**Config:** {mod_time.strftime('%d/%m/%Y %H:%M')}")
        except:
            st.write("**Config:** Erro ao ler")

    st.markdown("---")

    col3, col4 = st.columns(2)

    with col3:
        if st.button("🔄 Forçar Recarga de Dados"):
            st.cache_data.clear()
            st.success("✅ Cache limpo. Próxima página vai recarregar os dados.")

    with col4:
        config, _, yaml_handler = carregar_config()
        config_yaml = io.StringIO()
        yaml_handler.dump(config, config_yaml)

        st.download_button(
            label="💾 Backup config.yaml",
            data=config_yaml.getvalue(),
            file_name=f"config_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.yaml",
            mime="text/plain",
        )
