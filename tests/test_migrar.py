"""
Testes do runner de migrações (scripts/migrar.py) — lógica pura + comandos com
um cliente fake da Management API (zero rede).

scripts/ não é pacote; carrega-se o módulo pelo caminho.
"""
import importlib.util
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("migrar", RAIZ / "scripts" / "migrar.py")
migrar = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrar)


class ClienteFake:
    """Registra o SQL enviado; responde à leitura do ledger com `ledger`."""

    def __init__(self, ledger=None):
        self.executados = []                 # todo SQL, em ordem
        self._ledger = dict(ledger or {})    # versao -> checksum

    def executar(self, sql):
        self.executados.append(sql)
        if sql.strip().lower().startswith("select versao"):
            return [{"versao": v, "checksum": c} for v, c in self._ledger.items()]
        return []

    @property
    def sql_concat(self):
        return "\n".join(self.executados)


def _fp(nome):
    return migrar.DIR_SQL / nome


class TestLogicaPura:
    def test_ref_de_url(self):
        assert migrar._ref_de_url("https://abcd1234.supabase.co") == "abcd1234"
        assert migrar._ref_de_url("https://abcd1234.supabase.co/rest/v1") == "abcd1234"
        assert migrar._ref_de_url("") is None
        assert migrar._ref_de_url(None) is None

    def test_checksum_estavel_e_sensivel(self):
        assert migrar.checksum("abc") == migrar.checksum("abc")
        assert migrar.checksum("abc") != migrar.checksum("abd")

    def test_pendentes_ignora_aplicadas(self):
        arqs = [_fp("001_app_pedidos.sql"), _fp("002_app_parametros.sql"),
                _fp("003_app_integracoes.sql")]
        pend = migrar.pendentes(arqs, {"001_app_pedidos.sql", "002_app_parametros.sql"})
        assert [p.name for p in pend] == ["003_app_integracoes.sql"]

    def test_sql_aplicar_e_atomico_e_registra(self):
        sql = migrar.sql_aplicar("003_app_integracoes.sql", "create table x();", "deadbeef")
        assert sql.startswith("begin;")
        assert sql.rstrip().endswith("commit;")
        assert "create table x();" in sql
        assert "insert into app.schema_migrations" in sql
        assert "'003_app_integracoes.sql'" in sql

    def test_lit_escapa_aspas(self):
        assert migrar._lit("o'brien") == "'o''brien'"


class TestCredenciais:
    def test_ler_dotenv_parseia(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text(
            "# comentário\n"
            "\n"
            'export SUPABASE_ACCESS_TOKEN="sbp_abc"\n'
            "SUPABASE_PROJECT_REF='meuref'\n"
            "SEM_ASPAS=valor123\n",
            encoding="utf-8",
        )
        d = migrar._ler_dotenv(f)
        assert d["SUPABASE_ACCESS_TOKEN"] == "sbp_abc"   # aspas e `export ` removidos
        assert d["SUPABASE_PROJECT_REF"] == "meuref"
        assert d["SEM_ASPAS"] == "valor123"

    def test_ler_dotenv_inexistente_vazio(self, tmp_path):
        assert migrar._ler_dotenv(tmp_path / "nao_existe.env") == {}

    def test_ambiente_vence_o_dotenv(self, tmp_path, monkeypatch):
        # .env diz uma coisa; a variável de ambiente tem prioridade
        env = tmp_path / ".env"
        env.write_text("SUPABASE_ACCESS_TOKEN=do-arquivo\n", encoding="utf-8")
        monkeypatch.setattr(migrar, "DOTENV", env)
        monkeypatch.setenv("SUPABASE_ACCESS_TOKEN", "do-ambiente")
        monkeypatch.setenv("SUPABASE_PROJECT_REF", "ref-ambiente")
        assert migrar.carregar_credenciais() == ("do-ambiente", "ref-ambiente")

    def test_cai_no_dotenv_quando_sem_ambiente(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text(
            "SUPABASE_ACCESS_TOKEN=sbp_do_env\nSUPABASE_PROJECT_REF=ref_do_env\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(migrar, "DOTENV", env)
        monkeypatch.delenv("SUPABASE_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("SUPABASE_PROJECT_REF", raising=False)
        assert migrar.carregar_credenciais() == ("sbp_do_env", "ref_do_env")

    def test_sem_token_em_lugar_nenhum_estoura(self, tmp_path, monkeypatch):
        monkeypatch.setattr(migrar, "DOTENV", tmp_path / ".env")        # não existe
        monkeypatch.setattr(migrar, "SECRETS", tmp_path / "secrets.toml")  # não existe
        monkeypatch.delenv("SUPABASE_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("SUPABASE_PROJECT_REF", raising=False)
        with pytest.raises(migrar.MigracaoErro, match="SUPABASE_ACCESS_TOKEN"):
            migrar.carregar_credenciais()


class TestAplicar:
    def test_aplica_pendentes_e_registra_no_ledger(self, capsys):
        # ledger com 001/002 já baselined → só 003 é pendente
        reg = {"001_app_pedidos.sql": "x", "002_app_parametros.sql": "y"}
        cli = ClienteFake(ledger=reg)
        migrar.cmd_aplicar(cli)
        # bootstrap + leitura do ledger + exatamente 1 'begin;' (só o 003)
        assert cli.executados[0] == migrar.SQL_BOOTSTRAP
        begins = [s for s in cli.executados if s.startswith("begin;")]
        assert len(begins) == 1
        assert "003_app_integracoes.sql" in begins[0]
        assert "insert into app.schema_migrations" in begins[0]

    def test_dry_run_nao_executa_migracao(self):
        cli = ClienteFake(ledger={})
        migrar.cmd_aplicar(cli, dry_run=True)
        assert not any(s.startswith("begin;") for s in cli.executados)

    def test_nada_pendente(self, capsys):
        aplicadas = {a.name: "z" for a in migrar.listar_arquivos()}
        cli = ClienteFake(ledger=aplicadas)
        migrar.cmd_aplicar(cli)
        assert not any(s.startswith("begin;") for s in cli.executados)
        assert "Nada pendente" in capsys.readouterr().out


class TestMarcar:
    def test_registra_sem_rodar_o_conteudo(self):
        cli = ClienteFake()
        migrar.cmd_marcar(cli, ["001_app_pedidos.sql"])
        # registra a versao...
        assert any("'001_app_pedidos.sql'" in s and "insert into app.schema_migrations" in s
                   for s in cli.executados)
        # ...mas NUNCA envia o DDL do arquivo
        conteudo = _fp("001_app_pedidos.sql").read_text(encoding="utf-8")
        assert conteudo not in cli.sql_concat
        assert not any(s.startswith("begin;") for s in cli.executados)

    def test_arquivo_inexistente_estoura(self):
        with pytest.raises(migrar.MigracaoErro):
            migrar.cmd_marcar(ClienteFake(), ["999_nao_existe.sql"])


class TestClienteManagement:
    def test_erro_http_vira_migracao_erro(self):
        class RespErro:
            status_code = 403
            def json(self):
                return {"message": "Unauthorized"}

        class HttpFake:
            def post(self, *a, **k):
                return RespErro()

        cli = migrar.ClienteManagement("tok", "ref", http=HttpFake())
        with pytest.raises(migrar.MigracaoErro, match="403"):
            cli.executar("select 1;")

    def test_sucesso_devolve_linhas(self):
        class RespOk:
            status_code = 200
            def json(self):
                return [{"versao": "001_app_pedidos.sql", "checksum": "abc"}]

        class HttpFake:
            def __init__(self):
                self.chamadas = []
            def post(self, url, headers=None, json=None, timeout=None):
                self.chamadas.append((url, headers, json))
                return RespOk()

        http = HttpFake()
        cli = migrar.ClienteManagement("tok-123", "ref-xyz", http=http)
        linhas = cli.executar(migrar.SQL_LER_LEDGER)
        assert linhas == [{"versao": "001_app_pedidos.sql", "checksum": "abc"}]
        url, headers, body = http.chamadas[0]
        assert url.endswith("/projects/ref-xyz/database/query")
        assert headers["Authorization"] == "Bearer tok-123"
        assert body == {"query": migrar.SQL_LER_LEDGER}
