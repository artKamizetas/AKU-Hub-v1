"""
auth.py — Módulo de autenticação do Dashboard Bling

Usa streamlit-authenticator para login multi-usuário.
Credenciais configuradas em .streamlit/secrets.toml.

Uso para gerar hash de senha:
    python auth.py
"""

import streamlit as st
import streamlit_authenticator as stauth


def verificar_acesso():
    """
    Renderiza o widget de login e controla acesso.

    Retorna:
        (nome, username, role) se autenticado.
        Faz st.stop() se não autenticado.
    """
    auth_config = dict(st.secrets["auth_config"])
    credentials = dict(st.secrets["auth_config"]["credentials"])

    # Reconstruir dict de credenciais no formato esperado pela lib
    usernames = {}
    for username in credentials["usernames"]:
        user_data = dict(st.secrets["auth_config"]["credentials"]["usernames"][username])
        usernames[username] = user_data

    creds = {"usernames": usernames}

    authenticator = stauth.Authenticate(
        credentials=creds,
        cookie_name=auth_config.get("cookie_name", "aku_hub_auth"),
        cookie_key=auth_config.get("cookie_key", "chave_secreta_padrao"),
        cookie_expiry_days=auth_config.get("cookie_expiry_days", 7),
        auto_hash=True,
    )

    authenticator.login(
        location="main",
        fields={
            "Form name": "Login — AKU Hub",
            "Username": "Usuário",
            "Password": "Senha",
            "Login": "Entrar",
        },
    )

    if st.session_state.get("authentication_status"):
        nome = st.session_state.get("name", "")
        username = st.session_state.get("username", "")
        role = usernames.get(username, {}).get("role", "vendedor")
        # Guarda o role p/ páginas lerem via identidade_atual() sem instanciar
        # um 2º Authenticate/CookieManager (name/username já vêm da lib).
        st.session_state["_aku_role"] = role

        # Sidebar: info do usuário e logout
        with st.sidebar:
            st.write(f"👤 **{nome}**")
            authenticator.logout("Sair", "sidebar")

        return nome, username, role

    elif st.session_state.get("authentication_status") is False:
        st.error("Usuário ou senha incorretos.")
        st.stop()
    else:
        st.info("Por favor, faça login para acessar o dashboard.")
        st.stop()


def exigir_login():
    """
    Guarda para páginas individuais.
    Chame no topo de cada página para bloquear acesso sem autenticação.
    """
    if not st.session_state.get("authentication_status"):
        st.error("Acesso negado. Faça login pela página principal.")
        st.stop()


def identidade_atual():
    """
    Guard LEVE que devolve (nome, username, role) a partir do session_state,
    sem instanciar um 2º Authenticate/CookieManager.

    O app.py chama verificar_acesso() a CADA execução (reautenticando pelo
    cookie, mesmo em sessão nova pós-redirect OAuth) ANTES de a página rodar —
    então aqui a sessão já está autenticada. Chamar verificar_acesso() de novo
    na página criava um segundo CookieManager (ambos com key="init") e estourava
    StreamlitDuplicateElementKey.
    """
    if not st.session_state.get("authentication_status"):
        st.error("Acesso negado. Faça login pela página principal.")
        st.stop()
    return (st.session_state.get("name", ""),
            st.session_state.get("username", ""),
            st.session_state.get("_aku_role", "vendedor"))


if __name__ == "__main__":
    import hashlib
    senha = input("Digite a senha para gerar o hash: ")
    hashed = stauth.Hasher.hash(senha)
    print(f"\nHash bcrypt:\n{hashed}")
    print("\nCole este valor no campo 'password' do secrets.toml")
