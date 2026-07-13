"""
app.py — Ponto de Entrada do Dashboard Bling

Rode com:
    streamlit run app.py
"""

import streamlit as st

st.set_page_config(
    page_title="AKU Dashboard",
    page_icon="assets/aku-favicon.png",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# =================================================================
# AUTENTICAÇÃO
# =================================================================
from auth import verificar_acesso

nome, username, role = verificar_acesso()

# =================================================================
# NAVEGAÇÃO (filtrada por perfil)
# =================================================================
pages_all = [
    st.Page("pages/0_Home.py", title="Página Inicial", icon="📊", default=True),
    st.Page("pages/1_Daily.py", title="Daily", icon="📈"),
    st.Page("pages/2_Logistica.py", title="Logística", icon="📦"),
    st.Page("pages/3_Fabrica.py", title="Simulador de Produção", icon="🏭"),
    st.Page("pages/4_Pedidos.py", title="Pedidos de Compra", icon="🧾"),
    st.Page("pages/5_Configuracoes.py", title="Configurações", icon="⚙️"),
]

# Perfis de acesso
PAGINAS_POR_ROLE = {
    "admin": None,  # None = todas
    "supervisor": ("Daily", "Logística"),
    "vendedor": ("Página Inicial", "Daily"),
    "estoque": ("Logística",),
}

paginas_permitidas = PAGINAS_POR_ROLE.get(role)
if paginas_permitidas is None:
    pages = pages_all
else:
    pages = [p for p in pages_all if p.title in paginas_permitidas]

nav = st.navigation(pages)
nav.run()
