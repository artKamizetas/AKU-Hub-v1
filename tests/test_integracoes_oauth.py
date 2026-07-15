"""
Testes do fluxo OAuth2 (pedidos/integracoes/oauth.py) com HTTP fake —
sem rede. O RepoFake de integrações vem do test_integracoes_repositorio.
"""

import pandas as pd
import pytest

from pedidos.integracoes import oauth
from pedidos.integracoes.repositorio import IntegracaoNaoConectada
from tests.test_integracoes_repositorio import RepoFake


class RespostaFake:
    def __init__(self, status_code=200, corpo=None, text=""):
        self.status_code = status_code
        self._corpo = corpo or {}
        self.text = text

    def json(self):
        return self._corpo


class HttpFake:
    """Captura o POST e devolve a resposta programada."""

    def __init__(self, resposta: RespostaFake):
        self.resposta = resposta
        self.chamadas = []   # [(url, data, auth)]

    def post(self, url, data=None, auth=None):
        self.chamadas.append((url, dict(data or {}), auth))
        return self.resposta


def _resposta_token(access="acc-1", refresh="ref-1", expires_in=3600):
    return RespostaFake(200, {"access_token": access, "refresh_token": refresh,
                              "expires_in": expires_in})


class TestAuthorizeUrl:
    def test_bling_com_state_e_redirect(self):
        url = oauth.montar_authorize_url("bling", "cid", "https://app/configuracoes", "st-1")
        assert url.startswith(oauth.PLATAFORMAS["bling"]["authorize"] + "?")
        assert "response_type=code" in url and "client_id=cid" in url
        assert "state=st-1" in url
        assert "scope" not in url

    def test_olist_inclui_scope_openid(self):
        url = oauth.montar_authorize_url("olist", "cid", "https://app/configuracoes", "st-2")
        assert "scope=openid" in url
        assert "accounts.tiny.com.br" in url


class TestTrocarCode:
    def test_bling_usa_basic_auth_sem_redirect_no_body(self):
        http = HttpFake(_resposta_token())
        tokens = oauth.trocar_code("bling", "cid", "sec", "code-1",
                                   "https://app/configuracoes", http)
        url, data, auth = http.chamadas[0]
        assert auth == ("cid", "sec")
        assert data == {"grant_type": "authorization_code", "code": "code-1"}
        assert tokens["access_token"] == "acc-1"
        assert tokens["refresh_token"] == "ref-1"

    def test_olist_credenciais_no_body_com_redirect(self):
        http = HttpFake(_resposta_token())
        oauth.trocar_code("olist", "cid", "sec", "code-2",
                          "https://app/configuracoes", http)
        url, data, auth = http.chamadas[0]
        assert auth is None
        assert data["client_id"] == "cid" and data["client_secret"] == "sec"
        assert data["redirect_uri"] == "https://app/configuracoes"

    def test_expira_em_futuro_iso(self):
        http = HttpFake(_resposta_token(expires_in=3600))
        tokens = oauth.trocar_code("bling", "c", "s", "x", "https://r", http)
        expira = pd.Timestamp(tokens["expira_em"])
        assert expira > pd.Timestamp.now(tz="UTC") + pd.Timedelta(minutes=50)

    def test_resposta_nao_2xx_levanta(self):
        http = HttpFake(RespostaFake(400, text="invalid_grant"))
        with pytest.raises(oauth.OAuthFalhou, match="400"):
            oauth.trocar_code("bling", "c", "s", "x", "https://r", http)


class TestTokenValido:
    def test_valido_com_margem(self):
        integ = {"access_token": "a",
                 "token_expira_em": (pd.Timestamp.now(tz="UTC")
                                     + pd.Timedelta(hours=1)).isoformat()}
        assert oauth.token_valido(integ)

    def test_dentro_da_margem_e_invalido(self):
        integ = {"access_token": "a",
                 "token_expira_em": (pd.Timestamp.now(tz="UTC")
                                     + pd.Timedelta(seconds=60)).isoformat()}
        assert not oauth.token_valido(integ)   # margem default 120s

    def test_sem_token_ou_sem_expiracao(self):
        assert not oauth.token_valido({})
        assert not oauth.token_valido({"access_token": "a"})


class TestObterAccessToken:
    def _repo_conectado(self, expira_delta):
        repo = RepoFake()
        repo.salvar_chaves("bling", "cid", "sec", "https://r", "t")
        repo.concluir_oauth(
            "bling", "acc-atual", "ref-atual",
            (pd.Timestamp.now(tz="UTC") + expira_delta).isoformat(), "t")
        return repo

    def test_token_valido_retorna_sem_renovar(self):
        repo = self._repo_conectado(pd.Timedelta(hours=1))
        http = HttpFake(_resposta_token())
        assert oauth.obter_access_token("bling", repo, http) == "acc-atual"
        assert http.chamadas == []   # não tocou a rede

    def test_expirado_renova_persiste_e_loga(self):
        repo = self._repo_conectado(pd.Timedelta(seconds=-10))
        http = HttpFake(_resposta_token(access="acc-novo", refresh="ref-novo"))
        assert oauth.obter_access_token("bling", repo, http) == "acc-novo"
        integ = repo.ler("bling")
        assert integ["access_token"] == "acc-novo"
        assert integ["refresh_token"] == "ref-novo"
        eventos = repo.listar_eventos()
        assert eventos.iloc[0]["acao"] == "oauth_refresh"
        assert bool(eventos.iloc[0]["sucesso"]) is True

    def test_nunca_conectado_levanta(self):
        with pytest.raises(IntegracaoNaoConectada, match="não está conectado"):
            oauth.obter_access_token("bling", RepoFake(), HttpFake(_resposta_token()))

    def test_refresh_recusado_levanta_e_loga(self):
        repo = self._repo_conectado(pd.Timedelta(seconds=-10))
        http = HttpFake(RespostaFake(401, text="invalid_grant"))
        with pytest.raises(IntegracaoNaoConectada, match="reconecte"):
            oauth.obter_access_token("bling", repo, http)
        eventos = repo.listar_eventos()
        assert eventos.iloc[0]["acao"] == "oauth_refresh"
        assert bool(eventos.iloc[0]["sucesso"]) is False
