"""
auth.py — Autenticação e autorização do AKU Hub.

LOGIN: Google, via OIDC nativo do Streamlit (st.login/st.user). As chaves do
provedor ficam em st.secrets["auth"]/["auth.google"] e o callback é a rota
/oauth2callback, servida pelo próprio Streamlit — não colide com o
/configuracoes?code&state do OAuth das integrações Bling/Olist.

AUTORIZAÇÃO: a allowlist app.usuario (auth_store.py) decide se a pessoa entra
e o que ela vê. Não há auto-cadastro: e-mail fora da tabela é barrado e nada é
escrito no banco. O convite pela aba Usuários de Configurações é a única porta.

BREAK-GLASS: os e-mails em st.secrets["acesso"]["admins"] entram como admin
mesmo com o Supabase fora do ar. É a saída de emergência para lockout — e por
isso a lista é soberana: tirar alguém de lá faz parte de revogar o acesso.
"""

from dataclasses import dataclass

import streamlit as st

from auth_store import (
    ROLES_VALIDOS, normalizar_email, obter_repositorio_usuarios,
)

# Páginas visíveis por perfil. Chaveado pelo TÍTULO do st.Page definido no
# app.py — se um título mudar lá, mude aqui (há teste garantindo que todo
# perfil enxerga ao menos uma página).
PAGINAS_POR_ROLE = {
    "admin": None,  # None = todas
    "supervisor": ("Daily", "Logística"),
    "vendedor": ("Página Inicial", "Daily"),
    "estoque": ("Logística",),
}

TTL_ALLOWLIST = 300   # 5 min, igual ao carregar_config()

AUTORIZADO = "AUTORIZADO"
INATIVO = "INATIVO"
NAO_AUTORIZADO = "NAO_AUTORIZADO"
INDISPONIVEL = "INDISPONIVEL"


@dataclass(frozen=True)
class Acesso:
    estado: str
    role: str
    nome: str
    email: str


# =====================================================================
# NÚCLEO PURO — sem st.*, testável sem Streamlit nem Supabase
# =====================================================================

def resolver_acesso(email, nome, linha, admins_break_glass, fonte_ok) -> Acesso:
    """
    Decide o acesso de quem acabou de autenticar no Google.

    `linha` é o registro de app.usuario (None = e-mail não convidado) e
    `fonte_ok` diz se a allowlist pôde ser lida. A ordem das regras importa:
    o break-glass passa ANTES do fail-closed, senão um Supabase fora do ar
    trancaria também quem deveria consertá-lo.
    """
    email = normalizar_email(email)
    nome = str(nome or "").strip()

    if not email:
        return Acesso(NAO_AUTORIZADO, "", nome, "")

    if email in {normalizar_email(e) for e in (admins_break_glass or [])}:
        return Acesso(AUTORIZADO, "admin", nome or email, email)

    if not fonte_ok:
        return Acesso(INDISPONIVEL, "", nome, email)

    if not linha:
        return Acesso(NAO_AUTORIZADO, "", nome, email)

    if not linha.get("ativo"):
        return Acesso(INATIVO, "", nome, email)

    role = linha.get("role")
    if role not in ROLES_VALIDOS:
        # Lixo no banco (role de uma versão antiga, edição manual): trata como
        # não autorizado em vez de deixar passar com perfil indefinido.
        return Acesso(NAO_AUTORIZADO, "", nome, email)

    return Acesso(AUTORIZADO, role, nome or linha.get("nome") or email, email)


def paginas_do_role(role):
    """
    Títulos das páginas visíveis, ou None para "todas".

    Perfil desconhecido devolve tupla VAZIA, nunca None: o `.get()` que existia
    antes devolvia None para role fora do dicionário — o mesmo valor que
    significa "libera tudo".
    """
    if role in PAGINAS_POR_ROLE:
        return PAGINAS_POR_ROLE[role]
    return ()


def validar_edicao_usuarios(novas: list, atuais: list, email_logado: str) -> list:
    """
    Erros que impedem gravar a edição da aba Usuários ([] = pode salvar).

    As duas primeiras regras existem para que ninguém se tranque para fora com
    um clique — recuperar exige mexer direto no banco ou no break-glass.
    """
    erros = []
    email_logado = normalizar_email(email_logado)

    vistos = set()
    for linha in novas:
        email = normalizar_email(linha.get("email"))
        if not email:
            erros.append("Há uma linha sem e-mail.")
            continue
        if email in vistos:
            erros.append(f"E-mail repetido: {email}")
        vistos.add(email)
        if linha.get("role") not in ROLES_VALIDOS:
            erros.append(f"Perfil inválido para {email}: {linha.get('role')}")
        if len(str(linha.get("nome") or "")) > 120:
            erros.append(f"Nome muito longo para {email} (máx. 120 caracteres).")

    if not [l for l in novas if l.get("role") == "admin" and l.get("ativo")]:
        erros.append("A operação deixaria o sistema sem nenhum administrador ativo.")

    era_admin = any(normalizar_email(l.get("email")) == email_logado
                    and l.get("role") == "admin" and l.get("ativo")
                    for l in atuais)
    if era_admin:
        continua = any(normalizar_email(l.get("email")) == email_logado
                       and l.get("role") == "admin" and l.get("ativo")
                       for l in novas)
        if not continua:
            erros.append("Você não pode remover o próprio acesso de administrador — "
                         "peça a outro administrador.")

    return erros


# =====================================================================
# INTEGRAÇÃO COM O STREAMLIT
# =====================================================================

@st.cache_data(ttl=TTL_ALLOWLIST, show_spinner=False)
def _carregar_allowlist():
    """
    ({email: linha}, fonte_ok) — a tabela INTEIRA, não uma consulta por e-mail.

    São poucas linhas e o cache_data é global entre sessões: uma query a cada
    5 min para o app todo. E a invalidação vira global — ao salvar na aba
    Usuários, o próximo rerun de qualquer sessão já lê o estado novo, sem
    esperar o TTL.
    """
    return obter_repositorio_usuarios().mapa_por_email()


def invalidar_cache_usuarios():
    """Chamado pela aba Usuários após gravar. Direcionado de propósito: um
    st.cache_data.clear() global levaria junto o cache de dados de 1h."""
    _carregar_allowlist.clear()


def _admins_break_glass() -> set:
    return {normalizar_email(e)
            for e in st.secrets.get("acesso", {}).get("admins", [])}


def _resolver_sessao() -> Acesso:
    mapa, fonte_ok = _carregar_allowlist()
    email = normalizar_email(st.user.get("email"))
    acesso = resolver_acesso(email, st.user.get("name"), mapa.get(email),
                             _admins_break_glass(), fonte_ok)

    # Fail-closed para sessão nova; fail-soft dentro de uma sessão já
    # autorizada. A página de Configurações chama st.cache_data.clear() em
    # vários pontos: sem isto, um piscar do Supabase logo depois de "Salvar
    # parâmetros" expulsaria o próprio admin no meio da edição.
    ultimo = st.session_state.get("_aku_acesso")
    if acesso.estado == INDISPONIVEL and ultimo and ultimo.email == acesso.email:
        st.sidebar.warning("Sem contato com o banco — usando seu último acesso conhecido.")
        return ultimo
    return acesso


def _tela_login():
    _, meio, _ = st.columns([1, 2, 1])
    with meio:
        st.image("assets/aku-favicon.png", width=64)
        st.title("AKU Hub")
        st.caption("Acesso restrito. Entre com a conta Google autorizada.")
        if st.button("Entrar com Google", type="primary", width="stretch"):
            try:
                st.login("google")
            except Exception as e:
                # Authlib ausente no deploy, [auth]/[auth.google] incompletos:
                # mensagem legível em vez de traceback na cara do usuário.
                st.error("Autenticação indisponível. Avise o administrador — "
                         f"a configuração do login precisa ser revista. ({e})")


def _tela_bloqueio(acesso: Acesso):
    _, meio, _ = st.columns([1, 2, 1])
    with meio:
        st.title("AKU Hub")
        if acesso.estado == INATIVO:
            st.warning("Seu acesso ao AKU Hub foi desativado.")
        elif acesso.estado == INDISPONIVEL:
            st.error("Não foi possível validar seu acesso agora. Tente de novo em "
                     "instantes; se persistir, avise o administrador.")
        else:
            st.error("Este e-mail não tem acesso ao AKU Hub. "
                     "Peça a um administrador para liberá-lo.")
        # O e-mail visível é essencial: o caso comum é ter entrado com a conta
        # Google errada, e sem ele a pessoa fica presa ao cookie sem entender.
        st.caption(f"Conectado como **{acesso.email or 'desconhecido'}**")
        if st.button("Sair", width="stretch"):
            st.logout()


def _registrar_acesso_uma_vez(email: str):
    if st.session_state.get("_aku_acesso_registrado") == email:
        return
    st.session_state["_aku_acesso_registrado"] = email
    obter_repositorio_usuarios().registrar_acesso(email)


def verificar_acesso():
    """
    Portão principal — chamado pelo app.py a cada execução, antes da navegação.

    Retorna (nome, email, role) se autorizado; renderiza a tela do estado e
    st.stop() caso contrário. O 2º elemento é o E-MAIL (antes era o username do
    streamlit-authenticator): é ele que alimenta as colunas de auditoria.
    """
    if not st.user.is_logged_in:
        _tela_login()
        st.stop()

    acesso = _resolver_sessao()
    if acesso.estado != AUTORIZADO:
        _tela_bloqueio(acesso)
        st.stop()

    st.session_state["_aku_acesso"] = acesso
    _registrar_acesso_uma_vez(acesso.email)

    with st.sidebar:
        st.write(f"👤 **{acesso.nome}**")
        st.caption(acesso.email)
        if st.button("Sair", key="_aku_sair"):
            st.logout()

    return acesso.nome, acesso.email, acesso.role


def exigir_login():
    """Guarda para páginas individuais (defesa em profundidade — o app.py já
    passou por verificar_acesso() antes de qualquer página rodar)."""
    if not st.session_state.get("_aku_acesso"):
        st.error("Acesso negado. Faça login pela página principal.")
        st.stop()


def identidade_atual():
    """
    (nome, email, role) do session_state, sem reconsultar a allowlist.

    Existia para evitar um 2º CookieManager do streamlit-authenticator; esse
    motivo acabou (st.user vem do cookie lido no handshake do websocket, não
    instancia widget). Continua como leitura barata para as páginas.
    """
    exigir_login()
    a = st.session_state["_aku_acesso"]
    return a.nome, a.email, a.role


def e_admin() -> bool:
    """Gate NÃO bloqueante — para trechos de página que só existem para admin."""
    return bool(st.session_state.get("_aku_acesso")) and \
        st.session_state["_aku_acesso"].role == "admin"


def exigir_admin():
    """Gate bloqueante de página inteira. Retorna (nome, email, role)."""
    nome, email, role = identidade_atual()
    if role != "admin":
        st.error("⛔ Acesso negado. Apenas administradores podem acessar esta página.")
        st.stop()
    return nome, email, role
