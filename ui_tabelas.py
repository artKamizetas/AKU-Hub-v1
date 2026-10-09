"""
ui_tabelas.py — Padrão ÚNICO das tabelas do app.

Cada tabela nasceu tela a tela: alturas soltas (400, 500, 560, 640), cinco
formatadores de R$ duplicados, `R$ 125430` sem ponto de milhar e a mesma
grandeza com três nomes ("Sugestão Qtd", "Sugestão (pares)", "Qtd Sugerida").
Este módulo é o molde: a página diz QUAL É O TRABALHO da tabela e recebe o
desenho pronto.

Os quatro tipos (o que o usuário FAZ com a tabela):

    FILA     percorre linha a linha e age      linha compacta, ~24 linhas visíveis
    MEMORIA  confere como o número saiu        linha compacta, atrás de expander/toggle
    PLACAR   compara poucos itens              linha normal, sem rolagem
    EDITOR   digita valores                    linha normal (alvo de clique maior)

Uso nas páginas:

    from ui_tabelas import FILA, exibir, col_sku, col_pecas, col_moeda

    exibir(df, FILA, {
        "SKU": col_sku(),
        "SugestaoProducao": col_pecas("Sugestão"),
        "InvestimentoFabril": col_moeda("Investimento", casas=2),
    })

    # editor: o data_editor é da página; o molde entra pelos kwargs
    st.data_editor(df, **padrao_tabela(EDITOR, len(df)), column_config={...})

NÚMEROS. As colunas usam `format="localized"` do Streamlit, que segue o idioma
do NAVEGADOR: `125.430` em pt-BR, `125,430` em inglês — degradação aceitável
(o formato anterior, printf, não agrupava milhar em idioma nenhum). A unidade
vai no cabeçalho ("Investimento (R$)"), não repetida em cada célula.

ARREDONDAMENTO. O Streamlit TRUNCA o número na precisão da coluna (15.482,9
apareceria 15.482). `exibir()` arredonda antes, pelas casas declaradas em cada
coluna — por isso tabela só de leitura passa por `exibir()`, não por
`st.dataframe` direto.
"""

import math

import pandas as pd
import streamlit as st
from pandas.io.formats.style import Styler


FILA = "fila"
MEMORIA = "memoria"
PLACAR = "placar"
EDITOR = "editor"

# Altura do cabeçalho: fixa no Streamlit (2.1875rem), não é configurável.
ALTURA_CABECALHO = 35
# Bordas da grade + 1 px de folga: sem ela a última linha gera barra de rolagem.
_FOLGA = 3

# 28 px é o piso legível para o texto de 14 px das células (7 px de respiro).
# Só onde o usuário VARRE muitas linhas; onde ele clica para editar fica em 35.
ALTURA_LINHA = {FILA: 28, MEMORIA: 28, PLACAR: 35, EDITOR: 35}

# Linhas visíveis antes de rolar. FILA = o que cabe num monitor Full HD com os
# filtros acima, sem rolar a página.
MAX_LINHAS = {FILA: 24, MEMORIA: 16, PLACAR: 15, EDITOR: 15}


# =================================================================
# Texto fora de tabela (KPIs, legendas, rodapés)
# =================================================================
def num(v, casas: int = 0) -> str:
    """Número em pt-BR (milhar '.', decimal ','). Vazio/NaN vira '—'."""
    if v is None or pd.isna(v):
        return "—"
    s = f"{v:,.{casas}f}"
    return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def brl(v, casas: int = 0) -> str:
    """Valor em Real, pt-BR. Vazio/NaN vira '—'."""
    texto = num(v, casas)
    return texto if texto == "—" else f"R$ {texto}"


# =================================================================
# Altura e kwargs do tipo
# =================================================================
def altura_tabela(n_linhas: int, tipo: str, max_linhas: int = None) -> int:
    """Altura em px que mostra `n_linhas` (até o teto do tipo) sem sobra nem barra."""
    teto = max_linhas or MAX_LINHAS[tipo]
    visiveis = max(1, min(int(n_linhas), teto))
    return visiveis * ALTURA_LINHA[tipo] + ALTURA_CABECALHO + _FOLGA


def padrao_tabela(tipo: str, n_linhas: int, max_linhas: int = None) -> dict:
    """kwargs comuns a `st.dataframe` e `st.data_editor` para o tipo."""
    return {
        "width": "stretch",
        "hide_index": True,
        "row_height": ALTURA_LINHA[tipo],
        "height": altura_tabela(n_linhas, tipo, max_linhas),
        # Sem isto o Streamlit escreve "None" em toda célula sem valor. Vazio é
        # o correto: meta não cadastrada, cobertura sem antecipação.
        "placeholder": "",
    }


def _casas_da_coluna(coluna) -> int | None:
    """Casas decimais declaradas numa coluna numérica (pelo `step`), ou None."""
    if not coluna:
        return None
    tipo_cfg = coluna.get("type_config") or {}
    passo = tipo_cfg.get("step")
    if tipo_cfg.get("type") != "number" or not passo:
        return None
    return max(0, -int(math.floor(math.log10(passo) + 1e-9)))


def arredondar_para_exibicao(df: pd.DataFrame, colunas: dict) -> pd.DataFrame:
    """
    Cópia de `df` com as colunas float arredondadas nas casas da sua coluna.

    Existe porque o Streamlit trunca em vez de arredondar. Não toca em coluna
    inteira, de texto, ou sem casas declaradas.
    """
    casas = {
        nome: _casas_da_coluna(cfg) for nome, cfg in (colunas or {}).items()
        if nome in df.columns and pd.api.types.is_float_dtype(df[nome])
    }
    casas = {nome: c for nome, c in casas.items() if c is not None}
    return df.round(casas) if casas else df


def exibir(df, tipo: str, colunas: dict = None, *, max_linhas: int = None, **kwargs):
    """
    `st.dataframe` com o padrão do tipo. Devolve o que o `st.dataframe` devolve
    (o evento de seleção, quando `on_select` é passado).

    Aceita Styler (cor de exceção); nesse caso quem chama arredonda antes de
    estilizar — o Styler não deixa trocar o dado depois.
    """
    eh_styler = isinstance(df, Styler)
    n_linhas = len(df.data) if eh_styler else len(df)
    if not eh_styler:
        df = arredondar_para_exibicao(df, colunas)
    return st.dataframe(
        df, **padrao_tabela(tipo, n_linhas, max_linhas),
        column_config=colunas, **kwargs,
    )


# =================================================================
# Cor de exceção — "alterado à mão"
# =================================================================
# Âmbar + negrito = decisão manual que diverge do cálculo (atenção, não erro:
# vermelho segue reservado a ruptura/negativo). Texto escuro fixo para o
# contraste não depender do tema; o negrito mantém o sinal legível para quem
# não distingue a cor.
ESTILO_ALTERADO = "background-color: #FFE7A0; color: #3D3000; font-weight: 700"


def destacar(df: pd.DataFrame, celulas, estilo: str = ESTILO_ALTERADO) -> Styler:
    """
    Styler de `df` com `estilo` nas células indicadas — `celulas` é uma lista
    de (posição da linha, nome da coluna). Célula vazia continua vazia.

    Serve também ao `st.data_editor`, com um limite do Streamlit: o estilo só
    aparece em coluna NÃO editável (as editáveis o ignoram por inteiro — cor e
    negrito). Em compensação o Styler não entra na identidade do widget (só o
    dado entra): dá para repintar a cada tecla sem o editor reiniciar e perder
    o que foi digitado.
    """
    mapa = pd.DataFrame("", index=df.index, columns=df.columns)
    for pos, coluna in celulas:
        if coluna in mapa.columns and 0 <= int(pos) < len(mapa):
            mapa.iat[int(pos), mapa.columns.get_loc(coluna)] = estilo
    return df.style.apply(lambda _: mapa, axis=None).format(na_rep="")


# =================================================================
# Colunas prontas — o vocabulário único do app
# =================================================================
def col_texto(rotulo: str, *, ajuda: str = None, largura=None, fixa: bool = None):
    return st.column_config.TextColumn(rotulo, help=ajuda, width=largura, pinned=fixa)


def col_sku(rotulo: str = "SKU", *, ajuda: str = None):
    """Identidade da linha: fica FIXA à esquerda na rolagem lateral."""
    return st.column_config.TextColumn(rotulo, help=ajuda, pinned=True)


def col_produto(rotulo: str = "Produto", *, ajuda: str = None, largura="medium"):
    return st.column_config.TextColumn(rotulo, help=ajuda, width=largura)


def col_colegio(rotulo: str = "Colégio", *, ajuda: str = None):
    return st.column_config.TextColumn(rotulo, help=ajuda)


def col_tamanho(rotulo: str = "Tam.", *, ajuda: str = None):
    return st.column_config.TextColumn(rotulo, help=ajuda, width="small")


def col_numero(rotulo: str, *, casas: int = 0, ajuda: str = None, largura=None,
               **kwargs):
    """Número com milhar e `casas` decimais. `kwargs` vão ao NumberColumn (editor)."""
    return st.column_config.NumberColumn(
        rotulo, help=ajuda, width=largura, format="localized",
        step=10 ** -casas if casas else 1, **kwargs,
    )


def col_pecas(rotulo: str, *, casas: int = 0, ajuda: str = None, largura="small",
              **kwargs):
    """Quantidade em peças. Estreita por padrão — é o que deixa caber mais colunas."""
    return col_numero(rotulo, casas=casas, ajuda=ajuda, largura=largura, **kwargs)


def col_moeda(rotulo: str, *, casas: int = 0, ajuda: str = None, largura=None,
              **kwargs):
    """Valor em Real: a unidade vai no cabeçalho — "Investimento (R$)"."""
    return col_numero(f"{rotulo} (R$)", casas=casas, ajuda=ajuda, largura=largura,
                      **kwargs)


def col_pct(rotulo: str, *, casas: int = 0, ajuda: str = None, largura="small",
            **kwargs):
    """Percentual na escala 0–100 (o dado NÃO é fração)."""
    return st.column_config.NumberColumn(
        rotulo, help=ajuda, width=largura, format=f"%.{casas}f%%", **kwargs)


def col_progresso(rotulo: str, *, maximo: float = 100, ajuda: str = None):
    """Barra de progresso de um percentual na escala 0–100."""
    return st.column_config.ProgressColumn(
        rotulo, help=ajuda, format="%.0f%%", min_value=0, max_value=maximo)


def col_data(rotulo: str, *, hora: bool = False, ajuda: str = None):
    if hora:
        return st.column_config.DatetimeColumn(rotulo, help=ajuda, format="DD/MM/YYYY HH:mm")
    return st.column_config.DateColumn(rotulo, help=ajuda, format="DD/MM/YYYY")
