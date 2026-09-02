"""
metas.py — Motor puro de Metas Escalonadas (Prata/Ouro/Diamante) por loja.

Sem streamlit, sem pandas — só dicts, testável sem fixtures pesadas. Consumido
por etl/daily.py (monta os DataFrames) e pages/5_Configuracoes.py (valida antes
de salvar). Modelo e decisões em docs/requisitos/metas-escalonadas.md.

Meta é definida SÓ por loja (config["daily"]["metas_mensais"]); a meta do
vendedor é sempre DERIVADA por rateio da meta da loja (nunca digitada), via
a atribuição vendedor→loja com vigência mensal (config["daily"]["vendedores_loja"]).
Colégio não tem meta cadastrada — é só recorte do realizado (feito em daily.py).

Uso:
    from etl import metas
    m = metas.metas_da_loja(config, "Natal", "2026-08")
    resumo = metas.resumo_faturamento(vendido=42000, metas=m["faturamento"],
                                       dia_atual=12, dias_no_mes=31)
"""

NIVEIS = ("prata", "ouro", "diamante")
NOMES_NIVEL = {"prata": "Prata", "ouro": "Ouro", "diamante": "Diamante"}


# ---------------------------------------------------------------------
# Competência
# ---------------------------------------------------------------------

def chave_competencia(ano: int, mes: int) -> str:
    """(2026, 1) -> '2026-01'. String ordenável, chave JSON válida."""
    return f"{int(ano):04d}-{int(mes):02d}"


# ---------------------------------------------------------------------
# Metas da loja
# ---------------------------------------------------------------------

def _normalizar_niveis(bloco: dict) -> dict:
    """Garante as 3 chaves (prata/ouro/diamante) como float, faltantes None."""
    return {n: (float(bloco[n]) if bloco.get(n) is not None else None) for n in NIVEIS}


def metas_da_loja(config: dict, loja: str, competencia: str) -> dict:
    """
    Metas de faturamento e PA da loja na competência.

    Retorna {"faturamento": {prata,ouro,diamante} | None,
             "pa": {prata,ouro,diamante} | None,
             "origem": "configurada" | "estimada" | "ausente"}

    - "configurada": veio de daily.metas_mensais[competencia][loja].
    - "estimada": fallback do formato legado daily.metas[loja] (só faturamento,
      Ouro = valor legado, Prata = 0.85x, Diamante = 1.20x; PA fica None).
    - "ausente": nenhuma fonte tem essa loja/competência.
    """
    cfg_daily = config.get("daily", {}) or {}
    bloco_mes = (cfg_daily.get("metas_mensais") or {}).get(competencia) or {}
    bloco_loja = bloco_mes.get(loja)

    if bloco_loja:
        fat_bruto = bloco_loja.get("faturamento") or {}
        pa_bruto = bloco_loja.get("pa") or {}
        fat = _normalizar_niveis(fat_bruto) if fat_bruto else None
        pa = _normalizar_niveis(pa_bruto) if pa_bruto else None
        if fat or pa:
            return {"faturamento": fat, "pa": pa, "origem": "configurada"}

    legado = (cfg_daily.get("metas") or {}).get(loja)
    if legado is not None and float(legado) > 0:
        ouro = float(legado)
        return {
            "faturamento": {"prata": round(ouro * 0.85, 2), "ouro": ouro, "diamante": round(ouro * 1.20, 2)},
            "pa": None,
            "origem": "estimada",
        }

    return {"faturamento": None, "pa": None, "origem": "ausente"}


# ---------------------------------------------------------------------
# Atribuição vendedor → loja (vigência mensal, herda do mês anterior)
# ---------------------------------------------------------------------

def atribuicao_vendedores(config: dict, competencia: str) -> dict:
    """
    {vendedor_id: {"loja": str, "peso": float, "ativo": bool}} vigente na
    competência — a competência com edição mais recente <= a pedida (a chave
    'AAAA-MM' ordena como string igual a uma data). Sem edição em nenhum mês
    anterior ou igual: {}.
    """
    bloco = (config.get("daily", {}) or {}).get("vendedores_loja") or {}
    candidatas = sorted(k for k in bloco if k <= competencia)
    if not candidatas:
        return {}
    return bloco[candidatas[-1]] or {}


def metas_do_vendedor(config: dict, vendedor_id: str, loja: str, competencia: str) -> dict:
    """
    Meta do vendedor DERIVADA por rateio (nunca cadastrada): faturamento é
    a fração da meta da loja proporcional ao peso do vendedor entre os ativos
    da mesma loja; PA repete a meta da loja (razão não se rateia).
    """
    atrib = atribuicao_vendedores(config, competencia)
    pesos_loja = {
        vid: info for vid, info in atrib.items()
        if info.get("loja") == loja and info.get("ativo", True)
    }
    total_peso = sum(float(info.get("peso", 1.0)) for info in pesos_loja.values())
    peso_vendedor = float(pesos_loja.get(vendedor_id, {}).get("peso", 0.0)) if vendedor_id in pesos_loja else 0.0

    metas_loja = metas_da_loja(config, loja, competencia)
    fat_loja = metas_loja["faturamento"]

    fat_vendedor = None
    if fat_loja and total_peso > 0 and peso_vendedor > 0:
        fator = peso_vendedor / total_peso
        fat_vendedor = {n: (v * fator if v is not None else None) for n, v in fat_loja.items()}

    return {
        "faturamento": fat_vendedor,
        "pa": metas_loja["pa"],
        "origem": metas_loja["origem"],
        "peso": peso_vendedor,
        "loja": loja,
    }


# ---------------------------------------------------------------------
# Classificação de nível
# ---------------------------------------------------------------------

def classificar_nivel(realizado: float, metas: dict) -> dict:
    """
    {nivel, proximo_nivel, proximo_valor, falta, pct_do_proximo}

    - `metas` None -> tudo None (sem meta configurada).
    - `nivel`: maior nível ultrapassado ("Prata"/"Ouro"/"Diamante") ou None.
    - `proximo_nivel`/`proximo_valor`: o nível ainda não alcançado mais baixo;
      None quando já passou do Diamante (falta = 0, pct = 1.0 nesse caso).
    """
    if metas is None:
        return {"nivel": None, "proximo_nivel": None, "proximo_valor": None,
                "falta": None, "pct_do_proximo": None}

    realizado = float(realizado or 0)
    nivel_atual = None
    proximo = None
    for n in NIVEIS:
        valor = metas.get(n)
        if valor is None:
            continue
        if realizado >= valor:
            nivel_atual = NOMES_NIVEL[n]
        elif proximo is None:
            proximo = (NOMES_NIVEL[n], valor)

    if proximo is not None:
        prox_nome, prox_valor = proximo
        falta = max(prox_valor - realizado, 0.0)
        pct = (realizado / prox_valor) if prox_valor else None
    else:
        prox_nome, prox_valor = None, None
        falta = 0.0
        pct = 1.0

    return {"nivel": nivel_atual, "proximo_nivel": prox_nome, "proximo_valor": prox_valor,
            "falta": falta, "pct_do_proximo": pct}


# ---------------------------------------------------------------------
# Resumos (classificação + projeção)
# ---------------------------------------------------------------------

def resumo_faturamento(vendido: float, metas: dict, dia_atual: int, dias_no_mes: int) -> dict:
    """
    classificar_nivel(vendido, metas) + run_rate (projeção linear),
    nivel_projetado e ritmo_necessario (falta / dias restantes — o número
    acionável: "precisa vender R$X/dia para bater o próximo nível").
    """
    base = classificar_nivel(vendido, metas)
    dia_atual = int(dia_atual or 0)
    dias_no_mes = int(dias_no_mes or 0)

    run_rate = (vendido / dia_atual * dias_no_mes) if dia_atual > 0 else 0.0
    nivel_projetado = classificar_nivel(run_rate, metas)["nivel"] if metas else None

    dias_restantes = max(dias_no_mes - dia_atual, 0)
    if base["falta"] is None:
        ritmo_necessario = None
    elif base["falta"] <= 0:
        ritmo_necessario = 0.0
    elif dias_restantes > 0:
        ritmo_necessario = base["falta"] / dias_restantes
    else:
        ritmo_necessario = None   # mês acabou sem bater — não há "por dia" que resolva

    return {**base, "vendido": float(vendido or 0), "run_rate": run_rate,
            "nivel_projetado": nivel_projetado, "ritmo_necessario": ritmo_necessario}


def resumo_pa(pecas: float, pedidos: float, metas: dict) -> dict:
    """classificar_nivel sobre PA = peças/pedidos. Sem run rate (§2.3 da spec:
    o PA parcial do mês já É a projeção — não é uma soma que acelera)."""
    pedidos = float(pedidos or 0)
    pa = (float(pecas or 0) / pedidos) if pedidos > 0 else 0.0
    base = classificar_nivel(pa, metas)
    return {**base, "pa": pa, "pecas": float(pecas or 0), "pedidos": pedidos}


# ---------------------------------------------------------------------
# Agregação (multi-loja / multi-vendedor)
# ---------------------------------------------------------------------

def agregar_faturamento(linhas: list) -> dict:
    """linhas: [{"vendido": float, "metas": dict|None}, ...].
    Faturamento agrega por soma direta (meta e realizado)."""
    total_vendido = sum(float(l.get("vendido") or 0) for l in linhas)
    metas_soma = None
    for l in linhas:
        m = l.get("metas")
        if not m:
            continue
        if metas_soma is None:
            metas_soma = {n: 0.0 for n in NIVEIS}
        for n in NIVEIS:
            metas_soma[n] += float(m.get(n) or 0)
    return {"vendido": total_vendido, "metas": metas_soma}


def agregar_pa(linhas: list) -> dict:
    """linhas: [{"pecas": float, "pedidos": float, "metas": dict|None}, ...].
    PA NÃO é a média dos PAs — é Σpeças/Σpedidos. A meta agregada, pela mesma
    lógica, é a meta ponderada por pedidos (não a média simples das metas)."""
    total_pecas = sum(float(l.get("pecas") or 0) for l in linhas)
    total_pedidos = sum(float(l.get("pedidos") or 0) for l in linhas)
    pa = (total_pecas / total_pedidos) if total_pedidos > 0 else 0.0

    metas_soma = {n: 0.0 for n in NIVEIS}
    peso_total = 0.0
    tem_meta = False
    for l in linhas:
        m = l.get("metas")
        peso = float(l.get("pedidos") or 0)
        if m and peso > 0:
            tem_meta = True
            for n in NIVEIS:
                metas_soma[n] += float(m.get(n) or 0) * peso
            peso_total += peso

    metas_final = {n: metas_soma[n] / peso_total for n in NIVEIS} if (tem_meta and peso_total > 0) else None
    return {"pecas": total_pecas, "pedidos": total_pedidos, "pa": pa, "metas": metas_final}


# ---------------------------------------------------------------------
# Histórico de atingimento (série de competências)
# ---------------------------------------------------------------------

def competencias_anteriores(competencia: str, quantidade: int) -> list:
    """`quantidade` competências terminando em `competencia`, em ordem
    cronológica. Aritmética de calendário em ints — sem depender de pandas."""
    ano, mes = int(competencia[:4]), int(competencia[5:7])
    saida = []
    for passo in range(quantidade - 1, -1, -1):
        total = ano * 12 + (mes - 1) - passo
        saida.append(chave_competencia(total // 12, total % 12 + 1))
    return saida


def historico_atingimento(config: dict, lojas: list, realizado: dict,
                          competencias: list) -> list:
    """
    Classifica o nível conquistado em cada competência da série.

    `realizado`: {(competencia, loja): {"vendido", "pecas", "pedidos"}} —
    o que a página extrai do df_detalhado com um groupby só (barato: uma
    passada, não um processar_daily por mês).

    Devolve uma lista alinhada com `competencias`, cada item com o agregado
    das `lojas` e o nível de faturamento e de PA. Mês sem meta cadastrada sai
    com `nivel=None` e `tem_meta=False` — a lacuna é explícita, nunca um zero
    que pareceria desempenho ruim.
    """
    saida = []
    for comp in competencias:
        linhas_fat, linhas_pa, origens = [], [], set()
        for loja in lojas:
            r = realizado.get((comp, loja)) or {}
            m = metas_da_loja(config, loja, comp)
            origens.add(m["origem"])
            linhas_fat.append({"vendido": r.get("vendido", 0.0), "metas": m["faturamento"]})
            linhas_pa.append({"pecas": r.get("pecas", 0.0),
                              "pedidos": r.get("pedidos", 0.0), "metas": m["pa"]})
        agg_f = agregar_faturamento(linhas_fat)
        agg_p = agregar_pa(linhas_pa)
        cls_f = classificar_nivel(agg_f["vendido"], agg_f["metas"])
        cls_p = classificar_nivel(agg_p["pa"], agg_p["metas"]) if agg_p["pedidos"] else {"nivel": None}
        saida.append({
            "competencia": comp,
            "vendido": agg_f["vendido"],
            "pa": agg_p["pa"],
            "pecas": agg_p["pecas"],
            "pedidos": agg_p["pedidos"],
            "metas": agg_f["metas"],
            "nivel": cls_f["nivel"],
            "nivel_pa": cls_p["nivel"],
            "tem_meta": agg_f["metas"] is not None,
            # "configurada" só quando TODAS as lojas do recorte têm cadastro no
            # mês; se alguma caiu no fallback legado, o mês inteiro é "estimada".
            # Sem isso o gráfico exibiria a meta-fallback chapada nos 12 meses
            # como se fosse cadastro do gestor.
            "origem": ("configurada" if origens == {"configurada"}
                       else ("ausente" if origens == {"ausente"} else "estimada")),
        })
    return saida


# ---------------------------------------------------------------------
# Edição (transforma o que saiu do data_editor no dict persistido)
#
# Vivem aqui, e não na página, porque são o caminho de ESCRITA em
# app.parametros: um bug aqui corrompe a configuração de todo mundo.
# Puras → testáveis sem Streamlit e sem Supabase.
# ---------------------------------------------------------------------

def _niveis_da_linha(linha: dict, prefixo: str) -> dict | None:
    """Recorta {prata,ouro,diamante} de uma linha do editor pelo prefixo da
    métrica. Devolve None se os três vierem vazios — célula em branco é
    'não configurado', nunca zero (zero é uma meta legítima de valor zero)."""
    bloco = {}
    for nivel in NIVEIS:
        valor = linha.get(f"{prefixo}_{nivel}")
        # NaN do pandas não é igual a si mesmo — pega célula vazia sem importar pandas
        vazio = valor is None or (isinstance(valor, float) and valor != valor)
        bloco[nivel] = None if vazio else float(valor)
    return bloco if any(v is not None for v in bloco.values()) else None


def aplicar_edicao_metas(metas_mensais: dict, ano: int, loja: str,
                         linhas: list) -> tuple:
    """
    Aplica a edição de UMA loja em UM ano sobre o dict `metas_mensais`.

    `linhas`: dicts com `mes` (1-12) e as colunas `fat_*` / `pa_*` do editor.
    Devolve `(novo_dict, n_meses_configurados)`. Não muta a entrada.

    Regras: mês com as 6 células vazias REMOVE a meta daquela loja (é assim
    que o gestor apaga um mês); competência que ficou sem nenhuma loja sai do
    dict, para não deixar chave órfã. As demais lojas do mesmo mês e os
    demais anos passam intactos.
    """
    novo = {comp: dict(bloco or {}) for comp, bloco in (metas_mensais or {}).items()}
    n_meses = 0

    for linha in linhas:
        comp = chave_competencia(ano, int(linha["mes"]))
        fat = _niveis_da_linha(linha, "fat")
        pa = _niveis_da_linha(linha, "pa")
        bloco_mes = dict(novo.get(comp) or {})

        if fat is None and pa is None:
            bloco_mes.pop(loja, None)
        else:
            entrada = {}
            if fat:
                entrada["faturamento"] = fat
            if pa:
                entrada["pa"] = pa
            bloco_mes[loja] = entrada
            n_meses += 1

        if bloco_mes:
            novo[comp] = bloco_mes
        else:
            novo.pop(comp, None)

    return novo, n_meses


def aplicar_edicao_vendedores(vendedores_loja: dict, competencia: str,
                              linhas: list, sem_atribuicao: str) -> dict:
    """
    Aplica a atribuição vendedor→loja de UMA competência sobre o dict.

    `linhas`: dicts com `vendedor_id`, `loja`, `peso`, `ativo`. Linhas com
    loja == `sem_atribuicao` são descartadas (o vendedor fica de fora do
    rateio). Competência que ficou vazia é REMOVIDA — e, como a resolução
    herda da competência anterior mais recente, apagar um mês faz a
    atribuição voltar à do mês anterior, não sumir. Não muta a entrada.
    """
    novo = dict(vendedores_loja or {})
    entrada = {}
    for linha in linhas:
        if linha.get("loja") == sem_atribuicao:
            continue
        entrada[str(linha["vendedor_id"])] = {
            "loja": str(linha["loja"]),
            "peso": float(linha["peso"]),
            "ativo": bool(linha["ativo"]),
        }
    if entrada:
        novo[competencia] = entrada
    else:
        novo.pop(competencia, None)
    return novo


# ---------------------------------------------------------------------
# Atalhos de preenchimento da grade (usados pela 5_Configuracoes)
#
# Puros: recebem/devolvem a LISTA de linhas do editor. A UI só desenha o
# botão e persiste o resultado no preview de sessão.
# ---------------------------------------------------------------------

def _escalonar(ouro: float, prata_pct: float = 0.85, diamante_pct: float = 1.20) -> dict:
    """Deriva os três níveis a partir do Ouro (a âncora que o gestor pensa)."""
    return {"prata": round(ouro * prata_pct, 2), "ouro": round(ouro, 2),
            "diamante": round(ouro * diamante_pct, 2)}


def _vazia(linha: dict) -> bool:
    """Linha sem NENHUMA das 6 células de meta preenchidas."""
    for prefixo in ("fat", "pa"):
        for nivel in NIVEIS:
            v = linha.get(f"{prefixo}_{nivel}")
            if not (v is None or (isinstance(v, float) and v != v)):
                return False
    return True


def copiar_do_ano_anterior(linhas: list, metas_mensais: dict, ano: int,
                           loja: str, fator: float = 1.0) -> tuple:
    """
    Preenche a grade com as metas JÁ CADASTRADAS do ano anterior, opcionalmente
    multiplicadas por `fator` (1.10 = +10%).

    Diferente de `propor_do_realizado`, não depende do histórico de vendas —
    é o atalho que funciona mesmo com o realizado indisponível. Devolve
    `(novas_linhas, n_meses_copiados)`; não muta a entrada.
    """
    saida, copiados = [], 0
    for linha in linhas:
        nova = dict(linha)
        bloco = ((metas_mensais or {}).get(chave_competencia(ano - 1, int(linha["mes"])))
                 or {}).get(loja) or {}
        achou = False
        for prefixo, metrica in (("fat", "faturamento"), ("pa", "pa")):
            niveis = bloco.get(metrica) or {}
            for nivel in NIVEIS:
                v = niveis.get(nivel)
                if v is not None:
                    nova[f"{prefixo}_{nivel}"] = round(float(v) * fator, 2)
                    achou = True
        if achou:
            copiados += 1
        saida.append(nova)
    return saida, copiados


def replicar_nos_vazios(linhas: list) -> tuple:
    """
    Copia a PRIMEIRA linha preenchida para todos os meses ainda vazios.

    Atalho para a meta plana (mesma cifra o ano todo) — o gestor ajusta
    depois só os meses atípicos. Linhas já preenchidas nunca são
    sobrescritas. Devolve `(novas_linhas, n_meses_preenchidos)`.
    """
    modelo = next((l for l in linhas if not _vazia(l)), None)
    if modelo is None:
        return [dict(l) for l in linhas], 0

    campos = [f"{p}_{n}" for p in ("fat", "pa") for n in NIVEIS]
    saida, preenchidos = [], 0
    for linha in linhas:
        nova = dict(linha)
        if _vazia(linha):
            for campo in campos:
                nova[campo] = modelo.get(campo)
            preenchidos += 1
        saida.append(nova)
    return saida, preenchidos


# ---------------------------------------------------------------------
# Validação (usado por pages/5_Configuracoes.py antes de salvar)
# ---------------------------------------------------------------------

def validar_metas_mensais(metas_mensais: dict) -> list:
    """Lista de mensagens de erro; [] = válido. prata<=ouro<=diamante e >=0,
    só quando os 3 níveis de um bloco estão preenchidos (permite cadastro
    parcial em progresso sem barrar o save de outros meses)."""
    erros = []
    for comp, lojas in (metas_mensais or {}).items():
        for loja, blocos in (lojas or {}).items():
            for metrica in ("faturamento", "pa"):
                niveis = (blocos or {}).get(metrica) or {}
                p, o, d = niveis.get("prata"), niveis.get("ouro"), niveis.get("diamante")
                for nome, v in (("prata", p), ("ouro", o), ("diamante", d)):
                    if v is not None and v < 0:
                        erros.append(f"{comp} · {loja} · {metrica}.{nome} não pode ser negativo")
                if None not in (p, o, d) and not (p <= o <= d):
                    erros.append(
                        f"{comp} · {loja} · {metrica}: prata ({p}) ≤ ouro ({o}) ≤ diamante ({d}) violado"
                    )
    return erros
