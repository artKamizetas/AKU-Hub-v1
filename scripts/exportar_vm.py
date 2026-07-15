"""
exportar_vm.py — Exporta VM + Pulmão calculados para Excel

Gera a planilha VM_Calculado.xlsx na pasta data/ com o resultado
do cálculo de VM dinâmico e pulmão para todos os SKUs.

Uso (a partir da raiz do projeto):
    python scripts/exportar_vm.py
"""

import sys
import time
from pathlib import Path

# Raiz do projeto (o script vive em scripts/, um nível abaixo)
BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from etl.loader import carregar_dados, carregar_config
from etl.vm_dinamico import calcular_vm_por_sku


def main():
    t0 = time.time()

    config = carregar_config()   # yaml (defaults) + app.parametros (Supabase)

    # Carrega dados
    print("Carregando dados Bling...", end=" ", flush=True)
    dados = carregar_dados()
    print(f"OK ({time.time()-t0:.1f}s)")

    if not dados["validacao"]["ok"]:
        print(f"ERRO: {dados['validacao']['erros']}")
        return

    # Calcula VM
    print("Calculando VM + Pulmão...", end=" ", flush=True)
    t1 = time.time()
    vm_map = calcular_vm_por_sku(dados, config)
    print(f"OK — {len(vm_map)} SKUs ({time.time()-t1:.1f}s)")

    # Monta DataFrame
    import pandas as pd

    rows = []
    for sku, info in vm_map.items():
        rows.append({
            "SKU": sku,
            "Colégio": info["colegio"],
            "VM (prateleira)": info["vm"],
            "Pulmão (armário)": info["pulmao"],
            "Total na Loja": info["total"],
            "Fonte VM": info["fonte_vm"],
            "D Alta (pçs/dia)": info["d_alta"],
            "PA (pçs/atend)": info["pa"],
            "Desvio-Padrão diário": info["sigma"],
            "Pedidos/Dia": info["pedidos_dia"],
            "Taxa Cresc.": info["taxa_cresc"],
            "Correção": info["correcao"],
        })

    df = pd.DataFrame(rows)
    df = df.sort_values("Total na Loja", ascending=False).reset_index(drop=True)

    # Salva
    saida = BASE / "data" / "VM_Calculado.xlsx"
    df.to_excel(str(saida), index=False, sheet_name="VM_Calculado")
    print(f"\n✅ Exportado: {saida}")
    print(f"   {len(df)} SKUs | Tempo total: {time.time()-t0:.1f}s")

    # Resumo
    print(f"\n--- Resumo ---")
    print(f"VM médio:     {df['VM (prateleira)'].mean():.1f}")
    print(f"Pulmão médio: {df['Pulmão (armário)'].mean():.1f}")
    print(f"Total médio:  {df['Total na Loja'].mean():.1f}")

    fontes = df["Fonte VM"].value_counts()
    print(f"\nFontes:")
    for f, c in fontes.items():
        print(f"  {f}: {c} SKUs")

    # Top 10
    print(f"\nTop 10 — Maior Total na Loja:")
    top = df.head(10)
    for _, r in top.iterrows():
        print(f"  {r['SKU']:<30} VM={r['VM (prateleira)']:>3}  "
              f"Pulm={r['Pulmão (armário)']:>3}  Total={r['Total na Loja']:>3}  "
              f"(Desvio-Padrão={r['Desvio-Padrão diário']:.2f})")


if __name__ == "__main__":
    main()
