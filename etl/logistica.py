"""
logistica.py — Reposição de Loja (orquestrador do Estoque-Alvo)

Para cada loja × SKU: quanto a loja deveria ter (Alvo), quanto o CD separa
agora (Separar), o que o CD não cobre (Falta) e o que pode voltar (Excesso).
As regras vivem em `etl/reposicao.py`; aqui só se costuram os DataFrames.

Giro é mantido como indicador informativo e NÃO entra na conta.
"""

import math
from datetime import timedelta

import pandas as pd

from etl import demanda, reposicao
from pedidos import grade

ACAO_CORRIGIR = "🚫 Corrigir estoque"
ACAO_SEM_CD = "🚨 Sem estoque no CD"
ACAO_PARCIAL = "⚠️ Repor parcial"
ACAO_REPOR = "✨ Repor"
ACAO_RECOLHER = "↩️ Recolher"
ACAO_OK = "✅ OK"

# Ordem da fila: o que exige ação primeiro
ORDEM_ACOES = [ACAO_CORRIGIR, ACAO_SEM_CD, ACAO_PARCIAL, ACAO_REPOR, ACAO_RECOLHER, ACAO_OK]

COLUNAS = [
    "Loja", "SKU", "Produto", "Modelo", "Tamanho", "Colegio", "Categoria", "SuperCategoria",
    "Acao", "Separar", "Falta", "Excesso", "MotivoExcesso",
    "EstoqueLoja", "EstoqueCentral", "Alvo", "AlvoIdeal", "Necessidade",
    "EmTransito", "ChegadaPrevista",
    "Fase", "JanelaDias", "DemandaJanela", "PA", "NivelServico", "Seguranca",
    "Participacao", "TaxaCresc", "Gavetas", "LimitadoPorEspaco",
    "EmSortimento", "SortimentoOrigem", "GiroGlobal", "GiroLoja",
]


def _classificar(est_loja, est_central, separar, falta, excesso) -> str:
    if est_loja < 0 or est_central < 0:
        return ACAO_CORRIGIR
    if separar > 0:
        return ACAO_PARCIAL if falta > 0 else ACAO_REPOR
    if falta > 0:
        return ACAO_SEM_CD
    if excesso > 0:
        return ACAO_RECOLHER
    return ACAO_OK


def _motivo_excesso(excesso, em_sortimento: bool, alvo_ideal: int, horizonte: int,
                    colegio: str) -> str:
    if excesso <= 0:
        return ""
    if not em_sortimento:
        return "Colégio fora do sortimento da loja" if colegio else "Sem colégio e sem venda na loja"
    if alvo_ideal == 0:
        return "Sem venda na rede (produto novo ou parado)"
    return f"Acima do que a loja vende em {horizonte} dias"


def _resumo_em_transito(em_transito) -> tuple:
    """(qtd por produto, 1ª chegada por produto) do que está para chegar ao CD."""
    if em_transito is None or len(em_transito) == 0:
        return {}, {}
    df = em_transito.copy()
    df["ID_produto"] = df["ID_produto"].astype(str).str.strip()
    qtd = df.groupby("ID_produto")["Quantidade"].sum().to_dict()
    chegada = {}
    if "DataPrevista" in df.columns:
        chegada = pd.to_datetime(df["DataPrevista"], errors="coerce").groupby(df["ID_produto"]).min().to_dict()
    return qtd, chegada


def processar_logistica(dados: dict, config: dict, em_transito: pd.DataFrame = None,
                        data_hoje=None) -> pd.DataFrame:
    """
    Fila de reposição por loja × SKU (colunas em `COLUNAS`).

    `em_transito` (opcional): DataFrame [ID_produto, Quantidade, DataPrevista] do
    que já foi comprado e ainda não chegou ao CD. Sem ele as colunas
    `EmTransito`/`ChegadaPrevista` saem vazias — o resto da conta não depende dele.

    Só entram linhas que importam: SKU do sortimento da loja, ou com saldo na loja.
    """
    data_hoje = pd.Timestamp.now().normalize() if data_hoje is None else pd.Timestamp(data_hoje).normalize()
    params = reposicao.parametros(config)
    cfg_dep = config["depositos"]
    lojas_cfg = cfg_dep["lojas"]
    nomes_lojas = [l["nome"] for l in lojas_cfg]
    id_central = str(cfg_dep["central"]["deposito_id"]).strip()
    dias_giro = (config.get("logistica") or {}).get("dias_analise_giro", 30)
    exposicao = int(params["exposicao_minima"])
    horizonte = int(params["recolher_horizonte_dias"])
    map_colegios = config.get("colegios") or {}

    produtos = dados["produtos"]
    itens = dados["itens"]
    detalhes = demanda.aplicar_alias_colegio(dados["detalhes"], config)

    # ---------------- Demanda da rede (motor do Simulador) ----------------
    dem = demanda.calcular_demanda_mensal_por_sku(
        dados, config, ativo_crescimento=bool(params["aplicar_crescimento"]))
    demanda_mes = {}                     # id_produto → {mês: demanda da rede}
    for idp, mes, q in zip(dem["ID_produto"], dem["Mes"], dem["DemandaMensalProjetada"]):
        demanda_mes.setdefault(idp, {})[int(mes)] = float(q)
    taxa_cresc = dem.drop_duplicates("ID_produto").set_index("ID_produto")["TaxaCrescimento"].to_dict()
    participacao, pa_sku = reposicao.participacao_por_loja(dados, config)

    # ---------------- Estoque e giro (informativo) ----------------
    est = dados["estoque"].groupby(["ID_produto", "ID_deposito"])["saldoFisico"].sum().to_dict()
    mapa_pedido_loja = dados["pedidos"].set_index("ID")["Loja ID"].to_dict()
    recentes = itens[itens["Data"] >= data_hoje - timedelta(days=dias_giro)].copy()
    recentes["loja"] = recentes["ID_pedido"].map(mapa_pedido_loja)
    giro_global = recentes.groupby("ID_produto")["Quantidade"].sum().to_dict()
    giro_loja = recentes.groupby(["ID_produto", "loja"])["Quantidade"].sum().to_dict()

    # ---------------- Cadastro do produto ----------------
    det = detalhes.drop_duplicates("ID_produto", keep="last").set_index("ID_produto")[
        ["categoria", "Super_categoria", "Tamanho", "Marca_sku"]].to_dict("index")
    ids = [str(i).strip() for i in produtos["ID"]]
    skus = [str(s).strip() for s in produtos["codigo"]]
    nomes = list(produtos["Descricao"])
    onde = grade.identificar(skus, nomes, [det.get(i, {}).get("Tamanho", "") for i in ids])
    cadastro = {}
    for i, idp in enumerate(ids):
        d = det.get(idp, {})
        cadastro[idp] = {
            "sku": skus[i], "produto": nomes[i],
            "modelo": onde["linha"].iloc[i], "tamanho": onde["tamanho"].iloc[i],
            "colegio": str(d.get("Marca_sku", "") or "").strip(),
            "categoria": d.get("categoria", ""),
            "super_categoria": str(d.get("Super_categoria", "") or "").strip(),
        }

    sortimento, origem = reposicao.sortimento_efetivo(
        params, nomes_lojas, reposicao.colegios_vendidos_por_loja(dados, config, data_hoje))
    fase = reposicao.fase_atual(config, data_hoje)
    transito_qtd, transito_chegada = _resumo_em_transito(em_transito)
    fracoes_horizonte = demanda.fracionar_janela_por_mes(data_hoje, data_hoje + timedelta(days=horizonte))

    # ---------------- 1ª passada: alvo por loja (com teto de espaço) ----------------
    linhas = {}                          # (loja, id_produto) → dict da linha
    for loja_cfg in lojas_cfg:
        nome = loja_cfg["nome"]
        id_loja = str(loja_cfg["loja_id"]).strip()
        id_dep = str(loja_cfg["deposito_id"]).strip()
        janela = reposicao.janela_protecao_dias(params, fase, nome)
        fracoes = demanda.fracionar_janela_por_mes(data_hoje, data_hoje + timedelta(days=janela))

        por_modelo = {}                  # modelo → {id_produto: alvo ideal}
        for idp in ids:
            c = cadastro[idp]
            est_loja = est.get((idp, id_dep), 0)
            meses_rede = demanda_mes.get(idp, {})
            vivo = sum(meses_rede.values()) > 0          # produto com demanda na rede
            fatia = participacao.get((idp, id_loja), 0.0)
            # Produto sem colégio (revenda, acessório) não cabe no cadastro por
            # colégio: fica onde a própria loja o vende.
            em_sortimento = (c["colegio"] in sortimento[nome]) if c["colegio"] else fatia > 0
            if not (em_sortimento and vivo) and est_loja == 0:
                continue                                  # nada a decidir nesta loja

            meses_loja = {m: q * fatia for m, q in meses_rede.items()}
            dem_janela = reposicao.demanda_da_janela(meses_loja, fracoes)
            pa = pa_sku.get(idp, 1.0)
            nivel = (map_colegios.get(c["colegio"]) or {}).get(
                "nivel_servico", params["nivel_servico_default"])
            seguranca = reposicao.seguranca_loja(dem_janela, pa, nivel)
            ideal = (reposicao.alvo_ideal(dem_janela, seguranca, exposicao)
                     if (em_sortimento and vivo) else 0)
            if ideal > 0:
                por_modelo.setdefault(c["modelo"], {})[idp] = ideal

            linhas[(nome, idp)] = {
                "Loja": nome, "SKU": c["sku"], "Produto": c["produto"], "Modelo": c["modelo"],
                "Tamanho": c["tamanho"], "Colegio": c["colegio"], "Categoria": c["categoria"],
                "SuperCategoria": c["super_categoria"],
                "EstoqueLoja": est_loja, "EstoqueCentral": est.get((idp, id_central), 0),
                "AlvoIdeal": ideal, "Alvo": ideal,
                "EmTransito": transito_qtd.get(idp, float("nan")),
                "ChegadaPrevista": transito_chegada.get(idp, pd.NaT),
                "Fase": fase, "JanelaDias": janela, "DemandaJanela": round(dem_janela, 2),
                "PA": round(pa, 2), "NivelServico": nivel, "Seguranca": round(seguranca, 2),
                "Participacao": round(fatia, 4), "TaxaCresc": taxa_cresc.get(idp, 1.0),
                "Gavetas": 0, "LimitadoPorEspaco": False,
                "EmSortimento": em_sortimento, "SortimentoOrigem": origem[nome],
                "GiroGlobal": round(giro_global.get(idp, 0) / dias_giro, 2) if dias_giro > 0 else 0,
                "GiroLoja": round(giro_loja.get((idp, id_loja), 0) / dias_giro, 2) if dias_giro > 0 else 0,
                "_venda_diaria": dem_janela / janela,
                "_dem_horizonte": reposicao.demanda_da_janela(meses_loja, fracoes_horizonte),
            }

        # Espaço: as gavetas da loja vão para os modelos com mais fundo
        fundo = {m: sum(max(i - exposicao, 0) for i in alvos.values()) for m, alvos in por_modelo.items()}
        capacidade = {
            m: reposicao.capacidade_da_gaveta(params, cadastro[next(iter(alvos))]["super_categoria"])
            for m, alvos in por_modelo.items()
        }
        gavetas = reposicao.distribuir_gavetas(fundo, capacidade, reposicao.gavetas_da_loja(params, nome))
        for modelo, alvos in por_modelo.items():
            n_gavetas, coberto = gavetas.get(modelo, (0, 0))
            finais = reposicao.aplicar_teto_espaco(alvos, exposicao, coberto)
            for idp, alvo in finais.items():
                linha = linhas[(nome, idp)]
                linha["Alvo"] = alvo
                linha["Gavetas"] = n_gavetas
                linha["LimitadoPorEspaco"] = alvo < alvos[idp]

    # ---------------- 2ª passada: rateio do CD entre as lojas ----------------
    por_produto = {}
    for (nome, idp), linha in linhas.items():
        por_produto.setdefault(idp, []).append(nome)

    for idp, lojas_do_sku in por_produto.items():
        necessidades, estoques, vendas = {}, {}, {}
        for nome in lojas_do_sku:
            linha = linhas[(nome, idp)]
            negativo = linha["EstoqueLoja"] < 0 or linha["EstoqueCentral"] < 0
            linha["Necessidade"] = 0 if negativo else max(
                linha["Alvo"] - int(math.floor(linha["EstoqueLoja"])), 0)
            necessidades[nome] = linha["Necessidade"]
            estoques[nome] = linha["EstoqueLoja"]
            vendas[nome] = linha["_venda_diaria"]
        saldo_cd = linhas[(lojas_do_sku[0], idp)]["EstoqueCentral"]
        separar = reposicao.ratear_cd(necessidades, estoques, vendas, saldo_cd)

        for nome in lojas_do_sku:
            linha = linhas[(nome, idp)]
            linha["Separar"] = separar[nome]
            linha["Falta"] = linha["Necessidade"] - separar[nome]
            linha["Excesso"] = reposicao.excesso_loja(
                linha["EstoqueLoja"], linha["Alvo"], linha["_dem_horizonte"], linha["EmSortimento"])
            linha["MotivoExcesso"] = _motivo_excesso(
                linha["Excesso"], linha["EmSortimento"], linha["AlvoIdeal"], horizonte,
                linha["Colegio"])
            linha["Acao"] = _classificar(linha["EstoqueLoja"], linha["EstoqueCentral"],
                                         linha["Separar"], linha["Falta"], linha["Excesso"])

    df = pd.DataFrame(list(linhas.values()), columns=COLUNAS)
    if len(df) == 0:
        return df

    # Fila: ação mais urgente primeiro; dentro dela, o maior volume
    df["_ordem"] = df["Acao"].map({a: i for i, a in enumerate(ORDEM_ACOES)})
    df["_volume"] = df[["Separar", "Falta", "Excesso"]].max(axis=1)
    df = df.sort_values(["_ordem", "_volume", "Loja", "SKU"],
                        ascending=[True, False, True, True]).reset_index(drop=True)
    return df.drop(columns=["_ordem", "_volume"])
