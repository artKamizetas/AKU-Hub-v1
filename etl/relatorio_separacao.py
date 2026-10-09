"""
relatorio_separacao.py — Lista de separação impressa (Reposição de Loja)

Transforma a fila da Logística (`processar_logistica`) no documento que o
operador do CD imprime para separar: uma seção por loja, agrupada por Colégio →
Modelo, com os tamanhos lado a lado (a grade), total por modelo e caixa de
conferência. Só entra o que tem `Separar > 0`.

Puro: recebe o DataFrame e devolve estruturas/HTML. A página só entrega o
resultado ao navegador (imprimir ou baixar).
"""

import html

import pandas as pd

from pedidos import grade


def montar_linhas(df: pd.DataFrame) -> list:
    """
    Linhas do relatório, uma por loja × modelo:
        {loja, colegio, modelo, produto, tamanhos: [(tamanho, qtd)], total}
    Ordenadas por loja → colégio → modelo; tamanhos na ordem da grade.
    """
    if df is None or len(df) == 0:
        return []
    separar = df[df["Separar"] > 0]
    linhas = []
    for (loja, colegio, modelo), grupo in separar.groupby(["Loja", "Colegio", "Modelo"], sort=True):
        por_tamanho = grupo.groupby("Tamanho")["Separar"].sum()
        tamanhos = [(t, int(por_tamanho[t])) for t in grade.ordenar_tamanhos(por_tamanho.index)]
        linhas.append({
            "loja": loja, "colegio": colegio, "modelo": modelo,
            "produto": grade.produto_sem_variacao(grupo["Produto"].iloc[0]),
            "tamanhos": tamanhos,
            "total": int(grupo["Separar"].sum()),
        })
    return linhas


_ESTILO = """
  @page { size: A4; margin: 12mm; }
  * { box-sizing: border-box; }
  body { font-family: Arial, Helvetica, sans-serif; font-size: 11px; color: #000; margin: 0; }
  section { page-break-after: always; }
  section:last-of-type { page-break-after: auto; }
  header { display: flex; justify-content: space-between; align-items: flex-end;
           border-bottom: 2px solid #000; padding-bottom: 4px; margin-bottom: 8px; }
  h1 { font-size: 18px; margin: 0; }
  h2 { font-size: 13px; margin: 12px 0 4px; }
  .resumo { text-align: right; font-size: 11px; }
  table { width: 100%; border-collapse: collapse; page-break-inside: auto; }
  tr { page-break-inside: avoid; }
  th, td { border: 1px solid #777; padding: 3px 5px; vertical-align: middle; text-align: left; }
  th { background: #eee; font-size: 10px; }
  td.modelo { font-family: "Courier New", monospace; white-space: nowrap; }
  td.total, th.total { text-align: right; width: 44px; font-weight: bold; }
  td.ok, th.ok { width: 26px; }
  .t { display: inline-block; border: 1px solid #000; border-radius: 3px; padding: 1px 5px;
       margin: 1px 4px 1px 0; white-space: nowrap; }
  .t b { margin-right: 4px; }
  .rodape { margin-top: 18px; display: flex; gap: 24px; }
  .rodape div { flex: 1; border-top: 1px solid #000; padding-top: 3px; font-size: 10px; }
  .vazio { font-size: 13px; margin-top: 24px; }
"""


def _secao_loja(loja: str, linhas: list, data_texto: str) -> str:
    total = sum(l["total"] for l in linhas)
    partes = [
        "<section>",
        "<header>",
        f"<h1>Separação — {html.escape(str(loja))}</h1>",
        f'<div class="resumo">{html.escape(data_texto)}<br>'
        f"<b>{total}</b> peças · {len(linhas)} modelos</div>",
        "</header>",
    ]
    colegio_atual = None
    for linha in linhas:
        if linha["colegio"] != colegio_atual:
            if colegio_atual is not None:
                partes.append("</tbody></table>")
            colegio_atual = linha["colegio"]
            pecas = sum(l["total"] for l in linhas if l["colegio"] == colegio_atual)
            partes.append(f"<h2>{html.escape(str(colegio_atual) or 'Sem colégio')} — {pecas} peças</h2>")
            partes.append(
                '<table><thead><tr><th>Modelo</th><th>Produto</th><th>Tamanho · quantidade</th>'
                '<th class="total">Total</th><th class="ok">OK</th></tr></thead><tbody>')
        tamanhos = "".join(
            f'<span class="t"><b>{html.escape(str(t))}</b>{q}</span>' for t, q in linha["tamanhos"])
        partes.append(
            f'<tr><td class="modelo">{html.escape(str(linha["modelo"]))}</td>'
            f'<td>{html.escape(str(linha["produto"]))}</td><td>{tamanhos}</td>'
            f'<td class="total">{linha["total"]}</td><td class="ok"></td></tr>')
    if colegio_atual is not None:
        partes.append("</tbody></table>")
    partes.append('<div class="rodape"><div>Separado por</div><div>Conferido por</div>'
                  "<div>Recebido na loja por</div></div>")
    partes.append("</section>")
    return "\n".join(partes)


def montar_html(df: pd.DataFrame, data=None, imprimir_ao_abrir: bool = False) -> str:
    """
    Documento HTML (A4, CSS de impressão) da separação. Uma página por loja.
    `imprimir_ao_abrir=True` chama o diálogo de impressão do navegador ao carregar.
    """
    data_texto = pd.Timestamp.now() if data is None else pd.Timestamp(data)
    data_texto = data_texto.strftime("%d/%m/%Y %H:%M")
    linhas = montar_linhas(df)

    corpo = []
    for loja in dict.fromkeys(l["loja"] for l in linhas):
        corpo.append(_secao_loja(loja, [l for l in linhas if l["loja"] == loja], data_texto))
    if not corpo:
        corpo.append(f'<p class="vazio">Nada a separar em {html.escape(data_texto)}.</p>')

    script = "<script>window.onload = function () { window.print(); };</script>" if imprimir_ao_abrir else ""
    return (
        '<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8">'
        f"<title>Separação {html.escape(data_texto)}</title><style>{_ESTILO}</style></head>"
        f"<body>{''.join(corpo)}{script}</body></html>"
    )
