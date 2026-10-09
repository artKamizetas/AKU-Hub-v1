"""
Página: Configurações (Admin Only)

Organizada pela DECISÃO que o gestor quer tomar, não pelo formato do widget:

- Comercial ............... metas mensais e vendedores por loja
- Reposição de Loja ....... estoque-alvo, espaço (gavetas) e sortimento
- Produção ................ margem de segurança e calendário do motor
- Colégios e Crescimento .. a cascata do crescimento, do geral ao específico
- Integrações ............. Bling (compra) e Olist (venda)
- Usuários ................ allowlist de acesso
- Sistema ................. versões, mapeamento do Bling, cache e backup

Só a seção ATIVA é executada (segmented_control + if/elif, como na 4_Pedidos).
Com st.tabs todas as abas rodavam a cada rerun — crescimento observado,
leituras das integrações e gravação de `state` OAuth incluídos.
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
from ui_carga import carregar_com_feedback
from ui_tabelas import (
    EDITOR, MEMORIA, exibir, padrao_tabela, brl, num,
    col_texto, col_moeda, col_colegio, col_pecas,
)
from etl import config_edicao
from etl.reposicao import parametros as parametros_reposicao
from etl.config_store import extrair_parametros, obter_repositorio_parametros
from pedidos.integracoes.repositorio import obter_repositorio_integracoes
from pedidos.integracoes import oauth, bling as cliente_bling, olist as cliente_olist
from auth_store import (
    ROLES_VALIDOS, EmailInvalido, UsuarioJaExiste,
    normalizar_email, obter_repositorio_usuarios,
)

# Seções da página: slug → rótulo. O slug vai em ?secao=, o que deixa um link
# de outra tela (ou um favorito) cair direto na seção certa.
SECOES = {
    "comercial": ":material/target: Comercial",
    "reposicao": ":material/local_shipping: Reposição de Loja",
    "producao": ":material/factory: Produção",
    "colegios": ":material/school: Colégios e Crescimento",
    "integracoes": ":material/cable: Integrações",
    "usuarios": ":material/group: Usuários",
    "sistema": ":material/info: Sistema",
}
CHAVE_SECAO = "cfg_secao"

# =================================================================
# CALLBACK OAUTH (integrações) — processa o retorno capturado no topo
# O state foi persistido no banco (a sessão do Streamlit morre no redirect),
# então buscamos por ele para saber de qual plataforma é o retorno.
# =================================================================
_ret = st.session_state.pop("_oauth_retorno", None)
if _ret:
    # O resultado da conexão interessa à seção de Integrações: abre nela.
    st.session_state[CHAVE_SECAO] = "integracoes"
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
            st.success(f"{_plat.capitalize()} conectado com sucesso!", icon=":material/check_circle:")
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
    return brl(v)

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
        st.error(f"Falha ao salvar parâmetros no Supabase: {e}", icon=":material/error:")
        return False


def validar_config(config):
    """Valida estrutura mínima do config antes de salvar."""
    erros = []

    # Verificar seções obrigatórias
    obrigatorias = ["fonte", "depositos", "logistica", "daily", "fabrica", "planejamento", "reposicao", "demanda"]
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
        ("logistica.dias_analise_giro", ["logistica", "dias_analise_giro"]),
        ("reposicao.exposicao_minima", ["reposicao", "exposicao_minima"]),
        ("reposicao.cobertura_dias_alta", ["reposicao", "cobertura_dias_alta"]),
        ("reposicao.cobertura_dias_baixa", ["reposicao", "cobertura_dias_baixa"]),
        ("reposicao.recolher_horizonte_dias", ["reposicao", "recolher_horizonte_dias"]),
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


# Uma lista só de níveis de serviço para a página inteira (eram duas, e o 92%
# usado na baixa não existia nos seletores da Reposição nem dos colégios).
NIVEIS_SERVICO = [90, 92, 95, 97, 98, 99]


def _indice_ns(valor, padrao: int) -> int:
    """Posição de `valor` em NIVEIS_SERVICO; cai no `padrao` se estiver fora da lista."""
    nivel = round(float(valor)) if valor is not None else padrao
    return NIVEIS_SERVICO.index(nivel if nivel in NIVEIS_SERVICO else padrao)


def _indice_mes(valor, padrao: int) -> int:
    """Posição (0-11) do mês `valor` (1-12) no seletor; cai no `padrao` se inválido."""
    try:
        mes = int(valor)
    except (TypeError, ValueError):
        mes = padrao
    return (mes if 1 <= mes <= 12 else padrao) - 1


def _seletor(widget, rotulo: str, opcoes: list, chave: str, inicial=None, **kwargs):
    """
    `st.segmented_control`/`st.pills` de navegação: sempre com UMA opção marcada.

    Os dois permitem DESMARCAR clicando na opção ativa (devolvem None), o que
    deixaria a barra sem destaque e a tela sem dono. O callback devolve a
    última escolha antes do rerun. A última escolha mora numa chave que NÃO é
    de widget — o Streamlit apaga o estado do widget que deixa de ser
    desenhado, e é ela que faz a sub-aba ser lembrada ao voltar à seção.
    """
    ultima = f"_{chave}_ultima"
    if chave not in st.session_state:
        candidata = st.session_state.get(ultima, inicial)
        st.session_state[chave] = candidata if candidata in opcoes else opcoes[0]

    def _segurar():
        if st.session_state.get(chave) is None:
            st.session_state[chave] = st.session_state.get(ultima, opcoes[0])

    valor = widget(rotulo, opcoes, key=chave, on_change=_segurar,
                   label_visibility="collapsed", **kwargs)
    if valor is None:
        valor = st.session_state.get(ultima, opcoes[0])
    st.session_state[ultima] = valor
    return valor


def _salvar_secao(config, mensagem: str) -> None:
    """Valida, grava e avisa — o fecho comum dos formulários de parâmetros."""
    erros = validar_config(config)
    if erros:
        st.error("Corrija antes de salvar:", icon=":material/error:")
        for erro in erros:
            st.write(f"- {erro}")
        return
    if not salvar_parametros(config):
        st.stop()
    # Só o cache de CONFIG — não o de dados (TTL 1 h). O clear() global levava
    # junto a leitura do Supabase, e cada "Salvar" custava uma carga fria.
    carregar_config.clear()
    st.success(mensagem, icon=":material/check_circle:")


@st.cache_data(ttl=600, show_spinner="Levantando o realizado dos últimos meses…")
def _realizado_mensal():
    """Faturamento/peças/pedidos realizados por (ano, mês, loja) — a âncora
    que o gestor usa para decidir a meta. Vem do MESMO pipeline do Daily
    (processar_daily), então bate com o que a tela de acompanhamento mostra."""
    from etl.daily import processar_daily
    cfg = carregar_config()
    det, _, _ = processar_daily(carregar_dados(), cfg)
    sits = cfg["daily"]["situacoes_venda"]
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


# =================================================================
# SEÇÃO — COMERCIAL (metas mensais + vendedores por loja)
# =================================================================

def _bloco_metas():
    """Metas escalonadas (Prata/Ouro/Diamante) por loja × mês.
    Spec: docs/requisitos/metas-escalonadas.md"""
    st.markdown(
        "Três níveis por mês e por loja — **Prata** (o piso aceitável), **Ouro** "
        "(a meta de verdade) e **Diamante** (a superação). Valem para "
        "**Faturamento** e **PA** (peças por atendimento). Célula em branco = mês "
        "sem meta: o Daily avisa em vez de inventar um número. A meta de cada "
        "**vendedor** é rateada automaticamente a partir da meta da loja "
        "(a atribuição fica na aba *Vendedores por loja*)."
    )

    config = carregar_config()
    _lojas_cfg = [l["nome"] for l in config["depositos"]["lojas"]]
    _ano_atual = date.today().year

    try:
        _realizado = _realizado_mensal()
    except Exception as _e:
        _realizado = pd.DataFrame(columns=["_ano", "_mes", "LojaConfig", "faturamento", "pecas", "pedidos", "pa"])
        st.caption(f":orange[:material/warning:] Histórico de realizado indisponível ({_e}) — as colunas de referência ficam vazias.")

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

    # Sem realizado do ano anterior a coluna de referência vem vazia — e vazio
    # silencioso engana quem está definindo a meta: avisa e desabilita o Propor.
    _ref_ano = ano_meta - 1
    _tem_ref = len(_realizado[(_realizado["_ano"] == _ref_ano)
                              & (_realizado["LojaConfig"] == loja_meta)]) > 0
    if not _tem_ref:
        st.caption(
            f":material/info: Sem vendas de **{_ref_ano}** para {loja_meta} no histórico — a coluna de "
            "referência e o botão *Propor* ficam sem base neste ano. Defina as metas à mão "
            "ou use *Copiar metas*."
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
            help="Aplicado pelos atalhos *Propor* e *Copiar*. Use 0 para cópia literal.",
            key="metas_pct_cresc",
        )
    with col_a2:
        st.write("")
        if st.button("Propor a partir do realizado", icon=":material/auto_awesome:",
                     key="btn_metas_propor",
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
        if st.button(f"Copiar metas de {ano_meta - 1}", icon=":material/content_copy:",
                     key="btn_metas_copiar",
                     disabled=not _n_ano_ant, help=_ajuda_copiar):
            _novas, _n = _m.copiar_do_ano_anterior(
                _linhas_base, _metas_salvas, ano_meta, loja_meta,
                fator=1 + (_pct_cresc / 100))
            _aplicar_atalho(_novas)

        if st.button("Replicar nos meses vazios", icon=":material/keyboard_double_arrow_down:",
                     key="btn_metas_replicar",
                     help="Copia a primeira linha preenchida para todos os meses ainda "
                          "vazios (meta plana); os já preenchidos não são tocados."):
            _novas, _n = _m.replicar_nos_vazios(_linhas_base)
            if _n == 0:
                st.warning("Preencha ao menos um mês antes de replicar.")
            else:
                _aplicar_atalho(_novas)

    with col_a4:
        st.write("")
        if st.button("Descartar proposta", icon=":material/undo:", key="btn_metas_limpar",
                     type="tertiary", disabled=_preview is None):
            st.session_state.get("_metas_preview", {}).pop(_chave_preview, None)
            st.session_state["_metas_versao"] = _versao + 1
            st.rerun()

    if _preview is not None:
        st.warning("Proposta **não salva** na tabela. Clique em *Salvar metas* para gravá-la.",
                   icon=":material/warning:")

    # st.form: dentro dele o data_editor NÃO dispara rerun a cada célula —
    # a página só reprocessa no submit. Sem isso, cada tecla re-executava o
    # script INTEIRO (matriz de colégios, crescimento observado, leituras das
    # integrações), que era a lentidão relatada ao digitar as metas.
    with st.form("form_metas_mensais", border=False):
        df_metas_edit = st.data_editor(
            df_metas_base,
            column_config={
                "mes": st.column_config.TextColumn("Mês", disabled=True, width="small"),
                "fat_prata": st.column_config.NumberColumn("🥈 Prata (R$)", min_value=0.0, step=1000.0, format="localized"),
                "fat_ouro": st.column_config.NumberColumn("🥇 Ouro (R$)", min_value=0.0, step=1000.0, format="localized"),
                "fat_diamante": st.column_config.NumberColumn("💎 Diamante (R$)", min_value=0.0, step=1000.0, format="localized"),
                "pa_prata": st.column_config.NumberColumn("🥈 Prata (PA)", min_value=0.0, step=0.1, format="%.2f"),
                "pa_ouro": st.column_config.NumberColumn("🥇 Ouro (PA)", min_value=0.0, step=0.1, format="%.2f"),
                "pa_diamante": st.column_config.NumberColumn("💎 Diamante (PA)", min_value=0.0, step=0.1, format="%.2f"),
                "ref_fat": col_moeda(
                    f"Realizado {ano_meta - 1}", disabled=True,
                    ajuda="Faturamento do mesmo mês no ano anterior — a referência para calibrar a meta."),
                "ref_pa": st.column_config.NumberColumn(
                    f"PA {ano_meta - 1}", disabled=True, format="%.2f",
                    help="PA do mesmo mês no ano anterior."),
            },
            **padrao_tabela(EDITOR, len(df_metas_base)),
            key=f"editor_metas_{ano_meta}_{loja_meta}_{_versao}",
        )

        _salvar_metas = st.form_submit_button("Salvar metas", icon=":material/save:", type="primary")

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
            st.error("Corrija antes de salvar:", icon=":material/error:")
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
            st.success(f"Metas de **{loja_meta}** salvas — {n_meses} mês(es) configurado(s) em {ano_meta}.", icon=":material/check_circle:")


def _bloco_vendedores():
    """Atribuição vendedor → loja (vigência mensal; base do rateio da meta)."""
    config = carregar_config()
    _lojas_cfg = [l["nome"] for l in config["depositos"]["lojas"]]
    _ano_atual = date.today().year

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
        st.caption(f":material/info: {comp_vend} ainda não tem edição própria — exibindo a atribuição **herdada** do mês anterior. Salvar cria a vigência deste mês.")

    try:
        _dados_vend = carregar_dados()["vendedores"].copy()
    except Exception as _e:
        _dados_vend = pd.DataFrame(columns=["ID", "nome", "situacao", "id_loja_bling"])
        st.caption(f":orange[:material/warning:] Lista de vendedores indisponível ({_e}).")

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
            st.caption(f":orange[:material/warning:] {_sem} vendedor(es) sem loja no cadastro do Bling — atribua manualmente ou a meta deles não é rateada.")

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
                **padrao_tabela(EDITOR, len(df_vend)), key=f"editor_vendedores_{comp_vend}",
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

            _salvar_vend = st.form_submit_button("Salvar atribuição", icon=":material/save:", type="primary")

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
            st.success(f"Atribuição de {n_atrib} vendedor(es) salva, vigente a partir de {comp_vend}.", icon=":material/check_circle:")


def _secao_comercial():
    _visoes = ["Metas da loja", "Vendedores por loja"]
    visao = _seletor(st.pills, "Comercial", _visoes, "cfg_comercial_visao")
    if visao == _visoes[1]:
        _bloco_vendedores()
    else:
        _bloco_metas()


# =================================================================
# SEÇÃO — REPOSIÇÃO DE LOJA (estoque-alvo, espaço, sortimento)
# =================================================================

def _fechar_reposicao(config, mensagem: str) -> None:
    """Salva um bloco da Reposição. Na primeira gravação o legado some: os
    parâmetros do antigo VM (`vm.*`, `logistica.vm_padrao`) já não têm leitor."""
    config.pop("vm", None)
    (config.get("logistica") or {}).pop("vm_padrao", None)
    _salvar_secao(config, mensagem)


def _bloco_alvo_da_loja(config, params):
    with st.form("form_reposicao_alvo", border=False):
        with st.container(border=True):
            st.markdown("**Quanto a loja guarda**")
            st.caption(
                "Alvo de cada tamanho = venda prevista da loja nos dias de cobertura "
                "(mais o prazo de entrega) + uma margem de segurança. Nunca abaixo da exposição."
            )
            c1, c2, c3 = st.columns(3)
            exposicao = c1.number_input(
                "Exposição mínima (peças por tamanho)",
                value=int(params["exposicao_minima"]), min_value=0,
                help="Peças de cada tamanho × modelo na arara. É o piso do alvo de todo "
                     "produto vivo dos colégios que a loja atende.",
            )
            cobertura_alta = c2.number_input(
                "Cobertura na alta (dias)",
                value=int(params["cobertura_dias_alta"]), min_value=1,
                help="Dias de venda que a loja guarda na alta. Como a reposição sai todo "
                     "dia, poucos dias bastam — mais dias = mais estoque parado na loja.",
            )
            cobertura_baixa = c3.number_input(
                "Cobertura na baixa (dias)",
                value=int(params["cobertura_dias_baixa"]), min_value=1,
                help="Dias de venda que a loja guarda na baixa, quando a reposição é "
                     "semanal ou sob solicitação.",
            )
            c1, c2, c3 = st.columns(3)
            nivel_servico = c1.selectbox(
                "Nível de serviço padrão (%)",
                options=NIVEIS_SERVICO,
                index=_indice_ns(params["nivel_servico_default"], 95),
                help="Chance de NÃO faltar até a próxima reposição chegar. Define o tamanho "
                     "da margem de segurança. Vale para o colégio sem nível próprio "
                     "(Colégios e Crescimento → Por colégio).",
            )
            horizonte = c2.number_input(
                "Horizonte do excesso (dias)",
                value=int(params["recolher_horizonte_dias"]), min_value=1,
                help="Só é excesso (sugestão de recolher) o que a loja não vende neste "
                     "prazo. Curto demais, manda recolher antes do pico o que o pico vende.",
            )
            dias_analise = c3.number_input(
                "Janela do giro (dias)",
                value=int((config.get("logistica") or {}).get("dias_analise_giro", 30)), min_value=1,
                help="Dias de venda recentes usados para medir o giro mostrado na Logística. "
                     "É só indicador: não entra na conta do alvo.",
            )
        salvar = st.form_submit_button("Salvar alvo da loja", icon=":material/save:", type="primary")

    if salvar:
        rep = config.setdefault("reposicao", {})
        rep["exposicao_minima"] = int(exposicao)
        rep["cobertura_dias_alta"] = int(cobertura_alta)
        rep["cobertura_dias_baixa"] = int(cobertura_baixa)
        rep["nivel_servico_default"] = int(nivel_servico)
        rep["recolher_horizonte_dias"] = int(horizonte)
        rep["aplicar_crescimento"] = bool(params["aplicar_crescimento"])
        config.setdefault("logistica", {})["dias_analise_giro"] = int(dias_analise)
        _fechar_reposicao(config, "Alvo da loja salvo. A Logística recalcula na próxima abertura.")


def _bloco_espaco_da_loja(config, params, dados):
    nomes_lojas = [l["nome"] for l in config["depositos"]["lojas"]]
    capacidade = params["capacidade_gaveta"]
    padrao = int(capacidade.get(config_edicao.CAPACIDADE_PADRAO) or 50)

    df_lojas = pd.DataFrame([
        {
            "loja": nome,
            "prazo_entrega_dias": (params["lojas"].get(nome) or {}).get("prazo_entrega_dias"),
            "gavetas": (params["lojas"].get(nome) or {}).get("gavetas"),
        }
        for nome in nomes_lojas
    ])
    super_categorias = sorted({
        str(s).strip() for s in dados["detalhes"]["Super_categoria"].dropna()
        if str(s).strip() and str(s).strip().lower() != "nan"
    })
    df_capacidade = pd.DataFrame([
        {"super_categoria": s, "pecas": capacidade.get(s)} for s in super_categorias
    ])
    for df, colunas in ((df_lojas, ("prazo_entrega_dias", "gavetas")), (df_capacidade, ("pecas",))):
        for coluna in colunas:
            df[coluna] = pd.to_numeric(df[coluna], errors="coerce").astype("Int64")

    with st.form("form_reposicao_espaco", border=False):
        with st.container(border=True):
            st.markdown("**Prazo e espaço de cada loja**")
            st.caption(
                "As gavetas guardam o que não cabe na arara. O sistema as distribui entre os "
                "modelos de maior venda prevista (um campeão pode levar mais de uma); modelo "
                "sem gaveta fica só com a exposição. **Gavetas vazio** = loja sem limite de espaço."
            )
            c1, c2 = st.columns(2)
            with c1:
                lojas_editado = st.data_editor(
                    df_lojas,
                    column_config={
                        "loja": col_texto("Loja"),
                        "prazo_entrega_dias": st.column_config.NumberColumn(
                            "Prazo de entrega (dias)", min_value=0, step=1,
                            help="Dias entre separar no CD e a mercadoria estar na loja. "
                                 "Soma na cobertura."),
                        "gavetas": st.column_config.NumberColumn(
                            "Gavetas", min_value=0, step=1,
                            help="Quantas gavetas de fundo a loja tem. Zero = só a arara."),
                    },
                    disabled=["loja"],
                    **padrao_tabela(EDITOR, len(df_lojas)),
                    key="editor_reposicao_lojas",
                )
                cap_padrao = st.number_input(
                    "Peças por gaveta — padrão",
                    value=padrao, min_value=1,
                    help="Vale para a super categoria que ficar vazia na tabela ao lado.",
                )
            with c2:
                capacidade_editada = st.data_editor(
                    df_capacidade,
                    column_config={
                        "super_categoria": col_texto("Super categoria"),
                        "pecas": st.column_config.NumberColumn(
                            "Peças por gaveta", min_value=1, step=1,
                            help="Quantas peças deste tipo cabem numa gaveta. Vazio = o padrão."),
                    },
                    disabled=["super_categoria"],
                    **padrao_tabela(EDITOR, len(df_capacidade), max_linhas=8),
                    key="editor_reposicao_capacidade",
                )
        salvar = st.form_submit_button("Salvar prazo e espaço", icon=":material/save:", type="primary")

    if salvar:
        rep = config.setdefault("reposicao", {})
        rep["lojas"] = config_edicao.aplicar_edicao_lojas(_registros(lojas_editado))
        rep["capacidade_gaveta"] = config_edicao.aplicar_edicao_capacidade(
            _registros(capacidade_editada), cap_padrao)
        _fechar_reposicao(config, "Prazo e espaço salvos. A Logística recalcula na próxima abertura.")


def _bloco_sortimento(config, params, dados):
    from etl import reposicao as motor_reposicao

    nomes_lojas = [l["nome"] for l in config["depositos"]["lojas"]]
    hoje = pd.Timestamp.now().normalize()
    vendas = motor_reposicao.vendas_por_colegio_loja(dados, config, hoje)
    sugerido = motor_reposicao.colegios_vendidos_por_loja(dados, config, hoje)
    atual, origem = motor_reposicao.sortimento_efetivo(params, nomes_lojas, sugerido)

    # "Sugerir pelas vendas" troca a base do editor e a `key` (a edição pendente
    # é guardada por posição e reapareceria sobre a base nova).
    rev = st.session_state.get("rep_sortimento_rev", 0)
    base = sugerido if st.session_state.get("rep_sortimento_sugerir") else atual

    _, det = _base_colegios(config)
    colegios = sorted(
        {c for c in det["Colegio"].unique() if c and c != "nan"}
        | {c for marcados in atual.values() for c in marcados}
    )

    with st.container(border=True):
        st.markdown("**Colégios que cada loja atende**")
        st.caption(
            "A loja só recebe alvo para os colégios marcados; o estoque que ela tiver de "
            "um colégio desmarcado aparece na Logística como excesso a recolher."
        )
        sem_cadastro = [n for n in nomes_lojas if origem[n] == motor_reposicao.ORIGEM_VENDAS]
        if sem_cadastro:
            st.info(
                f"**{', '.join(sem_cadastro)}** ainda sem cadastro: a Logística usa os colégios "
                f"com {motor_reposicao.MIN_PECAS_SORTIMENTO}+ peças vendidas na loja em 12 meses "
                "(é o que vem marcado abaixo). Confira e salve para fixar.",
                icon=":material/info:",
            )

        if not colegios:
            st.info("Nenhum colégio com produto ativo encontrado.")
            return

        df = pd.DataFrame([
            {
                "colegio": c,
                **{n: c in base[n] for n in nomes_lojas},
                **{f"vendas_{n}": vendas.get(n, {}).get(c, 0.0) for n in nomes_lojas},
            }
            for c in colegios
        ])
        colunas = {"colegio": col_colegio()}
        for n in nomes_lojas:
            colunas[n] = st.column_config.CheckboxColumn(n, help=f"{n} atende este colégio.")
        for n in nomes_lojas:
            colunas[f"vendas_{n}"] = col_pecas(
                f"Vendas 12 m — {n}", largura=None,
                ajuda=f"Peças do colégio vendidas em {n} nos últimos 12 meses.")

        with st.form("form_reposicao_sortimento", border=False):
            editado = st.data_editor(
                df,
                column_config=colunas,
                disabled=["colegio"] + [f"vendas_{n}" for n in nomes_lojas],
                **padrao_tabela(EDITOR, len(df)),
                key=f"editor_reposicao_sortimento_{rev}",
            )
            salvar = st.form_submit_button("Salvar sortimento", icon=":material/save:", type="primary")

        if st.button("Sugerir pelas vendas", icon=":material/auto_awesome:",
                     help="Remarca a tabela pelos colégios que cada loja vendeu em 12 meses. "
                          "Só grava quando você salvar."):
            st.session_state["rep_sortimento_sugerir"] = True
            st.session_state["rep_sortimento_rev"] = rev + 1
            st.rerun()

    if salvar:
        config.setdefault("reposicao", {})["sortimento"] = config_edicao.aplicar_edicao_sortimento(
            _registros(editado), nomes_lojas)
        st.session_state.pop("rep_sortimento_sugerir", None)
        _fechar_reposicao(config, "Sortimento salvo. A Logística recalcula na próxima abertura.")


def _secao_reposicao():
    from etl import reposicao as motor_reposicao

    config = carregar_config()
    params = motor_reposicao.parametros(config)
    dados, _ = carregar_com_feedback()

    st.caption(
        "Quanto cada loja deve ter de cada tamanho (**estoque-alvo**), limitado pelo espaço "
        "dela. A tela de Logística compara o alvo com o estoque, sugere o que o CD separa e "
        "o que a loja pode devolver."
    )

    _bloco_alvo_da_loja(config, params)
    _bloco_espaco_da_loja(config, params, dados)
    _bloco_sortimento(config, params, dados)

    st.caption(
        "O interruptor que liga o **crescimento** na Reposição fica em "
        "*Colégios e Crescimento → Regras gerais*. A temporada de alta é a mesma da "
        "Produção (*Pico de vendas*)."
    )


# =================================================================
# SEÇÃO — PRODUÇÃO (margem de segurança + calendário do motor)
# =================================================================

def _secao_producao():
    config = carregar_config()
    cfg_dem = config.get("demanda", {})
    cfg_plan = config["planejamento"]
    cfg_fab = config["fabrica"]
    _meses = list(MESES_NOME_CFG.keys())

    st.caption(
        "Parâmetros do motor do **Simulador de Produção** (Sugestão por SKU e Visão Geral): "
        "a demanda parte das vendas da última alta × crescimento, e o pedido cobre essa "
        "demanda mais uma margem de segurança."
    )

    with st.form("form_producao", border=False):
        with st.container(border=True):
            st.markdown("**Margem de segurança**")
            st.caption(
                "O estoque de segurança cresce com o nível de serviço e com a variação. "
                "Vale o nível da ALTA quando o período da rodada contém meses de pico."
            )
            c1, c2, c3 = st.columns(3)
            dem_ns_alta = c1.selectbox(
                "Nível de serviço — alta (%)",
                options=NIVEIS_SERVICO,
                index=_indice_ns(cfg_dem.get("nivel_servico_alta"), 99),
                help="Chance de NÃO faltar no pico. Não pode faltar na volta às aulas → nível alto.",
            )
            dem_ns_baixa = c2.selectbox(
                "Nível de serviço — baixa (%)",
                options=NIVEIS_SERVICO,
                index=_indice_ns(cfg_dem.get("nivel_servico_baixa"), 92),
                help="Chance de NÃO faltar fora do pico. Menor = menos estoque parado.",
            )
            dem_cv = c3.number_input(
                "Variação da demanda",
                value=float(cfg_dem.get("variacao_demanda", 0.25)),
                min_value=0.0, max_value=2.0, step=0.05,
                help="Incerteza da previsão (0,25 = a demanda pode fugir ~25%). "
                     "Multiplica o estoque de segurança: maior = mais margem.",
            )

        with st.container(border=True):
            st.markdown("**Calendário do motor**")
            c1, c2 = st.columns([2, 1])
            dem_janela_alta = c1.multiselect(
                "Pico de vendas (meses da alta)",
                options=_meses,
                default=cfg_dem.get("janela_alta", [12, 1, 2]),
                format_func=lambda m: MESES_NOME_CFG[m],
                help="Âncora da demanda: as vendas reais destes meses na última temporada "
                     "definem o tamanho do pico de cada SKU. Ordem cronológica (ex: Dez, Jan, Fev).",
            )
            lead_time = c2.number_input(
                "Prazo de produção (semanas)",
                value=int(cfg_plan["lead_time_semanas"]), min_value=1,
                help="Tempo entre disparar a rodada e a mercadoria chegar.",
            )
            c1, c2, _ = st.columns(3)
            periodo_hist_ini = c1.date_input(
                "Histórico de referência — início",
                value=datetime.fromisoformat(cfg_plan["periodo_historico_inicio"]).date(),
                format="DD/MM/YYYY",
                help="Janela de vendas passadas que ensina o FORMATO do ano (como a baixa se "
                     "distribui mês a mês) e a base dos SKUs que só vendem na baixa. Use 12+ "
                     "meses. O tamanho do pico NÃO vem daqui.",
            )
            periodo_hist_fim = c2.date_input(
                "Histórico de referência — fim",
                value=datetime.fromisoformat(cfg_plan["periodo_historico_fim"]).date(),
                format="DD/MM/YYYY",
            )

        with st.expander("Avançado"):
            c1, c2, _ = st.columns(3)
            cobertura_meses = c1.number_input(
                "Cobertura sem rodadas (meses)",
                value=int(cfg_fab["cobertura_meses"]), min_value=1,
                help="Só entra quando não há plano de rodadas: a Sugestão por SKU passa a "
                     "cobrir este número fixo de meses.",
            )
            correcao_manual = c2.number_input(
                "Ajuste global (peças por SKU)",
                value=int(cfg_fab["correcao_manual"]), step=1,
                help="Somado à demanda projetada de TODOS os SKUs. Deixe 0, salvo decisão "
                     "deliberada: 1 peça aqui vira milhares de peças na rede.",
            )

        salvar = st.form_submit_button("Salvar Produção", icon=":material/save:", type="primary")

    if salvar:
        if not dem_janela_alta:
            st.error("Escolha ao menos um mês em *Pico de vendas* — é a âncora da demanda.",
                     icon=":material/error:")
        else:
            config.setdefault("demanda", {})
            config["demanda"]["nivel_servico_alta"] = dem_ns_alta
            config["demanda"]["nivel_servico_baixa"] = dem_ns_baixa
            config["demanda"]["variacao_demanda"] = dem_cv
            config["demanda"]["janela_alta"] = list(dem_janela_alta)
            config["planejamento"]["lead_time_semanas"] = lead_time
            config["planejamento"]["periodo_historico_inicio"] = periodo_hist_ini.isoformat()
            config["planejamento"]["periodo_historico_fim"] = periodo_hist_fim.isoformat()
            config["fabrica"]["cobertura_meses"] = cobertura_meses
            config["fabrica"]["correcao_manual"] = correcao_manual
            # rodadas_datas/cobertura_override NÃO são editados aqui (vivem no
            # Simulador → Visão Geral); o valor carregado só trafega de volta.
            _salvar_secao(config, "Produção salva. O Simulador recalcula na próxima abertura.")

    with st.container(border=True):
        st.markdown("**Plano de rodadas**")
        _datas = sorted(cfg_plan.get("rodadas_datas") or [])
        if _datas:
            _rot = " · ".join(pd.Timestamp(str(d)).strftime("%d/%m/%Y") for d in _datas)
            st.write(f"Disparos configurados: {_rot}")
        else:
            st.write("Nenhuma rodada configurada — a Sugestão por SKU usa a cobertura fixa do *Avançado*.")
        st.caption(
            "Datas e coberturas-alvo formam um plano só e são editadas no Simulador, "
            "onde o efeito de cada mudança aparece ao vivo."
        )
        st.page_link("pages/3_Fabrica.py", label="Editar no Simulador de Produção → Visão Geral", icon=":material/factory:")

    st.caption(
        "O interruptor que liga o **crescimento** na Produção fica em "
        "*Colégios e Crescimento → Regras gerais*."
    )


# =================================================================
# SEÇÃO — COLÉGIOS E CRESCIMENTO
# A ordem das abas internas é a ordem da cascata de
# demanda.taxa_crescimento_efetiva, do geral ao específico.
# =================================================================

def _base_colegios(config):
    """
    Colégios e grupos VIVOS — a base comum dos editores desta seção. Só produtos
    ativos (colégio descontinuado, ex: OVD, não aparece) e com o nome já
    normalizado pelo de-para. Devolve `(dados_ativos, det)`.
    """
    from etl.demanda import colegio_efetivo, restringir_a_ativos
    dados, _ = carregar_com_feedback()
    dados_ativos = restringir_a_ativos(dados)
    det = dados_ativos["detalhes"][["Marca_sku", "Grupo"]].copy()
    det["Colegio"] = (
        det["Marca_sku"].fillna("").astype(str).str.strip()
        .map(lambda v: colegio_efetivo(v, config))
    )
    det["GrupoC"] = det["Grupo"].fillna("").astype(str).str.strip()
    det = det[(det["Colegio"] != "") & (det["Colegio"] != "nan")]
    return dados_ativos, det


def _crescimento_medido(dados_ativos, config):
    """Camada observada do crescimento, ou None quando desligada — exatamente
    o que os motores recebem (demanda.py, na Produção e na Reposição)."""
    from etl.demanda import calcular_crescimento_observado
    if not (config.get("demanda", {}) or {}).get("crescimento_observado_ativo", True):
        return None
    return calcular_crescimento_observado(dados_ativos, config)


def _duas_casas(valor):
    """Multiplicador de crescimento arredondado para exibição (None passa)."""
    return None if valor is None else round(float(valor), 2)


def _registros(df) -> list:
    """Linhas do editor como dicts, com célula vazia (NaN/NA) virando None."""
    return df.astype(object).where(df.notna(), None).to_dict("records")


def _bloco_regras_crescimento():
    config = carregar_config()
    cfg_dem = config.get("demanda", {}) or {}

    st.markdown(
        "O crescimento multiplica as vendas da última alta para projetar a próxima. "
        "Para cada SKU vale a **primeira** regra que existir, nesta ordem:\n\n"
        "1. ajuste da **série** — aba *Por série*\n"
        "2. taxa do **colégio** — aba *Por colégio*\n"
        "3. crescimento **medido** do colégio naquele segmento\n"
        "4. crescimento **medido** do colégio inteiro\n"
        "5. **taxa padrão** (abaixo)"
    )

    with st.form("form_crescimento", border=False):
        with st.container(border=True):
            st.markdown("**De onde vem a taxa**")
            c1, c2 = st.columns([2, 1])
            usar_medido = c1.toggle(
                "Usar o crescimento medido nas vendas",
                value=bool(cfg_dem.get("crescimento_observado_ativo", True)),
                help="Mede, por colégio e por segmento, quanto a última alta cresceu sobre a "
                     "anterior (limitado entre 0,5× e 2×; só com 30+ peças). Desligado, as "
                     "regras 3 e 4 somem: o que não tem ajuste manual usa a taxa padrão.",
            )
            taxa_padrao = c2.number_input(
                "Taxa padrão (%)",
                value=float(config["fabrica"]["crescimento_pct"]), min_value=0.0, step=0.5,
                help="Usada quando não há ajuste manual nem medição confiável (colégio novo, "
                     "amostra pequena).",
            )

        with st.container(border=True):
            st.markdown("**Onde o crescimento entra**")
            c1, c2 = st.columns(2)
            aplicar_producao = c1.toggle(
                "Produção",
                value=bool(cfg_dem.get("aplicar_crescimento_fabrica", True)),
                help="Posição inicial do interruptor *Aplicar crescimento* do Simulador de "
                     "Produção — lá ele pode ser desligado só para comparar.",
            )
            aplicar_reposicao = c2.toggle(
                "Reposição de Loja",
                value=bool(parametros_reposicao(config)["aplicar_crescimento"]),
                help="Aplica o crescimento à demanda prevista de cada loja (estoque-alvo).",
            )

        salvar = st.form_submit_button("Salvar regras gerais", icon=":material/save:", type="primary")

    if salvar:
        config.setdefault("demanda", {})
        config["demanda"]["crescimento_observado_ativo"] = bool(usar_medido)
        config["demanda"]["aplicar_crescimento_fabrica"] = bool(aplicar_producao)
        config["fabrica"]["crescimento_pct"] = taxa_padrao
        config.setdefault("reposicao", {})["aplicar_crescimento"] = bool(aplicar_reposicao)
        _salvar_secao(config, "Regras de crescimento salvas.")


def _bloco_por_colegio():
    from etl.demanda import calcular_proporcao_baixa

    config = carregar_config()
    dados_ativos, det = _base_colegios(config)
    cfg_colegios = config.get("colegios") or {}
    ns_padrao = int(parametros_reposicao(config)["nivel_servico_default"])
    taxa_padrao = 1 + float(config.get("fabrica", {}).get("crescimento_pct", 0)) / 100
    prop_global = round(float(calcular_proporcao_baixa(dados_ativos, config)), 3)
    medido = _crescimento_medido(dados_ativos, config) or {}
    colegios = sorted(c for c in det["Colegio"].unique() if c and c != "nan")

    st.markdown(
        "Preencha **só onde você sabe de algo que os dados não sabem**. Célula **vazia** "
        "segue a regra geral e se atualiza sozinha a cada temporada; célula **preenchida** "
        "é decisão sua e fica fixa até você apagar."
    )
    st.caption(
        f"Vazio significa — **Taxa de crescimento**: o medido (sem medição, a taxa padrão "
        f"{num(taxa_padrao, 2)}×) · **Nível de serviço**: {ns_padrao}% (padrão da Reposição) · "
        f"**Proporção da baixa**: {num(prop_global, 3)} (medida na rede, últimos 2 ciclos)."
    )

    if not colegios:
        st.info("Nenhum colégio com produto ativo encontrado.")
        return

    df_colegios = pd.DataFrame([
        {
            "colegio": c,
            "taxa_crescimento": (cfg_colegios.get(c) or {}).get("taxa_crescimento"),
            "nivel_servico": (cfg_colegios.get(c) or {}).get("nivel_servico"),
            "proporcao_baixa": (cfg_colegios.get(c) or {}).get("proporcao_baixa"),
            "medido": _duas_casas((medido.get(c) or {}).get("__geral__")),
        }
        for c in colegios
    ])
    # Tipos explícitos: coluna toda vazia nasceria `object` e o editor não
    # saberia que é número. Int64 (anulável) mantém o nível de serviço inteiro.
    for _col in ("taxa_crescimento", "proporcao_baixa", "medido"):
        df_colegios[_col] = pd.to_numeric(df_colegios[_col], errors="coerce")
    df_colegios["nivel_servico"] = pd.to_numeric(
        df_colegios["nivel_servico"], errors="coerce").astype("Int64")

    # st.form: o data_editor só reprocessa a página no submit,
    # não a cada célula editada.
    with st.form("form_colegios_param", border=False):
        df_colegios_editado = st.data_editor(
            df_colegios,
            column_config={
                "colegio": col_colegio(),
                "taxa_crescimento": st.column_config.NumberColumn(
                    "Taxa de crescimento (×)", min_value=0.0, step=0.01, format="%.2f",
                    help="Multiplicador do colégio inteiro (1,10 = +10%). Vence o medido. "
                         "Vale para Produção e Reposição."),
                "nivel_servico": st.column_config.SelectboxColumn(
                    "Nível de serviço — Reposição (%)", options=NIVEIS_SERVICO,
                    help="Só a Reposição de Loja usa (margem de segurança). A Produção usa os níveis "
                         "de alta/baixa da seção Produção."),
                "proporcao_baixa": st.column_config.NumberColumn(
                    "Proporção da baixa — Produção", min_value=0.0, step=0.05, format="%.3f",
                    help="Quanto a baixa vende em relação à alta. Mude só no colégio que "
                         "você sabe ter cauda diferente (ex: vende o ano todo)."),
                "medido": st.column_config.NumberColumn(
                    "Crescimento medido (×)", format="%.2f",
                    help="Última alta sobre a anterior, no colégio inteiro. Vazio = amostra "
                         "pequena ou medição desligada."),
            },
            disabled=["colegio", "medido"],
            **padrao_tabela(EDITOR, len(df_colegios)),
            key="editor_colegios",
        )

        _salvar_colegios = st.form_submit_button("Salvar parâmetros por colégio", icon=":material/save:", type="primary")

    if _salvar_colegios:
        novo_colegios, n_overrides = config_edicao.aplicar_edicao_colegios(
            config.get("colegios"), _registros(df_colegios_editado))
        config["colegios"] = novo_colegios
        if not salvar_parametros(config):
            st.stop()
        # Só o cache de CONFIG — não o de dados (TTL 1 h).
        carregar_config.clear()
        st.success(
            f"{n_overrides} ajuste(s) manual(is) gravado(s) — o resto segue a regra geral.",
            icon=":material/check_circle:",
        )


def _bloco_por_serie():
    from etl import demanda as _dem

    config = carregar_config()
    dados_ativos, det = _base_colegios(config)
    cfg_colegios = config.get("colegios") or {}
    obs_cresc = _crescimento_medido(dados_ativos, config)
    mapa_seg_cfg = _dem.mapa_grupo_segmento(config)

    st.markdown(
        "A coluna **Crescimento aplicado** já vem com o que o motor usa hoje em cada série. "
        "Edite só onde você **sabe de algo que os dados não sabem** (turma nova, colégio em "
        "expansão). Célula deixada **igual ao _Sem ajuste_ fica viva** — re-mede sozinha a "
        "cada temporada; só o que você **mudar** vira ajuste fixo. Para desfazer um ajuste, "
        "volte a célula ao valor de _Sem ajuste_."
    )

    def _sem_ajuste(colegio, grupo):
        """O que o motor aplicaria SEM o ajuste da série — o próprio
        taxa_crescimento_efetiva, com `crescimento_grupos` do colégio removido.
        Usar o motor (e não reimplementar a cascata) é o que garante que a
        tela mostra exatamente o número aplicado."""
        entrada = {k: v for k, v in (cfg_colegios.get(colegio) or {}).items()
                   if k != "crescimento_grupos"}
        cfg_sem = {**config, "colegios": {**cfg_colegios, colegio: entrada}}
        # 2 casas, as mesmas do editor: ele TRUNCA a exibição na precisão do
        # step, então "aplicado" e "sem ajuste" têm de nascer do MESMO número —
        # senão 1,126 aparece 1,12 numa coluna e 1,126 na outra e parece ajuste.
        return round(_dem.taxa_crescimento_efetiva(colegio, cfg_sem, grupo, True, obs_cresc), 2)

    def _origem(colegio, grupo, manual):
        if manual is not None:
            return "ajuste da série"
        if "taxa_crescimento" in (cfg_colegios.get(colegio) or {}):
            return "taxa do colégio"
        obs = (obs_cresc or {}).get(colegio) or {}
        tem_medido = ((obs.get("segmentos") or {}).get(mapa_seg_cfg.get(grupo, "Outros")) is not None
                      or obs.get("__geral__") is not None)
        return "medido" if tem_medido else "taxa padrão"

    celulas = (
        det[det["GrupoC"].ne("") & det["GrupoC"].ne("nan")]
        .groupby(["Colegio", "GrupoC"]).size().reset_index(name="n_skus")
        .sort_values(["Colegio", "GrupoC"])
    )
    linhas_matriz = []
    for _, r in celulas.iterrows():
        col_, gr_ = r["Colegio"], r["GrupoC"]
        manual = ((cfg_colegios.get(col_) or {}).get("crescimento_grupos") or {}).get(gr_)
        base = _sem_ajuste(col_, gr_)
        linhas_matriz.append({
            "colegio": col_, "grupo": gr_,
            "taxa_crescimento": float(manual) if manual is not None else base,
            "base": base,
            "origem": _origem(col_, gr_, manual),
            "segmento": mapa_seg_cfg.get(gr_, "Outros"),
            "skus": int(r["n_skus"]),
        })
    df_matriz = pd.DataFrame(linhas_matriz)

    if len(df_matriz) == 0:
        st.info("Nenhuma série com produto ativo encontrada.")
        return

    # st.form: o data_editor só reprocessa a página no submit,
    # não a cada célula editada.
    with st.form("form_matriz_grupo", border=False):
        df_matriz_editado = st.data_editor(
            df_matriz,
            column_config={
                "colegio": col_colegio(),
                "grupo": col_texto("Série (grupo)", largura="small"),
                "taxa_crescimento": st.column_config.NumberColumn(
                    "Crescimento aplicado (×)", min_value=0.0, step=0.01, format="%.2f",
                    help="Multiplicador usado pelo motor nesta série (1,10 = +10%)."),
                "base": st.column_config.NumberColumn(
                    "Sem ajuste (×)", format="%.2f",
                    help="O que vale se você não mexer: a taxa do colégio, o medido "
                         "ou a taxa padrão — nessa ordem."),
                "origem": col_texto(
                    "Origem",
                    ajuda="De onde vem o valor aplicado: ajuste da série (você definiu aqui) · "
                          "taxa do colégio · medido · taxa padrão."),
                "segmento": col_texto("Segmento"),
                "skus": col_pecas("SKUs"),
            },
            disabled=["colegio", "grupo", "base", "origem", "segmento", "skus"],
            **padrao_tabela(EDITOR, len(df_matriz)),
            key="editor_matriz_grupo",
        )

        _salvar_matriz = st.form_submit_button("Salvar crescimento por série", icon=":material/save:", type="primary")

    if _salvar_matriz:
        novo_colegios, n_overrides = config_edicao.aplicar_edicao_crescimento_grupos(
            config.get("colegios"), _registros(df_matriz_editado))
        config["colegios"] = novo_colegios
        if not salvar_parametros(config):
            st.stop()
        # Só o cache de CONFIG — não o de dados (TTL 1 h).
        carregar_config.clear()
        st.success(
            f"{n_overrides} ajuste(s) de série gravado(s) — o resto segue a regra geral (vivo).",
            icon=":material/check_circle:",
        )


def _bloco_nomes_segmentos():
    st.markdown("**Nomes dos colégios**")
    st.markdown(
        "O colégio é extraído automaticamente da SKU e às vezes sai **errado** "
        "(ex: `27`, códigos soltos). Aqui você define **como cada valor cru aparece** "
        "em todo o sistema (Reposição, Fábrica, filtros). Deixe **igual** para manter; escreva "
        "**`Outros`** (ou outro nome) para renomear/agrupar o ruído. Só o que você "
        "mudar vira regra — o resto segue como está. A coluna _Sugestão_ é só uma dica."
    )

    from etl.demanda import parece_ruido

    config = carregar_config()
    alias_atual = config.get("colegios_alias") or {}
    # Só produtos ATIVOS: colégios descontinuados (ex: OVD) não devem aparecer
    # nos editores de Colégios / Grupo→Segmento.
    dados_colegios, det_cfg = _base_colegios(config)

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
        st.caption(f":orange[:material/warning:] {n_ruido} valor(es) cru(s) parecem ruído (sem letra) — sugeridos como _Outros_.")

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
            **padrao_tabela(EDITOR, len(df_alias)), key="editor_colegios_alias",
        )

        _salvar_alias = st.form_submit_button("Salvar Normalização de Colégios", icon=":material/save:", key="btn_salvar_alias", type="primary")

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
        st.success(f"{len(novo_alias)} regra(s) de colégio salva(s) ({n_outros} → Outros). Cache limpo.", icon=":material/check_circle:")

    st.divider()
    st.markdown("**Segmentos (agrupamento das séries)**")
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
            **padrao_tabela(EDITOR, len(df_seg)), key="editor_grupo_seg",
        )

        _salvar_seg = st.form_submit_button("Salvar Agrupamento de Segmentos", icon=":material/save:", key="btn_salvar_seg", type="primary")

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
        st.success(f"Agrupamento salvo — {len(set(novo_seg.values()))} segmento(s).", icon=":material/check_circle:")


def _secao_colegios():
    _visoes = ["Regras gerais", "Por colégio", "Por série", "Nomes e segmentos"]
    visao = _seletor(st.pills, "Colégios e Crescimento", _visoes, "cfg_colegios_visao")
    if visao == _visoes[1]:
        _bloco_por_colegio()
    elif visao == _visoes[2]:
        _bloco_por_serie()
    elif visao == _visoes[3]:
        _bloco_nomes_segmentos()
    else:
        _bloco_regras_crescimento()


# =================================================================
# SEÇÃO — INTEGRAÇÕES (Bling = compra AK · Olist = venda Art Kamizetas)
# =================================================================

def _secao_integracoes():
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
    def _situacoes_compra_bling() -> list:
        """Situações do módulo de pedidos de compra (id é por conta)."""
        token = oauth.obter_access_token("bling", obter_repositorio_integracoes())
        return cliente_bling.listar_situacoes_compra(token)

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
                st.caption(f":orange[:material/warning:] Não foi possível listar as formas de pagamento: {erro}")
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

        # Cancelamento pós-emissão: o Bling muda a situação do pedido pelo ID
        # da situação no módulo (por conta), não pelo nome. Mesmo desenho da
        # forma de pagamento: lista por nome, text_input como degradação.
        st.markdown("**Cancelamento** (pedido de compra já emitido)")
        salvo_canc = str(cfg.get("situacao_cancelado_id") or "")
        situacao_canc = salvo_canc

        situacoes, erro_sit = [], None
        if conectado:
            try:
                situacoes = _situacoes_compra_bling()
            except Exception as exc:
                erro_sit = str(exc)

        if situacoes:
            ids_sit = [s["id"] for s in situacoes]
            rotulos_sit = {s["id"]: s["nome"] for s in situacoes}
            if salvo_canc and salvo_canc not in ids_sit:
                ids_sit.insert(0, salvo_canc)
                rotulos_sit[salvo_canc] = f"(id {salvo_canc} — não está mais na lista)"
            # Nada salvo ainda: já aponta para a que se chama "Cancelado"
            sugerida = next((i for i in ids_sit
                             if rotulos_sit[i].strip().lower().startswith("cancel")), ids_sit[0])
            situacao_canc = st.selectbox(
                "Situação de cancelado", options=ids_sit,
                index=ids_sit.index(salvo_canc if salvo_canc in ids_sit else sugerida),
                format_func=lambda i: rotulos_sit.get(i, i),
                help="Para onde o pedido de compra vai ao ser cancelado pela página "
                     "Pedidos de Compra. O app confere depois se o pedido ficou "
                     "de fato Cancelado no Bling.",
                key="neg_bling_sit_canc_sel")
        else:
            if erro_sit:
                st.caption(":orange[:material/warning:] Não foi possível listar as "
                           f"situações de compra: {erro_sit}")
            situacao_canc = st.text_input(
                "ID da situação de cancelado", value=salvo_canc,
                help="Conecte a integração para escolher pelo nome.",
                key="neg_bling_sit_canc_txt")

        return {"forma_pagamento_id": str(forma_id or "").strip(),
                "prazo_pagamento_dias": int(prazo),
                "unidade_padrao": unidade.strip(),
                "situacao_cancelado_id": str(situacao_canc or "").strip()}

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
                st.caption(f":orange[:material/warning:] Não foi possível listar as formas de recebimento: {erro}")
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
        form de dados do pedido e devolve o que gravar junto (salvar_config
        substitui o jsonb inteiro — tudo precisa sair no mesmo submit).
        """
        repo_int = obter_repositorio_integracoes()
        integ = repo_int.ler(plataforma) or {}
        conectado = bool(integ.get("refresh_token"))
        rotulo = f"{titulo}   ·   {':green[:material/check_circle: conectado]' if conectado else ':material/link_off: não conectado'}"
        # Card retrátil: aberto durante o setup (sem conexão), recolhido depois — o
        # status vai no cabeçalho, para ler de relance sem precisar expandir.
        with st.expander(rotulo, expanded=not conectado):

            # Ordem dos blocos: no SETUP (sem conexão) as credenciais vêm primeiro
            # — sem elas não há como conectar. Depois de conectado elas quase
            # nunca mudam: vão para o fim, atrás de um interruptor, e o que se
            # consulta no dia a dia (conexão, dados do pedido) sobe.
            if conectado:
                area_conexao, area_dados, area_app = (
                    st.container(), st.container(), st.container())
            else:
                area_app, area_conexao, area_dados = (
                    st.container(), st.container(), st.container())

            with area_app:
                editar_app = (not conectado) or st.toggle(
                    "Alterar as credenciais do aplicativo (OAuth)",
                    key=f"ver_app_{plataforma}")
                if editar_app:
                    # -- Aplicativo OAuth (credenciais) --
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
                        if st.form_submit_button("Salvar credenciais", icon=":material/save:"):
                            repo_int.salvar_chaves(plataforma, cid, csecret, redir,
                                                   usuario)
                            st.success("Credenciais salvas.")
                            st.rerun()

                    if integ.get("redirect_uri"):
                        st.caption("Redirect a registrar no portal:")
                        st.code(integ["redirect_uri"], language=None)

            with area_conexao:
                # -- Conexão --
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
                            f"Autorizado por {quem}, mas o token venceu em "
                            f"{validade} (UTC). A renovação é automática — se a "
                            "sessão na plataforma tiver expirado, ela falha e é "
                            "preciso reconectar. Use **Testar conexão** antes de emitir.",
                            icon=":material/warning:")
                    else:
                        st.success(f"Conectado por {quem} · token expira {validade} (UTC)", icon=":material/check_circle:")
                else:
                    st.info("Não conectado.", icon=":material/link_off:")

                cc1, cc2 = st.columns(2)
                with cc1:
                    pronto_p_conectar = bool(integ.get("client_id") and integ.get("redirect_uri"))
                    if pronto_p_conectar:
                        state = oauth.gerar_state()
                        repo_int.salvar_state_oauth(plataforma, state, usuario)
                        url = oauth.montar_authorize_url(
                            plataforma, integ["client_id"], integ["redirect_uri"], state)
                        st.link_button("Conectar / Reconectar", url, icon=":material/link:",
                                       width="stretch")
                    else:
                        st.button("Conectar", icon=":material/link:", disabled=True, width="stretch",
                                  help="Salve Client ID e URL de redirecionamento primeiro.",
                                  key=f"conn_disabled_{plataforma}")
                with cc2:
                    if st.button("Testar conexão", icon=":material/science:", key=f"testar_{plataforma}",
                                 disabled=not conectado, width="stretch"):
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

            with area_dados:
                # -- Dados do pedido (IDs de negócio + pagamento) --
                with st.form(f"negocio_{plataforma}"):
                    st.markdown("**Dados do pedido**")
                    cfg = integ.get("config") or {}
                    valores = {}
                    for chave, rotulo, ajuda in campos_negocio:
                        valores[chave] = st.text_input(
                            rotulo, value=str(cfg.get(chave, "") or ""),
                            help=ajuda, key=f"neg_{plataforma}_{chave}")
                    extras = extras_form(cfg, conectado) if extras_form else {}
                    if st.form_submit_button("Salvar dados do pedido", icon=":material/save:"):
                        novo = {k: v.strip() for k, v in valores.items() if v.strip()}
                        # extras já vêm tipados (int/str) — só descarta string vazia
                        novo.update({k: v for k, v in extras.items()
                                     if not (isinstance(v, str) and not v)})
                        if plataforma == "olist" and "situacao" not in novo:
                            novo["situacao"] = 0
                        repo_int.salvar_config(plataforma, novo, usuario)
                        st.success("Dados do pedido salvos.")
                        st.rerun()

                # -- Só Bling: validar contrato do POST via GET (sem escrita) --
                if plataforma == "bling" and conectado:
                    if st.button("Validar contrato (GET pedido exemplo)", icon=":material/fact_check:",
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
            "bling", ":material/shopping_cart: Bling — Pedido de Compra (AK Uniformes)",
            [("fornecedor_id", "ID do fornecedor (Art Kamizetas)",
              "Cadastros → Fornecedores no Bling")],
            extras_form=_extras_bling,
        )
        _card_integracao(
            "olist", ":material/factory: Olist — Pedido de Venda (Art Kamizetas)",
            [("contato_id", "ID do contato/cliente (AK Uniformes)", "Contato no Olist"),
             ("vendedor_id", "ID do vendedor", "Obrigatório na API do Olist"),
             ("deposito_id", "ID do depósito", "Obrigatório na API do Olist"),
             ("situacao", "Situação inicial (0 = Aberta)", "Código de situação do pedido")],
            extras_form=_extras_olist,
        )

        with st.expander("Últimos eventos de integração", icon=":material/history:"):
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
                if "criado_em" in _eventos.columns:
                    _eventos["criado_em"] = (
                        pd.to_datetime(_eventos["criado_em"], errors="coerce", utc=True)
                        .dt.tz_convert("America/Fortaleza").dt.strftime("%d/%m/%Y %H:%M"))
                exibir(_eventos[_cols], MEMORIA, {
                    "criado_em": col_texto("Quando"),
                    "plataforma": col_texto("Plataforma", largura="small"),
                    "acao": col_texto("Ação"),
                    "sucesso": st.column_config.CheckboxColumn("Deu certo", width="small"),
                    "detalhe": col_texto(
                        "Detalhe / erro", largura="large",
                        ajuda="Motivo da falha ou resumo do evento (campo detalhe do log)"),
                    "criado_por": col_texto("Por"),
                })
            else:
                st.caption("Nenhum evento ainda.")


# =================================================================
# SEÇÃO — USUÁRIOS: allowlist de acesso (app.usuario, DDL 006)
# O login é Google; esta seção decide QUEM entra e O QUE vê. Não há
# auto-cadastro: o formulário de convite é a única porta de entrada.
# =================================================================

def _secao_usuarios():
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
        st.markdown("#### :material/person_add: Adicionar usuário")
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
            st.success(f"{normalizar_email(_novo_email)} liberado como {_novo_role}. "
                       "Peça para entrar com essa mesma conta Google.",
                       icon=":material/check_circle:")
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
                num_rows="fixed", **padrao_tabela(EDITOR, len(_df_usr)), key="editor_usuarios",
            )
            _salvar_usr = st.form_submit_button("Salvar alterações", icon=":material/save:", type="primary")

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
                    st.success(f"{len(_diff)} usuário(s) atualizado(s).", icon=":material/check_circle:")
                    st.rerun()

        st.divider()

        # --- Remoção ---
        st.markdown("#### :material/person_remove: Remover acesso")
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
                st.success(f"{_rm_email} removido.", icon=":material/check_circle:")
                st.rerun()
    elif not _erro_usr:
        st.info("Nenhum usuário cadastrado ainda.")


# =================================================================
# SEÇÃO — SISTEMA (versões, mapeamento do Bling, cache, backup)
# =================================================================

def _secao_sistema():
    config = carregar_config()

    col1, col2 = st.columns(2)
    with col1:
        with st.container(border=True):
            st.markdown("**Versões**")
            st.write(f"Python {__import__('sys').version.split()[0]} · "
                     f"Streamlit {st.__version__} · Pandas {pd.__version__}")
    with col2:
        with st.container(border=True):
            st.markdown("**Fonte de dados**")
            st.write("Supabase — espelho do Bling ERP · releitura a cada 1 hora")
            try:
                # Última gravação de parâmetros no Supabase (app.parametros)
                meta = obter_repositorio_parametros().ler_metadados()
                if meta:
                    _quando = pd.Timestamp(meta["atualizado_em"]).tz_convert("America/Fortaleza")
                    _quem = meta.get("atualizado_por") or "—"
                    st.caption(f"Parâmetros salvos pela última vez em {_quando:%d/%m/%Y %H:%M} por {_quem}")
                else:
                    st.caption("Parâmetros ainda não semeados (rode scripts/seed_parametros.py)")
            except Exception:
                st.caption("Supabase indisponível — usando os defaults do config.yaml")

    # Veio do topo do antigo form de parâmetros: são códigos do cadastro do
    # Bling, não decisão de gestão — só mudam se o Bling mudar.
    with st.container(border=True):
        st.markdown("**Mapeamento do Bling**")
        st.caption(
            "Os códigos que ligam o dashboard ao cadastro do Bling. Só mexa aqui se o "
            "cadastro do Bling mudar."
        )

        with st.form("form_status_ids", border=False):
            st.write("Situações de pedido contadas nos cartões do Daily")
            c1, c2, c3 = st.columns(3)
            status_aberto = c1.number_input(
                "Em aberto (ID)", value=int(config["daily"]["status_ids"]["em_aberto"]), step=1)
            status_andamento = c2.number_input(
                "Em andamento (ID)", value=int(config["daily"]["status_ids"]["em_andamento"]), step=1)
            status_pronto = c3.number_input(
                "Pronto para retirada (ID)",
                value=int(config["daily"]["status_ids"]["pronto_retirada"]), step=1)
            salvar_status = st.form_submit_button("Salvar situações", icon=":material/save:")

        if salvar_status:
            config["daily"]["status_ids"]["em_aberto"] = status_aberto
            config["daily"]["status_ids"]["em_andamento"] = status_andamento
            config["daily"]["status_ids"]["pronto_retirada"] = status_pronto
            _salvar_secao(config, "Situações de pedido salvas.")

        st.write("Lojas e depósitos em uso (definidos no `config.yaml`)")
        _central = config["depositos"]["central"]
        _df_locais = pd.DataFrame(
            [{"nome": _central.get("nome", "Estoque Central"), "loja_id": "",
              "deposito_id": str(_central["deposito_id"])}]
            + [{"nome": l["nome"], "loja_id": str(l["loja_id"]),
                "deposito_id": str(l["deposito_id"])}
               for l in config["depositos"]["lojas"]]
        )
        exibir(_df_locais, MEMORIA, {
            "nome": col_texto("Local"),
            "loja_id": col_texto("ID da loja", ajuda="Aparece nos PEDIDOS (onde a venda aconteceu)."),
            "deposito_id": col_texto("ID do depósito", ajuda="Aparece no ESTOQUE (onde a peça está)."),
        })
        st.caption(
            f"Situações que contam como **venda efetiva**: {config['daily']['situacoes_venda']} · "
            f"como **backlog** (consomem estoque sem faturar): "
            f"{config['fabrica'].get('situacoes_backlog', [])}. Também vêm do `config.yaml`."
        )

        # Veio da Home: os IDs internos servem a quem configura, não a quem vende.
        with st.expander("Todos os depósitos cadastrados no Bling"):
            _dados, _ = carregar_com_feedback()
            exibir(_dados["depositos"][["ID", "descricao"]], MEMORIA, {
                "ID": col_texto("ID"),
                "descricao": col_texto("Nome"),
            })

    with st.container(border=True):
        st.markdown("**Manutenção**")
        col3, col4 = st.columns(2)
        with col3:
            if st.button("Forçar recarga de dados", icon=":material/refresh:"):
                # Clear GLOBAL de propósito: é a intenção explícita do botão
                # (relê o espelho do Bling, não só os parâmetros).
                st.cache_data.clear()
                st.success("Cache limpo. A próxima tela relê o Supabase (~10 s).", icon=":material/check_circle:")
        with col4:
            # Backup do config EFETIVO (yaml defaults + parâmetros do Supabase
            # mesclados) — o que os motores realmente usam agora.
            st.download_button(
                label="Baixar backup do config efetivo", icon=":material/download:",
                data=yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
                file_name=f"config_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.yaml",
                mime="text/plain",
            )


# =================================================================
# INTERFACE PRINCIPAL — só a seção ativa é executada
# =================================================================

st.title(":material/settings: Configurações")

# Seção inicial: a da URL (?secao=) quando válida; senão a primeira. Depois
# disso quem manda é o widget — o session_state é a fonte, a URL é o espelho.
_secao = _seletor(
    st.segmented_control, "Seção", list(SECOES), CHAVE_SECAO,
    inicial=st.query_params.get("secao"), format_func=lambda s: SECOES[s],
)
if st.query_params.get("secao") != _secao:
    st.query_params["secao"] = _secao

st.caption("Cada bloco tem o seu **Salvar** — o que não foi salvo se perde ao trocar de seção.")

_RENDER_SECAO = {
    "comercial": _secao_comercial,
    "reposicao": _secao_reposicao,
    "producao": _secao_producao,
    "colegios": _secao_colegios,
    "integracoes": _secao_integracoes,
    "usuarios": _secao_usuarios,
    "sistema": _secao_sistema,
}
_RENDER_SECAO[_secao]()
