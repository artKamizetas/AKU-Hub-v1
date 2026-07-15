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
SINCRONIZADO = "SINCRONIZADO"         # reservado (fase do sincronizador)
CANCELADO = "CANCELADO"

# --- Estados da rodada congelada ---
RODADA_CONGELANDO = "CONGELANDO"   # inserção multi-request em andamento/abortada
RODADA_ABERTA = "ABERTA"           # congelamento concluído (commit lógico)
RODADA_CANCELADA = "CANCELADA"     # descartada — libera novo congelamento

# Transições permitidas: de → {para}
TRANSICOES = {
    RASCUNHO: {PRONTO, CANCELADO},
    PRONTO: {RASCUNHO, COMPRA_EMITINDO, CANCELADO},   # RASCUNHO = "reabrir p/ edição"
    COMPRA_EMITINDO: {COMPRA_EMITIDA, PRONTO},        # falha/destravar → volta a PRONTO
    COMPRA_EMITIDA: {VENDA_EMITINDO},                 # cancelar pós-emissão é no ERP, não aqui
    VENDA_EMITINDO: {EMITIDO, COMPRA_EMITIDA},        # falha/destravar → volta a COMPRA_EMITIDA
    EMITIDO: {SINCRONIZADO},                          # reservado (sincronizador futuro)
    SINCRONIZADO: set(),
    CANCELADO: set(),
}

# Badges p/ exibição na UI
ROTULOS_BADGE = {
    RASCUNHO: "📝 Rascunho",
    PRONTO: "✅ Pronto",
    COMPRA_EMITINDO: "⏳ Emitindo compra…",
    COMPRA_EMITIDA: "🛒 Compra emitida",
    VENDA_EMITINDO: "⏳ Emitindo venda…",
    EMITIDO: "📨 Emitido (Bling+Olist)",
    SINCRONIZADO: "🔄 Sincronizado",
    CANCELADO: "🚫 Cancelado",
}


def pode_transicionar(de: str, para: str) -> bool:
    """True se a transição de → para é permitida pela máquina de estados."""
    return para in TRANSICOES.get(de, set())


def editavel(status: str) -> bool:
    """Itens do pedido só podem ser editados em RASCUNHO (trava real no banco)."""
    return status == RASCUNHO


def emitindo(status: str) -> bool:
    """True para os locks transientes de emissão (UI mostra 'destravar')."""
    return status in (COMPRA_EMITINDO, VENDA_EMITINDO)
