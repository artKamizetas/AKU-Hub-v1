"""
emissor.py — Casos de uso da emissão em DOIS momentos.

    emitir_compra_bling(): pedido PRONTO → pedido de COMPRA no Bling (AK Uniformes)
    emitir_venda_olist():  COMPRA_EMITIDA → pedido de VENDA no Olist (Art Kamizetas)

Padrão de consistência (mesmo espírito do CONGELANDO da Fase 0):
  1. LOCK via CAS (PRONTO→COMPRA_EMITINDO / COMPRA_EMITIDA→VENDA_EMITINDO) —
     só uma sessão vence; a perdedora recebe EmissaoFalhou sem tocar o ERP.
  2. Idempotência: se o id do ERP JÁ está gravado (reemissão pós-falha de
     commit), pula o POST e vai direto ao commit.
  3. POST no ERP → grava ids → CAS de commit → evento de auditoria.
  4. Falha ANTES do POST: rollback automático do lock (volta ao estado
     anterior). Falha DEPOIS do POST (ex: Supabase caiu ao gravar o id):
     NÃO faz rollback — o pedido fica travado em *_EMITINDO e a UI exibe
     "emissão interrompida" com botão Destravar + instrução de conferir no
     ERP antes de reemitir (o pedido pode existir lá sem id gravado aqui).

Dependências injetáveis (repo_ped, repo_int, http) → testável com fakes.
"""

from pedidos import builder, estados
from pedidos.integracoes import bling, olist, oauth


class EmissaoFalhou(Exception):
    """Emissão não concluída — mensagem legível para a UI."""


def _carregar_contexto(repo_ped, pedido_id: str):
    """(pedido, itens, rodada_leve) — EmissaoFalhou se algo sumiu."""
    pedido = repo_ped.obter_pedido(pedido_id)
    if not pedido:
        raise EmissaoFalhou(f"Pedido {pedido_id} não encontrado.")
    itens = repo_ped.listar_itens(pedido_id)
    rodada = repo_ped.obter_rodada_leve(pedido["rodada_id"])
    if not rodada:
        raise EmissaoFalhou("Rodada congelada do pedido não encontrada.")
    return pedido, itens, rodada


def emitir_compra_bling(pedido_id: str, usuario: str, repo_ped, repo_int,
                        http=None) -> dict:
    """Pedido PRONTO → pedido de compra no Bling. Retorna {bling_id, bling_numero}."""
    pedido, itens, rodada = _carregar_contexto(repo_ped, pedido_id)

    if not repo_ped.transicionar_pedido(
            pedido_id, estados.PRONTO, estados.COMPRA_EMITINDO, usuario):
        raise EmissaoFalhou(
            "Outra sessão está emitindo este pedido (ou o estado mudou) — recarregue.")

    pos_post = False
    try:
        if str(pedido.get("bling_id") or "").strip():
            # Reemissão pós-falha de commit: o pedido JÁ existe no Bling
            res = {"bling_id": pedido["bling_id"],
                   "bling_numero": pedido.get("bling_numero", "")}
            pos_post = True
        else:
            integ = repo_int.ler("bling")
            token = oauth.obter_access_token("bling", repo_int, http)
            obs = builder.montar_observacoes_bling(rodada, pedido, itens)
            payload = bling.montar_payload_compra(
                pedido, itens, rodada, integ.get("config") or {}, obs)
            res = bling.criar_pedido_compra(token, payload, http)
            pos_post = True
            repo_ped.registrar_ids_emissao(pedido_id, res, usuario)

        if not repo_ped.transicionar_pedido(
                pedido_id, estados.COMPRA_EMITINDO, estados.COMPRA_EMITIDA, usuario):
            raise EmissaoFalhou(
                "Compra criada no Bling, mas a confirmação do estado falhou — "
                "use Destravar e confira o pedido no Bling.")

        repo_int.registrar_evento(
            "bling", "emitir_compra", True, pedido_id=pedido_id, usuario=usuario,
            detalhe={"bling_numero": res["bling_numero"], "titulo": pedido["titulo"]})
        return res

    except Exception as exc:
        if not pos_post:
            # ERP não foi tocado — seguro voltar a PRONTO
            repo_ped.transicionar_pedido(
                pedido_id, estados.COMPRA_EMITINDO, estados.PRONTO, usuario)
        repo_int.registrar_evento(
            "bling", "emitir_compra", False, pedido_id=pedido_id, usuario=usuario,
            detalhe={"erro": str(exc)[:300], "pos_post": pos_post})
        if isinstance(exc, EmissaoFalhou):
            raise
        raise EmissaoFalhou(str(exc)) from exc


def emitir_venda_olist(pedido_id: str, usuario: str, repo_ped, repo_int,
                       mapa_sku: dict = None, http=None) -> dict:
    """
    COMPRA_EMITIDA → pedido de venda no Olist. Retorna {olist_id, olist_numero}.
    `mapa_sku` (SKU→id Olist) pode vir pré-calculado da UI (cache); None = busca.
    """
    pedido, itens, rodada = _carregar_contexto(repo_ped, pedido_id)

    if not repo_ped.transicionar_pedido(
            pedido_id, estados.COMPRA_EMITIDA, estados.VENDA_EMITINDO, usuario):
        raise EmissaoFalhou(
            "Outra sessão está emitindo este pedido (ou o estado mudou) — recarregue.")

    pos_post = False
    try:
        if str(pedido.get("olist_id") or "").strip():
            res = {"olist_id": pedido["olist_id"],
                   "olist_numero": pedido.get("olist_numero", "")}
            pos_post = True
        else:
            integ = repo_int.ler("olist")
            token = oauth.obter_access_token("olist", repo_int, http)
            if mapa_sku is None:
                skus = itens[itens["quantidade_final"] > 0]["sku"].tolist()
                mapa_sku, faltantes = olist.mapear_produtos_por_sku(token, skus, http)
                if faltantes:
                    raise EmissaoFalhou(
                        f"{len(faltantes)} SKU(s) sem match no catálogo do Olist: "
                        f"{', '.join(faltantes[:8])}"
                        + ("…" if len(faltantes) > 8 else ""))
            obs = builder.montar_observacoes_bling(rodada, pedido, itens)
            payload = olist.montar_payload_venda(
                pedido, itens, rodada, integ.get("config") or {}, mapa_sku,
                pedido.get("bling_numero", ""), obs)
            res = olist.criar_pedido_venda(token, payload, http)
            pos_post = True
            repo_ped.registrar_ids_emissao(pedido_id, res, usuario)

        if not repo_ped.transicionar_pedido(
                pedido_id, estados.VENDA_EMITINDO, estados.EMITIDO, usuario):
            raise EmissaoFalhou(
                "Venda criada no Olist, mas a confirmação do estado falhou — "
                "use Destravar e confira o pedido no Olist.")

        repo_int.registrar_evento(
            "olist", "emitir_venda", True, pedido_id=pedido_id, usuario=usuario,
            detalhe={"olist_numero": res["olist_numero"], "titulo": pedido["titulo"]})
        return res

    except Exception as exc:
        if not pos_post:
            repo_ped.transicionar_pedido(
                pedido_id, estados.VENDA_EMITINDO, estados.COMPRA_EMITIDA, usuario)
        repo_int.registrar_evento(
            "olist", "emitir_venda", False, pedido_id=pedido_id, usuario=usuario,
            detalhe={"erro": str(exc)[:300], "pos_post": pos_post})
        if isinstance(exc, EmissaoFalhou):
            raise
        raise EmissaoFalhou(str(exc)) from exc


def destravar(pedido_id: str, usuario: str, repo_ped, repo_int) -> bool:
    """
    Volta um pedido preso em *_EMITINDO (crash entre POST e confirmação) ao
    estado anterior. A UI SEMPRE instrui conferir no ERP antes de reemitir —
    o pedido pode existir lá sem o id gravado aqui.
    """
    pedido = repo_ped.obter_pedido(pedido_id)
    status = pedido.get("status", "")
    destino = {estados.COMPRA_EMITINDO: estados.PRONTO,
               estados.VENDA_EMITINDO: estados.COMPRA_EMITIDA}.get(status)
    if destino is None:
        return False
    ok = repo_ped.transicionar_pedido(pedido_id, status, destino, usuario)
    if ok:
        plataforma = "bling" if status == estados.COMPRA_EMITINDO else "olist"
        repo_int.registrar_evento(plataforma, "destravar", True,
                                  pedido_id=pedido_id, usuario=usuario,
                                  detalhe={"de": status, "para": destino})
    return ok


def validar_pre_emissao_olist(itens, cfg: dict, mapa_sku: dict) -> list:
    """
    Checks puros antes de habilitar o botão de venda: config incompleta e
    SKUs sem match. Lista vazia = pode emitir.
    """
    erros = []
    faltando_cfg = [c for c in ("contato_id", "vendedor_id", "deposito_id")
                    if not str(cfg.get(c) or "").strip()]
    if faltando_cfg:
        erros.append(f"Config do Olist incompleta: {', '.join(faltando_cfg)} "
                     "(aba Integrações).")
    skus = itens[itens["quantidade_final"] > 0]["sku"].astype(str).tolist()
    faltantes = [s for s in skus if s not in (mapa_sku or {})]
    if faltantes:
        erros.append(f"{len(faltantes)} SKU(s) sem match no Olist: "
                     f"{', '.join(faltantes[:8])}" + ("…" if len(faltantes) > 8 else ""))
    return erros


def preview_payloads(pedido_id: str, repo_ped, repo_int, mapa_sku: dict = None) -> dict:
    """
    Payloads exatos que a emissão enviará (verificação humana SEM escrita).
    Cada chave é o payload (dict) ou {"erro": msg} quando não montável ainda.
    """
    pedido, itens, rodada = _carregar_contexto(repo_ped, pedido_id)
    obs = builder.montar_observacoes_bling(rodada, pedido, itens)
    out = {}

    try:
        cfg_b = (repo_int.ler("bling") or {}).get("config") or {}
        out["compra"] = bling.montar_payload_compra(pedido, itens, rodada, cfg_b, obs)
    except Exception as exc:
        out["compra"] = {"erro": str(exc)}

    try:
        cfg_o = (repo_int.ler("olist") or {}).get("config") or {}
        out["venda"] = olist.montar_payload_venda(
            pedido, itens, rodada, cfg_o, mapa_sku or {},
            pedido.get("bling_numero", ""), obs)
    except Exception as exc:
        out["venda"] = {"erro": str(exc)}

    return out
