"""
Página: Configurações do Sistema (Admin Only)
Gerencia todos os parâmetros de produção via UI:
- Parâmetros gerais (metas, IDs, períodos)
- Exceções de SKU (upload/download CSV)
- Upload de dados (Excel)
- Sistema (cache, backup config)
"""

import streamlit as st

# =================================================================
# RETORNO DO OAUTH (?code&state) — capturar ANTES do login
# Esta página é o ALVO do redirect das integrações. O usuário volta do Bling/
# Olist numa SESSÃO NOVA do Streamlit (session_state zerado). Guardamos code+
# state em session_state AGORA, antes de qualquer coisa que possa consumir a
# query string (o login), e limpamos a URL. O callback lá embaixo processa.
# =================================================================
_qp = st.query_params
if "code" in _qp and "state" in _qp and "_oauth_retorno" not in st.session_state:
    st.session_state["_oauth_retorno"] = {"code": _qp["code"], "state": _qp["state"]}
    st.query_params.clear()   # tira o code da URL (F5 não re-dispara a troca)

# verificar_acesso (NÃO exigir_login): reautentica pelo COOKIE numa sessão nova.
# exigir_login só olhava session_state, então o retorno do OAuth caía em
# "faça login pela página principal" e o callback nunca rodava.
from auth import verificar_acesso
_nome, username, role = verificar_acesso()

import yaml
from datetime import datetime, date
import pandas as pd

from etl.loader import carregar_dados, carregar_config
from etl.config_store import extrair_parametros, obter_repositorio_parametros
from pedidos.integracoes.repositorio import obter_repositorio_integracoes
from pedidos.integracoes import oauth, bling as cliente_bling, olist as cliente_olist

if role != "admin":
    st.error("⛔ Acesso negado. Apenas administradores podem acessar esta página.")
    st.stop()

# =================================================================
# CALLBACK OAUTH (integrações) — processa o retorno capturado no topo
# O state foi persistido no banco (a sessão do Streamlit morre no redirect),
# então buscamos por ele para saber de qual plataforma é o retorno.
# =================================================================
_ret = st.session_state.pop("_oauth_retorno", None)
if _ret:
    try:
        _repo_int = obter_repositorio_integracoes()
        _integ = _repo_int.buscar_por_state(_ret["state"])
        if not _integ:
            st.error("Retorno OAuth com state inválido ou expirado. Refaça a conexão.")
        else:
            _plat = _integ["id"]
            _tokens = oauth.trocar_code(
                _plat, _integ.get("client_id", ""), _integ.get("client_secret", ""),
                _ret["code"], _integ.get("redirect_uri", ""))
            _repo_int.concluir_oauth(_plat, _tokens["access_token"],
                                     _tokens["refresh_token"], _tokens["expira_em"],
                                     username or "admin")
            _repo_int.registrar_evento(_plat, "oauth_conectar", True, usuario=username)
            st.success(f"✅ {_plat.capitalize()} conectado com sucesso!")
    except Exception as _exc:
        st.error(f"Falha ao concluir a conexão OAuth: {_exc}")

MESES_NOME_CFG = {
    1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril", 5: "Maio", 6: "Junho",
    7: "Julho", 8: "Agosto", 9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro",
}

# =================================================================
# FUNÇÕES AUXILIARES
# =================================================================

def salvar_parametros(config) -> bool:
    """
    Grava a Categoria B do config no Supabase (app.parametros + histórico de
    auditoria). Substitui o antigo save em config.yaml — que era efêmero no
    Streamlit Cloud (evaporava a cada redeploy). O config.yaml do git segue
    como fonte dos defaults; carregar_config() mescla os dois.
    """
    try:
        obter_repositorio_parametros().salvar(
            extrair_parametros(config), usuario=username or "admin")
        return True
    except Exception as e:
        st.error(f"❌ Falha ao salvar parâmetros no Supabase: {e}")
        return False


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

tab1, tab2, tab_int, tab3 = st.tabs([
    "📋 Parâmetros Gerais",
    "📦 Exceções de SKU",
    "🔌 Integrações",
    "ℹ️ Sistema"
])

# =================================================================
# ABA 1 — PARÂMETROS GERAIS
# =================================================================

with tab1:
    config = carregar_config()

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
        st.info(
            "📅 O calendário de rodadas agora é editado no **Simulador de Produção → "
            "Visão Geral**, junto com as coberturas alvo — lá o efeito de cada data na "
            "produção aparece ao vivo. Datas e coberturas formam um plano só."
        )
        _datas_atuais = sorted(config["planejamento"].get("rodadas_datas") or [])
        if _datas_atuais:
            _rot = ", ".join(pd.Timestamp(str(d)).strftime("%d/%m/%Y") for d in _datas_atuais)
            st.caption(f"Datas configuradas atualmente: {_rot}")
        else:
            st.caption("Nenhuma data configurada ainda — defina no Simulador de Produção.")

        col_p1, col_p2, col_p3 = st.columns(3)
        with col_p1:
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
        # rodadas_datas NÃO é mais editado aqui (vive no Simulador → Visão Geral);
        # o valor carregado apenas trafega de volta no save (extrair_parametros).
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
            if not salvar_parametros(config):
                st.stop()
            st.cache_data.clear()
            st.success("✅ Configurações salvas com sucesso!")
            st.info("💡 Cache limpo. Os dados serão recarregados na próxima visualização das páginas.")

    # =================================================================
    # Normalização de Colégios (de-para Marca_sku cru → nome de exibição)
    # =================================================================
    st.markdown("---")
    st.subheader("Normalização de Colégios")
    st.markdown(
        "O colégio é extraído automaticamente da SKU e às vezes sai **errado** "
        "(ex: `27`, códigos soltos). Aqui você define **como cada valor cru aparece** "
        "em todo o sistema (VM, Fábrica, filtros). Deixe **igual** para manter; escreva "
        "**`Outros`** (ou outro nome) para renomear/agrupar o ruído. Só o que você "
        "mudar vira regra — o resto segue como está. A coluna _Sugestão_ é só uma dica."
    )

    from etl.demanda import colegio_efetivo, parece_ruido

    config = carregar_config()
    alias_atual = config.get("colegios_alias") or {}
    dados_colegios = carregar_dados()

    _crus = dados_colegios["detalhes"]["Marca_sku"].fillna("").astype(str).str.strip()
    _crus = _crus[(_crus != "") & (_crus.str.lower() != "nan")]
    _contagem = _crus.value_counts()

    df_alias = pd.DataFrame([
        {
            "marca_sku": raw,
            "skus": int(n),
            "colegio": str(alias_atual.get(raw, raw)),
            "sugestao": "Outros" if parece_ruido(raw) else "",
        }
        for raw, n in _contagem.items()
    ])
    n_ruido = int((df_alias["sugestao"] == "Outros").sum()) if len(df_alias) else 0
    if n_ruido:
        st.caption(f"⚠️ {n_ruido} valor(es) cru(s) parecem ruído (sem letra) — sugeridos como _Outros_.")

    df_alias_edit = st.data_editor(
        df_alias,
        column_config={
            "marca_sku": st.column_config.TextColumn("Valor cru (da SKU)", disabled=True),
            "skus": st.column_config.NumberColumn("SKUs", disabled=True),
            "colegio": st.column_config.TextColumn("Colégio (exibição)",
                                                   help="Deixe igual p/ manter; escreva 'Outros' para agrupar ruído"),
            "sugestao": st.column_config.TextColumn("Sugestão", disabled=True,
                                                    help="Heurística: valor sem letra parece ruído → sugere 'Outros'"),
        },
        hide_index=True, width="stretch", height=400, key="editor_colegios_alias",
    )
    if st.button("💾 Salvar Normalização de Colégios", key="btn_salvar_alias", type="primary"):
        novo_alias = {}
        for _, row in df_alias_edit.iterrows():
            raw = str(row["marca_sku"]).strip()
            disp = str(row["colegio"]).strip()
            if raw and disp and disp != raw:      # só grava o que MUDA (identidade = default)
                novo_alias[raw] = disp
        config["colegios_alias"] = novo_alias
        if not salvar_parametros(config):
            st.stop()
        st.cache_data.clear()
        n_outros = sum(1 for v in novo_alias.values() if v == "Outros")
        st.success(f"✅ {len(novo_alias)} regra(s) de colégio salva(s) ({n_outros} → Outros). Cache limpo.")

    st.markdown("---")
    st.subheader("Parâmetros por Colégio")
    st.markdown(
        "Taxa de crescimento **base** e nível de serviço por colégio (usados por VM e Fábrica). "
        "Colégio sem linha usa taxa 1.0 e o nível de serviço padrão. "
        "Os nomes abaixo já são os **normalizados** (pós de-para acima)."
    )

    config = carregar_config()
    cfg_colegios = config.get("colegios") or {}
    ns_default_atual = int(config.get("vm", {}).get("nivel_servico_default", 95))

    det_cfg = dados_colegios["detalhes"][["Marca_sku", "Grupo"]].copy()
    det_cfg["Colegio"] = (
        det_cfg["Marca_sku"].fillna("").astype(str).str.strip()
        .map(lambda v: colegio_efetivo(v, config))
    )
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
        width="stretch",
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
        if not salvar_parametros(config):
            st.stop()
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
        width="stretch",
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
        if not salvar_parametros(config):
            st.stop()
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
        hide_index=True, width="stretch", height=500, key="editor_grupo_seg",
    )
    if st.button("💾 Salvar Agrupamento de Segmentos", key="btn_salvar_seg", type="primary"):
        novo_seg = dict(config.get("grupo_segmento") or {})
        for _, row in df_seg_edit.iterrows():
            g = str(row["grupo"]).strip()
            s = str(row["segmento"]).strip()
            if g and s:
                novo_seg[g] = s
        config["grupo_segmento"] = novo_seg
        if not salvar_parametros(config):
            st.stop()
        st.cache_data.clear()
        st.success(f"✅ Agrupamento salvo — {len(set(novo_seg.values()))} segmento(s).")

# =================================================================
# ABA 2 — EXCEÇÕES DE SKU
# =================================================================

with tab2:
    config = carregar_config()

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
                    st.dataframe(df_novo, width="stretch")

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
                        if not salvar_parametros(config):
                            st.stop()
                        st.cache_data.clear()
                        st.success(f"✅ {len(excecoes_novo)} exceção(ões) aplicada(s)!")

            except Exception as e:
                st.error(f"❌ Erro ao processar CSV: {e}")

# =================================================================
# ABA — INTEGRAÇÕES (Bling = compra AK · Olist = venda Art Kamizetas)
# =================================================================

with tab_int:
    st.subheader("Integrações com os ERPs")
    st.caption(
        "Conecte o **Bling** (pedido de compra da AK Uniformes) e o **Olist** "
        "(pedido de venda da Art Kamizetas). As chaves ficam no Supabase, não no "
        "código. A emissão em si acontece na página Pedidos de Compra."
    )

    # `ler` devolve {} quando o DDL 003 não foi aplicado (o schema `app` degrada a
    # leitura para vazio em vez de estourar) — logo, dict vazio == emissão ainda não
    # ativada. As linhas bling/olist são semeadas pelo próprio DDL, então uma linha
    # presente é sinal confiável de que a migração rodou. Sem esse gate, os cards e o
    # expander de eventos tentariam ler tabelas inexistentes e derrubariam a página.
    _integracoes_disponivel = bool(obter_repositorio_integracoes().ler("bling"))
    if not _integracoes_disponivel:
        st.warning(
            "Tabela `app.integracao` não encontrada — a emissão ainda não está "
            "ativada. Aplique o DDL `docs/sql/003_app_integracoes.sql` no SQL Editor "
            "do Supabase (cria as tabelas e semeia as linhas bling/olist) e recarregue "
            "a página."
        )

    def _card_integracao(plataforma: str, titulo: str, campos_negocio: list):
        """Card de configuração + conexão OAuth de uma plataforma."""
        repo_int = obter_repositorio_integracoes()
        integ = repo_int.ler(plataforma) or {}
        conectado = bool(integ.get("refresh_token"))
        rotulo = f"{titulo}   ·   {'✅ conectado' if conectado else '❌ não conectado'}"
        # Card retrátil: aberto durante o setup (sem conexão), recolhido depois — o
        # status vai no cabeçalho, para ler de relance sem precisar expandir.
        with st.expander(rotulo, expanded=not conectado):

            # -- 1. Chaves do app OAuth --
            with st.form(f"chaves_{plataforma}"):
                st.markdown("**Credenciais do aplicativo (OAuth2)**")
                cid = st.text_input("Client ID", value=integ.get("client_id") or "",
                                    key=f"cid_{plataforma}")
                tem_secret = bool(integ.get("client_secret"))
                csecret = st.text_input(
                    "Client Secret", value="", type="password",
                    placeholder="••• salvo (deixe em branco p/ manter)" if tem_secret else "",
                    key=f"csec_{plataforma}")
                redir = st.text_input(
                    "URL de redirecionamento", value=integ.get("redirect_uri") or "",
                    help="Registre esta MESMA URL no portal da plataforma. "
                         "Deve ser a URL pública do app + /configuracoes.",
                    key=f"redir_{plataforma}")
                if st.form_submit_button("💾 Salvar credenciais"):
                    repo_int.salvar_chaves(plataforma, cid, csecret, redir,
                                           username or "admin")
                    st.success("Credenciais salvas.")
                    st.rerun()

            if integ.get("redirect_uri"):
                st.caption("Redirect a registrar no portal:")
                st.code(integ["redirect_uri"], language=None)

            # -- 2. Conexão --
            st.markdown("**Conexão**")
            if conectado:
                validade = "?"
                if integ.get("token_expira_em"):
                    exp = pd.Timestamp(str(integ["token_expira_em"]))
                    validade = exp.strftime("%d/%m/%Y %H:%M")
                st.success(f"✅ Conectado por {integ.get('conectado_por','?')} · "
                           f"token expira {validade}")
            else:
                st.info("❌ Não conectado.")

            cc1, cc2 = st.columns(2)
            with cc1:
                pronto_p_conectar = bool(integ.get("client_id") and integ.get("redirect_uri"))
                if pronto_p_conectar:
                    state = oauth.gerar_state()
                    repo_int.salvar_state_oauth(plataforma, state, username or "admin")
                    url = oauth.montar_authorize_url(
                        plataforma, integ["client_id"], integ["redirect_uri"], state)
                    st.link_button("🔗 Conectar / Reconectar", url, use_container_width=True)
                else:
                    st.button("🔗 Conectar", disabled=True, use_container_width=True,
                              help="Salve Client ID e URL de redirecionamento primeiro.",
                              key=f"conn_disabled_{plataforma}")
            with cc2:
                if st.button("🧪 Testar conexão", key=f"testar_{plataforma}",
                             disabled=not conectado, use_container_width=True):
                    try:
                        token = oauth.obter_access_token(plataforma, repo_int)
                        testar = (cliente_bling.testar_conexao if plataforma == "bling"
                                  else cliente_olist.testar_conexao)
                        ok, msg = testar(token)
                        repo_int.registrar_evento(plataforma, "testar_conexao", ok,
                                                  detalhe={"msg": msg}, usuario=username)
                        st.success(msg) if ok else st.error(msg)
                    except Exception as exc:
                        st.error(f"Falha: {exc}")

            # -- 3. IDs de negócio --
            with st.form(f"negocio_{plataforma}"):
                st.markdown("**IDs de negócio**")
                cfg = integ.get("config") or {}
                valores = {}
                for chave, rotulo, ajuda in campos_negocio:
                    valores[chave] = st.text_input(
                        rotulo, value=str(cfg.get(chave, "") or ""),
                        help=ajuda, key=f"neg_{plataforma}_{chave}")
                if st.form_submit_button("💾 Salvar IDs de negócio"):
                    novo = {k: v.strip() for k, v in valores.items() if v.strip()}
                    if plataforma == "olist" and "situacao" not in novo:
                        novo["situacao"] = 0
                    repo_int.salvar_config(plataforma, novo, username or "admin")
                    st.success("IDs de negócio salvos.")
                    st.rerun()

            # -- 4. Só Bling: validar contrato do POST via GET (sem escrita) --
            if plataforma == "bling" and conectado:
                if st.button("📋 Validar contrato (GET pedido exemplo)",
                             key="contrato_bling"):
                    try:
                        token = oauth.obter_access_token("bling", repo_int)
                        exemplo = cliente_bling.obter_pedido_compra_exemplo(token)
                        repo_int.registrar_evento("bling", "contrato_get", True,
                                                  usuario=username)
                        if exemplo:
                            st.caption("Shape real de um pedido de compra do Bling "
                                       "(confira contra o payload de emissão):")
                            st.json(exemplo, expanded=False)
                        else:
                            st.info("A conta ainda não tem pedidos de compra p/ inspecionar.")
                    except Exception as exc:
                        st.error(f"Falha: {exc}")

    if _integracoes_disponivel:
        _card_integracao(
            "bling", "🛒 Bling — Pedido de Compra (AK Uniformes)",
            [("fornecedor_id", "ID do fornecedor (Art Kamizetas)",
              "Cadastros → Fornecedores no Bling")],
        )
        _card_integracao(
            "olist", "🏭 Olist — Pedido de Venda (Art Kamizetas)",
            [("contato_id", "ID do contato/cliente (AK Uniformes)", "Contato no Olist"),
             ("vendedor_id", "ID do vendedor", "Obrigatório na API do Olist"),
             ("deposito_id", "ID do depósito", "Obrigatório na API do Olist"),
             ("situacao", "Situação inicial (0 = Aberta)", "Código de situação do pedido")],
        )

        with st.expander("📜 Últimos eventos de integração"):
            _eventos = obter_repositorio_integracoes().listar_eventos(20)
            if len(_eventos):
                _cols = [c for c in ["criado_em", "plataforma", "acao", "sucesso", "criado_por"]
                         if c in _eventos.columns]
                st.dataframe(_eventos[_cols], width="stretch", hide_index=True)
            else:
                st.caption("Nenhum evento ainda.")

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
            # Última gravação de parâmetros no Supabase (app.parametros)
            meta = obter_repositorio_parametros().ler_metadados()
            if meta:
                _quando = pd.Timestamp(meta["atualizado_em"]).tz_convert("America/Fortaleza")
                _quem = meta.get("atualizado_por") or "—"
                st.write(f"**Parâmetros:** {_quando:%d/%m/%Y %H:%M} por {_quem}")
            else:
                st.write("**Parâmetros:** ainda não semeados (rode scripts/seed_parametros.py)")
        except Exception:
            st.write("**Parâmetros:** Supabase indisponível — usando defaults do config.yaml")

    st.markdown("---")

    col3, col4 = st.columns(2)

    with col3:
        if st.button("🔄 Forçar Recarga de Dados"):
            st.cache_data.clear()
            st.success("✅ Cache limpo. Próxima página vai recarregar os dados.")

    with col4:
        # Backup do config EFETIVO (yaml defaults + parâmetros do Supabase
        # mesclados) — o que os motores realmente usam agora.
        config_efetivo = carregar_config()
        st.download_button(
            label="💾 Backup config efetivo",
            data=yaml.safe_dump(config_efetivo, allow_unicode=True, sort_keys=False),
            file_name=f"config_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.yaml",
            mime="text/plain",
        )
