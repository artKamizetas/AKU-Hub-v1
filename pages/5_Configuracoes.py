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

# Gate de admin. `usuario` é o e-mail da conta Google: alimenta as colunas de
# auditoria de tudo que esta página grava.
from auth import (
    exigir_admin, invalidar_cache_usuarios, paginas_do_role,
    validar_edicao_usuarios,
)
_nome, usuario, role = exigir_admin()

import yaml
from datetime import datetime, date
import pandas as pd

from etl.loader import carregar_dados, carregar_config
from etl.config_store import extrair_parametros, obter_repositorio_parametros
from pedidos.integracoes.repositorio import obter_repositorio_integracoes
from pedidos.integracoes import oauth, bling as cliente_bling, olist as cliente_olist
from auth_store import (
    ROLES_VALIDOS, EmailInvalido, UsuarioJaExiste,
    normalizar_email, obter_repositorio_usuarios,
)

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
                                     usuario)
            _repo_int.registrar_evento(_plat, "oauth_conectar", True, usuario=usuario)
            st.success(f"✅ {_plat.capitalize()} conectado com sucesso!")
    except Exception as _exc:
        st.error(f"Falha ao concluir a conexão OAuth: {_exc}")

MESES_NOME_CFG = {
    1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril", 5: "Maio", 6: "Junho",
    7: "Julho", 8: "Agosto", 9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro",
}


def _brl_cfg(v) -> str:
    """Real no formato pt-BR (milhar '.', decimal ',')."""
    if v is None:
        return "—"
    return "R$ " + f"{v:,.0f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")

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
            extrair_parametros(config), usuario=usuario)
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

tab1, tab2, tab_int, tab_usr, tab3 = st.tabs([
    "📋 Parâmetros Gerais",
    "📦 Exceções de SKU",
    "🔌 Integrações",
    "👥 Usuários",
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
        st.caption(
            "IDs de status de pedido do Bling. As **metas** migraram para a seção "
            "*Metas Mensais* logo abaixo — são por loja × mês, em três níveis."
        )
        col_c1, col_c2, col_c3 = st.columns(3)
        with col_c1:
            status_aberto = st.number_input(
                "Status ID — Em Aberto",
                value=int(config["daily"]["status_ids"]["em_aberto"]),
            )
        with col_c2:
            status_andamento = st.number_input(
                "Status ID — Em Andamento",
                value=int(config["daily"]["status_ids"]["em_andamento"]),
            )
        with col_c3:
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
            # Só o cache de CONFIG — não o de dados (TTL 1 h). O clear()
            # global levava junto a leitura do Supabase, e cada "Salvar"
            # custava uma carga fria (~10 s) na tela seguinte.
            carregar_config.clear()
            st.success("✅ Configurações salvas com sucesso!")
            st.info("💡 Cache limpo. Os dados serão recarregados na próxima visualização das páginas.")

    # =================================================================
    # Metas Mensais Escalonadas (Prata/Ouro/Diamante) por Loja × Mês
    # Spec: docs/requisitos/metas-escalonadas.md
    # =================================================================
    st.markdown("---")
    st.subheader("🎯 Metas Mensais")
    st.markdown(
        "Três níveis por mês e por loja — **Prata** (o piso aceitável), **Ouro** "
        "(a meta de verdade) e **Diamante** (a superação). Valem para "
        "**Faturamento** e **PA** (peças por atendimento). Célula em branco = mês "
        "sem meta: o Daily avisa em vez de inventar um número. A meta de cada "
        "**vendedor** é rateada automaticamente a partir da meta da loja "
        "(configure a atribuição logo abaixo)."
    )

    config = carregar_config()
    _lojas_cfg = [l["nome"] for l in config["depositos"]["lojas"]]
    _ano_atual = date.today().year

    @st.cache_data(ttl=600, show_spinner="Levantando o realizado dos últimos meses…")
    def _realizado_mensal():
        """Faturamento/peças/pedidos realizados por (ano, mês, loja) — a âncora
        que o gestor usa para decidir a meta. Vem do MESMO pipeline do Daily
        (processar_daily), então bate com o que a tela de acompanhamento mostra."""
        from etl.daily import processar_daily
        det, _, _ = processar_daily(carregar_dados(), carregar_config())
        sits = config["daily"]["situacoes_venda"]
        v = det[det["id_situacao"].isin(sits)].copy()
        v["_ano"] = v["Data"].dt.year
        v["_mes"] = v["Data"].dt.month
        g = (
            v.groupby(["_ano", "_mes", "LojaConfig"])
            .agg(faturamento=("Valor", "sum"), pecas=("Qtd Peças", "sum"),
                 pedidos=("ID_pedido", "nunique"))
            .reset_index()
        )
        g["pa"] = g.apply(lambda r: (r["pecas"] / r["pedidos"]) if r["pedidos"] else 0.0, axis=1)
        return g

    try:
        _realizado = _realizado_mensal()
    except Exception as _e:
        _realizado = pd.DataFrame(columns=["_ano", "_mes", "LojaConfig", "faturamento", "pecas", "pedidos", "pa"])
        st.caption(f"⚠️ Histórico de realizado indisponível ({_e}) — as colunas de referência ficam vazias.")

    col_ano, col_loja = st.columns([1, 2])
    with col_ano:
        _anos_opts = [_ano_atual - 1, _ano_atual, _ano_atual + 1]
        ano_meta = st.pills("Ano", _anos_opts, default=_ano_atual, key="metas_ano")
        if ano_meta is None:
            ano_meta = _ano_atual
    with col_loja:
        loja_meta = st.segmented_control("Loja", _lojas_cfg, default=_lojas_cfg[0], key="metas_loja")
        if loja_meta is None:
            loja_meta = _lojas_cfg[0]

    # O espelho mudou de codificação de situação em 2026: o histórico antigo
    # está quase todo em id_situacao=1, e `daily.situacoes_venda` (hoje [9] =
    # Atendido) não o alcança. Sem isso a coluna de referência vem vazia — e
    # vazio silencioso engana quem está definindo a meta.
    _ref_ano = ano_meta - 1
    _tem_ref = len(_realizado[(_realizado["_ano"] == _ref_ano)
                              & (_realizado["LojaConfig"] == loja_meta)]) > 0
    if not _tem_ref:
        st.caption(
            f"ℹ️ Sem realizado de **{_ref_ano}** para {loja_meta} nas situações contadas como venda "
            f"(`daily.situacoes_venda` = {config['daily']['situacoes_venda']}). O histórico anterior a "
            "2026 está gravado com outro código de situação no espelho — a coluna de referência e o "
            "botão *Propor* ficam sem base neste ano. Defina as metas manualmente ou ajuste "
            "`situacoes_venda` no `config.yaml`."
        )

    _metas_salvas = dict(config["daily"].get("metas_mensais") or {})

    def _linha_meta(mes: int) -> dict:
        """Uma linha do editor: metas salvas do mês + realizado do MESMO mês no
        ano anterior (a referência de quem decide o número)."""
        from etl import metas as _m
        comp = _m.chave_competencia(ano_meta, mes)
        bloco = (_metas_salvas.get(comp) or {}).get(loja_meta) or {}
        fat = bloco.get("faturamento") or {}
        pa = bloco.get("pa") or {}
        ref = _realizado[
            (_realizado["_ano"] == ano_meta - 1)
            & (_realizado["_mes"] == mes)
            & (_realizado["LojaConfig"] == loja_meta)
        ]
        return {
            "mes": MESES_NOME_CFG[mes],
            "fat_prata": fat.get("prata"), "fat_ouro": fat.get("ouro"), "fat_diamante": fat.get("diamante"),
            "pa_prata": pa.get("prata"), "pa_ouro": pa.get("ouro"), "pa_diamante": pa.get("diamante"),
            "ref_fat": float(ref["faturamento"].iloc[0]) if len(ref) else None,
            "ref_pa": float(ref["pa"].iloc[0]) if len(ref) else None,
        }

    # Preview de sessão: os atalhos abaixo escrevem aqui e o editor lê daqui.
    # A key do editor carrega ano/loja/versão — sem a versão, o data_editor
    # ignoraria a nova base (mantém o estado interno enquanto a key não muda).
    _chave_preview = f"{ano_meta}|{loja_meta}"
    _versao = st.session_state.get("_metas_versao", 0)
    _preview = st.session_state.get("_metas_preview", {}).get(_chave_preview)
    df_metas_base = _preview if _preview is not None else pd.DataFrame(
        [_linha_meta(m) for m in range(1, 13)])

    from etl import metas as _m
    _linhas_base = [
        {**r, "mes": {v: k for k, v in MESES_NOME_CFG.items()}[r["mes"]]}
        for r in df_metas_base.to_dict("records")
    ]

    def _aplicar_atalho(linhas_novas):
        """Escreve o resultado de um atalho no preview e força o redesenho."""
        df = pd.DataFrame([
            {**l, "mes": MESES_NOME_CFG[l["mes"]]} for l in linhas_novas
        ])[df_metas_base.columns]
        st.session_state.setdefault("_metas_preview", {})[_chave_preview] = df
        st.session_state["_metas_versao"] = _versao + 1
        st.rerun()

    # Há metas cadastradas no ano anterior para esta loja?
    _n_ano_ant = sum(
        1 for mes in range(1, 13)
        if ((_metas_salvas.get(_m.chave_competencia(ano_meta - 1, mes)) or {}).get(loja_meta))
    )

    col_a1, col_a2, col_a3, col_a4 = st.columns([2, 3, 3, 2])
    with col_a1:
        _pct_cresc = st.number_input(
            "Crescimento (%)", value=10.0, step=1.0,
            help="Aplicado pelos atalhos ✨ Propor e 📋 Copiar. Use 0 para cópia literal.",
            key="metas_pct_cresc",
        )
    with col_a2:
        st.write("")
        if st.button("✨ Propor a partir do realizado", key="btn_metas_propor",
                     disabled=not _tem_ref,
                     help=("Ouro = realizado do ano anterior + crescimento; Prata = 85% do Ouro; "
                           "Diamante = 120%." if _tem_ref
                           else f"Indisponível: sem realizado de {_ref_ano} para {loja_meta}.")):
            df_prop = df_metas_base.copy()
            fator = 1 + (_pct_cresc / 100)
            for i, r in df_prop.iterrows():
                if pd.notna(r["ref_fat"]) and r["ref_fat"]:
                    ouro = round(float(r["ref_fat"]) * fator, 2)
                    df_prop.at[i, "fat_ouro"] = ouro
                    df_prop.at[i, "fat_prata"] = round(ouro * 0.85, 2)
                    df_prop.at[i, "fat_diamante"] = round(ouro * 1.20, 2)
                if pd.notna(r["ref_pa"]) and r["ref_pa"]:
                    pa_ouro = round(float(r["ref_pa"]) * fator, 2)
                    df_prop.at[i, "pa_ouro"] = pa_ouro
                    df_prop.at[i, "pa_prata"] = round(pa_ouro * 0.85, 2)
                    df_prop.at[i, "pa_diamante"] = round(pa_ouro * 1.20, 2)
            st.session_state.setdefault("_metas_preview", {})[_chave_preview] = df_prop
            st.session_state["_metas_versao"] = _versao + 1
            st.rerun()
    with col_a3:
        st.write("")
        _ajuda_copiar = (
            f"Traz as metas já cadastradas de {ano_meta - 1} para esta loja "
            f"(+{_pct_cresc:.0f}%). Não depende do histórico de vendas."
            if _n_ano_ant else
            f"Indisponível: nenhuma meta cadastrada em {ano_meta - 1} para {loja_meta}."
        )
        if st.button(f"📋 Copiar metas de {ano_meta - 1}", key="btn_metas_copiar",
                     disabled=not _n_ano_ant, help=_ajuda_copiar):
            _novas, _n = _m.copiar_do_ano_anterior(
                _linhas_base, _metas_salvas, ano_meta, loja_meta,
                fator=1 + (_pct_cresc / 100))
            _aplicar_atalho(_novas)

        if st.button("⤵️ Replicar nos meses vazios", key="btn_metas_replicar",
                     help="Copia a primeira linha preenchida para todos os meses ainda "
                          "vazios (meta plana); os já preenchidos não são tocados."):
            _novas, _n = _m.replicar_nos_vazios(_linhas_base)
            if _n == 0:
                st.warning("Preencha ao menos um mês antes de replicar.")
            else:
                _aplicar_atalho(_novas)

    with col_a4:
        st.write("")
        if st.button("🧹 Descartar proposta", key="btn_metas_limpar",
                     type="tertiary", disabled=_preview is None):
            st.session_state.get("_metas_preview", {}).pop(_chave_preview, None)
            st.session_state["_metas_versao"] = _versao + 1
            st.rerun()

    if _preview is not None:
        st.warning("⚠️ Proposta **não salva** na tabela. Clique em *Salvar metas* para gravá-la.")

    # st.form: dentro dele o data_editor NÃO dispara rerun a cada célula —
    # a página só reprocessa no submit. Sem isso, cada tecla re-executava o
    # script INTEIRO (matriz de colégios, crescimento observado, leituras das
    # integrações), que era a lentidão relatada ao digitar as metas.
    with st.form("form_metas_mensais", border=False):
        df_metas_edit = st.data_editor(
            df_metas_base,
            column_config={
                "mes": st.column_config.TextColumn("Mês", disabled=True, width="small"),
                "fat_prata": st.column_config.NumberColumn("🥈 Prata (R$)", min_value=0.0, step=1000.0, format="R$ %.0f"),
                "fat_ouro": st.column_config.NumberColumn("🥇 Ouro (R$)", min_value=0.0, step=1000.0, format="R$ %.0f"),
                "fat_diamante": st.column_config.NumberColumn("💎 Diamante (R$)", min_value=0.0, step=1000.0, format="R$ %.0f"),
                "pa_prata": st.column_config.NumberColumn("🥈 Prata (PA)", min_value=0.0, step=0.1, format="%.2f"),
                "pa_ouro": st.column_config.NumberColumn("🥇 Ouro (PA)", min_value=0.0, step=0.1, format="%.2f"),
                "pa_diamante": st.column_config.NumberColumn("💎 Diamante (PA)", min_value=0.0, step=0.1, format="%.2f"),
                "ref_fat": st.column_config.NumberColumn(
                    f"Realizado {ano_meta - 1} (R$)", disabled=True, format="R$ %.0f",
                    help="Faturamento do mesmo mês no ano anterior — a referência para calibrar a meta."),
                "ref_pa": st.column_config.NumberColumn(
                    f"PA {ano_meta - 1}", disabled=True, format="%.2f",
                    help="PA do mesmo mês no ano anterior."),
            },
            hide_index=True, width="stretch",
            key=f"editor_metas_{ano_meta}_{loja_meta}_{_versao}",
        )

        _salvar_metas = st.form_submit_button("💾 Salvar metas", type="primary")

    if _salvar_metas:
        from etl import metas as _m
        _nome_para_mes = {v: k for k, v in MESES_NOME_CFG.items()}
        _linhas = [
            {**row, "mes": _nome_para_mes[row["mes"]]}
            for row in df_metas_edit.to_dict("records")
        ]
        novo, n_meses = _m.aplicar_edicao_metas(
            config["daily"].get("metas_mensais"), ano_meta, loja_meta, _linhas)

        erros_metas = _m.validar_metas_mensais(novo)
        if erros_metas:
            st.error("❌ Corrija antes de salvar:")
            for e in erros_metas:
                st.write(f"- {e}")
        else:
            config["daily"]["metas_mensais"] = novo
            if not salvar_parametros(config):
                st.stop()
            st.session_state.get("_metas_preview", {}).pop(_chave_preview, None)
            # Bump da versão = key nova no editor. Sem isso ele guardaria o
            # delta interno da edição já gravada e poderia sobrepor a base
            # recém-salva na próxima renderização.
            st.session_state["_metas_versao"] = _versao + 1
            # Só o cache de CONFIG — não o de dados (TTL 1 h). O clear()
            # global levava junto a leitura do Supabase, e cada "Salvar"
            # custava uma carga fria (~10 s) na tela seguinte.
            carregar_config.clear()
            st.success(f"✅ Metas de **{loja_meta}** salvas — {n_meses} mês(es) configurado(s) em {ano_meta}.")

    # -----------------------------------------------------------------
    # Atribuição Vendedor → Loja (vigência mensal; base do rateio da meta)
    # -----------------------------------------------------------------
    st.markdown("#### 👥 Vendedores por Loja")
    st.markdown(
        "Define **quem responde por qual loja** — é o que rateia a meta da loja "
        "entre os vendedores. O `peso` divide a meta proporcionalmente (1,0 = "
        "cota cheia; 0,5 = meio período). A atribuição **vale a partir da "
        "competência escolhida e herda para os meses seguintes** até você "
        "editar outro mês — só mexa quando alguém entrar, sair ou trocar de loja."
    )

    col_v1, col_v2 = st.columns([1, 1])
    with col_v1:
        ano_vend = st.number_input("Vigência — ano", value=_ano_atual, min_value=2020, max_value=2100,
                                   step=1, key="vend_ano")
    with col_v2:
        mes_vend = st.selectbox("Vigência — mês", list(MESES_NOME_CFG.keys()),
                                format_func=lambda m: MESES_NOME_CFG[m],
                                index=date.today().month - 1, key="vend_mes")

    from etl import metas as _m
    comp_vend = _m.chave_competencia(int(ano_vend), int(mes_vend))
    _atrib_vigente = _m.atribuicao_vendedores(config, comp_vend)
    _tem_edicao_propria = comp_vend in (config["daily"].get("vendedores_loja") or {})
    if _atrib_vigente and not _tem_edicao_propria:
        st.caption(f"ℹ️ {comp_vend} ainda não tem edição própria — exibindo a atribuição **herdada** do mês anterior. Salvar cria a vigência deste mês.")

    try:
        _dados_vend = carregar_dados()["vendedores"].copy()
    except Exception as _e:
        _dados_vend = pd.DataFrame(columns=["ID", "nome", "situacao", "id_loja_bling"])
        st.caption(f"⚠️ Lista de vendedores indisponível ({_e}).")

    from etl.loader import limpar_id
    if "situacao" in _dados_vend.columns:
        _dados_vend = _dados_vend[_dados_vend["situacao"].astype(str).str.strip().str.upper() == "A"]
    _mapa_loja_id = {str(l["loja_id"]).strip(): l["nome"] for l in config["depositos"]["lojas"]}
    SEM_ATRIB = "— sem atribuição —"

    linhas_vend = []
    for _, v in _dados_vend.iterrows():
        vid = str(v["ID"])
        salvo = _atrib_vigente.get(vid) or {}
        # Sem atribuição salva, propõe a loja que o próprio Bling registra
        loja_bling = _mapa_loja_id.get(limpar_id(v.get("id_loja_bling")), "")
        linhas_vend.append({
            "vendedor_id": vid,
            "vendedor": str(v.get("nome", vid)),
            "loja": salvo.get("loja") or loja_bling or SEM_ATRIB,
            "peso": float(salvo.get("peso", 1.0)),
            "ativo": bool(salvo.get("ativo", True)),
        })
    df_vend = pd.DataFrame(linhas_vend)

    if len(df_vend) == 0:
        st.info("Nenhum vendedor ativo encontrado no Bling (`situacao = 'A'`).")
    else:
        _sem = int((df_vend["loja"] == SEM_ATRIB).sum())
        if _sem:
            st.caption(f"⚠️ {_sem} vendedor(es) sem loja no cadastro do Bling — atribua manualmente ou a meta deles não é rateada.")

        # Mesmo motivo do form das metas: sem ele, cada célula editada
        # re-executava a página inteira.
        with st.form("form_vendedores_loja", border=False):
            df_vend_edit = st.data_editor(
                df_vend,
                column_config={
                    "vendedor_id": st.column_config.TextColumn("ID", disabled=True, width="small"),
                    "vendedor": st.column_config.TextColumn("Vendedor", disabled=True),
                    "loja": st.column_config.SelectboxColumn("Loja", options=_lojas_cfg + [SEM_ATRIB], required=True),
                    "peso": st.column_config.NumberColumn(
                        "Peso", min_value=0.0, max_value=5.0, step=0.1, format="%.1f",
                        help="Fração da meta da loja. 1,0 = cota cheia · 0,5 = meio período."),
                    "ativo": st.column_config.CheckboxColumn(
                        "Ativo", help="Desmarcado = não entra no rateio nem no acompanhamento do mês."),
                },
                hide_index=True, width="stretch", key=f"editor_vendedores_{comp_vend}",
            )

            # Rateio da atribuição SALVA/herdada: dentro do form o editor não
            # devolve a edição viva antes do submit, então isto descreve o que
            # vale hoje e se atualiza ao salvar.
            _metas_loja_prev = {l: _m.metas_da_loja(config, l, comp_vend)["faturamento"] for l in _lojas_cfg}
            _resumo_rateio = []
            for loja_n in _lojas_cfg:
                sel = df_vend[(df_vend["loja"] == loja_n) & (df_vend["ativo"])]
                peso_total = float(sel["peso"].sum())
                meta_ouro = (_metas_loja_prev.get(loja_n) or {}).get("ouro")
                if meta_ouro and peso_total > 0:
                    _resumo_rateio.append(
                        f"**{loja_n}**: meta Ouro {_brl_cfg(meta_ouro)} ÷ {len(sel)} vendedor(es) "
                        f"(peso {peso_total:.1f}) → {_brl_cfg(meta_ouro / peso_total)} por peso 1,0"
                    )
                elif meta_ouro:
                    _resumo_rateio.append(
                        f"**{loja_n}**: meta Ouro {_brl_cfg(meta_ouro)}, mas **nenhum vendedor ativo atribuído**")
            if _resumo_rateio:
                st.caption("Rateio atual em " + comp_vend + " (atualiza ao salvar) — " + " · ".join(_resumo_rateio))

            _salvar_vend = st.form_submit_button("💾 Salvar atribuição", type="primary")

        if _salvar_vend:
            novo_atrib = _m.aplicar_edicao_vendedores(
                config["daily"].get("vendedores_loja"), comp_vend,
                df_vend_edit.to_dict("records"), SEM_ATRIB)
            n_atrib = len(novo_atrib.get(comp_vend) or {})
            config["daily"]["vendedores_loja"] = novo_atrib
            if not salvar_parametros(config):
                st.stop()
            # Só o cache de CONFIG — não o de dados (TTL 1 h). O clear()
            # global levava junto a leitura do Supabase, e cada "Salvar"
            # custava uma carga fria (~10 s) na tela seguinte.
            carregar_config.clear()
            st.success(f"✅ Atribuição de {n_atrib} vendedor(es) salva, vigente a partir de {comp_vend}.")

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

    from etl.demanda import colegio_efetivo, parece_ruido, restringir_a_ativos

    config = carregar_config()
    alias_atual = config.get("colegios_alias") or {}
    # Só produtos ATIVOS: colégios descontinuados (ex: OVD) não devem aparecer
    # nos editores de Colégios / Grupo→Segmento.
    dados_colegios = restringir_a_ativos(carregar_dados())

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

    # st.form: o data_editor só reprocessa a página no submit,
    # não a cada célula editada.
    with st.form("form_colegios_alias", border=False):
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

        _salvar_alias = st.form_submit_button("💾 Salvar Normalização de Colégios", key="btn_salvar_alias", type="primary")

    if _salvar_alias:
        novo_alias = {}
        for _, row in df_alias_edit.iterrows():
            raw = str(row["marca_sku"]).strip()
            disp = str(row["colegio"]).strip()
            if raw and disp and disp != raw:      # só grava o que MUDA (identidade = default)
                novo_alias[raw] = disp
        config["colegios_alias"] = novo_alias
        if not salvar_parametros(config):
            st.stop()
        # Só o cache de CONFIG — não o de dados (TTL 1 h). O clear()
        # global levava junto a leitura do Supabase, e cada "Salvar"
        # custava uma carga fria (~10 s) na tela seguinte.
        carregar_config.clear()
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

    # st.form: o data_editor só reprocessa a página no submit,
    # não a cada célula editada.
    with st.form("form_colegios_param", border=False):
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

        _salvar_colegios = st.form_submit_button("💾 Salvar Colégios (taxa base)", key="btn_salvar_colegios", type="primary")

    if _salvar_colegios:
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
        # Só o cache de CONFIG — não o de dados (TTL 1 h). O clear()
        # global levava junto a leitura do Supabase, e cada "Salvar"
        # custava uma carga fria (~10 s) na tela seguinte.
        carregar_config.clear()
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

    # st.form: o data_editor só reprocessa a página no submit,
    # não a cada célula editada.
    with st.form("form_matriz_grupo", border=False):
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

        _salvar_matriz = st.form_submit_button("💾 Salvar Crescimento por Grupo", key="btn_salvar_matriz", type="primary")

    if _salvar_matriz:
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
        # Só o cache de CONFIG — não o de dados (TTL 1 h). O clear()
        # global levava junto a leitura do Supabase, e cada "Salvar"
        # custava uma carga fria (~10 s) na tela seguinte.
        carregar_config.clear()
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
    # st.form: o data_editor só reprocessa a página no submit,
    # não a cada célula editada.
    with st.form("form_grupo_segmento", border=False):
        df_seg_edit = st.data_editor(
            df_seg,
            column_config={
                "grupo": st.column_config.TextColumn("Grupo", disabled=True),
                "skus": st.column_config.NumberColumn("SKUs", disabled=True),
                "segmento": st.column_config.TextColumn("Segmento", help="Nome do balde — pode reutilizar ou criar novos"),
            },
            hide_index=True, width="stretch", height=500, key="editor_grupo_seg",
        )

        _salvar_seg = st.form_submit_button("💾 Salvar Agrupamento de Segmentos", key="btn_salvar_seg", type="primary")

    if _salvar_seg:
        novo_seg = dict(config.get("grupo_segmento") or {})
        for _, row in df_seg_edit.iterrows():
            g = str(row["grupo"]).strip()
            s = str(row["segmento"]).strip()
            if g and s:
                novo_seg[g] = s
        config["grupo_segmento"] = novo_seg
        if not salvar_parametros(config):
            st.stop()
        # Só o cache de CONFIG — não o de dados (TTL 1 h). O clear()
        # global levava junto a leitura do Supabase, e cada "Salvar"
        # custava uma carga fria (~10 s) na tela seguinte.
        carregar_config.clear()
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
                        # Só o cache de CONFIG — não o de dados (TTL 1 h). O clear()
                        # global levava junto a leitura do Supabase, e cada "Salvar"
                        # custava uma carga fria (~10 s) na tela seguinte.
                        carregar_config.clear()
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

    @st.cache_data(ttl=3600, show_spinner=False)
    def _formas_pagamento_bling() -> list:
        """Formas de pagamento da conta (id é por conta — não dá p/ hardcodar)."""
        token = oauth.obter_access_token("bling", obter_repositorio_integracoes())
        return cliente_bling.listar_formas_pagamento(token)

    @st.cache_data(ttl=3600, show_spinner=False)
    def _formas_recebimento_olist() -> list:
        """Formas de recebimento da conta Olist (id por conta) p/ o selectbox."""
        token = oauth.obter_access_token("olist", obter_repositorio_integracoes())
        return cliente_olist.listar_formas_recebimento(token)

    def _extras_bling(cfg: dict, conectado: bool) -> dict:
        """
        Pagamento do pedido de compra: mora aqui (e não na rodada) porque é
        característica fixa do acordo com a Art Kamizetas — não varia por rodada.
        Selectbox quando conectado (nomes em vez de IDs); text_input como
        degradação se a conta não estiver conectada ou o GET falhar.
        """
        st.markdown("**Pagamento** (usado nas parcelas do pedido de compra)")
        salvo = str(cfg.get("forma_pagamento_id") or "")
        forma_id = salvo

        formas, erro = [], None
        if conectado:
            try:
                formas = _formas_pagamento_bling()
            except Exception as exc:
                erro = str(exc)

        if formas:
            ids = [f["id"] for f in formas]
            rotulos = {f["id"]: f["descricao"] for f in formas}
            if salvo and salvo not in ids:      # forma removida/renomeada no Bling
                ids.insert(0, salvo)
                rotulos[salvo] = f"(id {salvo} — não está mais na lista)"
            forma_id = st.selectbox(
                "Forma de pagamento", options=ids,
                index=ids.index(salvo) if salvo in ids else 0,
                format_func=lambda i: rotulos.get(i, i),
                help="Cadastros → Formas de pagamento no Bling.",
                key="neg_bling_forma_sel")
        else:
            if erro:
                st.caption(f"⚠️ Não foi possível listar as formas de pagamento: {erro}")
            forma_id = st.text_input(
                "ID da forma de pagamento", value=salvo,
                help="Conecte a integração para escolher pelo nome.",
                key="neg_bling_forma_txt")

        prazo = st.number_input(
            "Prazo de pagamento (dias da emissão)", min_value=0, max_value=365,
            value=int(cfg.get("prazo_pagamento_dias") or 30), step=1,
            help="Vencimento da parcela = data de emissão + este prazo.",
            key="neg_bling_prazo")

        unidade = st.text_input(
            "Unidade de medida dos itens", value=str(cfg.get("unidade_padrao") or "PÇ"),
            help="O espelho do Supabase não traz a unidade do cadastro — "
                 "este valor vai em todos os itens do pedido.",
            key="neg_bling_unidade")

        return {"forma_pagamento_id": str(forma_id or "").strip(),
                "prazo_pagamento_dias": int(prazo),
                "unidade_padrao": unidade.strip()}

    def _extras_olist(cfg: dict, conectado: bool) -> dict:
        """
        Recebimento do pedido de venda. Sem campo de prazo de propósito: a
        compra e a venda são o mesmo acordo, então o prazo é o do card do Bling
        — duplicar o campo só criaria divergência.

        Forma de recebimento vira selectbox pelo GET /formas-recebimento (o id é
        por conta e o Olist não o mostra de forma óbvia — caçar o número à mão foi
        o que emitiu a venda com id inexistente). Degrada para text_input quando
        desconectado ou o GET falha. Meio de pagamento (opcional) segue como texto:
        a API v3 não expõe GET e a numeração de id é própria do Olist.
        """
        st.markdown("**Recebimento** (usado nas parcelas do pedido de venda)")
        salvo = str(cfg.get("forma_recebimento_id") or "")
        forma_id = salvo

        formas, erro = [], None
        if conectado:
            try:
                formas = _formas_recebimento_olist()
            except Exception as exc:
                erro = str(exc)

        if formas:
            # Só ativas no selectbox; inativas confundem (o Olist recusa emitir
            # com forma inativa). Preserva o salvo mesmo inativo/removido.
            ativas = [f for f in formas if f["ativa"]]
            ids = [f["id"] for f in ativas]
            rotulos = {f["id"]: f["nome"] for f in ativas}
            opcoes = [""] + ids                       # "" = sem bloco de pagamento
            rotulos[""] = "(nenhuma — emitir sem pagamento)"
            if salvo and salvo not in opcoes:         # forma inativa/removida no Olist
                opcoes.insert(1, salvo)
                nome_salvo = next((f["nome"] for f in formas if f["id"] == salvo), None)
                rotulos[salvo] = (f"{nome_salvo} (inativa)" if nome_salvo
                                  else f"(id {salvo} — não está mais na lista)")
            forma_id = st.selectbox(
                "Forma de recebimento", options=opcoes,
                index=opcoes.index(salvo) if salvo in opcoes else 0,
                format_func=lambda i: rotulos.get(i, i),
                help="Cadastros → Formas de recebimento no Olist.",
                key="neg_olist_forma_sel")
        else:
            if erro:
                st.caption(f"⚠️ Não foi possível listar as formas de recebimento: {erro}")
            forma_id = st.text_input(
                "ID da forma de recebimento", value=salvo,
                help="Conecte a integração para escolher pelo nome. Vazio = "
                     "pedido emitido sem bloco de pagamento.",
                key="neg_olist_forma_txt")

        meio = st.text_input(
            "ID do meio de pagamento (opcional)",
            value=str(cfg.get("meio_pagamento_id") or ""),
            help="A API v3 não lista os meios — deixe vazio se o Olist não "
                 "exigir na sua conta.",
            key="neg_olist_meio")
        st.caption("O prazo da parcela é o mesmo do pedido de compra "
                   "(card do Bling) — não se configura em dois lugares.")

        return {"forma_recebimento_id": str(forma_id or "").strip(),
                "meio_pagamento_id": meio.strip()}

    def _card_integracao(plataforma: str, titulo: str, campos_negocio: list,
                         extras_form=None):
        """
        Card de configuração + conexão OAuth de uma plataforma.
        `extras_form(cfg, conectado) -> dict` desenha campos extras DENTRO do
        form de IDs de negócio e devolve o que gravar junto (salvar_config
        substitui o jsonb inteiro — tudo precisa sair no mesmo submit).
        """
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
                                           usuario)
                    st.success("Credenciais salvas.")
                    st.rerun()

            if integ.get("redirect_uri"):
                st.caption("Redirect a registrar no portal:")
                st.code(integ["redirect_uri"], language=None)

            # -- 2. Conexão --
            # Ter refresh_token != estar utilizável: o refresh do Olist (Keycloak)
            # morre com a sessão SSO e só descobrimos na hora de renovar. Access
            # vencido há muito tempo = aviso, não o "✅ Conectado" que mentia.
            st.markdown("**Conexão**")
            if conectado:
                validade, exp = "?", None
                if integ.get("token_expira_em"):
                    exp = pd.Timestamp(str(integ["token_expira_em"]))
                    if exp.tzinfo is None:
                        exp = exp.tz_localize("UTC")
                    validade = exp.tz_convert(None).strftime("%d/%m/%Y %H:%M")
                quem = integ.get("conectado_por", "?")
                if exp is not None and not oauth.token_valido(integ):
                    st.warning(
                        f"⚠️ Autorizado por {quem}, mas o token venceu em "
                        f"{validade} (UTC). A renovação é automática — se a "
                        "sessão na plataforma tiver expirado, ela falha e é "
                        "preciso reconectar. Use **Testar conexão** antes de emitir.")
                else:
                    st.success(f"✅ Conectado por {quem} · token expira {validade} (UTC)")
            else:
                st.info("❌ Não conectado.")

            cc1, cc2 = st.columns(2)
            with cc1:
                pronto_p_conectar = bool(integ.get("client_id") and integ.get("redirect_uri"))
                if pronto_p_conectar:
                    state = oauth.gerar_state()
                    repo_int.salvar_state_oauth(plataforma, state, usuario)
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
                                                  detalhe={"msg": msg}, usuario=usuario)
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
                extras = extras_form(cfg, conectado) if extras_form else {}
                if st.form_submit_button("💾 Salvar IDs de negócio"):
                    novo = {k: v.strip() for k, v in valores.items() if v.strip()}
                    # extras já vêm tipados (int/str) — só descarta string vazia
                    novo.update({k: v for k, v in extras.items()
                                 if not (isinstance(v, str) and not v)})
                    if plataforma == "olist" and "situacao" not in novo:
                        novo["situacao"] = 0
                    repo_int.salvar_config(plataforma, novo, usuario)
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
                                                  usuario=usuario)
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
            extras_form=_extras_bling,
        )
        _card_integracao(
            "olist", "🏭 Olist — Pedido de Venda (Art Kamizetas)",
            [("contato_id", "ID do contato/cliente (AK Uniformes)", "Contato no Olist"),
             ("vendedor_id", "ID do vendedor", "Obrigatório na API do Olist"),
             ("deposito_id", "ID do depósito", "Obrigatório na API do Olist"),
             ("situacao", "Situação inicial (0 = Aberta)", "Código de situação do pedido")],
            extras_form=_extras_olist,
        )

        with st.expander("📜 Últimos eventos de integração"):
            _eventos = obter_repositorio_integracoes().listar_eventos(20)
            if len(_eventos):
                def _resumo_detalhe(det):
                    # jsonb → resumo legível: erro (falhas) tem prioridade, depois
                    # msg (sucessos), senão o JSON compacto. Vazio quando não há.
                    if not isinstance(det, dict):
                        return ""
                    return (det.get("erro") or det.get("msg")
                            or ", ".join(f"{k}={v}" for k, v in det.items()))
                _eventos = _eventos.copy()
                _eventos["detalhe"] = _eventos.get("detalhe").map(_resumo_detalhe) \
                    if "detalhe" in _eventos.columns else ""
                _cols = [c for c in ["criado_em", "plataforma", "acao", "sucesso",
                                     "detalhe", "criado_por"]
                         if c in _eventos.columns]
                st.dataframe(
                    _eventos[_cols], width="stretch", hide_index=True,
                    column_config={"detalhe": st.column_config.TextColumn(
                        "Detalhe / erro", width="large",
                        help="Motivo da falha ou resumo do evento (campo detalhe do log)")},
                )
            else:
                st.caption("Nenhum evento ainda.")

# =================================================================
# ABA 3 — INFORMAÇÕES DO SISTEMA
# =================================================================

# =================================================================
# ABA USUÁRIOS — allowlist de acesso (app.usuario, DDL 006)
# O login é Google; esta aba decide QUEM entra e O QUE vê. Não há
# auto-cadastro: o formulário de convite é a única porta de entrada.
# =================================================================

with tab_usr:
    st.subheader("Usuários com acesso")
    st.caption("O login é feito com conta Google. Só os e-mails desta lista conseguem entrar.")

    _repo_usr = obter_repositorio_usuarios()
    try:
        _usuarios = _repo_usr.listar()
        _erro_usr = ""
    except Exception as e:
        _usuarios, _erro_usr = [], str(e)

    if _erro_usr:
        st.error(f"Não foi possível ler a lista de usuários: {_erro_usr}")
        st.info("Se o DDL 006 ainda não foi aplicado, rode `python scripts/migrar.py aplicar`.")

    def _paginas_legivel(role_):
        p_ = paginas_do_role(role_)
        return "Todas as páginas" if p_ is None else (", ".join(p_) or "Nenhuma")

    # --- Adicionar (única porta de entrada) ---
    with st.form("form_novo_usuario"):
        st.markdown("#### ➕ Adicionar usuário")
        c1, c2, c3 = st.columns([3, 2, 2])
        _novo_email = c1.text_input("E-mail da conta Google", placeholder="nome@empresa.com")
        _novo_nome = c2.text_input("Nome", placeholder="Como aparece na sidebar")
        _novo_role = c3.selectbox("Perfil", options=list(ROLES_VALIDOS),
                                  index=list(ROLES_VALIDOS).index("vendedor"))
        st.caption(f"Perfil **{_novo_role}** vê: {_paginas_legivel(_novo_role)}")
        _add = st.form_submit_button("Adicionar", type="primary")

    if _add:
        try:
            _repo_usr.criar(_novo_email, _novo_nome, _novo_role, usuario=usuario)
            invalidar_cache_usuarios()
            st.success(f"✅ {normalizar_email(_novo_email)} liberado como {_novo_role}. "
                       "Peça para entrar com essa mesma conta Google.")
            st.rerun()
        except (EmailInvalido, UsuarioJaExiste) as e:
            st.error(str(e))

    st.divider()

    # --- Grade de edição ---
    if _usuarios:
        _df_usr = pd.DataFrame([{
            "email": u.get("email", ""),
            "nome": u.get("nome", "") or "",
            "role": u.get("role", ""),
            "ativo": bool(u.get("ativo")),
            "ve": _paginas_legivel(u.get("role")),
            "ultimo_acesso": pd.to_datetime(u.get("ultimo_acesso"), errors="coerce"),
        } for u in _usuarios])

        # Mesmo motivo do form dos vendedores: sem ele, cada célula editada
        # re-executava a página inteira.
        with st.form("form_usuarios", border=False):
            _df_usr_edit = st.data_editor(
                _df_usr,
                column_config={
                    # E-mail é a PK — trocar seria remover e cadastrar de novo.
                    "email": st.column_config.TextColumn("E-mail (Google)", disabled=True),
                    "nome": st.column_config.TextColumn("Nome", max_chars=120),
                    "role": st.column_config.SelectboxColumn(
                        "Perfil", options=list(ROLES_VALIDOS), required=True),
                    "ativo": st.column_config.CheckboxColumn(
                        "Ativo", help="Desmarcado = não consegue entrar, mas o perfil fica salvo."),
                    "ve": st.column_config.TextColumn(
                        "Vê hoje", disabled=True,
                        help="Páginas do perfil SALVO. Atualiza ao salvar."),
                    "ultimo_acesso": st.column_config.DatetimeColumn(
                        "Último acesso", disabled=True, format="DD/MM/YYYY HH:mm"),
                },
                # Usuário novo nasce no formulário acima, nunca aqui: um e-mail
                # digitado errado no grid viraria linha morta que nunca loga.
                num_rows="fixed", hide_index=True, width="stretch", key="editor_usuarios",
            )
            _salvar_usr = st.form_submit_button("💾 Salvar alterações", type="primary")

        if _salvar_usr:
            _novas = _df_usr_edit.to_dict("records")
            _erros = validar_edicao_usuarios(_novas, _df_usr.to_dict("records"), usuario)
            if _erros:
                for _e in _erros:
                    st.error(_e)
            else:
                _antes = {u["email"]: u for u in _df_usr.to_dict("records")}
                _diff = [l for l in _novas
                         if (l["nome"], l["role"], l["ativo"]) !=
                            (_antes[l["email"]]["nome"], _antes[l["email"]]["role"],
                             _antes[l["email"]]["ativo"])]
                if not _diff:
                    st.info("Nada mudou.")
                else:
                    _repo_usr.salvar_lote(_diff, usuario=usuario)
                    invalidar_cache_usuarios()
                    st.success(f"✅ {len(_diff)} usuário(s) atualizado(s).")
                    st.rerun()

        st.divider()

        # --- Remoção ---
        st.markdown("#### 🗑️ Remover acesso")
        st.caption("Remover apaga o cadastro. Para bloquear temporariamente, "
                   "prefira desmarcar **Ativo** acima.")
        _rm_col, _rm_btn = st.columns([3, 1])
        _rm_email = _rm_col.selectbox(
            "Usuário", options=[u["email"] for u in _usuarios], key="rm_usuario")
        if _rm_btn.button("Remover", key="btn_rm_usuario"):
            _restantes = [l for l in _df_usr.to_dict("records") if l["email"] != _rm_email]
            _erros = validar_edicao_usuarios(_restantes, _df_usr.to_dict("records"), usuario)
            if _erros:
                for _e in _erros:
                    st.error(_e)
            else:
                _repo_usr.remover(_rm_email)
                invalidar_cache_usuarios()
                st.success(f"✅ {_rm_email} removido.")
                st.rerun()
    elif not _erro_usr:
        st.info("Nenhum usuário cadastrado ainda.")


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
            # Clear GLOBAL de propósito: é a intenção explícita do botão
            # (relê o espelho do Bling, não só os parâmetros).
            st.cache_data.clear()
            st.success("✅ Cache limpo. A próxima tela relê o Supabase (~10 s).")

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
