"""
grade.py — Visão em GRADE dos itens do pedido (pura, sem I/O).

A lista de itens tem uma linha por SKU (= um tamanho). Quem produz pensa em
grade: um produto por linha, os tamanhos lado a lado — é onde uma grade furada
(tem P e G, falta M) fica visível. Este módulo faz a ida e a volta:

    grade, celulas = montar_grade(itens)        # itens → tabela pivotada
    alteracoes, novas = diff_grade(editada, itens, celulas)   # edição → escrita

A volta é por `id` do item (mapa `celulas`), nunca por posição: a grade não tem
a mesma ordem nem o mesmo nº de linhas da lista.

Também é a casa das três regras de leitura de SKU/produto usadas pelo catálogo
da inclusão manual (pedidos/catalogo.py) e pelo cliente do Olist — família do
SKU, nome sem a variação e ordem dos tamanhos. Uma regra só, um lugar só.
"""

import re

import pandas as pd


# Coluna dos itens sem tamanho nenhum (acessório, sob encomenda)
TAMANHO_UNICO = "Único"

COL_SKU = "SKU"
COL_PRODUTO = "Produto"

_VAZIOS = {"", "nan", "none", "null", "<na>"}

# Bloco de variação que o Bling cola no fim da descrição do produto-filho:
# ' Tamanho:M', ' Numeração:50', ' Idade:10', ' Numeração:29;COR:BRANCO'.
# É sempre UM token sem espaço no fim do texto — conferido no catálogo ativo
# (out/2026): casa os 1.173 produtos que têm pai e nenhum dos 258 que não têm.
_RE_VARIACAO = re.compile(r"\s+\S+:\S+\s*$")

# Escala de letras: prefixo de "extra" (X; E em 'EG'/'EXG') + PP…/M/G…
_RE_LETRAS = re.compile(r"^([XE]*)(P+|M|G+)$")
_RE_PLUS = re.compile(r"^G(\d+)$")          # G1, G2, G3 (plus size)
_RE_NUMERO = re.compile(r"^(\d+)")          # '02', '38', '30/36'


def _texto(valor) -> str:
    """str limpa; None/NaN/'nan' viram ''."""
    if valor is None:
        return ""
    if not isinstance(valor, str) and pd.isna(valor):
        return ""
    s = str(valor).strip()
    return "" if s.lower() in _VAZIOS else s


def sku_pai(sku: str):
    """
    SKU do produto-pai a partir do SKU-filho, tirando o sufixo de tamanho
    ('SES024MOLDIA-G' → 'SES024MOLDIA'). Heurística deliberadamente ingênua
    (corta no último '-'). None se não há '-' para cortar.
    """
    base = str(sku).rpartition("-")[0].strip()
    return base or None


def familia(sku: str) -> str:
    """Chave da linha da grade: o SKU pai, ou o próprio SKU se ele não tem variação."""
    return sku_pai(sku) or str(sku).strip()


def produto_sem_variacao(produto) -> str:
    """
    Nome do produto sem o bloco de variação do fim:
    'Neves - Calça Feminina em Helanca - EM Tamanho:M' → '… - EM'.
    Sem bloco de variação, devolve o texto como veio.
    """
    return _RE_VARIACAO.sub("", _texto(produto)).strip()


def _sufixo(sku) -> str:
    return str(sku).rpartition("-")[2].strip() if "-" in str(sku) else ""


def parece_tamanho(texto) -> bool:
    """
    True para o que é tamanho de verdade: a escala de letras (PP…XXGG, G1…) ou
    número de até 3 dígitos (02, 38, 110). Separa o sufixo 'KIDTEN01-34' (é a
    numeração) do '00436-0041509960' (é o código do fornecedor, não tamanho).
    """
    t = _texto(texto).upper()
    return bool(t) and (chave_tamanho(t)[0] == 0 or re.fullmatch(r"\d{1,3}", t) is not None)


def tamanho_efetivo(tamanho, sku) -> str:
    """
    Tamanho que vira COLUNA da grade: o campo `tamanho`; se vazio (o cadastro
    de tênis/meias não preenche), o sufixo do SKU quando ele parece tamanho;
    se nem isso, 'Único'.
    """
    t = _texto(tamanho)
    if t:
        return t
    sufixo = _sufixo(sku)
    return sufixo if parece_tamanho(sufixo) else TAMANHO_UNICO


def chave_tamanho(tamanho) -> tuple:
    """
    Chave de ordenação que segue a lógica da confecção, não a do alfabeto
    (que daria G, GG, M, P, PP…). Calculada por REGRA, não por lista fixa:

      bloco 0 — letras: cada P desce um degrau, M é o zero, cada G sobe um;
                o prefixo X (ou E) empurra para a ponta da escala.
                PPP < PP < P < M < G < GG < XG < XGG < XXG < XXGG < G1 < G2
      bloco 1 — números, pelo VALOR ('02' < '4' < '10' < '38'; '30/36' usa o 30)
      bloco 2 — o que não se reconhece, em ordem alfabética
      bloco 3 — 'Único', sempre por último

    Tamanho novo que siga o padrão (ex.: 'XXXGG') já cai no lugar certo sem
    ninguém mexer aqui.
    """
    t = _texto(tamanho).upper()
    if not t or t == TAMANHO_UNICO.upper():
        return (3, 0, "")

    m = _RE_LETRAS.match(t)
    if m:
        extras, corpo = len(m.group(1)), m.group(2)
        # o texto no fim desempata grafias do mesmo degrau ('EG' × 'XG') —
        # sem ele a ordem entre as duas mudaria de uma execução para outra
        if corpo == "M":
            return (0, 0, t)
        if corpo[0] == "P":
            return (0, -(len(corpo) + 10 * extras), t)
        return (0, len(corpo) + 10 * extras, t)

    m = _RE_PLUS.match(t)
    if m:
        return (0, 100 + int(m.group(1)), t)

    m = _RE_NUMERO.match(t)
    if m:
        return (1, int(m.group(1)), t)

    return (2, 0, t)


def ordenar_tamanhos(tamanhos) -> list:
    """Tamanhos distintos na ordem da grade (ver chave_tamanho)."""
    return sorted({str(t) for t in tamanhos}, key=chave_tamanho)


def identificar(skus, produtos, tamanhos) -> pd.DataFrame:
    """
    Onde cada SKU cai na grade: DataFrame [linha, produto, tamanho], na mesma
    ordem da entrada. É a regra ÚNICA usada pela grade do pedido e pelo
    catálogo da inclusão manual — as duas precisam dar o mesmo nome à mesma
    célula, senão o tamanho digitado na grade não acha o SKU no catálogo.

      1. caso normal: linha = SKU pai, produto = nome sem a variação,
         tamanho = tamanho_efetivo.
      2. SKU com '-' cujo sufixo NÃO é tamanho (revenda: '00436-0041509960'):
         não há família a agrupar — o SKU vira a própria linha, com o nome
         completo (a variação 'Tamanho:46/48;Cor:Preto' é o que o identifica).
      3. dois SKUs da mesma linha no MESMO tamanho (cadastro sujo): a linha
         inteira passa a usar o sufixo do SKU como coluna; se ainda colidir,
         cada SKU vira a própria linha. Nenhum item some numa célula alheia.
    """
    skus = [str(s).strip() for s in skus]
    cheio = [_texto(p) for p in produtos]
    base = pd.DataFrame({
        "sku": skus,
        "linha": [familia(s) for s in skus],
        "produto": [produto_sem_variacao(p) for p in cheio],
        "tamanho": [tamanho_efetivo(t, s) for t, s in zip(tamanhos, skus)],
        "_cheio": cheio,
    })
    if len(base) == 0:
        return base[["linha", "produto", "tamanho"]]

    def isolar(mascara):
        base.loc[mascara, "linha"] = base.loc[mascara, "sku"]
        base.loc[mascara, "produto"] = base.loc[mascara, "_cheio"]
        base.loc[mascara, "tamanho"] = TAMANHO_UNICO

    isolar((base["tamanho"] == TAMANHO_UNICO) & base["sku"].str.contains("-"))

    repetida = base.duplicated(["linha", "tamanho"], keep=False)
    sujas = base["linha"].isin(base.loc[repetida, "linha"])
    base.loc[sujas, "tamanho"] = [_sufixo(s) or TAMANHO_UNICO for s in base.loc[sujas, "sku"]]
    isolar(base.duplicated(["linha", "tamanho"], keep=False))

    return base[["linha", "produto", "tamanho"]]


def montar_grade(itens: pd.DataFrame) -> tuple:
    """
    Pivota os itens do pedido: linhas = SKU pai + produto (sem a variação),
    colunas = tamanhos na ordem da confecção, valores = quantidade_final.
    Célula vazia (None) = aquele tamanho não está no pedido — diferente de 0,
    que é um item que existe e foi zerado.

    Retorna (grade, celulas):
      grade   — DataFrame [SKU, Produto, <tamanho>…], índice 0..n-1
      celulas — {(sku_da_linha, tamanho): id do item} — o caminho de volta
    """
    if itens is None or len(itens) == 0:
        return pd.DataFrame(columns=[COL_SKU, COL_PRODUTO]), {}

    base = identificar(itens["sku"], itens["produto"], itens["tamanho"])
    base["id"] = itens["id"].values
    base["qtd"] = pd.to_numeric(itens["quantidade_final"],
                                errors="coerce").fillna(0).astype(int).values

    tamanhos = ordenar_tamanhos(base["tamanho"])
    celulas, linhas = {}, []
    for chave, g in base.groupby("linha", sort=True):
        registro = {COL_SKU: chave, COL_PRODUTO: g["produto"].iloc[0]}
        registro.update({t: None for t in tamanhos})
        for _, it in g.iterrows():
            registro[it["tamanho"]] = int(it["qtd"])
            celulas[(chave, it["tamanho"])] = it["id"]
        linhas.append(registro)

    grade = pd.DataFrame(linhas, columns=[COL_SKU, COL_PRODUTO] + tamanhos)
    # Int64 anulável: mantém a célula vazia vazia (float mostraria '10.0')
    for t in tamanhos:
        grade[t] = pd.to_numeric(grade[t], errors="coerce").astype("Int64")
    return grade, celulas


def _qtd(valor) -> int:
    """Célula da grade → inteiro ≥ 0 (vazia/apagada = 0)."""
    if valor is None or pd.isna(valor):
        return 0
    return max(int(valor), 0)


def quantidades_por_item(grade_editada: pd.DataFrame, celulas: dict) -> dict:
    """{id do item: quantidade} lida da grade — alimenta os totais ao vivo."""
    por_linha = grade_editada.set_index(COL_SKU)
    return {item_id: _qtd(por_linha.at[linha, tam])
            for (linha, tam), item_id in celulas.items()}


def diff_grade(grade_editada: pd.DataFrame, itens: pd.DataFrame, celulas: dict) -> tuple:
    """
    Compara a grade editada com o banco e separa as duas intenções:

      alteracoes — [{"id", "quantidade_final"}] dos itens que JÁ existem e
                   mudaram (mesmo formato de repositorio.atualizar_quantidades);
                   célula apagada = 0.
      novas      — [{"sku_pai", "tamanho", "quantidade"}] de células que não
                   tinham item e receberam quantidade > 0: um tamanho a
                   INCLUIR. Resolver contra o catálogo é trabalho de quem chama.
    """
    atual = dict(zip(itens["id"], pd.to_numeric(itens["quantidade_final"],
                                               errors="coerce").fillna(0).astype(int)))
    alteracoes = [
        {"id": item_id, "quantidade_final": qtd}
        for item_id, qtd in quantidades_por_item(grade_editada, celulas).items()
        if qtd != int(atual.get(item_id, 0))
    ]

    novas = []
    tamanhos = [c for c in grade_editada.columns if c not in (COL_SKU, COL_PRODUTO)]
    for _, linha in grade_editada.iterrows():
        for tam in tamanhos:
            if (linha[COL_SKU], tam) in celulas:
                continue
            qtd = _qtd(linha[tam])
            if qtd > 0:
                novas.append({"sku_pai": linha[COL_SKU], "tamanho": tam, "quantidade": qtd})
    return alteracoes, novas


# ---------------------------------------------------------------------------
# O que foi alterado em relação à sugestão (destaque na tela)
# ---------------------------------------------------------------------------
def aplicar_edicoes(df: pd.DataFrame, edicoes: dict) -> pd.DataFrame:
    """
    Cópia de `df` com as edições pendentes do `st.data_editor` aplicadas —
    `edicoes` é o `edited_rows` do estado do widget ({posição da linha:
    {coluna: valor}}; célula apagada vem como None). É o que o editor vai
    devolver, calculado ANTES de ele ser desenhado: o destaque das quantidades
    alteradas precisa saber o que foi digitado para pintar a tabela.
    """
    out = df.copy()
    for pos, colunas in (edicoes or {}).items():
        pos = int(pos)
        if not 0 <= pos < len(out):
            continue
        for coluna, valor in colunas.items():
            if coluna not in out.columns:
                continue
            if valor is None and pd.api.types.is_integer_dtype(out[coluna]):
                out[coluna] = out[coluna].astype("Int64")   # int64 puro não guarda vazio
            out.loc[out.index[pos], coluna] = pd.NA if valor is None else valor
    return out


def divergencias(itens: pd.DataFrame, qtd_por_item: dict, base: dict = None) -> list:
    """
    Itens cuja quantidade VIGENTE (salva + digitada, por id) difere da de
    REFERÊNCIA: [{"id", "sku", "tamanho", "sugerida", "final"}], na ordem de
    `itens`. Item fora de `qtd_por_item` conta como 0.

    Sem `base`, a referência é o que o cálculo sugeriu — item incluído à mão
    tem sugerida 0 e por isso sempre aparece (o motor não o sugeriu).

    Com `base` ({id do item: quantidade}), a referência passa a ser ela: é a
    alteração pós-emissão, em que o que importa é "difere do que está nos
    ERPs". Item fora da base foi incluído durante a alteração (referência 0).
    A chave do retorno continua `sugerida` — é a quantidade de referência.
    """
    out = []
    for item_id, sku, tamanho, sugerida in zip(
            itens["id"], itens["sku"], itens["tamanho"], itens["quantidade_sugerida"]):
        referencia = _qtd(sugerida) if base is None else _qtd(base.get(item_id))
        final = _qtd(qtd_por_item.get(item_id))
        if final != referencia:
            out.append({"id": item_id, "sku": sku,
                        "tamanho": tamanho_efetivo(tamanho, sku),
                        "sugerida": referencia, "final": final})
    return out
