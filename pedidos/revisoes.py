"""
revisoes.py — Leitura PURA das revisões do pedido (app.pedido_compra_revisao).

Uma revisão é "o que foi aos ERPs" numa versão: a emissão (nº 1), cada
alteração e o cancelamento. Daqui saem as três perguntas que o pós-emissão
precisa responder, sem I/O (as funções recebem a lista já lida pelo
repositório, em ordem de `numero`):

  - linha_de_base()      → o que está nos ERPs agora (última revisão concluída);
  - confirmada_no_erp()  → o que AQUELE ERP recebeu por último (num envio
                           parcial os dois lados ficam em versões diferentes);
  - envio_parcial()      → um ERP recebeu a versão nova e o outro não.

Mora fora do repositório porque é regra, não acesso: um erro aqui faz a tela
mostrar como "emitido" o que não está no ERP, ou o descarte restaurar a
quantidade errada — precisa de teste sem Supabase.
"""

from pedidos.estados import REVISAO_CANCELAMENTO

ERPS = ("bling", "olist")


def _texto(valor) -> str:
    texto = "" if valor is None else str(valor).strip()
    return "" if texto.lower() in ("nan", "none") else texto


def _qtd(valor) -> int:
    try:
        return int(round(float(valor)))
    except (TypeError, ValueError):
        return 0


def retrato_itens(itens) -> list:
    """
    Retrato das quantidades do pedido para gravar numa revisão: TODOS os itens,
    zerados inclusive (o descarte precisa saber que o item existia com zero).
    Aceita o DataFrame de repo.listar_itens() ou uma lista de dicts.
    """
    linhas = itens.to_dict("records") if hasattr(itens, "to_dict") else list(itens or [])
    return [{
        "item_id": linha.get("id"),
        "sku": _texto(linha.get("sku")),
        "id_produto_bling": _texto(linha.get("id_produto_bling")),
        "quantidade": _qtd(linha.get("quantidade_final")),
        "custo_unit": float(linha.get("custo_unit") or 0),
    } for linha in linhas]


def _concluida(revisao: dict) -> bool:
    return bool(_texto(revisao.get("concluida_em")))


def linha_de_base(revisoes: list) -> dict:
    """
    Última revisão CONCLUÍDA que não é cancelamento — o que está nos ERPs
    agora. {} se o pedido nunca teve revisão (emitido antes do DDL 008).
    """
    for revisao in reversed(revisoes or []):
        if _concluida(revisao) and revisao.get("tipo") != REVISAO_CANCELAMENTO:
            return revisao
    return {}


def pendente(revisoes: list) -> dict:
    """A revisão mais recente, se ainda não foi concluída. {} caso contrário."""
    if not revisoes:
        return {}
    ultima = revisoes[-1]
    return {} if _concluida(ultima) else ultima


def envio_parcial(revisoes: list) -> dict:
    """
    Revisão pendente em que pelo menos um ERP já confirmou: os dois sistemas
    estão em versões diferentes. É o que proíbe descartar a alteração (o
    descarte só restaura o NOSSO lado) e o que a tela mostra como aviso.
    """
    rev = pendente(revisoes)
    if rev and any(_texto(rev.get(f"{erp}_ok_em")) for erp in ERPS):
        return rev
    return {}


def confirmada_no_erp(revisoes: list, erp: str) -> dict:
    """
    Última revisão (não cancelamento) que AQUELE ERP confirmou — concluída ou
    não. É contra ela que se detecta edição feita direto no ERP: comparar com
    a linha de base acusaria como "edição manual" o que nós mesmos enviamos
    num envio parcial.
    """
    for revisao in reversed(revisoes or []):
        if (revisao.get("tipo") != REVISAO_CANCELAMENTO
                and _texto(revisao.get(f"{erp}_ok_em"))):
            return revisao
    return {}


def quantidades(revisao: dict) -> dict:
    """item_id → quantidade daquela revisão (itens zerados inclusive)."""
    return {linha.get("item_id"): _qtd(linha.get("quantidade"))
            for linha in (revisao or {}).get("itens") or []}


def itens_no_erp(revisao: dict) -> list:
    """
    O que aquela revisão pôs no ERP: só quantidade > 0 (item zerado não vai no
    payload), como [{"sku", "id_produto_bling", "quantidade"}].
    """
    return [{"sku": _texto(linha.get("sku")),
             "id_produto_bling": _texto(linha.get("id_produto_bling")),
             "quantidade": _qtd(linha.get("quantidade"))}
            for linha in (revisao or {}).get("itens") or []
            if _qtd(linha.get("quantidade")) > 0]


def diferencas(itens, base: dict) -> list:
    """
    O que a alteração em curso muda em relação à linha de base, na ordem de
    `itens`: [{"id", "sku", "produto", "tamanho", "emitida", "nova",
    "custo_unit"}]. Item que não está na base foi incluído durante a alteração
    (emitida = 0). Item zerado nos dois lados não é diferença.
    """
    na_base = quantidades(base)
    linhas = itens.to_dict("records") if hasattr(itens, "to_dict") else list(itens or [])
    saida = []
    for linha in linhas:
        emitida = na_base.get(linha.get("id"), 0)
        nova = _qtd(linha.get("quantidade_final"))
        if emitida != nova:
            saida.append({
                "id": linha.get("id"), "sku": _texto(linha.get("sku")),
                "produto": _texto(linha.get("produto")),
                "tamanho": _texto(linha.get("tamanho")),
                "emitida": emitida, "nova": nova,
                "custo_unit": float(linha.get("custo_unit") or 0),
            })
    return saida
