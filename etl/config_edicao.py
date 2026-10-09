"""
etl/config_edicao.py — Regras PURAS de escrita dos editores de Colégios e da
Reposição de Loja (lojas, capacidade das gavetas, sortimento).

Transformam o que sai do `data_editor` da página de Configurações no dict
`config["colegios"]` gravado em app.parametros. Vivem aqui, e não na página,
pelo mesmo motivo de `metas.aplicar_edicao_metas`: são caminho de ESCRITA — um
bug aqui muda o crescimento da rede inteira sem erro nenhum — e precisam de
teste sem Streamlit nem Supabase.

Regra de ouro: só vira override o que o planejador DIGITOU. Na cascata de
`demanda.taxa_crescimento_efetiva` a simples presença de `taxa_crescimento` no
colégio já vence o crescimento medido; gravar o default em todas as linhas (o
que a tela fazia) desligaria a camada observada no primeiro "Salvar".

Sem streamlit/pandas — só dicts.
"""

import math

# Campos editáveis por colégio → conversor do valor gravado.
CAMPOS_COLEGIO = {
    "taxa_crescimento": float,
    "nivel_servico": int,
    "proporcao_baixa": float,
}

TOLERANCIA = 1e-6


def _vazio(valor) -> bool:
    """Célula sem valor: None, texto em branco ou NaN (como o editor devolve)."""
    if valor is None:
        return True
    if isinstance(valor, str):
        return not valor.strip()
    try:
        return math.isnan(valor)
    except TypeError:
        return False


def _copia(colegios: dict) -> dict:
    return {c: dict(v) for c, v in (colegios or {}).items() if isinstance(v, dict)}


def aplicar_edicao_colegios(colegios: dict, linhas: list) -> tuple:
    """
    Aplica a tabela "Por colégio" sobre o dict `colegios`.

    `linhas`: dicts com `colegio` e os campos de CAMPOS_COLEGIO. Célula
    PREENCHIDA vira override; célula VAZIA remove o override (o colégio volta
    a seguir o medido/padrão). Devolve `(novo_dict, n_overrides)`. Não muta a
    entrada.

    `crescimento_grupos` (a matriz por série) e os colégios que não estão na
    tabela passam intactos; colégio que ficou sem nenhum campo sai do dict.
    """
    novo = _copia(colegios)
    n_overrides = 0
    for linha in linhas:
        colegio = str(linha.get("colegio") or "").strip()
        if not colegio:
            continue
        entrada = dict(novo.get(colegio) or {})
        for campo, converter in CAMPOS_COLEGIO.items():
            valor = linha.get(campo)
            if _vazio(valor):
                entrada.pop(campo, None)
            else:
                entrada[campo] = converter(valor)
                n_overrides += 1
        if entrada:
            novo[colegio] = entrada
        else:
            novo.pop(colegio, None)
    return novo, n_overrides


def aplicar_edicao_crescimento_grupos(colegios: dict, linhas: list) -> tuple:
    """
    Aplica a matriz "Por série" (colégio × grupo) sobre o dict `colegios`.

    `linhas`: dicts com `colegio`, `grupo`, `taxa_crescimento` (o que o
    planejador deixou na célula) e `base` (o que o motor aplicaria SEM ajuste
    da série). Só vira override a célula que DIFERE da base; a que ficou igual
    — ou vazia — continua viva e re-mede sozinha a cada temporada. Devolve
    `(novo_dict, n_overrides)`. Não muta a entrada.

    Os demais campos do colégio e os colégios fora da matriz passam intactos.
    """
    novo = _copia(colegios)
    por_colegio = {}
    for linha in linhas:
        colegio = str(linha.get("colegio") or "").strip()
        grupo = str(linha.get("grupo") or "").strip()
        if not colegio or not grupo:
            continue
        ajustes = por_colegio.setdefault(colegio, {})
        taxa, base = linha.get("taxa_crescimento"), linha.get("base")
        if _vazio(taxa):
            continue
        if _vazio(base) or abs(float(taxa) - float(base)) > TOLERANCIA:
            ajustes[grupo] = round(float(taxa), 4)

    n_overrides = 0
    for colegio, ajustes in por_colegio.items():
        entrada = dict(novo.get(colegio) or {})
        if ajustes:
            entrada["crescimento_grupos"] = ajustes
            n_overrides += len(ajustes)
        else:
            entrada.pop("crescimento_grupos", None)
        if entrada:
            novo[colegio] = entrada
        else:
            novo.pop(colegio, None)
    return novo, n_overrides


# =====================================================================
# Reposição de Loja — `config["reposicao"]`
# =====================================================================

CAPACIDADE_PADRAO = "_padrao"


def _inteiro_ou_none(valor, minimo: int = 0):
    return None if _vazio(valor) else max(int(round(float(valor))), minimo)


def aplicar_edicao_lojas(linhas: list) -> dict:
    """
    Tabela "Lojas" → `reposicao.lojas` = {loja: {prazo_entrega_dias, gavetas}}.

    `gavetas` VAZIO não é zero: a chave fica de fora e a loja segue SEM teto de
    espaço (zero gavetas é uma decisão — a loja só tem a arara). Prazo vazio
    vira 0. O bloco é substituído inteiro no merge, então toda loja da tabela
    sai daqui.
    """
    novo = {}
    for linha in linhas:
        loja = str(linha.get("loja") or "").strip()
        if not loja:
            continue
        entrada = {"prazo_entrega_dias": _inteiro_ou_none(linha.get("prazo_entrega_dias")) or 0}
        gavetas = _inteiro_ou_none(linha.get("gavetas"))
        if gavetas is not None:
            entrada["gavetas"] = gavetas
        novo[loja] = entrada
    return novo


def aplicar_edicao_capacidade(linhas: list, padrao) -> dict:
    """
    Tabela "Capacidade da gaveta" → `reposicao.capacidade_gaveta`.

    Só a super categoria PREENCHIDA e diferente do padrão vira entrada; a vazia
    segue o `_padrao`, que assim pode mudar sem ter de reeditar linha a linha.
    """
    base = _inteiro_ou_none(padrao, minimo=1) or 50
    novo = {CAPACIDADE_PADRAO: base}
    for linha in linhas:
        super_categoria = str(linha.get("super_categoria") or "").strip()
        pecas = _inteiro_ou_none(linha.get("pecas"), minimo=1)
        if super_categoria and super_categoria != CAPACIDADE_PADRAO and pecas is not None and pecas != base:
            novo[super_categoria] = pecas
    return novo


def aplicar_edicao_sortimento(linhas: list, lojas: list) -> dict:
    """
    Matriz "Colégio × loja" → `reposicao.sortimento` = {loja: [colégios]}.

    TODA loja de `lojas` sai com chave, mesmo sem nenhum colégio marcado: é a
    presença da chave que diz ao motor "isto é cadastro" — sem ela a loja
    voltaria ao derivado das vendas e o desmarcado ressuscitaria.
    """
    novo = {str(loja): [] for loja in lojas}
    for linha in linhas:
        colegio = str(linha.get("colegio") or "").strip()
        if not colegio:
            continue
        for loja in novo:
            if linha.get(loja) is True:
                novo[loja].append(colegio)
    return {loja: sorted(set(colegios)) for loja, colegios in novo.items()}
