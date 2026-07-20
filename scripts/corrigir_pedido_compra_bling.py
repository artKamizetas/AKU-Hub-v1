"""
corrigir_pedido_compra_bling.py — Correção PONTUAL de um pedido de compra JÁ
emitido no Bling (one-off, NÃO é funcionalidade da plataforma).

Contexto: os primeiros pedidos emitidos saíram sem `codigoFornecedor` (nosso
SKU), sem `unidade` e sem bloco de `parcelas` — campos que o payload de emissão
só passou a mandar depois. Este script corrige esses pedidos no lugar, sem
cancelar/refazer e sem tocar no fluxo da 4_Pedidos.

Estratégia de risco mínimo:
  1. GET /pedidos/compras/{id} → o documento REAL vira a base;
  2. sobrescreve APENAS os campos que faltavam (por item: codigoFornecedor,
     unidade, descricaoDetalhada; no pedido: parcelas);
  3. preserva numero, data, situacao, categoria, ordemCompra, observações e
     qualquer campo do item que já exista lá (notaFiscal, aliquotaIPI…);
  4. PUT /pedidos/compras/{id} com o documento mesclado.

Nada de quantidade/valor é alterado: os itens são casados por produto.id e as
quantidades vêm do que JÁ está no Bling, não do nosso banco — assim uma edição
feita à mão no ERP não é atropelada.

Uso (da raiz do repo, com .streamlit/secrets.toml configurado):
    python scripts/corrigir_pedido_compra_bling.py --numero 398          # dry-run
    python scripts/corrigir_pedido_compra_bling.py --numero 398 --aplicar
    python scripts/corrigir_pedido_compra_bling.py --bling-id 123456     # por id
    ... --forma-pagamento 859669    # ID ainda não salvo na aba Integrações

DRY-RUN por padrão: sem --aplicar nada é escrito, só imprime o diff e o payload.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from pedidos import builder
from pedidos.integracoes import bling, oauth
from pedidos.integracoes.repositorio import obter_repositorio_integracoes
from pedidos.repositorio import _conn_app, RepositorioPedidos, TAB_PEDIDO


def _http():
    import httpx
    return httpx.Client(timeout=30)


def obter_pedido_bling(token: str, bling_id: str, http) -> dict:
    """GET do documento real — é ele que vira a base do PUT."""
    resp = http.get(f"{bling.BASE}/pedidos/compras/{bling_id}",
                    headers=bling._headers(token))
    if resp.status_code >= 300:
        raise SystemExit(f"❌ GET falhou: {bling._erro_legivel(resp)}")
    return (resp.json() or {}).get("data") or {}


def localizar_pedido_local(repo: RepositorioPedidos, numero: str = None,
                           bling_id: str = None) -> dict:
    """Nosso pedido (app.pedido_compra) pela chave de emissão."""
    filtro = {"bling_numero": str(numero)} if numero else {"bling_id": str(bling_id)}
    linhas = repo._selecionar(TAB_PEDIDO, filtro)
    if not linhas:
        raise SystemExit(f"❌ Nenhum pedido nosso com {filtro} — confira o número.")
    if len(linhas) > 1:
        raise SystemExit(f"❌ {len(linhas)} pedidos com {filtro} — ambíguo.")
    return linhas[0]


def montar_correcao(doc: dict, itens: pd.DataFrame, cfg: dict,
                    mes_disparo: int, ano_disparo: int) -> tuple:
    """
    Documento corrigido + lista legível do que muda. PURO (sem I/O) — o que
    permite conferir o diff antes de qualquer escrita.
    """
    corrigido = json.loads(json.dumps(doc))   # cópia profunda
    mudancas = []

    unidade = str(cfg.get("unidade_padrao") or bling.UNIDADE_PADRAO).strip()
    # nosso item por id de produto do Bling — a chave que casa os dois lados
    nossos = {str(l["id_produto_bling"]).strip(): l for _, l in itens.iterrows()}

    for item in corrigido.get("itens") or []:
        pid = str((item.get("produto") or {}).get("id") or "").strip()
        nosso = nossos.get(pid)
        if nosso is None:
            mudancas.append(f"  ⚠️  item produto.id={pid} não existe no nosso "
                            "pedido — deixado intacto")
            continue

        sku = str(nosso["sku"])
        if not str(item.get("codigoFornecedor") or "").strip():
            item["codigoFornecedor"] = sku
            mudancas.append(f"  {sku}: codigoFornecedor ← {sku}")
        if not str(item.get("unidade") or "").strip():
            item["unidade"] = unidade
            mudancas.append(f"  {sku}: unidade ← {unidade}")
        if not str(item.get("descricaoDetalhada") or "").strip():
            memoria = builder.montar_descricao_item(nosso, mes_disparo, ano_disparo)
            if memoria:
                item["descricaoDetalhada"] = memoria
                mudancas.append(f"  {sku}: descricaoDetalhada ← {memoria}")

    # Parcela: vencimento conta da data de EMISSÃO real do pedido (a que está
    # no Bling), não de hoje — o prazo foi negociado a partir dela.
    if not (corrigido.get("parcelas") or []):
        forma_id = str(cfg.get("forma_pagamento_id") or "").strip()
        if not forma_id:
            mudancas.append("  ⚠️  forma_pagamento_id não configurado "
                            "(aba Integrações) — parcela NÃO será criada")
        elif not corrigido.get("data"):
            mudancas.append("  ⚠️  pedido sem data de emissão no Bling — "
                            "parcela NÃO será criada")
        else:
            emissao = pd.Timestamp(str(corrigido["data"])).normalize()
            prazo = int(cfg.get("prazo_pagamento_dias") or bling.PRAZO_PAGAMENTO_PADRAO)
            total = sum(float(i.get("valor") or 0) * float(i.get("quantidade") or 0)
                        for i in corrigido.get("itens") or [])
            vencimento = (emissao + pd.Timedelta(days=prazo)).date()
            corrigido["parcelas"] = [{
                "valor": round(total, 2),
                "dataVencimento": str(vencimento),
                "formaPagamento": {"id": int(forma_id)},
            }]
            mudancas.append(f"  parcela ← R$ {total:,.2f} vence {vencimento} "
                            f"(emissão {emissao.date()} + {prazo}d, forma {forma_id})")

    return corrigido, mudancas


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--numero", help="Número do pedido de compra no Bling (ex: 398)")
    g.add_argument("--bling-id", help="ID interno do pedido no Bling")
    ap.add_argument("--aplicar", action="store_true",
                    help="Executa o PUT. Sem esta flag, só mostra o que faria.")
    ap.add_argument("--forma-pagamento",
                    help="ID da forma de pagamento, se ainda não estiver salvo "
                         "na aba Integrações (só para esta execução — não grava).")
    ap.add_argument("--prazo-dias", type=int,
                    help="Prazo de vencimento em dias (default: config ou 30).")
    args = ap.parse_args()

    repo = RepositorioPedidos(_conn_app())
    repo_int = obter_repositorio_integracoes()
    cfg = dict((repo_int.ler("bling") or {}).get("config") or {})
    # Overrides de execução: permitem corrigir o pedido antes de o parâmetro
    # existir na config (a aba Integrações continua sendo a fonte definitiva).
    if args.forma_pagamento:
        cfg["forma_pagamento_id"] = args.forma_pagamento
    if args.prazo_dias is not None:
        cfg["prazo_pagamento_dias"] = args.prazo_dias

    pedido = localizar_pedido_local(repo, args.numero, args.bling_id)
    bling_id = str(pedido["bling_id"])
    itens = repo.listar_itens(pedido["id"])
    rodada = repo.obter_rodada_leve(pedido["rodada_id"])

    print(f"\n📋 Pedido: {pedido['titulo']}")
    print(f"   nosso id={pedido['id']} · Bling id={bling_id} "
          f"nº={pedido.get('bling_numero')} · status={pedido['status']}")

    http = _http()
    token = oauth.obter_access_token("bling", repo_int, http)
    doc = obter_pedido_bling(token, bling_id, http)
    if not doc:
        raise SystemExit("❌ GET não devolveu o pedido — id errado?")
    print(f"   no Bling: nº={doc.get('numero')} · data={doc.get('data')} · "
          f"{len(doc.get('itens') or [])} itens · "
          f"{len(doc.get('parcelas') or [])} parcelas")

    corrigido, mudancas = montar_correcao(
        doc, itens, cfg,
        int(rodada.get("mes_disparo") or 0), int(rodada.get("ano_disparo") or 0))

    if not mudancas:
        print("\n✅ Nada a corrigir — o pedido já está completo no Bling.")
        return
    print("\n🔧 Mudanças:")
    for m in mudancas:
        print(m)

    print("\n📦 Payload do PUT:")
    print(json.dumps(corrigido, ensure_ascii=False, indent=1))

    if not args.aplicar:
        print("\n🧪 DRY-RUN — nada foi enviado. Rode de novo com --aplicar "
              "para gravar no Bling.")
        return

    resp = http.put(f"{bling.BASE}/pedidos/compras/{bling_id}",
                    headers=bling._headers(token), json=corrigido)
    if resp.status_code >= 300:
        raise SystemExit(f"\n❌ PUT falhou: {bling._erro_legivel(resp)}")
    print(f"\n✅ Pedido {doc.get('numero')} corrigido no Bling.")
    repo_int.registrar_evento(
        "bling", "corrigir_compra_oneoff", True, pedido_id=pedido["id"],
        usuario="script", detalhe={"bling_numero": str(doc.get("numero")),
                                   "mudancas": len(mudancas)})


if __name__ == "__main__":
    main()
