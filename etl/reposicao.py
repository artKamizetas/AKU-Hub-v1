"""
reposicao.py — Regras PURAS do Estoque-Alvo da loja (Reposição de Loja v2)

Um único número por loja × SKU, no lugar do antigo "VM + Pulmão":

    Demanda da loja    = demanda da rede (etl/demanda.py) × participação da loja no SKU
    Janela de proteção = cobertura da fase (alta | baixa) + prazo de entrega da loja
    Demanda da janela  = Σ demanda da loja nos dias [hoje, hoje + janela)
    Segurança          = Fator de Serviço × √(Demanda da janela × PA)
    Alvo ideal         = max(exposição mínima, ⌈Demanda da janela + Segurança⌉)
    Alvo               = Alvo ideal limitado pelo espaço (arara + gavetas do modelo)

Aqui ficam as peças pequenas e testáveis; quem as costura é
`etl/logistica.py::processar_logistica`. Sem streamlit. Spec:
docs/requisitos/reposicao-loja-v2.md.
"""

import math

import pandas as pd

from etl import demanda

FASE_ALTA = "alta"
FASE_BAIXA = "baixa"

ORIGEM_CADASTRO = "cadastro"
ORIGEM_VENDAS = "vendas"

CAPACIDADE_PADRAO = "_padrao"

DEFAULTS = {
    "exposicao_minima": 2,
    "cobertura_dias_alta": 3,
    "cobertura_dias_baixa": 15,
    "nivel_servico_default": 95,
    "aplicar_crescimento": True,
    "recolher_horizonte_dias": 120,
}

# Sortimento derivado das vendas: o colégio só conta se a loja vendeu ao menos
# isto em 12 meses (mesmo gate de volume do crescimento observado). Sem ele,
# uma peça avulsa de um colégio de outra cidade traria a grade inteira.
MIN_PECAS_SORTIMENTO = 30

# Chaves que já existiam no bloco `vm` e podem estar gravadas em app.parametros:
# valem enquanto a seção nova não for salva pela primeira vez.
_LEGADO_VM = ("nivel_servico_default", "aplicar_crescimento")


def parametros(config: dict) -> dict:
    """
    Parâmetros efetivos da reposição: `config["reposicao"]` → legado `config["vm"]`
    (só nível de serviço e toggle de crescimento) → DEFAULTS. Devolve também
    `lojas`, `capacidade_gaveta` e `sortimento` já como dicts (nunca None).
    """
    rep = config.get("reposicao") or {}
    vm = config.get("vm") or {}
    out = {}
    for chave, padrao in DEFAULTS.items():
        if rep.get(chave) is not None:
            out[chave] = rep[chave]
        elif chave in _LEGADO_VM and vm.get(chave) is not None:
            out[chave] = vm[chave]
        else:
            out[chave] = padrao
    out["lojas"] = rep.get("lojas") or {}
    out["capacidade_gaveta"] = rep.get("capacidade_gaveta") or {}
    out["sortimento"] = rep.get("sortimento") or {}
    return out


def fase_atual(config: dict, data_hoje) -> str:
    """Alta se o mês de hoje está em `demanda.janela_alta` (a mesma do Simulador)."""
    janela = (config.get("demanda", {}) or {}).get("janela_alta", [12, 1, 2])
    return FASE_ALTA if pd.Timestamp(data_hoje).month in {int(m) for m in janela} else FASE_BAIXA


def janela_protecao_dias(params: dict, fase: str, nome_loja: str) -> int:
    """Cobertura da fase + prazo de entrega da loja (dias). Nunca menor que 1."""
    cobertura = params["cobertura_dias_alta"] if fase == FASE_ALTA else params["cobertura_dias_baixa"]
    prazo = (params["lojas"].get(nome_loja) or {}).get("prazo_entrega_dias") or 0
    return max(int(cobertura) + int(prazo), 1)


def gavetas_da_loja(params: dict, nome_loja: str):
    """Nº de gavetas cadastradas da loja; None = sem cadastro (sem teto de espaço)."""
    valor = (params["lojas"].get(nome_loja) or {}).get("gavetas")
    return None if valor is None else max(int(valor), 0)


def capacidade_da_gaveta(params: dict, super_categoria: str) -> int:
    """Peças que cabem numa gaveta daquela super categoria (→ `_padrao` → 50)."""
    cap = params["capacidade_gaveta"]
    valor = cap.get(str(super_categoria).strip()) or cap.get(CAPACIDADE_PADRAO) or 50
    return max(int(valor), 1)


# =====================================================================
# Demanda por loja
# =====================================================================

def participacao_por_loja(dados: dict, config: dict) -> tuple:
    """
    Fatia de cada loja na venda de cada SKU, na MESMA janela em que o motor de
    demanda ancorou: a última alta completa; para o SKU sem venda na alta
    (só-de-baixa), os meses de baixa do período histórico.

    O denominador é a rede inteira (todas as lojas do Bling, inclusive a venda
    institucional), então o que não é de nenhuma loja física sai sozinho: a
    soma das participações de Natal + Mossoró pode ser < 1.

    Retorna `(participacao, pa)`:
      participacao[(id_produto, loja_id)] = fração 0-1
      pa[id_produto] = peças por pedido do SKU na rede (mín. 1.0)
    """
    itens = demanda.restringir_a_ativos(dados)["itens"]
    mapa_loja = dados["pedidos"].set_index("ID")["Loja ID"].to_dict()

    it = itens[["ID_produto", "ID_pedido", "Quantidade", "Data"]].copy()
    it["loja"] = it["ID_pedido"].map(mapa_loja).fillna("").astype(str).str.strip()

    temporada = demanda._ultima_temporada_alta(config)
    na_alta = pd.Series(False, index=it.index)
    for ts in temporada.values():
        na_alta |= (it["Data"].dt.year == ts.year) & (it["Data"].dt.month == ts.month)

    cfg_plan = config.get("planejamento", {}) or {}
    dt_ini = pd.Timestamp(str(cfg_plan.get("periodo_historico_inicio", "2025-01-01")))
    dt_fim = pd.Timestamp(str(cfg_plan.get("periodo_historico_fim", "2026-02-28")))
    na_baixa = ((it["Data"] >= dt_ini) & (it["Data"] <= dt_fim)
                & ~it["Data"].dt.month.isin(set(temporada.keys())))

    participacao, pa = {}, {}
    ja_resolvidos = set()
    for base in (it[na_alta], it[na_baixa]):     # a alta tem prioridade
        rede = base.groupby("ID_produto")["Quantidade"].sum()
        pedidos = base.groupby("ID_produto")["ID_pedido"].nunique()
        loja = base.groupby(["ID_produto", "loja"])["Quantidade"].sum()
        novos = {idp for idp, q in rede.items() if q > 0 and idp not in ja_resolvidos}
        for (idp, id_loja), q in loja.items():
            if idp in novos and id_loja:
                participacao[(idp, id_loja)] = float(q) / float(rede[idp])
        for idp in novos:
            n_ped = int(pedidos.get(idp, 0))
            pa[idp] = max(float(rede[idp]) / n_ped, 1.0) if n_ped > 0 else 1.0
        ja_resolvidos |= novos
    return participacao, pa


def demanda_da_janela(demanda_por_mes: dict, fracoes: list) -> float:
    """
    Demanda nos dias da janela: Σ demanda do mês × fração de dias do mês dentro
    da janela. `fracoes` = saída de `demanda.fracionar_janela_por_mes` (a janela
    olha para FRENTE, então no fim de dezembro já carrega janeiro).
    """
    return float(sum(float(demanda_por_mes.get(mes, 0.0)) * fracao for mes, fracao in fracoes))


def seguranca_loja(demanda_janela: float, pa: float, nivel_servico: float) -> float:
    """
    Segurança = Fator de Serviço × √(demanda da janela × PA).

    A venda de loja é intermitente (o SKU mediano sai 1×/semana no pico) e em
    pacotes (um cliente leva várias peças): é um Poisson composto, cuja variância
    é ≈ demanda × peças por atendimento. Substitui o desvio-padrão diário medido
    em anos de histórico, que media sazonalidade e não ruído.
    """
    if demanda_janela <= 0:
        return 0.0
    fator = demanda._nivel_para_z(nivel_servico)
    return fator * math.sqrt(demanda_janela * max(float(pa), 1.0))


def alvo_ideal(demanda_janela: float, seguranca: float, exposicao_minima: int) -> int:
    """Quanto a loja guardaria se espaço não fosse problema (nunca abaixo da exposição)."""
    return max(int(exposicao_minima), math.ceil(demanda_janela + seguranca - 1e-9))


# =====================================================================
# Sortimento (quais colégios cada loja atende)
# =====================================================================

def sortimento_efetivo(params: dict, nomes_lojas: list, colegios_vendidos: dict) -> tuple:
    """
    Colégios de cada loja. Loja COM chave em `reposicao.sortimento` usa o
    cadastro (lista vazia = não atende nada); loja SEM chave cai no derivado das
    vendas (`colegios_vendidos[nome]`), para a tela funcionar antes do cadastro.

    Retorna `(sortimento, origem)`: {loja: set(colégios)}, {loja: "cadastro"|"vendas"}.
    """
    cadastro = params["sortimento"]
    sortimento, origem = {}, {}
    for nome in nomes_lojas:
        if nome in cadastro:
            sortimento[nome] = {str(c).strip() for c in (cadastro[nome] or []) if str(c).strip()}
            origem[nome] = ORIGEM_CADASTRO
        else:
            sortimento[nome] = set(colegios_vendidos.get(nome) or set())
            origem[nome] = ORIGEM_VENDAS
    return sortimento, origem


def vendas_por_colegio_loja(dados: dict, config: dict, data_hoje, meses: int = 12) -> dict:
    """{nome_loja: {colégio: peças}} vendidas na loja nos últimos `meses`."""
    itens = demanda.restringir_a_ativos(dados)["itens"]
    detalhes = demanda.aplicar_alias_colegio(dados["detalhes"], config)
    mapa_loja = dados["pedidos"].set_index("ID")["Loja ID"].to_dict()
    mapa_colegio = detalhes.set_index("ID_produto")["Marca_sku"].to_dict()

    corte = pd.Timestamp(data_hoje) - pd.DateOffset(months=meses)
    recentes = itens[itens["Data"] >= corte]
    vendas = pd.DataFrame({
        "loja": recentes["ID_pedido"].map(mapa_loja).fillna("").astype(str).str.strip(),
        "colegio": recentes["ID_produto"].map(mapa_colegio).fillna("").astype(str).str.strip(),
        "qtd": recentes["Quantidade"],
    })
    por_loja = vendas[vendas["colegio"] != ""].groupby(["loja", "colegio"])["qtd"].sum()

    resultado = {}
    for loja_cfg in config["depositos"]["lojas"]:
        id_loja = str(loja_cfg["loja_id"]).strip()
        resultado[loja_cfg["nome"]] = {
            colegio: float(q) for (loja, colegio), q in por_loja.items() if loja == id_loja}
    return resultado


def colegios_vendidos_por_loja(dados: dict, config: dict, data_hoje, meses: int = 12,
                               min_pecas: int = MIN_PECAS_SORTIMENTO) -> dict:
    """
    {nome_loja: set(colégios)} com ao menos `min_pecas` vendidas na loja nos
    últimos `meses` — a sugestão de sortimento, e o que vale enquanto a loja não
    tem cadastro.
    """
    vendas = vendas_por_colegio_loja(dados, config, data_hoje, meses)
    return {loja: {c for c, q in por_colegio.items() if q >= min_pecas}
            for loja, por_colegio in vendas.items()}


# =====================================================================
# Espaço (gavetas)
# =====================================================================

def distribuir_gavetas(fundo_por_modelo: dict, capacidade_por_modelo: dict, n_gavetas) -> dict:
    """
    Distribui as gavetas da loja entre os modelos. `fundo` = peças do modelo que
    não cabem na arara (Σ tamanhos de `alvo ideal − exposição`).

    Uma a uma, a gaveta vai para o modelo com mais fundo ainda descoberto — um
    campeão pode levar mais de uma. `n_gavetas=None` (loja sem cadastro) = sem
    teto: todo modelo recebe as que pedir.

    Retorna {modelo: (gavetas, peças cobertas)} só para quem tem fundo > 0.
    """
    pedidas = []                       # (peças que a gaveta cobre, modelo, ordem)
    for modelo, fundo in fundo_por_modelo.items():
        fundo = int(fundo)
        capacidade = max(int(capacidade_por_modelo.get(modelo, 50)), 1)
        ordem = 0
        while fundo > 0:
            cobre = min(capacidade, fundo)
            pedidas.append((cobre, str(modelo), ordem))
            fundo -= cobre
            ordem += 1

    # Dentro de um modelo a cobertura não cresce de uma gaveta para a seguinte,
    # então pegar as N maiores do conjunto respeita a ordem (a 2ª nunca entra
    # sem a 1ª); `ordem` desempata gavetas iguais do mesmo modelo.
    pedidas.sort(key=lambda g: (-g[0], g[1], g[2]))
    concedidas = pedidas if n_gavetas is None else pedidas[:max(int(n_gavetas), 0)]

    resultado = {modelo: (0, 0) for modelo, fundo in fundo_por_modelo.items() if int(fundo) > 0}
    chave = {str(m): m for m in resultado}
    for cobre, modelo, _ in concedidas:
        gavetas, coberto = resultado[chave[modelo]]
        resultado[chave[modelo]] = (gavetas + 1, coberto + cobre)
    return resultado


def aplicar_teto_espaco(ideais: dict, exposicao_minima: int, pecas_cobertas: int) -> dict:
    """
    Alvo de cada SKU do modelo dado o fundo que as gavetas cobrem.

    Fundo todo coberto → alvo = ideal. Coberto em parte → cada tamanho recebe a
    sua proporção do que cabe (maiores restos levam as sobras, para a soma fechar
    exatamente no que cabe). Sem gaveta → fica na exposição.
    """
    base = {sku: min(int(ideal), int(exposicao_minima)) for sku, ideal in ideais.items()}
    fundo = {sku: max(int(ideal) - base[sku], 0) for sku, ideal in ideais.items()}
    total = sum(fundo.values())
    cabe = max(int(pecas_cobertas), 0)
    if total <= cabe:
        return {sku: int(ideal) for sku, ideal in ideais.items()}

    cota = {sku: f * cabe / total for sku, f in fundo.items()}
    extra = {sku: int(math.floor(c)) for sku, c in cota.items()}
    sobra = cabe - sum(extra.values())
    for sku in sorted(cota, key=lambda s: (-(cota[s] - extra[s]), str(s)))[:sobra]:
        extra[sku] += 1
    return {sku: base[sku] + extra[sku] for sku in ideais}


# =====================================================================
# Rateio do CD e excesso
# =====================================================================

def ratear_cd(necessidades: dict, estoques: dict, venda_diaria: dict, saldo_cd) -> dict:
    """
    Divide o saldo do CD entre as lojas que precisam do mesmo SKU.

    Se o CD cobre todo mundo, cada loja leva o que precisa. Se não, a peça vai,
    uma a uma, para QUEM VAI ZERAR PRIMEIRO: a loja com menos dias de cobertura
    `(estoque + já alocado) ÷ venda diária`. Loja sem venda prevista fica por
    último; empate → maior necessidade restante. Garante Σ alocado ≤ saldo.
    """
    saldo = max(int(saldo_cd), 0)
    restante = {loja: max(int(n), 0) for loja, n in necessidades.items()}
    alocado = {loja: 0 for loja in necessidades}
    if sum(restante.values()) <= saldo:
        return dict(restante)

    def cobertura(loja):
        taxa = float(venda_diaria.get(loja) or 0.0)
        if taxa <= 0:
            return math.inf
        return (max(float(estoques.get(loja) or 0.0), 0.0) + alocado[loja]) / taxa

    ordem = {loja: i for i, loja in enumerate(necessidades)}
    while saldo > 0:
        candidatas = [loja for loja, n in restante.items() if n > 0]
        if not candidatas:
            break
        escolhida = min(candidatas, key=lambda l: (cobertura(l), -restante[l], ordem[l]))
        alocado[escolhida] += 1
        restante[escolhida] -= 1
        saldo -= 1
    return alocado


def excesso_loja(estoque_loja, alvo: int, demanda_horizonte: float, em_sortimento: bool) -> int:
    """
    Peças que podem voltar ao CD. Só é excesso o que a loja não vende no
    horizonte (`recolher_horizonte_dias`) — sem isso, novembro mandaria recolher
    o que janeiro vende. Fora do sortimento, todo o estoque é excesso.
    """
    estoque = max(float(estoque_loja or 0), 0.0)
    if not em_sortimento:
        return int(math.floor(estoque))
    piso = max(int(alvo), math.ceil(float(demanda_horizonte) - 1e-9))
    return max(int(math.floor(estoque - piso)), 0)
