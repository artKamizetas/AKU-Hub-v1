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

DEPOIS da emissão (DDL 008):

    enviar_alteracao(): EM_ALTERACAO → sobrepõe os itens no Olist e no Bling
    cancelar_emitido(): pedido emitido → cancela nos dois ERPs → CANCELADO

  - O FILTRO é o status nativo de cada ERP: compra "Em aberto" no Bling e
    venda "Aberta" no Olist (avaliar_abertura). Nada além disso bloqueia.
  - Olist PRIMEIRO, Bling depois: o Olist é o lado da fábrica, onde uma
    recusa importa — se ele recusar, nada mudou em lugar nenhum.
  - PUT/PATCH são idempotentes: falha depois de tocar um ERP deixa o pedido
    no lock (*_ENVIANDO) e a UI oferece CONCLUIR, que reexecuta tudo. Não há
    o risco de duplicata do POST — por isso não é o mesmo "Destravar".
  - Falha ANTES de tocar qualquer ERP: rollback ao estado de repouso.
  - Edição feita direto no ERP não bloqueia: é detectada (comparar_com_erp)
    e exige um `sobrepor=True` explícito.

Dependências injetáveis (repo_ped, repo_int, http) → testável com fakes.
"""

import pandas as pd

from pedidos import builder, estados, revisoes
from pedidos.integracoes import bling, olist, oauth


class EmissaoFalhou(Exception):
    """Emissão não concluída — mensagem legível para a UI."""


class DivergenciaNoErp(EmissaoFalhou):
    """
    O que está no ERP não é o que enviamos por último: alguém editou o pedido
    direto lá. Não é erro — é uma pergunta: `divergencias` vai para a tela e o
    envio só segue com `sobrepor=True`.
    """

    def __init__(self, divergencias: list):
        super().__init__(
            f"{len(divergencias)} item(ns) foram alterados direto no ERP — "
            "confira a diferença e confirme a sobreposição.")
        self.divergencias = divergencias


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


def resolver_ids_olist(skus, repo_int, http=None, dormir=None) -> tuple:
    """
    SKU → id interno do Olist, em CAMADAS do mais barato ao mais caro:
      1. cache persistente (app.olist_produto_cache) — 0 chamadas p/ SKU já visto;
      2. resolução por família (1 GET no pai + 1 na grade, cobre ~7 tamanhos);
      3. fallback exato por SKU (?codigo=) para o que sobrou.
    O que a API resolveu (inclusive irmãos da grade não pedidos) volta ao cache,
    então lotes seguintes do mesmo colégio saem quase todos do cache.

    Retorna ({sku: id_olist}, [skus_sem_match]). Token buscado só se houver
    SKU fora do cache (cache quente = nenhuma ida à API, nem refresh de token).
    """
    distintos = sorted({str(s).strip() for s in skus if str(s).strip()})
    if not distintos:
        return {}, []

    mapa = dict(repo_int.ler_cache_produtos_olist(distintos))
    faltam = [s for s in distintos if s not in mapa]
    if faltam:
        token = oauth.obter_access_token("olist", repo_int, http)
        por_familia, pendentes = olist.mapear_por_familia(token, faltam, http, dormir)
        por_exato, _ = olist.mapear_produtos_por_sku(
            token, sorted(pendentes), http, dormir)
        novos = {**por_familia, **por_exato}
        if novos:
            repo_int.gravar_cache_produtos_olist(novos)
        mapa.update(novos)

    faltantes = [s for s in distintos if s not in mapa]
    return {s: mapa[s] for s in distintos if s in mapa}, faltantes


def _registrar_revisao_emissao(repo_ped, pedido_id: str, usuario: str, erps: tuple) -> None:
    """
    Linha de base do pós-emissão: revisão 1 na compra, carimbo do Olist na
    venda. NUNCA derruba a emissão — o ERP já foi tocado e o pedido já mudou
    de estado; se isto falhar (ou o DDL 008 não existir), a linha de base é
    reconstruída ao abrir a 1ª alteração (repositorio.abrir_alteracao).
    """
    try:
        base = repo_ped.obter_linha_de_base(pedido_id)
        if not base:
            repo_ped.registrar_revisao(
                pedido_id, estados.REVISAO_EMISSAO, usuario,
                motivo="Emissão", erps_ok=erps, concluida=True)
        elif "olist" in erps and not base.get("olist_ok_em"):
            repo_ped.confirmar_revisao(base["id"], "olist")
    except Exception:
        pass


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

        _registrar_revisao_emissao(repo_ped, pedido_id, usuario, ("bling",))
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
                mapa_sku, faltantes = resolver_ids_olist(skus, repo_int, http)
                if faltantes:
                    raise EmissaoFalhou(
                        f"{len(faltantes)} SKU(s) sem match no catálogo do Olist: "
                        f"{', '.join(faltantes[:8])}"
                        + ("…" if len(faltantes) > 8 else ""))
            obs = builder.montar_observacoes_bling(rodada, pedido, itens)
            # Prazo vem do Bling: a compra e a venda são o mesmo acordo, o
            # vencimento tem de bater nos dois ERPs.
            cfg_b = (repo_int.ler("bling") or {}).get("config") or {}
            payload = olist.montar_payload_venda(
                pedido, itens, rodada, integ.get("config") or {}, mapa_sku,
                pedido.get("bling_numero", ""), obs,
                prazo_dias=cfg_b.get("prazo_pagamento_dias"))
            res = olist.criar_pedido_venda(token, payload, http)
            pos_post = True
            repo_ped.registrar_ids_emissao(pedido_id, res, usuario)

        if not repo_ped.transicionar_pedido(
                pedido_id, estados.VENDA_EMITINDO, estados.EMITIDO, usuario):
            raise EmissaoFalhou(
                "Venda criada no Olist, mas a confirmação do estado falhou — "
                "use Destravar e confira o pedido no Olist.")

        _registrar_revisao_emissao(repo_ped, pedido_id, usuario, ("bling", "olist"))
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
               estados.VENDA_EMITINDO: estados.COMPRA_EMITIDA,
               # pós-emissão: a alteração volta a ser editável (a revisão
               # parcial, se houver, fica como marca — ver revisoes.envio_parcial);
               # o cancelamento volta ao estado emitido
               estados.ALTERACAO_ENVIANDO: estados.EM_ALTERACAO,
               estados.CANCELAMENTO_ENVIANDO: estados.estado_emitido(pedido)}.get(status)
    if destino is None:
        return False
    ok = repo_ped.transicionar_pedido(pedido_id, status, destino, usuario)
    if ok:
        plataforma = {estados.COMPRA_EMITINDO: "bling",
                      estados.VENDA_EMITINDO: "olist"}.get(status, "bling+olist")
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


def checar_prontidao_olist(repo_int, http=None) -> list:
    """
    O Olist consegue receber a venda AGORA? Checa token e config — sem tocar
    no catálogo (barato o bastante p/ rodar com o pedido ainda em PRONTO).

    Existe porque a emissão é em dois momentos: a compra no Bling é
    IRREVERSÍVEL daqui, e sem este aviso um problema do lado do Olist só
    aparecia depois — deixando pedidos parados em COMPRA_EMITIDA sem par.
    Retorna lista de avisos (vazia = pronto). Nunca levanta.
    """
    avisos = []
    try:
        integ = repo_int.ler("olist") or {}
    except Exception as exc:
        return [f"Não foi possível ler a integração do Olist: {exc}"]

    try:
        oauth.obter_access_token("olist", repo_int, http)
    except Exception as exc:
        avisos.append(f"Olist sem token utilizável ({exc}). Reconecte na aba "
                      "Integrações ANTES de emitir a compra.")

    cfg = integ.get("config") or {}
    faltando = [c for c in ("contato_id", "vendedor_id", "deposito_id")
                if not str(cfg.get(c) or "").strip()]
    if faltando:
        avisos.append(f"Config do Olist incompleta: {', '.join(faltando)} "
                      "(aba Integrações).")
    return avisos


def validar_pre_emissao_bling(itens, cfg: dict) -> list:
    """
    Checks puros antes de habilitar o botão de COMPRA: config incompleta
    (fornecedor_id), nada a emitir e itens sem id de produto do Bling. Lista
    vazia = pode emitir. Espelha validar_pre_emissao_olist — dá o feedback
    ANTES do clique (o erro do payload deixava de aparecer para o usuário).
    """
    erros = []
    if not str(cfg.get("fornecedor_id") or "").strip():
        erros.append("Config do Bling incompleta: fornecedor_id (Art Kamizetas) "
                     "— preencha na aba Integrações.")
    if not str(cfg.get("forma_pagamento_id") or "").strip():
        erros.append("Config do Bling sem forma de pagamento — escolha na aba "
                     "Integrações (o pedido sairia sem parcela/vencimento).")
    validos = itens[itens["quantidade_final"] > 0]
    if len(validos) == 0:
        erros.append("Nenhum item com quantidade final > 0 — nada a emitir.")
    elif "id_produto_bling" in validos.columns:
        sem_id = validos[
            validos["id_produto_bling"].astype(str).str.strip()
            .isin(["", "nan", "None", "<NA>"])
        ]["sku"].astype(str).tolist()
        if sem_id:
            erros.append(f"{len(sem_id)} item(ns) sem id de produto do Bling: "
                         f"{', '.join(sem_id[:8])}" + ("…" if len(sem_id) > 8 else ""))
    return erros


def preview_payloads(pedido_id: str, repo_ped, repo_int, mapa_sku: dict = None) -> dict:
    """
    Payloads exatos que a emissão enviará (verificação humana SEM escrita).
    Cada chave é o payload (dict) ou {"erro": msg} quando não montável ainda.
    """
    pedido, itens, rodada = _carregar_contexto(repo_ped, pedido_id)
    obs = builder.montar_observacoes_bling(rodada, pedido, itens)
    out = {}

    # Lido fora dos try: o prazo do Bling alimenta os DOIS payloads.
    cfg_b = (repo_int.ler("bling") or {}).get("config") or {}

    try:
        out["compra"] = bling.montar_payload_compra(pedido, itens, rodada, cfg_b, obs)
    except Exception as exc:
        out["compra"] = {"erro": str(exc)}

    try:
        cfg_o = (repo_int.ler("olist") or {}).get("config") or {}
        out["venda"] = olist.montar_payload_venda(
            pedido, itens, rodada, cfg_o, mapa_sku or {},
            pedido.get("bling_numero", ""), obs,
            prazo_dias=cfg_b.get("prazo_pagamento_dias"))
    except Exception as exc:
        out["venda"] = {"erro": str(exc)}

    return out


# ===========================================================================
# PÓS-EMISSÃO — alterar e cancelar um pedido que já está nos ERPs
# ===========================================================================
def _tem(valor) -> bool:
    texto = "" if valor is None else str(valor).strip()
    return bool(texto) and texto.lower() not in ("nan", "none")


def consultar_erps(pedido: dict, repo_int, http=None, dormir=None) -> dict:
    """
    Lê o pedido como está AGORA em cada ERP: {"bling": {...}, "olist": {...}}
    (normalizados pelos clientes). None para o ERP onde o pedido não existe —
    quem só emitiu a compra não tem venda no Olist. Levanta se um ERP onde o
    pedido deveria existir não responde (ou o pedido foi excluído lá).
    """
    erps = {"bling": None, "olist": None}
    if _tem(pedido.get("bling_id")):
        token = oauth.obter_access_token("bling", repo_int, http)
        erps["bling"] = bling.obter_pedido_compra(token, pedido["bling_id"], http)
    if _tem(pedido.get("olist_id")):
        token = oauth.obter_access_token("olist", repo_int, http)
        erps["olist"] = olist.obter_pedido(token, pedido["olist_id"], http, dormir)
    return erps


def avaliar_abertura(pedido: dict, erp_bling, erp_olist) -> list:
    """
    O FILTRO do pós-emissão (PURO): o pedido só pode ser alterado enquanto está
    em aberto no status NATIVO de cada ERP onde existe — compra "Em aberto" no
    Bling e venda "Aberta" no Olist. Lista vazia = pode. Cada impedimento diz
    o ERP, o número e a situação encontrada.
    """
    impedimentos = []
    if _tem(pedido.get("bling_id")):
        if not erp_bling:
            impedimentos.append("Bling: não foi possível ler a situação da compra.")
        elif erp_bling.get("situacao_valor") != bling.SITUACAO_EM_ABERTO:
            impedimentos.append(
                f"Bling: a compra nº {pedido.get('bling_numero') or '?'} está "
                f"**{erp_bling.get('situacao_rotulo')}** — só pedido Em aberto "
                "pode ser alterado.")
    if _tem(pedido.get("olist_id")):
        if not erp_olist:
            impedimentos.append("Olist: não foi possível ler a situação da venda.")
        elif erp_olist.get("situacao") != olist.SITUACAO_ABERTA:
            impedimentos.append(
                f"Olist: a venda nº {pedido.get('olist_numero') or '?'} está "
                f"**{erp_olist.get('situacao_rotulo')}** — só pedido Aberta "
                "pode ser alterado.")
    return impedimentos


def _por_sku(itens_erp, id_para_sku: dict = None) -> dict:
    """SKU → quantidade somada (o ERP pode ter a mesma linha duas vezes)."""
    soma = {}
    for item in itens_erp or []:
        sku = (id_para_sku or {}).get(str(item.get("id_produto") or "")) \
            or str(item.get("sku") or "").strip() \
            or f"(produto {item.get('id_produto')})"
        soma[sku] = soma.get(sku, 0) + int(round(float(item.get("quantidade") or 0)))
    return soma


def comparar_com_erp(enviado_bling, enviado_olist, erp_bling, erp_olist) -> list:
    """
    Edição feita direto no ERP (PURO): compara o que cada ERP tem AGORA com o
    que NÓS enviamos a ele por último (`enviado_*` = revisoes.itens_no_erp da
    revisão confirmada naquele ERP). Devolve só os SKUs em que algum ERP
    diverge:

        [{"sku", "enviado", "bling", "olist", "diverge_bling", "diverge_olist"}]

    `bling`/`olist` = quantidade no ERP agora (0 = a linha sumiu de lá; None =
    o pedido não existe naquele ERP). SKU com `enviado` 0 foi incluído direto
    no ERP. No Bling o casamento é pelo id do produto (o SKU ali é texto
    livre do campo "código do fornecedor"); no Olist, pelo SKU do cadastro.
    """
    lados = {}
    if erp_bling is not None and enviado_bling is not None:
        id_para_sku = {i["id_produto_bling"]: i["sku"] for i in enviado_bling
                       if i.get("id_produto_bling")}
        lados["bling"] = ({i["sku"]: int(i["quantidade"]) for i in enviado_bling},
                          _por_sku(erp_bling.get("itens"), id_para_sku))
    if erp_olist is not None and enviado_olist is not None:
        lados["olist"] = ({i["sku"]: int(i["quantidade"]) for i in enviado_olist},
                          _por_sku(erp_olist.get("itens")))

    skus = sorted({sku for enviado, atual in lados.values() for sku in (*enviado, *atual)})
    saida = []
    for sku in skus:
        linha = {"sku": sku, "enviado": 0, "bling": None, "olist": None,
                 "diverge_bling": False, "diverge_olist": False}
        for erp, (enviado, atual) in lados.items():
            linha[erp] = atual.get(sku, 0)
            linha["enviado"] = max(linha["enviado"], enviado.get(sku, 0))
            linha[f"diverge_{erp}"] = atual.get(sku, 0) != enviado.get(sku, 0)
        if linha["diverge_bling"] or linha["diverge_olist"]:
            saida.append(linha)
    return saida


def divergencias_do_pedido(pedido_id: str, erps: dict, repo_ped) -> list:
    """comparar_com_erp() alimentado pelas revisões do pedido."""
    historico = repo_ped.listar_revisoes(pedido_id)
    return comparar_com_erp(
        revisoes.itens_no_erp(revisoes.confirmada_no_erp(historico, "bling")),
        revisoes.itens_no_erp(revisoes.confirmada_no_erp(historico, "olist")),
        erps.get("bling"), erps.get("olist"))


def enviar_alteracao(pedido_id: str, motivo: str, usuario: str, repo_ped, repo_int,
                     sobrepor: bool = False, http=None, dormir=None,
                     progresso=None) -> dict:
    """
    EM_ALTERACAO → sobrepõe o pedido nos ERPs → volta ao estado emitido.
    Chamado sobre um pedido em ALTERACAO_ENVIANDO é a RETOMADA ("Concluir"):
    reenvia a mesma versão (os itens ficam travados no lock) — PUT é idempotente.

    Tudo o que pode falhar sem tocar ERP (gate, divergência, tokens, payloads,
    SKU sem cadastro no Olist) roda ANTES do primeiro PUT. Retorna
    {"revisao", "alertas"} (alertas = avisos do Bling sobre o PUT).
    `progresso(texto)` é opcional: a tela o usa para narrar as etapas.
    """
    avisar = progresso or (lambda _texto: None)
    pedido, itens, rodada = _carregar_contexto(repo_ped, pedido_id)
    retomando = pedido.get("status") == estados.ALTERACAO_ENVIANDO
    if not retomando:
        if not str(motivo or "").strip():
            raise EmissaoFalhou("Informe o motivo da alteração.")
        if not repo_ped.transicionar_pedido(
                pedido_id, estados.EM_ALTERACAO, estados.ALTERACAO_ENVIANDO, usuario):
            raise EmissaoFalhou(
                "Outra sessão está enviando este pedido (ou o estado mudou) — recarregue.")

    destino = estados.estado_emitido(pedido)
    tocou = retomando          # numa retomada o envio anterior pode ter tocado um ERP
    etapa = "bling"
    revisao, criada_agora = {}, False
    try:
        historico = repo_ped.listar_revisoes(pedido_id)
        base = revisoes.linha_de_base(historico)
        revisao = revisoes.pendente(historico) if retomando else {}
        mudou = bool(revisoes.diferencas(itens, base))

        # Retomada de um envio que JÁ fechou nos ERPs (só o commit do estado
        # falhou): não há o que reenviar.
        if retomando and not revisao and not mudou:
            if not repo_ped.transicionar_pedido(
                    pedido_id, estados.ALTERACAO_ENVIANDO, destino, usuario):
                raise EmissaoFalhou("O estado mudou em outra sessão — recarregue.")
            return {"revisao": base.get("numero"), "alertas": []}

        if len(itens[itens["quantidade_final"] > 0]) == 0:
            raise EmissaoFalhou(
                "A alteração zerou todos os itens — para desistir da compra use "
                "\"Cancelar pedido emitido\".")
        if not revisao and not mudou and not revisoes.envio_parcial(historico):
            raise EmissaoFalhou(
                "Nenhuma quantidade difere do que está emitido — descarte a alteração.")

        avisar("Conferindo a situação do pedido nos ERPs…")
        erps = consultar_erps(pedido, repo_int, http, dormir)
        impedimentos = avaliar_abertura(pedido, erps["bling"], erps["olist"])
        if impedimentos:
            raise EmissaoFalhou(" ".join(impedimentos))
        if not revisao and not sobrepor:
            divergencias = comparar_com_erp(
                revisoes.itens_no_erp(revisoes.confirmada_no_erp(historico, "bling")),
                revisoes.itens_no_erp(revisoes.confirmada_no_erp(historico, "olist")),
                erps["bling"], erps["olist"])
            if divergencias:
                raise DivergenciaNoErp(divergencias)

        # --- Preparo completo dos dois lados, ainda sem tocar em ERP ---
        obs = builder.montar_observacoes_bling(rodada, pedido, itens)
        cfg_b = (repo_int.ler("bling") or {}).get("config") or {}
        token_b = oauth.obter_access_token("bling", repo_int, http)
        payload_b = bling.montar_payload_alteracao_compra(
            bling.montar_payload_compra(pedido, itens, rodada, cfg_b, obs), erps["bling"])

        token_o, itens_o = None, []
        if erps["olist"] is not None:
            etapa = "olist"
            token_o = oauth.obter_access_token("olist", repo_int, http)
            skus = itens[itens["quantidade_final"] > 0]["sku"].tolist()
            mapa_sku, faltantes = resolver_ids_olist(skus, repo_int, http, dormir)
            if faltantes:
                raise EmissaoFalhou(
                    f"{len(faltantes)} SKU(s) sem match no catálogo do Olist: "
                    f"{', '.join(faltantes[:8])}" + ("…" if len(faltantes) > 8 else ""))
            itens_o = olist.montar_itens_venda(itens, mapa_sku)

        if not revisao:
            revisao = repo_ped.registrar_revisao(
                pedido_id, estados.REVISAO_ALTERACAO, usuario, motivo)
            criada_agora = True

        # --- Olist primeiro (lado da fábrica) ---
        if erps["olist"] is not None:
            avisar(f"Olist: atualizando a venda nº {pedido.get('olist_numero') or '?'}…")
            olist.atualizar_itens_pedido(token_o, pedido["olist_id"], itens_o, http, dormir)
            tocou = True
            olist.atualizar_pedido(token_o, pedido["olist_id"], {
                "dataPrevista": str(pd.Timestamp(str(rodada["data_chegada"])).date()),
                "observacoes": obs,
                "observacoesInternas": str(pedido["titulo"]),
            }, http, dormir)
            repo_ped.confirmar_revisao(revisao["id"], "olist")
            repo_int.registrar_evento(
                "olist", "alterar_venda", True, pedido_id=pedido_id, usuario=usuario,
                detalhe={"revisao": revisao.get("numero"), "motivo": revisao.get("motivo")})

        # --- Bling ---
        etapa = "bling"
        avisar(f"Bling: atualizando a compra nº {pedido.get('bling_numero') or '?'}…")
        res = bling.alterar_pedido_compra(token_b, pedido["bling_id"], payload_b, http)
        tocou = True
        repo_ped.confirmar_revisao(revisao["id"], "bling")
        repo_int.registrar_evento(
            "bling", "alterar_compra", True, pedido_id=pedido_id, usuario=usuario,
            detalhe={"revisao": revisao.get("numero"), "motivo": revisao.get("motivo"),
                     "alertas": res["alertas"]})

        repo_ped.confirmar_revisao(revisao["id"], concluir=True)
        if not repo_ped.transicionar_pedido(
                pedido_id, estados.ALTERACAO_ENVIANDO, destino, usuario):
            raise EmissaoFalhou(
                "Alteração enviada aos ERPs, mas a confirmação do estado falhou — "
                "use Concluir.")
        return {"revisao": revisao.get("numero"), "alertas": res["alertas"]}

    except Exception as exc:
        if not tocou:
            # Nenhum ERP foi tocado — volta a ser editável, sem revisão órfã
            if criada_agora:
                _sem_falhar(lambda: repo_ped.remover_revisao(revisao["id"]))
            repo_ped.transicionar_pedido(
                pedido_id, estados.ALTERACAO_ENVIANDO, estados.EM_ALTERACAO, usuario)
        if isinstance(exc, DivergenciaNoErp):
            raise                                   # pergunta, não falha: sem evento
        repo_int.registrar_evento(
            etapa, "alterar_pedido", False, pedido_id=pedido_id, usuario=usuario,
            detalhe={"erro": str(exc)[:300], "tocou_erp": tocou})
        if isinstance(exc, EmissaoFalhou):
            raise
        raise EmissaoFalhou(str(exc)) from exc


def _sem_falhar(fn) -> None:
    """Limpeza de melhor esforço dentro de um tratamento de erro."""
    try:
        fn()
    except Exception:
        pass


def planejar_cancelamento(pedido: dict, erp_bling, erp_olist) -> tuple:
    """
    O que fazer em cada ERP para cancelar (PURO): ({"bling", "olist"}, impedimentos).
    Por ERP: "cancelar" (está em aberto), "feito" (já está cancelado lá — o
    cancelamento à mão no ERP só precisa ser registrado aqui) ou None (o pedido
    não existe naquele ERP). Qualquer outra situação é impedimento: pedido em
    andamento/atendido/faturado não se cancela por aqui.
    """
    acoes, impedimentos = {"bling": None, "olist": None}, []
    if _tem(pedido.get("bling_id")):
        valor = (erp_bling or {}).get("situacao_valor")
        if not erp_bling:
            impedimentos.append("Bling: não foi possível ler a situação da compra.")
        elif valor == bling.SITUACAO_CANCELADO:
            acoes["bling"] = "feito"
        elif valor == bling.SITUACAO_EM_ABERTO:
            acoes["bling"] = "cancelar"
        else:
            impedimentos.append(
                f"Bling: a compra nº {pedido.get('bling_numero') or '?'} está "
                f"**{erp_bling.get('situacao_rotulo')}** — só pedido Em aberto "
                "pode ser cancelado.")
    if _tem(pedido.get("olist_id")):
        situacao = (erp_olist or {}).get("situacao")
        if not erp_olist:
            impedimentos.append("Olist: não foi possível ler a situação da venda.")
        elif situacao == olist.SITUACAO_CANCELADA:
            acoes["olist"] = "feito"
        elif situacao == olist.SITUACAO_ABERTA:
            acoes["olist"] = "cancelar"
        else:
            impedimentos.append(
                f"Olist: a venda nº {pedido.get('olist_numero') or '?'} está "
                f"**{erp_olist.get('situacao_rotulo')}** — só pedido Aberta "
                "pode ser cancelado.")
    return acoes, impedimentos


def validar_pre_cancelamento(cfg_bling: dict) -> list:
    """Config que o cancelamento exige (PURO). Lista vazia = pode."""
    if not str((cfg_bling or {}).get("situacao_cancelado_id") or "").strip():
        return ["Config do Bling sem a situação de cancelado — escolha na aba Integrações."]
    return []


def cancelar_emitido(pedido_id: str, motivo: str, usuario: str, repo_ped, repo_int,
                     http=None, dormir=None, progresso=None) -> dict:
    """
    Pedido emitido → cancela a venda no Olist e a compra no Bling → CANCELADO.
    Muda a SITUAÇÃO nos ERPs (não exclui): os pedidos ficam lá como registro, e
    `bling_id`/`olist_id` ficam aqui como trilha. Chamado sobre um pedido em
    CANCELAMENTO_ENVIANDO é a retomada ("Concluir"): o que já está cancelado
    num ERP conta como feito.

    Retorna {"bling": acao, "olist": acao} com o que foi feito em cada lado.
    """
    avisar = progresso or (lambda _texto: None)
    pedido = repo_ped.obter_pedido(pedido_id)
    if not pedido:
        raise EmissaoFalhou(f"Pedido {pedido_id} não encontrado.")
    status = pedido.get("status", "")
    retomando = status == estados.CANCELAMENTO_ENVIANDO
    if not retomando:
        if not estados.alteravel(status):
            raise EmissaoFalhou(
                f"Pedido está {status} — só pedido emitido e em repouso pode ser "
                "cancelado nos ERPs.")
        if not str(motivo or "").strip():
            raise EmissaoFalhou("Informe o motivo do cancelamento.")
        if not repo_ped.transicionar_pedido(
                pedido_id, status, estados.CANCELAMENTO_ENVIANDO, usuario):
            raise EmissaoFalhou(
                "Outra sessão está agindo neste pedido (ou o estado mudou) — recarregue.")

    tocou = retomando
    etapa = "bling"
    revisao, criada_agora = {}, False
    try:
        avisar("Conferindo a situação do pedido nos ERPs…")
        erps = consultar_erps(pedido, repo_int, http, dormir)
        acoes, impedimentos = planejar_cancelamento(pedido, erps["bling"], erps["olist"])
        if impedimentos:
            raise EmissaoFalhou(" ".join(impedimentos))

        # --- Preparo, ainda sem tocar em ERP ---
        token_b = id_cancelado = token_o = None
        if acoes["bling"] == "cancelar":
            cfg_b = (repo_int.ler("bling") or {}).get("config") or {}
            faltas = validar_pre_cancelamento(cfg_b)
            if faltas:
                raise EmissaoFalhou(" ".join(faltas))
            id_cancelado = cfg_b["situacao_cancelado_id"]
            token_b = oauth.obter_access_token("bling", repo_int, http)
        if acoes["olist"] == "cancelar":
            token_o = oauth.obter_access_token("olist", repo_int, http)

        revisao = revisoes.pendente(repo_ped.listar_revisoes(pedido_id))
        if revisao.get("tipo") != estados.REVISAO_CANCELAMENTO:
            revisao = repo_ped.registrar_revisao(
                pedido_id, estados.REVISAO_CANCELAMENTO, usuario, motivo)
            criada_agora = True

        # --- Olist primeiro (lado da fábrica) ---
        if acoes["olist"]:
            etapa = "olist"
            if acoes["olist"] == "cancelar":
                avisar(f"Olist: cancelando a venda nº {pedido.get('olist_numero') or '?'}…")
                olist.cancelar_pedido(token_o, pedido["olist_id"], http, dormir)
                tocou = True
            repo_ped.confirmar_revisao(revisao["id"], "olist")
            repo_int.registrar_evento(
                "olist", "cancelar_venda", True, pedido_id=pedido_id, usuario=usuario,
                detalhe={"acao": acoes["olist"], "motivo": revisao.get("motivo")})

        # --- Bling ---
        etapa = "bling"
        if acoes["bling"] == "cancelar":
            avisar(f"Bling: cancelando a compra nº {pedido.get('bling_numero') or '?'}…")
            bling.mudar_situacao_compra(token_b, pedido["bling_id"], id_cancelado, http)
            tocou = True
            bling.conferir_cancelamento_compra(token_b, pedido["bling_id"], http)
        repo_ped.confirmar_revisao(revisao["id"], "bling")
        repo_int.registrar_evento(
            "bling", "cancelar_compra", True, pedido_id=pedido_id, usuario=usuario,
            detalhe={"acao": acoes["bling"], "motivo": revisao.get("motivo")})

        repo_ped.confirmar_revisao(revisao["id"], concluir=True)
        if not repo_ped.transicionar_pedido(
                pedido_id, estados.CANCELAMENTO_ENVIANDO, estados.CANCELADO, usuario):
            raise EmissaoFalhou(
                "Cancelado nos ERPs, mas a confirmação do estado falhou — use Concluir.")
        return acoes

    except Exception as exc:
        if not tocou:
            if criada_agora:
                _sem_falhar(lambda: repo_ped.remover_revisao(revisao["id"]))
            repo_ped.transicionar_pedido(
                pedido_id, estados.CANCELAMENTO_ENVIANDO,
                estados.estado_emitido(pedido), usuario)
        repo_int.registrar_evento(
            etapa, "cancelar_pedido", False, pedido_id=pedido_id, usuario=usuario,
            detalhe={"erro": str(exc)[:300], "tocou_erp": tocou})
        if isinstance(exc, EmissaoFalhou):
            raise
        raise EmissaoFalhou(str(exc)) from exc
