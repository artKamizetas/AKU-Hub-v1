"""
seed_parametros.py — Semeia app.parametros com a Categoria B do config.yaml.

Rodar UMA vez após aplicar docs/sql/002_app_parametros.sql (idempotente:
rodar de novo sobrescreve com o yaml atual e registra no histórico).

Uso (da raiz do repo, com .streamlit/secrets.toml configurado):
    python scripts/seed_parametros.py
"""
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from etl.config_store import extrair_parametros, obter_repositorio_parametros

RAIZ = Path(__file__).resolve().parent.parent


def main():
    with open(RAIZ / "config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    dados = extrair_parametros(config)
    print("Blocos extraídos (Categoria B):", ", ".join(sorted(dados.keys())))

    repo = obter_repositorio_parametros()
    atual = repo.ler()
    if atual:
        print(f"⚠️  app.parametros já tem {len(atual)} bloco(s) — serão sobrescritos pelo yaml.")

    repo.salvar(dados, usuario="seed_parametros")
    salvo = repo.ler()
    ok = salvo == dados
    print("✅ Seed aplicado e verificado." if ok else "❌ Releitura difere do enviado!")
    if not ok:
        print(json.dumps({"enviado": dados, "lido": salvo}, indent=2, ensure_ascii=False)[:2000])
        sys.exit(1)


if __name__ == "__main__":
    main()
