"""
exportar_estoque_alvo.py — Exporta a fila da Reposição de Loja para Excel

Gera data/Estoque_Alvo.xlsx com o resultado integral de
`etl/logistica.py::processar_logistica` (alvo, separar, falta, excesso e os
termos da conta por loja × SKU).

Uso (a partir da raiz do projeto):
    python scripts/exportar_estoque_alvo.py
"""

import sys
import time
from pathlib import Path

# Raiz do projeto (o script vive em scripts/, um nível abaixo)
BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from etl.loader import carregar_dados, carregar_config
from etl.logistica import processar_logistica


def main():
    t0 = time.time()

    config = carregar_config()   # yaml (defaults) + app.parametros (Supabase)

    print("Carregando dados Bling...", end=" ", flush=True)
    dados = carregar_dados()
    print(f"OK ({time.time()-t0:.1f}s)")

    if not dados["validacao"]["ok"]:
        print(f"ERRO: {dados['validacao']['erros']}")
        return

    print("Calculando estoque-alvo...", end=" ", flush=True)
    t1 = time.time()
    df = processar_logistica(dados, config)
    print(f"OK — {len(df)} linhas ({time.time()-t1:.1f}s)")

    saida = BASE / "data" / "Estoque_Alvo.xlsx"
    saida.parent.mkdir(exist_ok=True)
    df.to_excel(str(saida), index=False, sheet_name="Estoque_Alvo")
    print(f"\nExportado: {saida}")

    print("\n--- Resumo por loja ---")
    resumo = df.groupby("Loja").agg(
        SKUs=("SKU", "count"), Alvo=("Alvo", "sum"), Estoque=("EstoqueLoja", "sum"),
        Separar=("Separar", "sum"), Falta=("Falta", "sum"), Excesso=("Excesso", "sum"),
        LimitadosPorEspaco=("LimitadoPorEspaco", "sum"))
    print(resumo.to_string())

    print("\n--- Por ação ---")
    print(df.groupby(["Loja", "Acao"]).size().unstack(fill_value=0).to_string())


if __name__ == "__main__":
    main()
