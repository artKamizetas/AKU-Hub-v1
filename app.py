"""
app.py — Ponto de Entrada do Dashboard Bling

Rode com:
    streamlit run app.py
"""

import streamlit as st

st.set_page_config(
    page_title="AKU Hub",
    page_icon="assets/aku-favicon.png",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# =================================================================
# AUTENTICAÇÃO
# =================================================================
import auth

nome, usuario, role = auth.verificar_acesso()

# =================================================================
# NAVEGAÇÃO (filtrada por perfil)
# =================================================================
pages_all = [
    st.Page("pages/0_Home.py", title="Página Inicial", icon=":material/home:", default=True),
    st.Page("pages/1_Daily.py", title="Daily", icon=":material/trending_up:"),
    st.Page("pages/2_Logistica.py", title="Logística", icon=":material/local_shipping:"),
    st.Page("pages/3_Fabrica.py", title="Simulador de Produção", icon=":material/factory:"),
    st.Page("pages/4_Pedidos.py", title="Pedidos de Compra", icon=":material/receipt_long:"),
    # url_path fixo: é o redirect_uri do OAuth das integrações (a plataforma
    # devolve o navegador para .../configuracoes com ?code&state).
    st.Page("pages/5_Configuracoes.py", title="Configurações", icon=":material/settings:",
            url_path="configuracoes"),
]

# Perfis de acesso: o mapa role → páginas vive no auth.py (junto da resolução
# de acesso e do teste que garante que todo perfil enxerga ao menos uma página).
paginas_permitidas = auth.paginas_do_role(role)
if paginas_permitidas is None:
    pages = pages_all
else:
    pages = [p for p in pages_all if p.title in paginas_permitidas]

if not pages:
    # st.navigation([]) levanta exceção. Um perfil sem nenhuma página só
    # acontece com dado inconsistente, mas a tela precisa dizer isso.
    st.error("Seu perfil não tem nenhuma página liberada. Fale com o administrador.")
    st.stop()

nav = st.navigation(pages)
nav.run()
