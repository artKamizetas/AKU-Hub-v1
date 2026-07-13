"""
demanda.py — Motor único de demanda por SKU × Colégio × Mês.

Usado tanto por etl/fabrica.py (sugestão tática por SKU) quanto por
etl/planejamento.py (simulação de rodadas anuais), pra que as duas visões
derivem sempre dos mesmos números — antes eram dois cálculos desconectados.

Reaproveita o padrão vetorizado (pré-cálculo por groupby + loop só de
lookup em dict) já usado em etl/vm_dinamico.py.
"""

import math
import pandas as pd


NOMES_MES = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun",
             "Jul", "Ago", "Set", "Out", "Nov", "Dez"]

NIVEL_SERVICO_Z = {90: 1.28, 92: 1.41, 95: 1.65, 97: 1.88, 98: 2.05, 99: 2.33}


def _nivel_para_z(nivel: float) -> float:
    """Fator de Serviço (multiplicador) do nível de serviço (%). Aceita fração (0-1) ou percentual."""
    if nivel <= 1:
        nivel = nivel * 100
    return NIVEL_SERVICO_Z.get(round(nivel), 1.65)


def estoque_seguranca(demanda_intervalo: float, contem_mes_alta: bool, config: dict,
                      meses_intervalo: float = 1.0) -> float:
    """
    Estoque de Segurança do intervalo de proteção (política order-up-to, R,S):
        Estoque de Segurança = Fator de Serviço × σ_intervalo
        σ_intervalo = Variação da Demanda × demanda_mensal × √(meses)  (Silver-Pyke-Peterson)
                    = Variação da Demanda × (demanda_intervalo / meses) × √(meses)
                    = Variação da Demanda × demanda_intervalo / √(meses)

    A segurança cresce com a RAIZ do tempo, não linearmente — demanda de N
    meses é proporcionalmente mais previsível (pooling). Sem o √, um intervalo
    longo (ex: rodada que cobre 9 meses) infla a segurança em ~√9 = 3×.

    Fator de Serviço usa nivel_servico_alta se o intervalo cobre algum mês
    de alta (não pode faltar no pico), senão nivel_servico_baixa. Variação da
    Demanda = coeficiente de variação da demanda mensal, calibrável em
    config["demanda"]["variacao_demanda"].
    """
    cfg = config.get("demanda", {})
    ns = cfg.get("nivel_servico_alta", 99) if contem_mes_alta else cfg.get("nivel_servico_baixa", 92)
    variacao_demanda = cfg.get("variacao_demanda", 0.25)   # coeficiente de variação da demanda mensal
    n = max(float(meses_intervalo), 1.0)
    fator_servico = _nivel_para_z(ns)                # Fator de Serviço do nível de serviço
    return fator_servico * variacao_demanda * max(demanda_intervalo, 0) / math.sqrt(n)


def calcular_sazonalidade_empresa(dados: dict, config: dict) -> pd.DataFrame:
    """
    Peso relativo de cada mês-calendário, agregando as vendas da empresa
    inteira no período config["planejamento"]["periodo_historico_inicio"/"fim"].

    PesoNormalizado: soma = 12.0 (mês médio = 1.0).
    Usado como fallback de última instância pela sazonalidade por colégio.
    """
    cfg = config.get("planejamento", {})
    dt_ini = pd.Timestamp(str(cfg.get("periodo_historico_inicio", "2025-01-01")))  # str(): ruamel
    dt_fim = pd.Timestamp(str(cfg.get("periodo_historico_fim", "2026-02-28")))

    itens = dados["itens"]
    vendas = itens[(itens["Data"] >= dt_ini) & (itens["Data"] <= dt_fim)].copy()

    if len(vendas) == 0:
        return pd.DataFrame({
            "Mes": range(1, 13),
            "NomeMes": NOMES_MES,
            "Vendas": 0,
            "PesoRelativo": [1 / 12] * 12,
            "PesoNormalizado": [1.0] * 12,
        })

    vendas["Mes"] = vendas["Data"].dt.month
    vendas["AnoMes"] = vendas["Data"].dt.to_period("M")

    por_anomes = vendas.groupby("AnoMes")["Quantidade"].sum().reset_index()
    por_anomes["Mes"] = por_anomes["AnoMes"].dt.month
    media_por_mes = por_anomes.groupby("Mes")["Quantidade"].mean()

    rows = []
    for m in range(1, 13):
        rows.append({
            "Mes": m,
            "NomeMes": NOMES_MES[m - 1],
            "Vendas": round(media_por_mes.get(m, 0), 1),
        })

    df = pd.DataFrame(rows)
    total = df["Vendas"].sum()

    if total > 0:
        df["PesoRelativo"] = df["Vendas"] / total
        df["PesoNormalizado"] = round(df["PesoRelativo"] * 12, 3)
    else:
        df["PesoRelativo"] = 1 / 12
        df["PesoNormalizado"] = 1.0

    return df


def calcular_sazonalidade_por_colegio(dados: dict, config: dict) -> pd.DataFrame:
    """
    Peso sazonal mensal por colégio (Marca_sku), com fallback em 3 níveis,
    CÉLULA A CÉLULA (colégio, mês) — não um chaveamento binário por colégio:

    1. Colégio tem histórico suficiente (config["demanda"]) E tem venda
       nesse mês específico -> usa o peso próprio do colégio nesse mês.
    2. Colégio sem histórico suficiente, OU essa célula (colégio, mês)
       específica está zerada mesmo com o colégio tendo bom histórico
       -> usa o peso da empresa inteira nesse mês.
    3. Empresa inteira sem nenhuma venda no período -> peso uniforme 1.0
       (já é o que calcular_sazonalidade_empresa retorna nesse caso).

    Retorna DataFrame [Colegio, Mes, NomeMes, Vendas, PesoNormalizado,
    FonteSazonalidade], FonteSazonalidade em {"colegio", "empresa", "uniforme"}.
    Colégio "" (produto sem Marca_sku) sempre cai no fallback da empresa.
    """
    cfg_demanda = config.get("demanda") or {}
    min_vendas = cfg_demanda.get("min_vendas_colegio", 30)
    min_meses = cfg_demanda.get("min_meses_colegio", 6)

    saz_empresa = calcular_sazonalidade_empresa(dados, config)
    peso_empresa = saz_empresa.set_index("Mes")["PesoNormalizado"].to_dict()
    empresa_tem_dado = saz_empresa["Vendas"].sum() > 0
    fonte_fallback = "empresa" if empresa_tem_dado else "uniforme"

    cfg = config.get("planejamento", {})
    dt_ini = pd.Timestamp(str(cfg.get("periodo_historico_inicio", "2025-01-01")))  # str(): ruamel
    dt_fim = pd.Timestamp(str(cfg.get("periodo_historico_fim", "2026-02-28")))

    itens = dados["itens"]
    detalhes = aplicar_alias_colegio(dados["detalhes"], config)
    det_map = detalhes.set_index("ID_produto")["Marca_sku"].to_dict()

    colegios_cadastrados = sorted(
        c for c in detalhes["Marca_sku"].dropna().astype(str).str.strip().unique() if c
    )

    vendas = itens[(itens["Data"] >= dt_ini) & (itens["Data"] <= dt_fim)].copy()
    vendas["Colegio"] = vendas["ID_produto"].map(det_map).fillna("").astype(str).str.strip()
    vendas["Mes"] = vendas["Data"].dt.month
    vendas["AnoMes"] = vendas["Data"].dt.to_period("M")

    por_anomes = (
        vendas.groupby(["Colegio", "AnoMes"])["Quantidade"].sum().reset_index()
    )
    por_anomes["Mes"] = por_anomes["AnoMes"].dt.month
    media_por_colegio_mes = por_anomes.groupby(["Colegio", "Mes"])["Quantidade"].mean()

    n_vendas_por_colegio = vendas.groupby("Colegio")["Quantidade"].sum()
    n_meses_por_colegio = (
        vendas[vendas["Quantidade"] > 0].groupby("Colegio")["Mes"].nunique()
    )
    stats = pd.DataFrame({"n_vendas": n_vendas_por_colegio, "n_meses": n_meses_por_colegio}).fillna(0)

    rows = []
    for colegio in colegios_cadastrados:
        n_vendas_c = float(stats["n_vendas"].get(colegio, 0))
        n_meses_c = int(stats["n_meses"].get(colegio, 0))
        suficiente = (n_vendas_c >= min_vendas) and (n_meses_c >= min_meses)

        vendas_por_mes = {m: media_por_colegio_mes.get((colegio, m), 0.0) for m in range(1, 13)}

        if suficiente:
            total_c = sum(vendas_por_mes.values())
            peso_bruto = (
                {m: (vendas_por_mes[m] / total_c * 12) for m in range(1, 13)}
                if total_c > 0 else {m: 1.0 for m in range(1, 13)}
            )
            for m in range(1, 13):
                if vendas_por_mes[m] > 0:
                    rows.append({
                        "Colegio": colegio, "Mes": m, "NomeMes": NOMES_MES[m - 1],
                        "Vendas": round(vendas_por_mes[m], 1),
                        "PesoNormalizado": round(peso_bruto[m], 3),
                        "FonteSazonalidade": "colegio",
                    })
                else:
                    rows.append({
                        "Colegio": colegio, "Mes": m, "NomeMes": NOMES_MES[m - 1],
                        "Vendas": 0, "PesoNormalizado": peso_empresa.get(m, 1.0),
                        "FonteSazonalidade": fonte_fallback,
                    })
        else:
            for m in range(1, 13):
                rows.append({
                    "Colegio": colegio, "Mes": m, "NomeMes": NOMES_MES[m - 1],
                    "Vendas": round(vendas_por_mes[m], 1),
                    "PesoNormalizado": peso_empresa.get(m, 1.0),
                    "FonteSazonalidade": fonte_fallback,
                })

    # Colégio "" (produto sem Marca_sku) — sempre fallback da empresa
    for m in range(1, 13):
        rows.append({
            "Colegio": "", "Mes": m, "NomeMes": NOMES_MES[m - 1],
            "Vendas": 0, "PesoNormalizado": peso_empresa.get(m, 1.0),
            "FonteSazonalidade": fonte_fallback,
        })

    return pd.DataFrame(rows)


# Nível intermediário: agrupa as siglas de Grupo em SEGMENTO, pra medir o
# crescimento em células mais gordas/estáveis (EF1·EF2·EFD → Fundamental, etc.).
# Ver docs/memória "grupos-series-map". Pode migrar p/ config no futuro.
SEGMENTO_POR_GRUPO = {
    "BER": "Infantil", "EIN": "Infantil", "IDF": "Infantil",   # IDF: Infantil+EdFís → principal Infantil
    "EIF": "Inf+Fund",                                          # combo Infantil+Fundamental (balde próprio)
    "EF1": "Fundamental", "EF2": "Fundamental", "EFD": "Fundamental",
    "FDF": "Fundamental",                                       # FDF: Fundamental+EdFís → principal Fundamental
    "EME": "Médio", "PRE": "Médio", "CUR": "Médio",
    "EFM": "Médio",                                             # combo Fund+Médio → Médio (onde a expansão importa)
    "TEI": "Tempo Integral",                                    # modalidade (balde próprio)
    "DIA": "Diário",                                            # uniforme diário (legado SES; isolado)
    "EDF": "Ed. Física",                                        # uniforme de ed. física (todo aluno)
    "ESP": "Esporte", "EQP": "Esporte",                         # times/equipe (subconjunto opcional)
    "OPC": "Outros",
}


def mapa_grupo_segmento(config: dict = None) -> dict:
    """
    Mapa grupo→segmento EFETIVO: o default `SEGMENTO_POR_GRUPO` do código,
    sobrescrito pelo que o usuário definiu em config["grupo_segmento"]
    (editável na página de Configurações). Permite testar outros agrupamentos
    sem mexer no código.
    """
    m = dict(SEGMENTO_POR_GRUPO)
    if config and config.get("grupo_segmento"):
        m.update({str(k).strip(): str(v).strip() for k, v in config["grupo_segmento"].items()})
    return m


def segmento_do_grupo(grupo, config: dict = None) -> str:
    return mapa_grupo_segmento(config).get(str(grupo).strip(), "Outros")


# --- Normalização de colégio (Marca_sku cru → nome canônico) ---------------
# O colégio chega cru do Supabase (extração por string da SKU na pipeline
# externa) e produz ruído em SKUs fora de padrão (ex: "27"). Este de-para,
# configurável em config["colegios_alias"], normaliza o nome sem tocar na
# origem. Espelha mapa_grupo_segmento. Ver docs/requisitos/normalizacao-colegios.md.
def mapa_colegio(config: dict = None) -> dict:
    """
    De-para EFETIVO Marca_sku(cru) → colégio canônico (config["colegios_alias"]).
    Contém só o que MUDA — valor ausente mantém o cru (identidade, decisão D1=A).
    """
    m = {}
    if config and config.get("colegios_alias"):
        m.update({str(k).strip(): str(v).strip() for k, v in config["colegios_alias"].items()})
    return m


def colegio_efetivo(marca_sku, config: dict = None) -> str:
    """Colégio canônico (aplica o de-para). Default = identidade (mantém o cru)."""
    raw = str(marca_sku or "").strip()
    return mapa_colegio(config).get(raw, raw)


def aplicar_alias_colegio(detalhes: pd.DataFrame, config: dict = None) -> pd.DataFrame:
    """
    Cópia de `detalhes` com Marca_sku normalizada para o colégio canônico,
    preservando o valor cru em Marca_sku_raw. No-op se não houver Marca_sku ou
    de-para. Normalização única chamada no topo de cada entrypoint ETL — assim
    todo consumidor (cálculo, coluna de saída, filtro de tela) vê o mesmo nome.
    """
    if "Marca_sku" not in detalhes.columns:
        return detalhes
    alias = mapa_colegio(config)
    if not alias:
        return detalhes
    out = detalhes.copy()
    if "Marca_sku_raw" not in out.columns:
        out["Marca_sku_raw"] = out["Marca_sku"]
    out["Marca_sku"] = out["Marca_sku"].map(
        lambda v: alias.get(str(v or "").strip(), str(v or "").strip()))
    return out


def parece_ruido(marca_sku) -> bool:
    """
    Heurística p/ SUGERIR (nunca aplicar sozinho) que um colégio cru é ruído:
    nenhum caractere alfabético (ex: "27", "31"). Só orienta o editor da UI —
    a decisão continua manual (D1=A).
    """
    s = str(marca_sku or "").strip()
    return bool(s) and not any(c.isalpha() for c in s)


def calcular_crescimento_observado(dados: dict, config: dict, data_hoje=None) -> dict:
    """
    Camada OBSERVADA do crescimento (modelo híbrido): mede o crescimento realizado
    por colégio e por colégio×segmento a partir das ALTAS (Dez-Fev), que é sinal
    limpo — a baixa sofre ruptura de estoque. Usa a última transição de alta
    (ciclo N / ciclo N-1), limita a [0.5, 2.0] e só aceita a célula se o volume
    mínimo do par ≥ 30 pçs (senão vira ruído; ex: DRM 22×). Serve de baseline
    quando não há override manual — ver taxa_crescimento_efetiva.

    Retorna {colegio: {"__geral__": g|None, "segmentos": {segmento: g}}}.
    """
    CLAMP = (0.5, 2.0)
    VOL_MIN = 30
    data_hoje = pd.Timestamp.now().normalize() if data_hoje is None else pd.Timestamp(data_hoje)

    janela = _ordenar_janela_cronologica((config.get("demanda", {}) or {}).get("janela_alta", [12, 1, 2]))
    if not janela:
        return {}
    ult = janela[-1]
    janela_set = set(janela)

    det = aplicar_alias_colegio(dados["detalhes"], config)
    colg = det.set_index("ID_produto")["Marca_sku"].to_dict()
    grp = det.set_index("ID_produto")["Grupo"].to_dict()

    df = dados["itens"][["ID_produto", "Data", "Quantidade"]].copy()
    df["ID_produto"] = df["ID_produto"].astype(str).str.strip()
    df["mes"] = df["Data"].dt.month
    df = df[df["mes"].isin(janela_set)]
    df["colegio"] = df["ID_produto"].map(lambda i: str(colg.get(i, "")).strip())
    df = df[(df["colegio"] != "") & (df["colegio"].str.lower() != "nan")]
    mapa_seg = mapa_grupo_segmento(config)
    df["segmento"] = df["ID_produto"].map(lambda i: mapa_seg.get(str(grp.get(i, "")).strip(), "Outros"))
    # ciclo da alta: meses "antes da virada" (ex: Dez) pertencem ao ano seguinte
    df["ciclo"] = df["Data"].dt.year + (df["mes"] > ult).astype(int)

    def fim_ciclo(y):
        return pd.Timestamp(year=int(y), month=ult, day=1) + pd.offsets.MonthEnd(0)

    ciclos = sorted(c for c in df["ciclo"].unique() if fim_ciclo(c) < data_hoje)
    if len(ciclos) < 2:
        return {}
    c_prev, c_cur = ciclos[-2], ciclos[-1]
    df = df[df["ciclo"].isin([c_prev, c_cur])]

    def growth(prev, cur):
        if prev < VOL_MIN or cur < 1:
            return None
        return round(min(max(cur / prev, CLAMP[0]), CLAMP[1]), 3)

    out = {}
    tot = df.groupby(["colegio", "ciclo"])["Quantidade"].sum().unstack("ciclo").fillna(0.0)
    for col, r in tot.iterrows():
        out[col] = {"__geral__": growth(r.get(c_prev, 0), r.get(c_cur, 0)), "segmentos": {}}
    seg = df.groupby(["colegio", "segmento", "ciclo"])["Quantidade"].sum().unstack("ciclo").fillna(0.0)
    for (col, s), r in seg.iterrows():
        g = growth(r.get(c_prev, 0), r.get(c_cur, 0))
        if g is not None:
            out.setdefault(col, {"__geral__": None, "segmentos": {}})["segmentos"][s] = g
    return out


def taxa_crescimento_efetiva(colegio: str, config: dict, grupo: str = None,
                             ativo: bool = True, observado: dict = None) -> float:
    """
    Fator de crescimento (multiplicador) por SKU, com resolução em cascata
    (manual do planejador SEMPRE vence a camada observada dos dados):
        1. config["colegios"][colégio]["crescimento_grupos"][grupo]   (manual, grupo)
        2. config["colegios"][colégio]["taxa_crescimento"]            (manual, colégio)
        3. observado[colégio]["segmentos"][segmento(grupo)]           (dados, colégio×segmento)
        4. observado[colégio]["__geral__"]                           (dados, colégio)
        5. 1 + fabrica.crescimento_pct/100                           (fallback global)

    `observado` = saída de calcular_crescimento_observado (None = pula 3-4, volta
    ao comportamento antigo). ativo=False desliga tudo (retorna 1.0) — toggle
    "com/sem crescimento" da fábrica e da logística, pra comparar.
    """
    if not ativo:
        return 1.0
    col_cfg = (config.get("colegios") or {}).get(colegio) or {}
    grupos = col_cfg.get("crescimento_grupos") or {}
    if grupo is not None and grupo in grupos:
        return float(grupos[grupo])
    if "taxa_crescimento" in col_cfg:
        return float(col_cfg["taxa_crescimento"])
    if observado:
        obs = observado.get(colegio) or {}
        if grupo is not None:
            seg_g = (obs.get("segmentos") or {}).get(segmento_do_grupo(grupo, config))
            if seg_g is not None:
                return float(seg_g)
        if obs.get("__geral__") is not None:
            return float(obs["__geral__"])
    crescimento_pct = config.get("fabrica", {}).get("crescimento_pct", 0)
    return 1 + crescimento_pct / 100


def _ordenar_janela_cronologica(meses: list) -> list:
    """
    Ordena os meses de uma temporada em ordem cronológica, detectando a virada
    de ano pela MAIOR lacuna cíclica (a temporada começa no mês após ela).
    Ex: {12,1,2} (em qualquer ordem de entrada) -> [12, 1, 2].
    Robusto a `janela_alta` fora de ordem vinda da UI.
    """
    ms = sorted(set(int(m) for m in meses))
    if len(ms) <= 1:
        return ms
    gaps = [((ms[(i + 1) % len(ms)] - ms[i]) % 12) or 12 for i in range(len(ms))]
    idx_max = gaps.index(max(gaps))
    start = (idx_max + 1) % len(ms)
    return ms[start:] + ms[:start]


def _ultima_temporada_alta(config: dict, data_hoje=None) -> dict:
    """
    Mapeia cada mês da janela de alta (config["demanda"]["janela_alta"]) para o
    (ano, mês) concreto da última temporada de alta COMPLETA (toda no passado).
    Resolve virada de ano e é robusto à ordem de entrada da janela.

    Ex: hoje jul/2026, janela [12,1,2] -> {12: 2025-12, 1: 2026-01, 2: 2026-02}.
    """
    data_hoje = pd.Timestamp.now().normalize() if data_hoje is None else pd.Timestamp(data_hoje)
    janela = _ordenar_janela_cronologica((config.get("demanda", {}) or {}).get("janela_alta", [12, 1, 2]))
    if not janela:
        return {}

    ultimo_mes = janela[-1]
    ano_fim = data_hoje.year
    fim_temporada = pd.Timestamp(year=ano_fim, month=ultimo_mes, day=1) + pd.offsets.MonthEnd(0)
    if fim_temporada > data_hoje:
        ano_fim -= 1

    temporada = {}
    ano = ano_fim
    anterior = None
    for m in reversed(janela):
        if anterior is not None and m > anterior:
            ano -= 1
        temporada[m] = pd.Timestamp(year=ano, month=m, day=1)
        anterior = m
    return temporada


def calcular_proporcao_baixa(dados: dict, config: dict, data_hoje=None) -> float:
    """
    Proporção GLOBAL que a baixa representa da alta (Σbaixa ÷ Σalta da empresa
    inteira), medida nos últimos 2 ciclos completos (baixa recente). É a BASE
    ÚNICA — o backtest (2023-25) mostrou que fatiar por categoria/SKU não melhora
    na média (teto de erro ~48% em qualquer eixo); os poucos gigantes de cauda
    curta (tamanho central) são pegos por override manual (proporcao_baixa_efetiva).
    Ex: 0.44 = "a baixa vende 44% do que a alta vende".
    """
    data_hoje = pd.Timestamp.now().normalize() if data_hoje is None else pd.Timestamp(data_hoje)
    janela = _ordenar_janela_cronologica((config.get("demanda", {}) or {}).get("janela_alta", [12, 1, 2]))
    if not janela:
        return 0.0
    ult = janela[-1]
    janela_set = set(janela)
    meses_baixa = [m for m in range(1, 13) if m not in janela_set]
    ult_baixa = max(meses_baixa) if meses_baixa else 11

    it = dados["itens"][["Data", "Quantidade"]].copy()
    it["mes"] = it["Data"].dt.month
    it["ano"] = it["Data"].dt.year
    it["fase"] = it["mes"].apply(lambda m: "A" if m in janela_set else "B")
    # ciclo: alta de meses "antes da virada" (ex: Dez) conta pro ano seguinte
    it["ciclo"] = it["ano"] + ((it["fase"] == "A") & (it["mes"] > ult)).astype(int)

    def ciclo_completo(y):
        return pd.Timestamp(year=int(y), month=ult_baixa, day=28) < data_hoje

    ciclos = sorted(c for c in it["ciclo"].unique() if ciclo_completo(c))
    if not ciclos:
        return 0.0
    sub = it[it["ciclo"].isin(ciclos[-2:])]              # últimos 2 ciclos completos
    tot = sub.groupby("fase")["Quantidade"].sum()
    alta, baixa = float(tot.get("A", 0.0)), float(tot.get("B", 0.0))
    return round(baixa / alta, 4) if alta > 0 else 0.0


def proporcao_baixa_efetiva(sku: str, colegio: str, config: dict, base_global: float) -> float:
    """
    Proporção da baixa aplicada a um SKU, em cascata (manual do gestor vence):
        1. config.excecoes_sku[sku].proporcao_baixa    (manual, SKU — cauda idiossincrática)
        2. config.colegios[colégio].proporcao_baixa    (manual, colégio — ex: vende o ano todo)
        3. base_global                                 (medido, empresa)
    """
    exc = (config.get("excecoes_sku") or {}).get(sku)
    if isinstance(exc, dict) and exc.get("proporcao_baixa") is not None:
        return float(exc["proporcao_baixa"])
    col = (config.get("colegios") or {}).get(colegio) or {}
    if col.get("proporcao_baixa") is not None:
        return float(col["proporcao_baixa"])
    return float(base_global)


def distribuicao_mensal_baixa(dados: dict, config: dict) -> dict:
    """
    Distribuição mensal AGREGADA dos meses de baixa (normalizada, soma = 1):
    que fração da demanda de baixa cai em cada mês. Usada pra espalhar a
    demanda de baixa de cada SKU sem depender do dado esparso do SKU.
    Meses de alta recebem 0.
    """
    cfg = config.get("demanda", {})
    janela_alta = set(cfg.get("janela_alta", [12, 1, 2]))
    meses_baixa = [m for m in range(1, 13) if m not in janela_alta]

    saz = calcular_sazonalidade_empresa(dados, config)
    peso = saz.set_index("Mes")["PesoNormalizado"].to_dict()

    pesos_baixa = {m: max(peso.get(m, 0.0), 0.0) for m in meses_baixa}
    total = sum(pesos_baixa.values())
    if total <= 0:
        return {m: (1.0 / len(meses_baixa) if m in meses_baixa else 0.0) for m in range(1, 13)}
    return {m: (pesos_baixa.get(m, 0.0) / total) for m in range(1, 13)}


def calcular_demanda_mensal_por_sku(dados: dict, config: dict, ativo_crescimento: bool = None) -> pd.DataFrame:
    """
    Demanda mensal projetada por SKU (12 meses), modelo ANCORADO NA ALTA:

      - Meses de alta (config["demanda"]["janela_alta"]): vendas reais do SKU
        na última temporada de alta completa × crescimento (colégio×grupo).
      - Meses de baixa: demanda de baixa do SKU (= demanda_alta × proporção
        da baixa da categoria) espalhada pela distribuição mensal da baixa.

    A alta define forma e magnitude; a baixa só adiciona volume — o dado
    esparso da baixa nunca entra no nível do SKU.

    ativo_crescimento: liga/desliga o fator de crescimento (toggle). None =
    lê config["demanda"]["aplicar_crescimento_fabrica"].

    Retorna [SKU, ID_produto, Colegio, Grupo, Categoria, Mes, NomeMes,
    TaxaCrescimento, DemandaAlta, DemandaMensalProjetada, Fase].
    """
    if ativo_crescimento is None:
        ativo_crescimento = config.get("demanda", {}).get("aplicar_crescimento_fabrica", True)

    cfg_fab = config.get("fabrica", {})
    excecoes = config.get("excecoes_sku") or {}
    correcao_global = cfg_fab.get("correcao_manual", 0)

    produtos = dados["produtos"]
    itens = dados["itens"]
    detalhes = aplicar_alias_colegio(dados["detalhes"], config)

    col_map = detalhes.set_index("ID_produto")["Marca_sku"].to_dict()
    grp_map = detalhes.set_index("ID_produto")["Grupo"].to_dict()
    cat_map = detalhes.set_index("ID_produto")["categoria"].to_dict()          # display (granular)
    supercat_map = detalhes.set_index("ID_produto")["Super_categoria"].to_dict()  # fator manutenção

    temporada = _ultima_temporada_alta(config)          # {mes: Timestamp(ano,mes)}
    meses_alta = sorted(temporada.keys())
    distribuicao_baixa = distribuicao_mensal_baixa(dados, config)      # {mes: fração}, soma=1 na baixa
    proporcao_baixa_base = calcular_proporcao_baixa(dados, config)     # global (últimos 2 ciclos)

    # Camada observada do crescimento (colégio×segmento), baseline sob os overrides
    # manuais. Desligável em demanda.crescimento_observado_ativo (=> volta ao +10% cego).
    obs_cresc = (calcular_crescimento_observado(dados, config)
                 if (config.get("demanda", {}) or {}).get("crescimento_observado_ativo", True)
                 else None)

    # Vendas por produto em cada mês concreto da última temporada de alta
    vendas_alta_por_mes = {}
    for m, ts in temporada.items():
        mask = (itens["Data"].dt.year == ts.year) & (itens["Data"].dt.month == ts.month)
        vendas_alta_por_mes[m] = itens[mask].groupby("ID_produto")["Quantidade"].sum().to_dict()

    # Vendas de BAIXA recentes por produto (período histórico, só meses de
    # baixa) — fallback p/ SKUs sem nenhuma venda no pico (produtos que só saem
    # fora da alta). Exclui meses de alta pra não capturar o pico de anos anteriores.
    cfg_plan = config.get("planejamento", {})
    dt_ini = pd.Timestamp(str(cfg_plan.get("periodo_historico_inicio", "2025-01-01")))  # str(): ruamel
    dt_fim = pd.Timestamp(str(cfg_plan.get("periodo_historico_fim", "2026-02-28")))
    janela_alta_set = set(meses_alta)
    _it_baixa = itens[(itens["Data"] >= dt_ini) & (itens["Data"] <= dt_fim)]
    _it_baixa = _it_baixa[~_it_baixa["Data"].dt.month.isin(janela_alta_set)]
    vendas_baixa_recentes = _it_baixa.groupby("ID_produto")["Quantidade"].sum().to_dict()

    resultados = []
    for _, prod in produtos.iterrows():
        id_prod = str(prod["ID"]).strip()
        sku = prod["codigo"]
        colegio = str(col_map.get(id_prod, "")).strip()
        grupo = str(grp_map.get(id_prod, "")).strip()
        categoria = str(cat_map.get(id_prod, "")).strip()

        taxa_cresc = taxa_crescimento_efetiva(colegio, config, grupo, ativo_crescimento, obs_cresc)
        exc = excecoes.get(sku, {}) if isinstance(excecoes.get(sku), dict) else {}
        correcao_sku = exc.get("correcao", 0)

        supercat = str(supercat_map.get(id_prod, "")).strip()
        vendas_alta_sku = {m: float(vendas_alta_por_mes[m].get(id_prod, 0)) for m in meses_alta}
        vendas_ultima_alta = sum(vendas_alta_sku.values())
        proporcao_baixa = proporcao_baixa_efetiva(sku, colegio, config, proporcao_baixa_base)

        if vendas_ultima_alta > 0:
            # Fluxo normal: ancora na alta, a baixa entra como acréscimo
            demanda_alta = vendas_ultima_alta * taxa_cresc
            demanda_baixa = demanda_alta * proporcao_baixa
            vende_na_alta = True
        else:
            # SKU sem venda na alta (produto só de baixa): usa as vendas de
            # baixa recentes como base, distribuídas na baixa; alta fica 0.
            demanda_alta = 0.0
            demanda_baixa = float(vendas_baixa_recentes.get(id_prod, 0)) * taxa_cresc
            vende_na_alta = False

        for m in range(1, 13):
            if m in temporada:
                demanda = (vendas_alta_sku[m] * taxa_cresc) if vende_na_alta else 0.0
                fase = "alta"
            else:
                demanda = demanda_baixa * distribuicao_baixa.get(m, 0.0)
                fase = "baixa"
            demanda += correcao_global + correcao_sku
            resultados.append({
                "SKU": sku, "ID_produto": id_prod, "Colegio": colegio,
                "Grupo": grupo, "Categoria": categoria,
                "Mes": m, "NomeMes": NOMES_MES[m - 1],
                "TaxaCrescimento": taxa_cresc,
                "DemandaAlta": round(demanda_alta, 2),
                "DemandaMensalProjetada": demanda,
                "Fase": fase,
            })

    return pd.DataFrame(resultados)


def agregar_demanda_mensal_total(demanda_sku: pd.DataFrame, sazonalidade_empresa: pd.DataFrame) -> pd.DataFrame:
    """
    Reduz a demanda por SKU pro formato agregado [Mes, NomeMes, Peso, Demanda]
    usado pelos gráficos da Visão Geral — soma bottom-up, arredonda só no
    final (nunca por SKU antes de agregar).
    """
    df = demanda_sku.groupby(["Mes", "NomeMes"], as_index=False)["DemandaMensalProjetada"].sum()
    df = df.merge(sazonalidade_empresa[["Mes", "PesoNormalizado"]], on="Mes", how="left")
    df = df.rename(columns={"DemandaMensalProjetada": "Demanda", "PesoNormalizado": "Peso"})
    df["Demanda"] = df["Demanda"].round().astype(int)
    return df.sort_values("Mes").reset_index(drop=True)[["Mes", "NomeMes", "Peso", "Demanda"]]


def custo_medio_ponderado(demanda_sku_periodo: pd.DataFrame, produtos: pd.DataFrame) -> float:
    """
    Custo médio ponderado pela demanda projetada de cada SKU no período
    (em vez da média simples ingênua do catálogo inteiro). Fallback pra
    média simples se a demanda do período for zero.
    """
    fallback = float(produtos["preco_custo"].mean()) if len(produtos) > 0 else 0.0

    if len(demanda_sku_periodo) == 0:
        return fallback

    soma_demanda = demanda_sku_periodo["DemandaMensalProjetada"].sum()
    if soma_demanda <= 0:
        return fallback

    custo_map = produtos.set_index("codigo")["preco_custo"].to_dict()
    df = demanda_sku_periodo.copy()
    df["Custo"] = df["SKU"].map(custo_map).fillna(0.0)
    return float((df["DemandaMensalProjetada"] * df["Custo"]).sum() / soma_demanda)


def fracionar_janela_por_mes(janela_inicio: pd.Timestamp, janela_fim: pd.Timestamp) -> list:
    """
    Quebra o intervalo [janela_inicio, janela_fim) em (mês-calendário 1-12,
    fração de dias do mês dentro da janela) — pra ponderar meses parciais
    nas bordas da janela de cobertura proporcionalmente aos dias.
    """
    resultado = []
    cursor = pd.Timestamp(janela_inicio)
    fim = pd.Timestamp(janela_fim)

    while cursor < fim:
        inicio_mes = cursor.replace(day=1)
        proximo_mes = inicio_mes + pd.DateOffset(months=1)
        fim_deste_trecho = min(proximo_mes, fim)

        dias_na_janela = (fim_deste_trecho - cursor).days
        dias_totais_do_mes = (proximo_mes - inicio_mes).days
        fracao = dias_na_janela / dias_totais_do_mes if dias_totais_do_mes > 0 else 0.0

        resultado.append((cursor.month, fracao))
        cursor = fim_deste_trecho

    return resultado


def _candidatas_rodadas(config: dict, data_hoje: pd.Timestamp) -> list:
    """
    Todas as ocorrências de rodada (disparo → chegada), ordenadas por data de
    chegada. Retorna [] se não houver rodadas configuradas.

    Calendário EXPLÍCITO (config["planejamento"]["rodadas_datas"], lista de
    datas ISO de disparo, ex ["2026-07-20", "2026-10-01", "2027-02-01"]): cada
    data é UMA ocorrência real — nada se repete no ano seguinte. Enxerga este
    ano E o próximo como eles realmente são (rodada atrasada este ano,
    antecipada no próximo). A ÚLTIMA data serve de fecho do intervalo da
    penúltima. Chegada = disparo + lead time em DIAS (semanas × 7).
    """
    cfg_plan = config.get("planejamento", {})
    lt_semanas = cfg_plan.get("lead_time_semanas", 4)

    # str(): tolera DoubleQuotedScalarString (ruamel)
    datas = sorted(pd.Timestamp(str(e)).normalize()
                   for e in (cfg_plan.get("rodadas_datas") or []))

    candidatas = []
    for i, d in enumerate(datas):
        candidatas.append({
            "numero": i + 1,
            "mes_disparo": d.month, "ano_disparo": d.year,
            "data_disparo": d,
            "data_chegada": d + pd.Timedelta(days=lt_semanas * 7),
        })
    return candidatas


def _cobre_texto(janela_inicio: pd.Timestamp, janela_fim: pd.Timestamp) -> str:
    meses = fracionar_janela_por_mes(janela_inicio, janela_fim)
    if not meses:
        return "—"
    return f"{NOMES_MES[meses[0][0] - 1]}–{NOMES_MES[meses[-1][0] - 1]}"


def listar_rodadas_selecionaveis(config: dict, data_hoje=None) -> list:
    """
    Uma janela por rodada futura selecionável na aba tática: para cada rodada
    configurada, sua próxima ocorrência a partir de hoje, cobrindo da chegada
    dela até a chegada da rodada seguinte ("pedido daquela rodada").

    Ordenada por data de disparo (a rodada mais iminente primeiro = default).
    Retorna [] se não houver rodadas configuradas.
    """
    data_hoje = pd.Timestamp.now().normalize() if data_hoje is None else pd.Timestamp(data_hoje)
    candidatas = _candidatas_rodadas(config, data_hoje)
    if not candidatas:
        return []

    upcoming = [c for c in candidatas if c["data_chegada"] >= data_hoje]
    vistos = set()
    janelas = []
    for c in upcoming:
        if c["numero"] in vistos:
            continue
        vistos.add(c["numero"])
        seguinte = next((x for x in candidatas if x["data_chegada"] > c["data_chegada"]), None)
        if seguinte is None:
            continue
        janela_inicio = c["data_chegada"]
        janela_fim = seguinte["data_chegada"]
        label = (
            f"Rodada {c['numero']} — disparo {NOMES_MES[c['mes_disparo'] - 1]}/{c['ano_disparo']} "
            f"→ chega {NOMES_MES[janela_inicio.month - 1]}/{janela_inicio.year} "
            f"(cobre {_cobre_texto(janela_inicio, janela_fim)})"
        )
        janelas.append({
            "tem_rodadas": True,
            "modo": "rodada_selecionada",
            "numero": c["numero"],
            "mes_disparo": c["mes_disparo"],
            "ano_disparo": c["ano_disparo"],
            "janela_inicio": janela_inicio,
            "janela_fim": janela_fim,
            "cobertura_meses": max((janela_fim - janela_inicio).days, 1) / 30,
            "label": label,
        })

    janelas.sort(key=lambda j: pd.Timestamp(year=j["ano_disparo"], month=j["mes_disparo"], day=1))
    return janelas


def montar_janela_rodada(config: dict, mes_disparo: int, ano_disparo: int, data_hoje=None) -> dict:
    """
    Recupera a janela de uma rodada específica (identificada por mês+ano de
    disparo) — usado pela aba tática quando o usuário escolhe a rodada no
    seletor. Cai no fallback automático se a rodada não for encontrada.
    """
    for j in listar_rodadas_selecionaveis(config, data_hoje):
        if j["mes_disparo"] == mes_disparo and j["ano_disparo"] == ano_disparo:
            return j
    return proxima_janela_cobertura(config, data_hoje)


def proxima_janela_cobertura(config: dict, data_hoje=None) -> dict:
    """
    Janela de cobertura tática AUTOMÁTICA (default): do dia de hoje até a
    chegada da rodada de produção SEGUINTE à próxima (a próxima já está "a
    caminho" — o pedido de agora precisa cobrir até a rodada depois dela
    chegar). Sem rodadas configuradas, cai no fallback fixo
    (fabrica.cobertura_meses).
    """
    data_hoje = pd.Timestamp.now().normalize() if data_hoje is None else pd.Timestamp(data_hoje)

    cfg_fab = config.get("fabrica", {})
    candidatas = _candidatas_rodadas(config, data_hoje)

    if not candidatas:
        cobertura_meses_fixo = cfg_fab.get("cobertura_meses", 2)
        janela_fim = data_hoje + pd.DateOffset(months=cobertura_meses_fixo)
        return {
            "tem_rodadas": False,
            "modo": "fallback_fixo",
            "janela_inicio": data_hoje,
            "janela_fim": janela_fim,
            "cobertura_meses": float(cobertura_meses_fixo),
            "rodada_proxima": None,
            "rodada_seguinte": None,
            "label": f"Cobertura fixa ({cobertura_meses_fixo} meses) — nenhuma rodada de produção configurada",
        }

    proxima = next((c for c in candidatas if c["data_chegada"] >= data_hoje), candidatas[-1])
    idx = candidatas.index(proxima)
    depois_da_proxima = candidatas[idx + 1]

    janela_inicio = data_hoje
    janela_fim = depois_da_proxima["data_chegada"]

    label = (
        f"Cobre até a Rodada {depois_da_proxima['numero']} "
        f"({NOMES_MES[depois_da_proxima['mes_disparo'] - 1]}/{depois_da_proxima['ano_disparo']} "
        f"→ chega {NOMES_MES[depois_da_proxima['data_chegada'].month - 1]}/{depois_da_proxima['data_chegada'].year})"
    )

    return {
        "tem_rodadas": True,
        "modo": "rodadas",
        "janela_inicio": janela_inicio,
        "janela_fim": janela_fim,
        "cobertura_meses": max((janela_fim - janela_inicio).days, 1) / 30,
        "rodada_proxima": proxima,
        "rodada_seguinte": depois_da_proxima,
        "label": label,
    }


def _sequencia_rodadas(config: dict, data_hoje: pd.Timestamp) -> list:
    """
    Sequência cronológica de rodadas cuja CHEGADA é hoje ou no futuro, cada uma
    com a data de chegada da rodada seguinte (fim do intervalo de proteção).
    Base da simulação order-up-to. O calendário explícito é finito e deliberado
    (este ano + próximo); a última data configurada não gera pedido próprio —
    só fecha o intervalo da penúltima.
    """
    candidatas = _candidatas_rodadas(config, data_hoje)
    if not candidatas:
        return []
    seq = []
    for i, c in enumerate(candidatas):
        if c["data_chegada"] < data_hoje:
            continue
        if i + 1 >= len(candidatas):
            continue
        seq.append({**c, "data_chegada_seguinte": candidatas[i + 1]["data_chegada"]})
    return seq


# Alias público — usado por pedidos/builder.py p/ resolver a identidade
# absoluta (datas de disparo/chegada) de uma rodada no congelamento.
sequencia_rodadas = _sequencia_rodadas


def _par_ceil(x: float) -> int:
    """Arredonda pra cima forçando número par (produção em pares)."""
    q = math.ceil(max(x, 0))
    return q + 1 if q % 2 != 0 else q


def simular_politica_reabastecimento(dados: dict, config: dict,
                                     ativo_crescimento: bool = None,
                                     data_hoje=None, dem: pd.DataFrame = None) -> pd.DataFrame:
    """
    Política de revisão periódica order-up-to (R,S) com PROJEÇÃO FORWARD por SKU.

    Para cada SKU, caminha as rodadas em ordem cronológica mantendo o estoque
    projetado (estoque atual − backlog, consumido mês a mês e reabastecido a
    cada chegada). Em cada rodada r:
        DemandaPeriodo_r   = demanda no intervalo [chegada_r, chegada_{r+1})
        EstoqueSeguranca_r = Fator de Serviço × Variação da Demanda × DemandaPeriodo_r
        EstoqueAlvo_r      = DemandaPeriodo_r + EstoqueSeguranca_r
        Pedido_r           = arredonda_par(máx(EstoqueAlvo_r − EstoqueProjetado_na_chegada, 0))

    É o motor comum: a Sugestão por SKU é o Pedido da rodada selecionada; a
    Visão Geral é a soma por rodada. Retorna uma linha por (SKU, rodada) com
    [SKU, ID_produto, rodada, mes_disparo, ano_disparo, data_disparo,
     data_chegada, data_chegada_seguinte, MesesIntervalo, DemandaPeriodo,
     DemandaPeriodoAlta, DemandaPeriodoBaixa, EstoqueSeguranca, EstoqueAlvo,
     EstoqueProjetado, Pedido, contem_alta].
    """
    data_hoje = pd.Timestamp.now().normalize() if data_hoje is None else pd.Timestamp(data_hoje)

    if dem is None:
        dem = calcular_demanda_mensal_por_sku(dados, config, ativo_crescimento)
    janela_alta = set((config.get("demanda", {}) or {}).get("janela_alta", [12, 1, 2]))

    demanda_por_sku, tem_demanda_sku, idprod_por_sku = {}, {}, {}
    for sku, g in dem.groupby("SKU"):
        dpm = g.set_index("Mes")["DemandaMensalProjetada"].to_dict()
        demanda_por_sku[sku] = dpm
        tem_demanda_sku[sku] = sum(dpm.values()) > 0    # pico OU manutenção
        idprod_por_sku[sku] = g["ID_produto"].iloc[0]

    est = dados["estoque"].groupby("ID_produto")["saldoFisico"].sum().to_dict()
    sit_backlog = config.get("fabrica", {}).get("situacoes_backlog", [])
    map_ped_sit = dados["pedidos"].set_index("ID")["id_situacao"].to_dict()
    it = dados["itens"].copy()
    it["_sit"] = it["ID_pedido"].map(map_ped_sit)
    backlog = it[it["_sit"].isin(sit_backlog)].groupby("ID_produto")["Quantidade"].sum().to_dict()

    seq = _sequencia_rodadas(config, data_hoje)
    if not seq:
        return pd.DataFrame(columns=[
            "SKU", "ID_produto", "rodada", "mes_disparo", "ano_disparo",
            "data_disparo", "data_chegada", "data_chegada_seguinte",
            "MesesIntervalo", "DemandaPeriodo", "DemandaPeriodoAlta",
            "DemandaPeriodoBaixa", "EstoqueSeguranca",
            "EstoqueAlvo", "EstoqueProjetado", "Pedido", "contem_alta",
        ])

    rows = []
    for sku, dem_mes in demanda_por_sku.items():
        idp = idprod_por_sku[sku]
        stock = float(est.get(idp, 0)) - float(backlog.get(idp, 0))
        tem_demanda = tem_demanda_sku[sku]
        prev = data_hoje

        for r in seq:
            A = r["data_chegada"]
            # Consome demanda de prev até a chegada desta rodada
            for m, frac in fracionar_janela_por_mes(prev, A):
                stock -= dem_mes.get(m, 0) * frac
            estoque_projetado = stock

            # Intervalo de proteção: desta chegada até a próxima
            meses_int = fracionar_janela_por_mes(A, r["data_chegada_seguinte"])
            demanda_periodo_alta = sum(
                dem_mes.get(m, 0) * frac for m, frac in meses_int if m in janela_alta)
            demanda_periodo_baixa = sum(
                dem_mes.get(m, 0) * frac for m, frac in meses_int if m not in janela_alta)
            demanda_periodo = demanda_periodo_alta + demanda_periodo_baixa
            n_meses_int = sum(frac for _, frac in meses_int)
            contem_alta = any((m in janela_alta) and frac > 0 for m, frac in meses_int)
            seguranca = estoque_seguranca(demanda_periodo, contem_alta, config, n_meses_int)
            estoque_alvo = demanda_periodo + seguranca

            pedido = _par_ceil(estoque_alvo - estoque_projetado)
            pedido_sem_estoque = _par_ceil(estoque_alvo)   # pedido se ignorasse o estoque (só demanda+segurança)
            if pedido == 0 and estoque_projetado <= 0 and tem_demanda:
                pedido = 2  # mínimo existencial: tem demanda mas estoque zerou
            stock = estoque_projetado + pedido  # a rodada chega

            rows.append({
                "SKU": sku, "ID_produto": idp, "rodada": r["numero"],
                "mes_disparo": r["mes_disparo"], "ano_disparo": r["ano_disparo"],
                "data_disparo": r["data_disparo"], "data_chegada": A,
                "data_chegada_seguinte": r["data_chegada_seguinte"],
                "MesesIntervalo": n_meses_int,
                "DemandaPeriodo": demanda_periodo,
                "DemandaPeriodoAlta": demanda_periodo_alta,
                "DemandaPeriodoBaixa": demanda_periodo_baixa,
                "EstoqueSeguranca": seguranca,
                "EstoqueAlvo": estoque_alvo, "EstoqueProjetado": estoque_projetado,
                "Pedido": pedido, "PedidoSemEstoque": pedido_sem_estoque,
                "contem_alta": contem_alta,
            })
            prev = A

    return pd.DataFrame(rows)
