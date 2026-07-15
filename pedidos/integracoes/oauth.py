"""
oauth.py — Fluxo OAuth2 (authorization_code + refresh) das duas plataformas.

Genérico e parametrizado por PLATAFORMAS; sem Streamlit; cliente HTTP
injetável (`http`) para os testes — default httpx.

Peculiaridade do Streamlit que molda o desenho: após o redirect de
autorização o app abre uma SESSÃO NOVA (session_state morre), então o
`state` anti-CSRF vive no banco (app.integracao.oauth_state) e o callback
identifica a plataforma via repositorio.buscar_por_state().

URLs/formatos marcados p/ confirmação na 1ª conexão real — ajustar aqui é
o único ponto de mudança.
"""

import secrets as _secrets
from urllib.parse import urlencode

import pandas as pd

from pedidos.integracoes.repositorio import IntegracaoNaoConectada


# Fonte única dos endpoints/formatos OAuth por plataforma.
# auth_token: como client_id/secret vão na troca de token —
#   "basic" = header Authorization Basic (Bling)
#   "body"  = campos no corpo x-www-form-urlencoded (Keycloak/Olist)
PLATAFORMAS = {
    "bling": {
        "authorize": "https://www.bling.com.br/Api/v3/oauth/authorize",
        "token": "https://www.bling.com.br/Api/v3/oauth/token",
        "auth_token": "basic",
        "scope": None,
        "redirect_na_troca": False,   # Bling usa o redirect registrado no portal
    },
    "olist": {
        "authorize": "https://accounts.tiny.com.br/realms/tiny/protocol/openid-connect/auth",
        "token": "https://accounts.tiny.com.br/realms/tiny/protocol/openid-connect/token",
        "auth_token": "body",
        "scope": "openid",
        "redirect_na_troca": True,    # Keycloak exige redirect_uri também na troca
    },
}

MARGEM_EXPIRACAO_S = 120   # renova o access token este tanto antes de vencer


class OAuthFalhou(Exception):
    """Erro na troca/renovação de token (resposta não-2xx da plataforma)."""


def _http_default():
    import httpx
    return httpx.Client(timeout=30)


def gerar_state() -> str:
    return _secrets.token_urlsafe(32)


def montar_authorize_url(plataforma: str, client_id: str, redirect_uri: str,
                         state: str) -> str:
    """URL para o usuário autorizar o app na plataforma (abre no navegador)."""
    cfg = PLATAFORMAS[plataforma]
    params = {"response_type": "code", "client_id": client_id, "state": state,
              "redirect_uri": redirect_uri}
    if cfg["scope"]:
        params["scope"] = cfg["scope"]
    return f"{cfg['authorize']}?{urlencode(params)}"


def _post_token(plataforma: str, client_id: str, client_secret: str,
                data: dict, http=None) -> dict:
    """POST no endpoint de token; normaliza a resposta p/ o nosso formato."""
    cfg = PLATAFORMAS[plataforma]
    http = http or _http_default()

    kwargs = {"data": dict(data)}
    if cfg["auth_token"] == "basic":
        kwargs["auth"] = (client_id, client_secret)
    else:
        kwargs["data"].update({"client_id": client_id, "client_secret": client_secret})

    resp = http.post(cfg["token"], **kwargs)
    if resp.status_code >= 300:
        raise OAuthFalhou(
            f"{plataforma}: endpoint de token retornou {resp.status_code} — "
            f"{str(resp.text)[:300]}")

    corpo = resp.json()
    expira_em = (pd.Timestamp.now(tz="UTC")
                 + pd.Timedelta(seconds=int(corpo.get("expires_in", 0)))).isoformat()
    return {
        "access_token": corpo["access_token"],
        "refresh_token": corpo.get("refresh_token", ""),
        "expira_em": expira_em,
    }


def trocar_code(plataforma: str, client_id: str, client_secret: str, code: str,
                redirect_uri: str, http=None) -> dict:
    """authorization_code → tokens. Retorna {access_token, refresh_token, expira_em}."""
    data = {"grant_type": "authorization_code", "code": code}
    if PLATAFORMAS[plataforma]["redirect_na_troca"]:
        data["redirect_uri"] = redirect_uri
    return _post_token(plataforma, client_id, client_secret, data, http)


def renovar_token(plataforma: str, client_id: str, client_secret: str,
                  refresh_token: str, http=None) -> dict:
    """refresh_token → novo access (e possivelmente novo refresh)."""
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    return _post_token(plataforma, client_id, client_secret, data, http)


def token_valido(integ: dict, margem_s: int = MARGEM_EXPIRACAO_S) -> bool:
    """True se o access token existe e ainda vale por > margem_s segundos."""
    if not integ.get("access_token") or not integ.get("token_expira_em"):
        return False
    expira = pd.Timestamp(str(integ["token_expira_em"]))
    if expira.tzinfo is None:
        expira = expira.tz_localize("UTC")
    return expira > pd.Timestamp.now(tz="UTC") + pd.Timedelta(seconds=margem_s)


def obter_access_token(plataforma: str, repo_int, http=None) -> str:
    """
    Access token válido da plataforma, renovando (e persistindo) se expirado.
    IntegracaoNaoConectada se nunca conectado ou refresh recusado (→ UI
    mostra "Reconectar").
    """
    integ = repo_int.ler(plataforma)
    if not integ or not integ.get("refresh_token"):
        raise IntegracaoNaoConectada(
            f"{plataforma} não está conectado — conecte na aba Integrações.")

    if token_valido(integ):
        return integ["access_token"]

    try:
        tokens = renovar_token(plataforma, integ.get("client_id", ""),
                               integ.get("client_secret", ""),
                               integ["refresh_token"], http)
    except OAuthFalhou as exc:
        repo_int.registrar_evento(plataforma, "oauth_refresh", False,
                                  detalhe={"erro": str(exc)[:300]})
        raise IntegracaoNaoConectada(
            f"{plataforma}: renovação de token recusada — reconecte na aba "
            f"Integrações. ({exc})") from exc

    repo_int.atualizar_tokens(plataforma, tokens["access_token"],
                              tokens["refresh_token"], tokens["expira_em"])
    repo_int.registrar_evento(plataforma, "oauth_refresh", True)
    return tokens["access_token"]
