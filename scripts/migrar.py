"""
migrar.py — Aplica as migrações DDL de docs/sql/ no Supabase via Management API.

Elimina o copiar-e-colar no SQL Editor. Roda FORA do app (sua máquina ou CI),
com um Personal Access Token do Supabase — credencial poderosa que NUNCA vai
para os secrets do app (o dashboard segue só com a service_key do PostgREST,
CRUD-only). Menor privilégio: o processo que atende cliques não carrega uma
chave capaz de DROP TABLE.

Por que Management API e não a service_key: a service_key fala com o PostgREST,
que faz CRUD e NÃO roda DDL (CREATE/ALTER TABLE). A Management API executa SQL
bruto em https://api.supabase.com/v1/projects/{ref}/database/query (o mesmo motor
do SQL Editor).

Credenciais (variável de ambiente vence; se faltar, cai no arquivo .env da raiz,
que é git-ignored — copie .env.example para .env e preencha):
  - Token   → SUPABASE_ACCESS_TOKEN  (obrigatório; gere em Supabase → Account →
              Access Tokens). No .env ou no ambiente; NUNCA em
              .streamlit/secrets.toml (iria para o app).
  - Projeto → SUPABASE_PROJECT_REF  ou, se ausente, extraído de
              .streamlit/secrets.toml [supabase].url (o ref não é segredo — está
              na URL pública https://<ref>.supabase.co).

Uso (da raiz do repo):
    python scripts/migrar.py                 # status: aplicadas × pendentes
    python scripts/migrar.py aplicar         # roda as pendentes, em ordem
    python scripts/migrar.py aplicar --dry-run
    python scripts/migrar.py marcar 001_app_pedidos.sql 002_app_parametros.sql
        # BASELINE: registra como aplicadas SEM rodar — para as migrações que
        # você já aplicou à mão no SQL Editor (senão o runner tentaria recriar
        # objetos que já existem).

Ledger em app.schema_migrations (versao = nome do arquivo). Cada 'aplicar' roda o
arquivo E registra o ledger na MESMA transação (begin/commit) — falhou, nada
persiste. As migrações precisam ser transaction-safe (sem CREATE INDEX
CONCURRENTLY & cia.; nenhuma das atuais usa).
"""
import argparse
import hashlib
import os
import sys
import tomllib
from pathlib import Path

import httpx

RAIZ = Path(__file__).resolve().parent.parent
DIR_SQL = RAIZ / "docs" / "sql"
SECRETS = RAIZ / ".streamlit" / "secrets.toml"
DOTENV = RAIZ / ".env"
MANAGEMENT_BASE = "https://api.supabase.com/v1"
TABELA_LEDGER = "app.schema_migrations"


class MigracaoErro(Exception):
    """Erro de credencial, de arquivo ou vindo da Management API."""


# ---------------------------------------------------------------------------
# Credenciais (token do ambiente; ref pode vir do secrets.toml — não é segredo)
# ---------------------------------------------------------------------------
def _ref_de_url(url: str) -> str | None:
    """https://<ref>.supabase.co → <ref>."""
    host = (url or "").split("://")[-1].split("/")[0]
    ref = host.split(".")[0] if host else ""
    return ref or None


def _ref_do_secrets() -> str | None:
    """Lê [supabase].url de .streamlit/secrets.toml sem importar streamlit."""
    if not SECRETS.exists():
        return None
    try:
        dados = tomllib.loads(SECRETS.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return None
    return _ref_de_url(dados.get("supabase", {}).get("url", ""))


def _ler_dotenv(arq: Path = None) -> dict:
    """Parser mínimo de KEY=VALUE do .env da raiz (git-ignored). {} se não existe.

    Aceita linhas em branco, comentários (#), prefixo `export ` e aspas simples/
    duplas ao redor do valor. Não faz interpolação — é só para o token/ref.
    """
    arq = arq or DOTENV
    if not arq.exists():
        return {}
    out = {}
    for linha in arq.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#"):
            continue
        if linha.startswith("export "):
            linha = linha[len("export "):]
        if "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        chave, valor = chave.strip(), valor.strip()
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in ("'", '"'):
            valor = valor[1:-1]   # remove um par de aspas ao redor
        if chave:
            out[chave] = valor
    return out


def carregar_credenciais() -> tuple[str, str]:
    dotenv = _ler_dotenv()
    token = (os.environ.get("SUPABASE_ACCESS_TOKEN")
             or dotenv.get("SUPABASE_ACCESS_TOKEN") or "").strip()
    ref = (os.environ.get("SUPABASE_PROJECT_REF")
           or dotenv.get("SUPABASE_PROJECT_REF") or "").strip() or _ref_do_secrets()
    if not token:
        raise MigracaoErro(
            "Defina SUPABASE_ACCESS_TOKEN (Supabase → Account → Access Tokens).\n"
            "Jeito recomendado — arquivo .env na raiz do repo (git-ignored):\n"
            "    cp .env.example .env      # e preencha o token\n"
            "Ou no ambiente:  export SUPABASE_ACCESS_TOKEN=sbp_xxx\n"
            "NUNCA coloque esse token em .streamlit/secrets.toml — ele não pode ir "
            "para o app."
        )
    if not ref:
        raise MigracaoErro(
            "Project ref não encontrado. Defina SUPABASE_PROJECT_REF ou configure "
            "[supabase].url em .streamlit/secrets.toml."
        )
    return token, ref


# ---------------------------------------------------------------------------
# Cliente da Management API (http injetável nos testes)
# ---------------------------------------------------------------------------
class ClienteManagement:
    def __init__(self, token: str, ref: str, http=None):
        self._token = token
        self._ref = ref
        self._http = http or httpx

    def executar(self, sql: str) -> list:
        """POST de SQL bruto. Devolve as linhas (lista) ou estoura MigracaoErro."""
        url = f"{MANAGEMENT_BASE}/projects/{self._ref}/database/query"
        resp = self._http.post(
            url,
            headers={"Authorization": f"Bearer {self._token}"},
            json={"query": sql},
            timeout=60,
        )
        if resp.status_code >= 300:
            raise MigracaoErro(f"Management API {resp.status_code}: {_extrair_erro(resp)}")
        try:
            corpo = resp.json()
        except ValueError:
            return []
        return corpo if isinstance(corpo, list) else []


def _extrair_erro(resp) -> str:
    try:
        corpo = resp.json()
    except ValueError:
        return (getattr(resp, "text", "") or "").strip()[:500]
    if isinstance(corpo, dict):
        return corpo.get("message") or corpo.get("error") or str(corpo)
    return str(corpo)


# ---------------------------------------------------------------------------
# Lógica pura (testável sem rede)
# ---------------------------------------------------------------------------
def listar_arquivos() -> list[Path]:
    return sorted(DIR_SQL.glob("*.sql"))


def checksum(conteudo: str) -> str:
    return hashlib.sha256(conteudo.encode("utf-8")).hexdigest()


def pendentes(arquivos: list[Path], aplicadas: set[str]) -> list[Path]:
    return [a for a in arquivos if a.name not in aplicadas]


def _lit(valor: str) -> str:
    """Literal SQL com aspas simples escapadas."""
    return "'" + valor.replace("'", "''") + "'"


SQL_BOOTSTRAP = f"""
create schema if not exists app;
create table if not exists {TABELA_LEDGER} (
  versao      text primary key,
  aplicado_em timestamptz not null default now(),
  checksum    text
);
""".strip()

SQL_LER_LEDGER = f"select versao, checksum from {TABELA_LEDGER} order by versao;"


def sql_registrar(versao: str, sha: str) -> str:
    return (
        f"insert into {TABELA_LEDGER} (versao, checksum) "
        f"values ({_lit(versao)}, {_lit(sha)}) "
        f"on conflict (versao) do update set "
        f"checksum = excluded.checksum, aplicado_em = now();"
    )


def sql_aplicar(versao: str, conteudo: str, sha: str) -> str:
    """Migração + registro no ledger na MESMA transação (atômico)."""
    return f"begin;\n{conteudo.strip()}\n{sql_registrar(versao, sha)}\ncommit;"


def ler_ledger(cliente) -> dict:
    """versao -> checksum das migrações já registradas (após bootstrap)."""
    linhas = cliente.executar(SQL_LER_LEDGER) or []
    return {l["versao"]: l.get("checksum") for l in linhas}


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------
def cmd_status(cliente) -> list[Path]:
    cliente.executar(SQL_BOOTSTRAP)
    reg = ler_ledger(cliente)
    arqs = listar_arquivos()
    print(f"Migrações em {DIR_SQL.relative_to(RAIZ)} · ledger {TABELA_LEDGER}\n")
    for a in arqs:
        sha = checksum(a.read_text(encoding="utf-8"))
        if a.name in reg:
            drift = "" if reg[a.name] == sha else "   ⚠️ conteúdo mudou desde a aplicação"
            print(f"  ✅ {a.name}{drift}")
        else:
            print(f"  ⬜ {a.name}   (pendente)")
    pend = pendentes(arqs, set(reg))
    print(f"\n{len(pend)} pendente(s) · {len(reg)} aplicada(s).")
    if pend:
        print("Rode:  python scripts/migrar.py aplicar")
    return pend


def cmd_aplicar(cliente, dry_run: bool = False) -> None:
    cliente.executar(SQL_BOOTSTRAP)
    reg = ler_ledger(cliente)
    pend = pendentes(listar_arquivos(), set(reg))
    if not pend:
        print("Nada pendente. ✅")
        return
    print(f"{len(pend)} migração(ões) pendente(s):")
    for a in pend:
        print(f"  → {a.name}")
    if dry_run:
        print("\n(dry-run: nada foi executado)")
        return
    for a in pend:
        conteudo = a.read_text(encoding="utf-8")
        print(f"\nAplicando {a.name} …", end=" ", flush=True)
        cliente.executar(sql_aplicar(a.name, conteudo, checksum(conteudo)))
        print("ok")
    print("\n✅ Todas as pendentes aplicadas.")


def cmd_marcar(cliente, nomes: list[str]) -> None:
    cliente.executar(SQL_BOOTSTRAP)
    validos = {a.name for a in listar_arquivos()}
    for nome in nomes:
        if nome not in validos:
            raise MigracaoErro(
                f"'{nome}' não existe em {DIR_SQL.relative_to(RAIZ)}. "
                f"Opções: {', '.join(sorted(validos))}"
            )
    for nome in nomes:
        conteudo = (DIR_SQL / nome).read_text(encoding="utf-8")
        cliente.executar(sql_registrar(nome, checksum(conteudo)))
        print(f"  baseline (registrada sem rodar): {nome}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(
        description="Migrações DDL do schema `app` via Supabase Management API."
    )
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("status", help="lista aplicadas × pendentes (padrão)")
    pa = sub.add_parser("aplicar", help="roda as migrações pendentes, em ordem")
    pa.add_argument("--dry-run", action="store_true",
                    help="mostra o que rodaria, sem executar")
    pm = sub.add_parser("marcar", help="registra arquivos como aplicados SEM rodar (baseline)")
    pm.add_argument("arquivos", nargs="+", help="nomes em docs/sql/ (ex: 001_app_pedidos.sql)")
    args = p.parse_args(argv)

    try:
        token, ref = carregar_credenciais()
        cliente = ClienteManagement(token, ref)
        if args.cmd == "aplicar":
            cmd_aplicar(cliente, dry_run=args.dry_run)
        elif args.cmd == "marcar":
            cmd_marcar(cliente, args.arquivos)
        else:
            cmd_status(cliente)
    except MigracaoErro as e:
        print(f"\n❌ {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
