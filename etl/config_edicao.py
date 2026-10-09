"""
etl/config_edicao.py — Regras PURAS de escrita dos editores de Colégios.

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
