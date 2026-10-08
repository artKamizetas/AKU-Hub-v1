"""
catalogo.py — Catálogo de produtos para a INCLUSÃO MANUAL de itens (puro).

O pedido nasce da simulação, mas o gestor pode incluir à mão um produto que o
motor não sugeriu. Este módulo transforma `dados["produtos"]` +
`dados["detalhes"]` (já carregados pelo loader) na lista de onde ele escolhe, e
resolve as duas perguntas da tela:

    cat = montar_catalogo(produtos, detalhes)
    fam = listar_familias(cat)                       # o seletor de produto
    itens, faltantes = resolver_celulas(cat, novas)  # tamanhos digitados na grade

O catálogo é o VIVO (produtos ativos agora), não o snapshot da rodada: o caso de
uso é justamente o produto que a simulação não trouxe — inclusive um cadastrado
depois do congelamento.
"""

import pandas as pd

from pedidos import builder, grade
from pedidos.estados import ORIGEM_MANUAL

COLUNAS = ["sku", "id_produto_bling", "produto", "tamanho", "categoria",
           "super_categoria", "colegio", "custo_unit",
           "familia", "produto_pai", "tamanho_grade"]


def montar_catalogo(produtos: pd.DataFrame, detalhes: pd.DataFrame) -> pd.DataFrame:
    """
    Um registro por SKU ativo, com as mesmas dimensões que o builder grava no
    pedido (Colégio/SuperCategoria normalizados IGUAL ao agrupar_pedidos — é o
    que faz o filtro "mesmo colégio e supercategoria" casar com o pedido).

    Sai do catálogo o produto-PAI do Bling (o registro 'NEV019CLFEME' quando
    existem 'NEV019CLFEME-P', '-M'…): ele é o agrupador das variações, não uma
    peça que se compra. Sem esse corte ele apareceria na grade como um tamanho
    'Único' ao lado de P/M/G.
    """
    if produtos is None or len(produtos) == 0:
        return pd.DataFrame(columns=COLUNAS)

    det = detalhes[["ID_produto", "categoria", "Super_categoria", "Tamanho", "Marca_sku"]] \
        .drop_duplicates(subset=["ID_produto"], keep="last") \
        .rename(columns={"ID_produto": "ID"})
    base = produtos[["ID", "codigo", "Descricao", "preco_custo"]] \
        .drop_duplicates(subset=["codigo"], keep="last") \
        .merge(det, on="ID", how="left")

    skus = base["codigo"].astype(str).str.strip()
    cat = pd.DataFrame({
        "sku": skus.values,
        "id_produto_bling": base["ID"].astype(str).str.strip().values,
        "produto": base["Descricao"].fillna("").astype(str).values,
        "tamanho": [grade._texto(t) for t in base["Tamanho"]],
        "categoria": [grade._texto(c) for c in base["categoria"]],
        "super_categoria": [builder._normalizar_dim(v, builder.SEM_SUPERCATEGORIA)
                            for v in base["Super_categoria"]],
        "colegio": [builder._normalizar_dim(v, builder.SEM_COLEGIO)
                    for v in base["Marca_sku"]],
        "custo_unit": pd.to_numeric(base["preco_custo"], errors="coerce")
                        .fillna(0).round(2).values,
    })
    cat = cat[cat["sku"] != ""]

    pais = {grade.sku_pai(s) for s in cat["sku"]} - {None}
    cat = cat[~cat["sku"].isin(pais)].copy()

    # Linha/coluna pela MESMA regra da grade do pedido (grade.identificar)
    onde = grade.identificar(cat["sku"], cat["produto"], cat["tamanho"])
    cat["familia"] = onde["linha"].values
    cat["produto_pai"] = onde["produto"].values
    cat["tamanho_grade"] = onde["tamanho"].values
    return cat[COLUNAS].sort_values("sku").reset_index(drop=True)


def filtrar_escopo(catalogo: pd.DataFrame, colegio: str, super_categoria: str) -> pd.DataFrame:
    """Só o que pertence ao pedido — o título dele (COLÉGIO - SUPERCAT) vai para o Bling."""
    return catalogo[(catalogo["colegio"] == colegio)
                    & (catalogo["super_categoria"] == super_categoria)]


def listar_familias(catalogo: pd.DataFrame) -> pd.DataFrame:
    """Um registro por produto (família de tamanhos): familia, produto_pai, n_tamanhos."""
    if len(catalogo) == 0:
        return pd.DataFrame(columns=["familia", "produto_pai", "n_tamanhos"])
    return (catalogo.groupby("familia", sort=True)
            .agg(produto_pai=("produto_pai", "first"), n_tamanhos=("sku", "count"))
            .reset_index())


def tamanhos_da_familia(catalogo: pd.DataFrame, fam: str) -> pd.DataFrame:
    """SKUs de um produto, na ordem da grade (PP, P, M, G…)."""
    membros = catalogo[catalogo["familia"] == fam].copy()
    membros["_ordem"] = membros["tamanho_grade"].map(grade.chave_tamanho)
    return membros.sort_values("_ordem").drop(columns="_ordem").reset_index(drop=True)


def montar_itens_manuais(linhas: pd.DataFrame, quantidades: dict) -> list:
    """
    Linhas do catálogo + {sku: quantidade} → itens prontos para
    repositorio.adicionar_itens. Só entra quantidade > 0.

    quantidade_sugerida = 0 e memória vazia: o motor NÃO sugeriu este item, e a
    diferença para a quantidade_final é exatamente a auditoria da inclusão.
    """
    itens = []
    for _, p in linhas.iterrows():
        qtd = int(quantidades.get(p["sku"], 0) or 0)
        if qtd <= 0:
            continue
        itens.append({
            "sku": str(p["sku"]),
            "id_produto_bling": str(p["id_produto_bling"]),
            "produto": str(p["produto"]),
            "tamanho": str(p["tamanho"]),
            "categoria": str(p["categoria"]),
            "quantidade_sugerida": 0,
            "quantidade_final": qtd,
            "custo_unit": round(float(p["custo_unit"] or 0), 2),
            "memoria_sugerida": {},
            "origem": ORIGEM_MANUAL,
        })
    return itens


def resolver_celulas(catalogo: pd.DataFrame, novas: list) -> tuple:
    """
    Células novas da grade ([{"sku_pai", "tamanho", "quantidade"}], de
    grade.diff_grade) → (itens manuais, faltantes). Uma célula só vira item se
    aquele tamanho EXISTE no cadastro ativo; o resto volta em `faltantes`
    (['NEV019CLFEME · XXGG', …]) para a tela avisar — nunca se inventa SKU.
    """
    itens, faltantes = [], []
    for nova in novas:
        achado = catalogo[(catalogo["familia"] == nova["sku_pai"])
                          & (catalogo["tamanho_grade"] == nova["tamanho"])]
        if len(achado) != 1:
            faltantes.append(f"{nova['sku_pai']} · {nova['tamanho']}")
            continue
        itens += montar_itens_manuais(achado, {achado.iloc[0]["sku"]: nova["quantidade"]})
    return itens, faltantes
