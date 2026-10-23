"""
estados.py — Máquina de estados do Pedido de Compra (pura, sem I/O).

Tabela de verdade compartilhada entre repositorio.py (validação de transição),
emissor.py (locks de emissão) e a UI (badges, habilitar edição/ações).

A emissão acontece em DOIS momentos separados (decisão de negócio):
primeiro a COMPRA no Bling (conta AK Uniformes), depois a VENDA no Olist
(conta Art Kamizetas — o `numeroOrdemCompra` do Olist referencia o nº do
Bling, por isso a ordem é obrigatória). Os estados transientes *_EMITINDO
são o LOCK anti duplo-clique (compare-and-swap), mesmo papel do CONGELANDO
da rodada: só uma sessão vence o CAS e chama o ERP; falha → rollback para
o estado anterior. SINCRONIZADO segue reservado (sincronizador futuro).

DEPOIS da emissão o pedido ainda pode ser ALTERADO ou CANCELADO, sobrepondo os
dois ERPs — desde que esteja em aberto nos dois (o status nativo de cada ERP é
o filtro; ver emissor.avaliar_abertura). EM_ALTERACAO é um estado de REPOUSO
editável: o que está em `quantidade_final` é rascunho da alteração, e o que
está nos ERPs é a linha de base (última revisão concluída). Não se reusa o
RASCUNHO: ele significa "não existe nos ERPs" e libera cancelar pedido/rodada.
ALTERACAO_ENVIANDO / CANCELAMENTO_ENVIANDO são os locks CAS do envio — como
PUT/PATCH são idempotentes, uma falha no meio se resolve reexecutando
("Concluir"), sem o risco de duplicata do POST da emissão.

ALTERACAO_PRONTA é o PRONTO do pós-emissão: a digitação da alteração acabou e
os itens travam (o trigger do banco só libera RASCUNHO/EM_ALTERACAO). O envio
SÓ sai dele — mesmo desenho de RASCUNHO → PRONTO → emitir. Descartar exige
reabrir antes (o descarte reescreve os itens, que em PRONTA estão travados).

CANCELADO → RASCUNHO ("reabrir cancelado") é a única saída de CANCELADO, e só
para pedido que NUNCA foi aos ERPs, numa rodada ainda ABERTA
(impedimentos_reabrir). Cancelado nos ERPs fica como registro: o grupo volta
numa próxima rodada. A guarda vive em repositorio.reabrir_cancelado — o
transicionar_pedido genérico recusa sair de CANCELADO.

Estados da RODADA congelada (CONGELANDO/ABERTA/CANCELADA) vivem só no
repositório — a rodada não tem transições de negócio além do congelamento.
"""

# --- Estados do pedido ---
RASCUNHO = "RASCUNHO"
PRONTO = "PRONTO"
COMPRA_EMITINDO = "COMPRA_EMITINDO"   # lock: emissão da compra (Bling) em curso
COMPRA_EMITIDA = "COMPRA_EMITIDA"     # compra criada no Bling; venda pendente
VENDA_EMITINDO = "VENDA_EMITINDO"     # lock: emissão da venda (Olist) em curso
EMITIDO = "EMITIDO"                   # compra (Bling) + venda (Olist) emitidas
EM_ALTERACAO = "EM_ALTERACAO"         # pós-emissão: itens editáveis, ERPs ainda na versão anterior
ALTERACAO_PRONTA = "ALTERACAO_PRONTA" # pós-emissão: alteração digitada, itens travados, pronta p/ enviar
ALTERACAO_ENVIANDO = "ALTERACAO_ENVIANDO"        # lock: sobrepondo os ERPs com a alteração
CANCELAMENTO_ENVIANDO = "CANCELAMENTO_ENVIANDO"  # lock: cancelando nos ERPs
SINCRONIZADO = "SINCRONIZADO"         # reservado (fase do sincronizador)
CANCELADO = "CANCELADO"

# --- Estados da rodada congelada ---
RODADA_CONGELANDO = "CONGELANDO"   # inserção multi-request em andamento/abortada
RODADA_ABERTA = "ABERTA"           # congelamento concluído (commit lógico)
RODADA_CANCELADA = "CANCELADA"     # descartada — libera novo congelamento

# --- Origem do ITEM do pedido (DDL 007) ---
ORIGEM_SIMULACAO = "SIMULACAO"   # nasceu do congelamento (o motor sugeriu)
ORIGEM_MANUAL = "MANUAL"         # incluído pelo gestor no rascunho

# --- Tipo da REVISÃO (app.pedido_compra_revisao, DDL 008): o que foi aos ERPs ---
REVISAO_EMISSAO = "EMISSAO"            # a 1ª versão, criada na emissão da compra
REVISAO_ALTERACAO = "ALTERACAO"        # sobreposição dos itens pós-emissão
REVISAO_CANCELAMENTO = "CANCELAMENTO"  # cancelamento nos ERPs

# Transições permitidas: de → {para}
# "Estado emitido" = COMPRA_EMITIDA ou EMITIDO (ver estado_emitido): alteração e
# cancelamento pós-emissão saem dele e voltam a ele. CANCELADO a partir de um
# pedido emitido só passa pelo lock — nunca direto (o ERP precisa ser cancelado).
# CANCELADO → RASCUNHO só pela guarda (repositorio.reabrir_cancelado).
TRANSICOES = {
    RASCUNHO: {PRONTO, CANCELADO},
    PRONTO: {RASCUNHO, COMPRA_EMITINDO, CANCELADO},   # RASCUNHO = "reabrir p/ edição"
    COMPRA_EMITINDO: {COMPRA_EMITIDA, PRONTO},        # falha/destravar → volta a PRONTO
    COMPRA_EMITIDA: {VENDA_EMITINDO, EM_ALTERACAO, CANCELAMENTO_ENVIANDO},
    VENDA_EMITINDO: {EMITIDO, COMPRA_EMITIDA},        # falha/destravar → volta a COMPRA_EMITIDA
    EMITIDO: {SINCRONIZADO, EM_ALTERACAO, CANCELAMENTO_ENVIANDO},
    EM_ALTERACAO: {ALTERACAO_PRONTA, COMPRA_EMITIDA, EMITIDO},          # marcar pronta | descartar
    ALTERACAO_PRONTA: {EM_ALTERACAO, ALTERACAO_ENVIANDO},               # reabrir | enviar
    # ok | voltar a editar | falha ANTES de tocar ERP (volta à origem)
    ALTERACAO_ENVIANDO: {COMPRA_EMITIDA, EMITIDO, EM_ALTERACAO, ALTERACAO_PRONTA},
    CANCELAMENTO_ENVIANDO: {CANCELADO, COMPRA_EMITIDA, EMITIDO},        # ok | falha/destravar
    SINCRONIZADO: set(),
    CANCELADO: {RASCUNHO},                            # reabrir — só pela guarda
}

# Badges p/ exibição na UI
ROTULOS_BADGE = {
    RASCUNHO: "📝 Rascunho",
    PRONTO: "✅ Pronto",
    COMPRA_EMITINDO: "⏳ Emitindo compra…",
    COMPRA_EMITIDA: "🛒 Compra emitida",
    VENDA_EMITINDO: "⏳ Emitindo venda…",
    EMITIDO: "📨 Emitido (Bling+Olist)",
    EM_ALTERACAO: "✏️ Em alteração",
    ALTERACAO_PRONTA: "☑️ Alteração pronta",
    ALTERACAO_ENVIANDO: "⏳ Enviando alteração…",
    CANCELAMENTO_ENVIANDO: "⏳ Cancelando nos ERPs…",
    SINCRONIZADO: "🔄 Sincronizado",
    CANCELADO: "🚫 Cancelado",
}


def pode_transicionar(de: str, para: str) -> bool:
    """True se a transição de → para é permitida pela máquina de estados."""
    return para in TRANSICOES.get(de, set())


def editavel(status: str) -> bool:
    """
    Itens editáveis: RASCUNHO (antes da emissão) e EM_ALTERACAO (depois dela).
    A trava real é o trigger do banco (DDL 008), que aceita os mesmos dois.
    """
    return status in (RASCUNHO, EM_ALTERACAO)


def em_alteracao(status: str) -> bool:
    """
    Há uma versão pós-emissão pendente (digitando ou pronta p/ enviar): os ERPs
    seguem na linha de base e a tela compara contra ela.
    """
    return status in (EM_ALTERACAO, ALTERACAO_PRONTA)


def _tem_id(valor) -> bool:
    texto = "" if valor is None else str(valor).strip()
    return bool(texto) and texto.lower() not in ("nan", "none")


def impedimentos_reabrir(pedido, status_rodada: str) -> list:
    """
    Por que este pedido CANCELADO não pode voltar a RASCUNHO (lista vazia = pode).

    - Foi aos ERPs (bling_id/olist_id): lá ele está cancelado, e RASCUNHO
      significa "não existe nos ERPs" — reemitir criaria um 2º pedido.
    - Rodada não está ABERTA: rodada cancelada libera novo congelamento
      (uq_rodada_congelada_ativa), e reabrir deixaria dois pedidos vivos para o
      mesmo Colégio × Super Categoria.
    """
    get = pedido.get if hasattr(pedido, "get") else (lambda _k: None)
    if get("status") != CANCELADO:
        return ["Só pedido cancelado pode ser reaberto."]
    motivos = []
    if _tem_id(get("bling_id")) or _tem_id(get("olist_id")):
        motivos.append("Pedido já foi cancelado nos ERPs — este grupo volta a ser "
                       "comprado numa próxima rodada.")
    if status_rodada != RODADA_ABERTA:
        motivos.append("A rodada deste pedido não está aberta — congele uma nova rodada.")
    return motivos


def emitindo(status: str) -> bool:
    """True para os locks transientes de emissão (UI mostra 'destravar')."""
    return status in (COMPRA_EMITINDO, VENDA_EMITINDO)


def alteravel(status: str) -> bool:
    """
    Pedido que já existe em pelo menos um ERP e está em repouso: é daqui que se
    abre uma alteração ou um cancelamento pós-emissão. Poder de fato depende do
    status NATIVO de cada ERP (emissor.avaliar_abertura) — isto é só a nossa metade.
    """
    return status in (COMPRA_EMITIDA, EMITIDO)


def enviando_alteracao(status: str) -> bool:
    """
    Locks do envio pós-emissão. Separado de emitindo(): lá o "Destravar" avisa
    de DUPLICATA (POST não é idempotente); aqui reexecutar é seguro (PUT/PATCH).
    """
    return status in (ALTERACAO_ENVIANDO, CANCELAMENTO_ENVIANDO)


def estado_emitido(pedido) -> str:
    """
    Para onde o pedido volta ao sair de uma alteração/cancelamento: EMITIDO se
    a venda já foi ao Olist, senão COMPRA_EMITIDA. Derivado do `olist_id`, não
    gravado — não há como o "estado de origem" divergir do que os ERPs têm.
    """
    olist_id = pedido.get("olist_id") if hasattr(pedido, "get") else None
    return EMITIDO if _tem_id(olist_id) else COMPRA_EMITIDA
