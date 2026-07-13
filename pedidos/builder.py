"""
builder.py — Monta os payloads do congelamento (puro, sem I/O de rede).

Recebe o DataFrame de sugestão por SKU (etl/fabrica.py::processar_fabrica) e
o config, e devolve dicts prontos para o repositório inserir no schema `app`:

    snapshot  = montar_snapshot_rodada(df, config, mes, ano, ...)
    grupos    = agrupar_pedidos(df, produtos, usuario, mes, ano)

Regra central: o builder trabalha sobre o DataFrame CONGELADO (calculado no
clique), nunca sobre dados vivos — o motor ancora em Timestamp.now() e
recalcular depois dá números diferentes.

Também define o padrão de escrita das observações internas do Bling
(montar_titulo / montar_observacoes_bling): o título estático é persistido em
pedido_compra.titulo; o bloco completo é sempre recomposto no momento do uso
(preview/CSV na Fase 0, emissão na fase futura) para refletir as quantidades
finais editadas — nunca texto pré-gravado que envelhece.
"""

import json

import pandas as pd

from etl import demanda


# Rótulos para dimensões vazias — mantêm a unique constraint
# (rodada_id, colegio, super_categoria) significativa
SEM_COLEGIO = "(sem colégio)"
SEM_SUPERCATEGORIA = "(sem supercategoria)"


def _normalizar_dim(valor, fallback: str) -> str:
    """Normaliza Colegio/SuperCategoria: NaN/''/'nan' viram o rótulo fallback."""
    if valor is None or pd.isna(valor):
        return fallback
    s = str(valor).strip()
    if not s or s.lower() == "nan":
        return fallback
    return s


def identidade_rodada(config: dict, mes_disparo: int, ano_disparo: int,
                      data_referencia) -> dict:
    """
    Identidade ABSOLUTA da rodada (datas de disparo/chegada) a partir do
    calendário config["planejamento"]["rodadas_datas"]. O nº da rodada é
    índice na lista e muda se o calendário for editado — por isso o snapshot
    congela as datas, não o número.

    Levanta ValueError se a rodada não existir na sequência (rodada passada,
    calendário editado, ou fallback de cobertura fixa sem rodadas).
    """
    data_referencia = pd.Timestamp(data_referencia).normalize()
    for r in demanda.sequencia_rodadas(config, data_referencia):
        if r["mes_disparo"] == mes_disparo and r["ano_disparo"] == ano_disparo:
            return {
                "mes_disparo": int(r["mes_disparo"]),
                "ano_disparo": int(r["ano_disparo"]),
                "data_disparo": pd.Timestamp(r["data_disparo"]),
                "data_chegada": pd.Timestamp(r["data_chegada"]),
                "data_chegada_seguinte": pd.Timestamp(r["data_chegada_seguinte"]),
                "rodada_numero": int(r["numero"]),
            }
    raise ValueError(
        f"Rodada {mes_disparo:02d}/{ano_disparo} não encontrada no calendário "
        f"de rodadas (planejamento.rodadas_datas) a partir de "
        f"{data_referencia.date()} — só rodadas futuras do calendário podem ser congeladas."
    )


def montar_snapshot_rodada(df_resultado: pd.DataFrame, config: dict,
                           mes_disparo: int, ano_disparo: int,
                           ativo_crescimento: bool, usuario: str,
                           data_referencia=None) -> dict:
    """
    Dict pronto para INSERT em app.rodada_congelada (sem id/status — o
    repositório controla o ciclo CONGELANDO→ABERTA).

    resultado_skus = saída INTEGRAL do processar_fabrica (todos os SKUs,
    inclusive sugestão 0) — documento de conferência posterior.
    config_snapshot = config completo — elimina o risco de esquecer um bloco
    que influencia o motor (colegios, excecoes_sku, grupo_segmento...).
    """
    data_referencia = (pd.Timestamp.now().normalize() if data_referencia is None
                       else pd.Timestamp(data_referencia).normalize())
    ident = identidade_rodada(config, mes_disparo, ano_disparo, data_referencia)

    janela_label = str(df_resultado["JanelaLabel"].iloc[0]) if len(df_resultado) else ""

    # to_json → loads: sanitiza NaN→null e tipos numpy (int64/float64 não são
    # JSON-serializáveis; to_dict("records") cru quebraria no httpx)
    resultado_skus = json.loads(df_resultado.to_json(orient="records", date_format="iso"))
    # default=str: tolera datetime.date/Timestamp que o yaml.safe_load produz
    # em campos de data do config.yaml
    config_snapshot = json.loads(json.dumps(config, default=str))

    return {
        "mes_disparo": ident["mes_disparo"],
        "ano_disparo": ident["ano_disparo"],
        "data_disparo": ident["data_disparo"].strftime("%Y-%m-%d"),
        "data_chegada": ident["data_chegada"].strftime("%Y-%m-%d"),
        "data_chegada_seguinte": ident["data_chegada_seguinte"].strftime("%Y-%m-%d"),
        "rodada_numero": ident["rodada_numero"],
        "janela_label": janela_label,
        "data_referencia": data_referencia.strftime("%Y-%m-%d"),
        "congelada_por": str(usuario),
        "ativo_crescimento": bool(ativo_crescimento),
        "config_snapshot": config_snapshot,
        "resultado_skus": resultado_skus,
    }


def agrupar_pedidos(df_resultado: pd.DataFrame, produtos: pd.DataFrame,
                    usuario: str, mes_disparo: int, ano_disparo: int) -> list:
    """
    Divide a sugestão congelada em pedidos por Colégio × SuperCategoria
    ("Calças do Neves", "Camisetas do Neves"...). Só SKUs com sugestão > 0.

    Retorna [{colegio, super_categoria, titulo, criado_por, itens: [...]}],
    itens com quantidade_final = quantidade_sugerida (ponto de partida da
    edição) e id_produto_bling congelado agora (produto pode inativar depois).
    """
    com_producao = df_resultado[df_resultado["SugestaoProducao"] > 0].copy()
    if len(com_producao) == 0:
        return []

    # SKU (codigo) → ID Bling, a partir de produtos ativos (loader já limpou IDs)
    prod = produtos[["ID", "codigo"]].drop_duplicates(subset=["codigo"], keep="last")
    id_por_sku = prod.set_index("codigo")["ID"].astype(str).to_dict()

    com_producao["_colegio"] = com_producao["Colegio"].map(
        lambda v: _normalizar_dim(v, SEM_COLEGIO))
    com_producao["_supercat"] = com_producao["SuperCategoria"].map(
        lambda v: _normalizar_dim(v, SEM_SUPERCATEGORIA))

    grupos = []
    for (colegio, supercat), g in com_producao.groupby(["_colegio", "_supercat"], sort=True):
        itens = []
        for _, linha in g.iterrows():
            sku = str(linha["SKU"])
            itens.append({
                "sku": sku,
                "id_produto_bling": id_por_sku.get(sku, ""),
                "produto": str(linha.get("Produto", "")),
                "tamanho": str(linha.get("Tamanho", "")),
                "categoria": str(linha.get("Categoria", "")),
                "quantidade_sugerida": int(linha["SugestaoProducao"]),
                "quantidade_final": int(linha["SugestaoProducao"]),
                "custo_unit": round(float(linha.get("CustoUnit", 0) or 0), 2),
            })
        grupos.append({
            "colegio": colegio,
            "super_categoria": supercat,
            "titulo": montar_titulo(colegio, supercat, mes_disparo, ano_disparo),
            "criado_por": str(usuario),
            "itens": itens,
        })
    return grupos


def validar_pre_congelamento(df_resultado: pd.DataFrame) -> list:
    """
    Checks puros antes de congelar. Lista de erros vazia = pode congelar.
    """
    erros = []
    if df_resultado is None or len(df_resultado) == 0:
        erros.append("A sugestão de produção está vazia — nada para congelar.")
        return erros

    com_producao = df_resultado[df_resultado["SugestaoProducao"] > 0]
    if len(com_producao) == 0:
        erros.append("Nenhum SKU com sugestão de produção > 0 — nada para pedir.")

    duplicados = com_producao["SKU"][com_producao["SKU"].duplicated()].unique().tolist()
    if duplicados:
        erros.append(f"SKUs duplicados na sugestão: {', '.join(map(str, duplicados[:5]))}")

    if "JanelaLabel" not in df_resultado.columns or not str(df_resultado["JanelaLabel"].iloc[0]).strip():
        erros.append("Rodada sem identificação de janela (JanelaLabel ausente).")

    return erros


# =================================================================
# Padrão de escrita p/ o campo de observações internas do Bling
# =================================================================

def montar_titulo(colegio: str, super_categoria: str,
                  mes_disparo: int, ano_disparo: int) -> str:
    """
    Título padronizado, curto e escaneável nas listagens do Bling.
    Ex: 'AKU-PC · NEVES · CALÇAS · R08/2026'
    """
    return (f"AKU-PC · {str(colegio).strip().upper()} · "
            f"{str(super_categoria).strip().upper()} · "
            f"R{int(mes_disparo):02d}/{int(ano_disparo)}")


def _fmt_brl(x: float) -> str:
    return f"R$ {x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _mes_ano(ts) -> str:
    ts = pd.Timestamp(str(ts))
    return f"{demanda.NOMES_MES[ts.month - 1]}/{ts.year}"


def montar_observacoes_bling(rodada: dict, pedido: dict, itens: pd.DataFrame) -> str:
    """
    Texto padronizado COMPLETO para o campo de observações internas do pedido
    de compra no Bling (API v3: observacoesInternas — nome exato do campo é
    verificado na fase de emissão).

    Determinístico e sempre recomposto no momento do uso: os totais refletem
    quantidade_final (edições pós-congelamento), nunca texto pré-gravado.
    O 'ref:' (app.pedido_compra.id) é a chave de reconciliação entre o nosso
    pedido e o futuro espelho public.pedidos_compra.
    """
    disparo = pd.Timestamp(f"{int(rodada['ano_disparo'])}-{int(rodada['mes_disparo']):02d}-01")

    validos = itens[itens["quantidade_final"] > 0]
    n_skus = len(validos)
    pares = int(validos["quantidade_final"].sum())
    investimento = float((validos["quantidade_final"] * validos["custo_unit"]).sum())

    congelada_em = pd.Timestamp(str(rodada["congelada_em"])).strftime("%d/%m/%Y") \
        if rodada.get("congelada_em") else "—"

    return "\n".join([
        str(pedido["titulo"]),
        (f"Rodada: disparo {_mes_ano(disparo)} → chega {_mes_ano(rodada['data_chegada'])} · "
         f"cobre {_mes_ano(rodada['data_chegada'])}→{_mes_ano(rodada['data_chegada_seguinte'])}"),
        f"Itens: {n_skus} SKUs · {pares} pares · {_fmt_brl(investimento)}",
        (f"Origem: Simulador AKU · congelada em {congelada_em} "
         f"por {rodada.get('congelada_por', '—')} · ref: {pedido.get('id', '—')}"),
    ])
