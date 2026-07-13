"""
estados.py — Máquina de estados do Pedido de Compra (pura, sem I/O).

Tabela de verdade compartilhada entre repositorio.py (validação de transição)
e a UI (badges, habilitar edição). Os estados EMITINDO/EMITIDO/SINCRONIZADO
são reservados para as fases futuras (emissão no Bling + sincronizador) —
nenhum código da Fase 0 transiciona para eles, mas o CHECK do banco e esta
tabela já os conhecem para não exigir migração depois.

Estados da RODADA congelada (CONGELANDO/ABERTA/CANCELADA) vivem só no
repositório — a rodada não tem transições de negócio além do congelamento.
"""

# --- Estados do pedido ---
RASCUNHO = "RASCUNHO"
PRONTO = "PRONTO"
EMITINDO = "EMITINDO"          # reservado (fase de emissão)
EMITIDO = "EMITIDO"            # reservado (fase de emissão)
SINCRONIZADO = "SINCRONIZADO"  # reservado (fase do sincronizador)
CANCELADO = "CANCELADO"

# --- Estados da rodada congelada ---
RODADA_CONGELANDO = "CONGELANDO"   # inserção multi-request em andamento/abortada
RODADA_ABERTA = "ABERTA"           # congelamento concluído (commit lógico)
RODADA_CANCELADA = "CANCELADA"     # descartada — libera novo congelamento

# Transições permitidas: de → {para}
TRANSICOES = {
    RASCUNHO: {PRONTO, CANCELADO},
    PRONTO: {RASCUNHO, EMITINDO, CANCELADO},   # RASCUNHO = "reabrir p/ edição"
    EMITINDO: {EMITIDO, PRONTO},               # reservado (fase futura)
    EMITIDO: {SINCRONIZADO},                   # reservado (fase futura)
    SINCRONIZADO: set(),
    CANCELADO: set(),
}

# Badges p/ exibição na UI
ROTULOS_BADGE = {
    RASCUNHO: "📝 Rascunho",
    PRONTO: "✅ Pronto",
    EMITINDO: "📤 Emitindo",
    EMITIDO: "📨 Emitido",
    SINCRONIZADO: "🔄 Sincronizado",
    CANCELADO: "🚫 Cancelado",
}


def pode_transicionar(de: str, para: str) -> bool:
    """True se a transição de → para é permitida pela máquina de estados."""
    return para in TRANSICOES.get(de, set())


def editavel(status: str) -> bool:
    """Itens do pedido só podem ser editados em RASCUNHO (trava real no banco)."""
    return status == RASCUNHO
