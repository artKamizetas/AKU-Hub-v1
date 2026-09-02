"""
loader.py — Leitura e Validação dos Dados do Bling (via Supabase).

Lê o Postgres do Supabase via PostgREST (`postgrest`) usando SUPABASE_URL +
SERVICE_KEY de st.secrets["supabase"]. Tabelas mapeadas em TABELAS_SUPABASE e
colunas renomeadas via COLUNAS_SUPABASE para casar com o SCHEMA esperado pelas
páginas e módulos ETL.

Uso:
    from etl.loader import carregar_dados
    dados = carregar_dados()
    dados["pedidos"]  # DataFrame dos pedidos
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import streamlit as st


# Mapa de abas esperadas e suas colunas obrigatórias
SCHEMA = {
    "Pedidos": ["ID", "Pedido", "id_situacao", "Vendedor", "Loja ID", "Data",
                "Total Produtos", "Total Venda", "Cliente"],
    "Itens": ["ID_pedido", "ID_produto", "Quantidade", "Data", "Valor Unidade", "Desconto Item"],
    "Produtos": ["ID", "codigo", "Descricao", "situacao", "preco_custo"],
    "EstoqueV3": ["ID_deposito", "ID_produto", "saldoFisico"],
    "Produtos_detalhes": ["ID_produto", "Codigo", "categoria", "Super_categoria",
                          "Grupo", "Tamanho"],
    "Vendedores": ["ID", "nome"],
    "Lojas": ["ID", "descricao", "Situacao"],
    "Situações": ["ID", "descricao"],
    "Depósitos": ["ID", "descricao"],
}


# Mapa: aba do SCHEMA → nome da tabela no Supabase (schema 'public').
# Confirmado via scripts/inspecionar_supabase.py (Fase 0).
TABELAS_SUPABASE = {
    "Pedidos": "pedidos",
    "Itens": "itens",
    "Produtos": "produtos",
    "EstoqueV3": "estoque",
    "Produtos_detalhes": "produto_detalhes",
    "Vendedores": "vendedores",
    "Lojas": "lojas",
    "Situações": "situacoes_vendas",
    "Depósitos": "depositos",
}

# Renomeio de colunas: aba → {coluna_no_supabase: coluna_do_SCHEMA}.
# IDs usados são os do Bling (*_bling), não o `id` surrogate do Supabase —
# config e joins entre tabelas usam IDs Bling. Confirmado na Fase 0.
COLUNAS_SUPABASE = {
    "Pedidos": {
        "id_bling": "ID", "numero": "Pedido", "id_situacao_bling": "id_situacao",
        "id_vendedor_bling": "Vendedor", "id_loja_bling": "Loja ID",
        "data": "Data", "valor_total": "Total Venda", "cliente": "Cliente",
        "desconto": "Desconto",  # usado por daily.py (não está no SCHEMA)
    },
    "Itens": {
        "id_pedido_bling": "ID_pedido", "id_produto_bling": "ID_produto",
        "quantidade": "Quantidade", "valor_unidade": "Valor Unidade",
        "desconto_item": "Desconto Item",
    },  # 'Data' não existe em itens — enriquecida via join em Pedidos
    "Produtos": {"id_bling": "ID", "descricao": "Descricao"},
    "EstoqueV3": {"id_deposito_bling": "ID_deposito", "id_produto_bling": "ID_produto"},
    "Produtos_detalhes": {
        "id_produto_bling": "ID_produto", "codigo": "Codigo",
        "super_categoria": "Super_categoria", "linha": "Grupo",
        "tamanho": "Tamanho", "marca": "Marca_sku",
    },
    "Vendedores": {"id_bling": "ID"},
    "Lojas": {"id_bling": "ID", "situcao": "Situacao"},  # 'situcao' = typo na origem
    "Situações": {"id_bling": "ID"},
    "Depósitos": {"id_bling": "ID"},
}


def limpar_id(valor):
    """
    Limpa IDs que o pandas converte para float (ex: 203379922.0 → "203379922").
    Trata: float, int, str com '.0' no final, NaN.
    """
    if pd.isna(valor):
        return ""
    s = str(valor).strip()
    # Remove '.0' do final de IDs numéricos (artefato do pandas lendo float)
    if s.endswith(".0"):
        try:
            return str(int(float(s)))
        except (ValueError, OverflowError):
            pass
    return s


def converter_data_flexivel(valor):
    """
    Converte datas em múltiplos formatos:
      - ISO: YYYY-MM-DD (ex: 2026-02-24)
      - BR: DD/MM/YYYY (ex: 24/02/2026)

    Retorna datetime ou NaT se não conseguir converter.
    """
    if pd.isna(valor):
        return pd.NaT

    s = str(valor).strip()
    if not s:
        return pd.NaT

    # Tenta formato ISO primeiro
    if "-" in s and len(s) == 10:
        try:
            return pd.to_datetime(s, format="%Y-%m-%d")
        except (ValueError, TypeError):
            pass

    # Tenta formato BR (DD/MM/YYYY)
    if "/" in s:
        try:
            return pd.to_datetime(s, format="%d/%m/%Y")
        except (ValueError, TypeError):
            pass

    # Fallback: deixa o pandas tentar com dayfirst=True
    try:
        return pd.to_datetime(s, errors="coerce", dayfirst=True)
    except Exception:
        return pd.NaT

def converter_serie_data(serie: pd.Series) -> pd.Series:
    """
    Versão VETORIZADA de converter_data_flexivel, para uma coluna inteira.

    O espelho entrega ISO (YYYY-MM-DD) em praticamente 100% das linhas, mas a
    conversão elemento a elemento custava ~5,8 s nas 140k linhas de
    Pedidos+Itens — mais da metade do tempo de transformação depois que a
    leitura ficou rápida. Faz UMA passada vetorizada no formato ISO e cai no
    elemento a elemento só no que sobrou (datas BR, lixo), preservando
    exatamente a semântica de converter_data_flexivel.
    """
    conv = pd.to_datetime(serie, format="%Y-%m-%d", errors="coerce")
    resto = conv.isna() & serie.notna()
    if resto.any():
        conv = conv.copy()
        conv.loc[resto] = serie[resto].apply(converter_data_flexivel)
    return conv


_PAGE_SIZE = 1000        # teto do PostgREST (max-rows) — não adianta aumentar
_MAX_WORKERS = 16       # 16 pega ~90% do ganho; 32 rende só +10% (medido)
_TENTATIVAS_PAGINA = 3  # o fan-out multiplicou os requests: 5xx vira provável

# Colunas que o loader descarta logo depois (dropna) — filtro empurrado para o
# servidor. Em `itens`, 41% das linhas têm id_pedido_bling nulo: baixá-las custa
# ~68 páginas para nada.
FILTROS_NAO_NULOS = {"itens": ("id_pedido_bling",)}

# tabela real → aba do SCHEMA (rótulo humano usado no progresso)
_ABA_POR_TABELA = {v: k for k, v in TABELAS_SUPABASE.items() if v}


@st.cache_data(ttl=300)
def carregar_config() -> dict:
    """
    Config efetivo do app: config.yaml (defaults, Categoria A) mesclado com
    app.parametros do Supabase (Categoria B — o que o gestor edita na página
    de Configurações e precisa sobreviver a redeploy no Streamlit Cloud).

    Degradação graciosa: Supabase indisponível → yaml puro + aviso (o app
    continua funcionando com os defaults do git).

    Único ponto de leitura de config do app — páginas e scripts NÃO devem
    abrir config.yaml diretamente. Página 5 salva via config_store e chama
    st.cache_data.clear() (invalida este cache junto).
    """
    from pathlib import Path

    import yaml

    from etl.config_store import deep_merge, obter_repositorio_parametros

    caminho = Path(__file__).resolve().parent.parent / "config.yaml"
    with open(caminho, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    try:
        dados = obter_repositorio_parametros().ler()
        if dados:
            config = deep_merge(config, dados)
    except Exception as e:  # sem Supabase (dev offline, DDL não aplicado…)
        try:
            st.warning(
                f"⚠️ Parâmetros do Supabase indisponíveis — usando defaults "
                f"do config.yaml (alterações da página de Configurações podem "
                f"não estar refletidas). Detalhe: {e}"
            )
        except Exception:
            pass  # fora do Streamlit (scripts CLI): segue com o yaml puro

    return config


def fingerprint_config(config: dict) -> str:
    """
    Assinatura curta do config efetivo, para entrar na cache key das funções
    pesadas das páginas (`_processar`, `_processar_daily`).

    Essas funções recebem o config como `_config` — fora do hash, porque dict
    não é hashável. Enquanto a invalidação era `st.cache_data.clear()` global,
    isso não fazia falta: salvar um parâmetro derrubava tudo junto. Agora que o
    clear é cirúrgico (só `carregar_config`), sem esta assinatura o resultado
    ficaria preso ao config ANTIGO até o TTL — o gestor salvaria uma meta e não
    veria efeito nenhum na tela.
    """
    import hashlib
    import json

    bruto = json.dumps(config, sort_keys=True, default=str)
    return hashlib.sha256(bruto.encode("utf-8")).hexdigest()[:12]


@st.cache_resource
def _conn_supabase():
    """
    Cliente PostgREST (cacheado por sessão). Usa SERVICE_KEY (ignora RLS).
    `postgrest` é o subconjunto da Data API do supabase-py — mesmas
    credenciais (SUPABASE_URL + SERVICE_KEY), sem deps que exigem compilador.

    HTTP/1.1 forçado (http2=False) — HTTP/2 multiplexa streams em 1 conexão TCP
    e tem race em uso concorrente por threads (ConnectionTerminated PROTOCOL_ERROR).
    HTTP/1.1 usa pool de conexões separadas, totalmente thread-safe.
    """
    import httpx
    from postgrest import SyncPostgrestClient

    cfg = st.secrets["supabase"]
    key = cfg["service_key"]
    schema = cfg.get("schema", "public")
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    http_client = httpx.Client(
        http2=False,
        headers=headers,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=20),
    )
    return SyncPostgrestClient(
        f"{cfg['url'].rstrip('/')}/rest/v1",
        schema=schema,
        headers=headers,
        http_client=http_client,
    )


def _aplicar_filtros(q, tabela: str):
    """
    Aplica os filtros server-side da tabela. A MESMA função é usada no count e
    na leitura das páginas — se as duas condições divergirem, os offsets
    desalinham e a leitura fica silenciosamente furada (linhas puladas).
    """
    for col in FILTROS_NAO_NULOS.get(tabela, ()):
        q = q.not_.is_(col, "null")
    return q


def _contar(client, tabela: str):
    """Contagem exata (já filtrada) — base do plano de páginas."""
    q = _aplicar_filtros(client.from_(tabela).select("id", count="exact"), tabela)
    return tabela, (q.limit(1).execute().count or 0)


def planejar_paginas(contagens: dict, tamanho: int = _PAGE_SIZE) -> list:
    """
    {tabela: n_linhas} → lista plana [(tabela, offset), ...] de páginas a buscar.

    PURA (sem rede) para ser testável. Tabela vazia rende 1 job: a contagem pode
    estar velha, e a leitura confirma. Contagem múltipla exata do tamanho NÃO
    gera job vazio extra (a cauda cuida do crescimento).
    """
    jobs = []
    for tabela, n in contagens.items():
        for inicio in range(0, max(int(n or 0), 1), tamanho):
            jobs.append((tabela, inicio))
    return jobs


def _ler_pagina(client, tabela: str, inicio: int) -> list:
    """
    Uma página de _PAGE_SIZE linhas, com retry próprio.

    `.order("id")` é OBRIGATÓRIO: sem ORDER BY explícito o PostgREST não garante
    ordenação estável entre requests, e páginas buscadas em paralelo podem
    duplicar ou pular linhas.

    O retry existe porque o fan-out multiplicou os requests (de ~9 para ~162):
    um 5xx transitório que antes era raro agora derrubaria a carga inteira.
    """
    erro = None
    for tentativa in range(_TENTATIVAS_PAGINA):
        try:
            q = _aplicar_filtros(client.from_(tabela).select("*").order("id"), tabela)
            return q.range(inicio, inicio + _PAGE_SIZE - 1).execute().data or []
        except Exception as e:
            erro = e
            if tentativa < _TENTATIVAS_PAGINA - 1:
                time.sleep(0.5 * (tentativa + 1))
    raise RuntimeError(
        f"Falha ao ler '{tabela}' (offset {inicio}) após "
        f"{_TENTATIVAS_PAGINA} tentativas: {erro}"
    )


def montar_dataframe(lotes: list) -> pd.DataFrame:
    """
    Lotes (em qualquer ordem — as_completed não preserva) → DataFrame único.

    PURA. Ordena por `id` e remove duplicatas: a ordem das linhas precisa ser
    determinística porque o tratamento de 'Produtos_detalhes' faz
    drop_duplicates(keep="last") — com ordem instável, qual linha sobrevive
    mudaria a cada carga.
    """
    linhas = [linha for lote in lotes for linha in lote]
    df = pd.DataFrame(linhas)
    if "id" in df.columns:
        df = (
            df.drop_duplicates(subset=["id"], keep="last")
            .sort_values("id", kind="stable")
            .reset_index(drop=True)
        )
    return df


def _drenar_cauda(client, tabela: str, lotes: list, ultimo_offset: int) -> None:
    """
    Se a última página planejada veio CHEIA, a tabela cresceu entre o count e a
    leitura (a pipeline externa escreve o tempo todo). Continua em série até uma
    página curta. Normalmente não roda nenhuma vez.
    """
    inicio = ultimo_offset + _PAGE_SIZE
    while True:
        lote = _ler_pagina(client, tabela, inicio)
        if not lote:
            return
        lotes.append(lote)
        if len(lote) < _PAGE_SIZE:
            return
        inicio += _PAGE_SIZE


def _renomear(aba: str, df: pd.DataFrame) -> pd.DataFrame:
    """Aplica COLUNAS_SUPABASE e normaliza strings vazias."""
    rename = COLUNAS_SUPABASE.get(aba)
    if rename:
        # Evita colisão: Supabase tem colunas surrogate (ex: 'id_situacao')
        # com o mesmo nome do alvo do rename de uma coluna *_bling.
        # Dropa o surrogate colidente antes de renomear.
        alvos = set(rename.values())
        fontes = set(rename.keys())
        colidem = [c for c in df.columns if c in alvos and c not in fontes]
        if colidem:
            df = df.drop(columns=colidem)
        df = df.rename(columns=rename)

    # Strings vazias → pd.NA p/ dropna() funcionar corretamente
    return df.replace("", pd.NA)


@st.cache_data(ttl=3600)
def _ler_supabase(_progresso=None) -> dict:
    """
    Lê todas as tabelas do Supabase e retorna {nome_aba_SCHEMA: DataFrame}.
    Cacheado por 1 hora.

    Estratégia: FILA PLANA DE PÁGINAS. Conta as 9 tabelas (paralelo, ~1 s),
    monta a lista completa de páginas de _PAGE_SIZE linhas e busca TODAS numa
    pool só. A versão anterior paralelizava entre TABELAS e paginava em série
    dentro de cada uma — o relógio de parede virava a corrente serial da maior
    tabela (`itens`, 166 idas e voltas), ~117 s. Com a fila plana: ~10 s.

    Aumentar a página não é opção: o Supabase corta em 1.000 linhas (max-rows).

    `_progresso`: callback opcional (feitas, total, etapa). O prefixo `_` é a
    convenção do Streamlit para NÃO entrar no hash da cache key — sem ele cada
    rerun traria um callback novo, chave nova, e o cache nunca acertaria.
    É chamado SÓ da thread principal: chamada de st.* de dentro de um worker
    não tem ScriptRunContext, não renderiza e ainda polui o log.
    """
    client = _conn_supabase()
    tabelas = [t for t in TABELAS_SUPABASE.values() if t]

    # 1) Contagem exata de cada tabela (já com o filtro server-side aplicado)
    if _progresso:
        _progresso(0, 1, "Contando registros")
    with ThreadPoolExecutor(max_workers=len(tabelas)) as pool:
        contagens = dict(pool.map(lambda t: _contar(client, t), tabelas))

    # 2) Plano de páginas e 3) busca paralela
    jobs = planejar_paginas(contagens)
    total = len(jobs)
    lotes = {t: [] for t in tabelas}
    feitas = 0

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futuros = {pool.submit(_ler_pagina, client, t, i): (t, i) for t, i in jobs}
        for fut in as_completed(futuros):
            tabela, _ = futuros[fut]
            lotes[tabela].append(fut.result())
            feitas += 1
            if _progresso:
                _progresso(feitas, total, _ABA_POR_TABELA.get(tabela, tabela))

    # 4) Cauda: tabela que cresceu entre o count e a leitura.
    # A última página planejada tem offset ((n-1)//PAGE)*PAGE e devolve
    # n - offset linhas — ou seja, ela só vem CHEIA quando n é múltiplo exato
    # de _PAGE_SIZE. Nesse caso pode haver linhas novas depois dela.
    if _progresso:
        _progresso(total, total, "Finalizando")
    for tabela, n in contagens.items():
        if n and n % _PAGE_SIZE == 0:
            _drenar_cauda(client, tabela, lotes[tabela], n - _PAGE_SIZE)

    # 5) Monta os DataFrames e aplica o rename
    todas_abas = {}
    for aba, tabela in TABELAS_SUPABASE.items():
        if not tabela:
            continue  # aba não mapeada — validação acusa aba ausente
        todas_abas[aba] = _renomear(aba, montar_dataframe(lotes[tabela]))

    # ----------------------------------------------------------------
    # Ajustes de schema (diferenças estruturais Supabase × SCHEMA)
    # ----------------------------------------------------------------
    ped = todas_abas.get("Pedidos")
    itn = todas_abas.get("Itens")

    # 'Total Produtos' não existe em pedidos no Supabase; só é tipada no
    # loader e não é usada adiante → espelha 'Total Venda'.
    if ped is not None and "Total Produtos" not in ped.columns and "Total Venda" in ped.columns:
        ped["Total Produtos"] = ped["Total Venda"]

    # 'itens' do Supabase não tem data do pedido — enriquecer com Pedidos.Data
    # (usado em planejamento/logística p/ filtro por período).
    # IMPORTANTE: postgrest devolve id_pedido_bling como float (NULL → NaN força
    # float64); astype(str) geraria '...0' e o merge falharia. Usa limpar_id
    # dos dois lados para normalizar a chave.
    if itn is not None and ped is not None and "Data" not in itn.columns:
        chave = ped[["ID", "Data"]].copy()
        chave["_k"] = chave["ID"].apply(limpar_id)
        itn = itn.copy()
        itn["_k"] = itn["ID_pedido"].apply(limpar_id)
        itn = itn.merge(chave[["_k", "Data"]], on="_k", how="left").drop(columns="_k")
        todas_abas["Itens"] = itn

    # Instante da leitura — alimenta o rodapé de frescor das páginas. Chave
    # com prefixo `_`: fica fora do laço de validação, que itera sobre SCHEMA.
    todas_abas["_carregado_em"] = pd.Timestamp.now()

    return todas_abas


def invalidar_cache_dados() -> None:
    """
    Derruba o cache de dados (leitura + transformação) sem tocar no resto.

    São DOIS caches encadeados: `carregar_dados` (tipagem/limpeza) consome
    `_ler_supabase` (rede). Limpar só o primeiro relê o cache do segundo e a
    "recarga" não recarregaria nada — por isso os dois, sempre juntos.
    Preferir esta função ao `st.cache_data.clear()` global, que levaria junto
    o cache de config e o da allowlist de acesso.
    """
    carregar_dados.clear()
    _ler_supabase.clear()


@st.cache_data(ttl=3600)
def carregar_dados(_progresso=None) -> dict:
    """
    Lê os dados do Bling do Supabase e retorna um dicionário de DataFrames.

    Retorna:
        {
            "pedidos":       DataFrame,
            "itens":         DataFrame,
            "produtos":      DataFrame,   ← apenas ativos (situacao == "A")
            "estoque":       DataFrame,
            "detalhes":      DataFrame,
            "vendedores":    DataFrame,
            "lojas":         DataFrame,
            "situacoes":     DataFrame,
            "depositos":     DataFrame,
            "carregado_em":  Timestamp,   ← instante da leitura (rodapé de frescor)
            "validacao": {"ok": bool, "erros": list, "avisos": list}
        }

    `_progresso`: callback opcional (feitas, total, etapa) repassado ao
    `_ler_supabase`. Prefixo `_` = fora do hash da cache key (convenção do
    Streamlit); em cache hit o corpo não roda e o callback nunca é chamado —
    correto, não há o que reportar quando é instantâneo.
    """
    erros = []
    avisos = []

    try:
        todas_abas = _ler_supabase(_progresso=_progresso)
    except Exception as e:
        st.error(f"❌ Erro ao conectar ao Supabase: {e}")
        return {"validacao": {"ok": False, "erros": [f"Erro ao ler Supabase: {e}"], "avisos": []}}

    # ----------------------------------------------------------------
    # Validação: presença de abas e colunas obrigatórias
    # ----------------------------------------------------------------
    for aba, colunas_requeridas in SCHEMA.items():
        if aba not in todas_abas:
            erros.append(f"Aba ausente: '{aba}'")
            continue

        df = todas_abas[aba]
        colunas_existentes = [str(c).strip() for c in df.columns]
        for col in colunas_requeridas:
            if col not in colunas_existentes:
                erros.append(f"Coluna '{col}' ausente na aba '{aba}'")

        if len(df) == 0:
            avisos.append(f"Aba '{aba}' está vazia (sem dados)")

    if erros:
        return {"validacao": {"ok": False, "erros": erros, "avisos": avisos}}

    # ----------------------------------------------------------------
    # Limpeza e tipagem de cada aba
    # ----------------------------------------------------------------
    dados = {}

    # --- Pedidos ---
    df = todas_abas["Pedidos"].copy()
    df = df.dropna(subset=["ID"])
    df["ID"] = df["ID"].apply(limpar_id)
    df["Loja ID"] = df["Loja ID"].apply(limpar_id)
    df["Vendedor"] = df["Vendedor"].apply(limpar_id)
    df["id_situacao"] = pd.to_numeric(df["id_situacao"], errors="coerce")
    # Converte datas em formato ISO (YYYY-MM-DD) ou BR (DD/MM/YYYY)
    df["Data"] = converter_serie_data(df["Data"])
    df["Total Venda"] = pd.to_numeric(
        df["Total Venda"].astype(str).str.replace(",", "."), errors="coerce"
    ).fillna(0)
    df["Total Produtos"] = pd.to_numeric(
        df["Total Produtos"].astype(str).str.replace(",", "."), errors="coerce"
    ).fillna(0)
    dados["pedidos"] = df

    # --- Itens ---
    df = todas_abas["Itens"].copy()
    df = df.dropna(subset=["ID_pedido"])
    df["ID_pedido"] = df["ID_pedido"].apply(limpar_id)
    df["ID_produto"] = df["ID_produto"].apply(limpar_id)
    df["Quantidade"] = pd.to_numeric(df["Quantidade"], errors="coerce").fillna(0)
    df["Valor Unidade"] = pd.to_numeric(
        df["Valor Unidade"].astype(str).str.replace(",", "."), errors="coerce"
    ).fillna(0)
    df["Desconto Item"] = pd.to_numeric(
        df["Desconto Item"].astype(str).str.replace(",", "."), errors="coerce"
    ).fillna(0)
    df["Data"] = converter_serie_data(df["Data"])
    dados["itens"] = df

    # --- Produtos ---
    df = todas_abas["Produtos"].copy()
    df = df.dropna(subset=["ID"])
    df["ID"] = df["ID"].apply(limpar_id)
    df["situacao"] = df["situacao"].astype(str).str.strip().str.upper()
    df["preco_custo"] = pd.to_numeric(
        df["preco_custo"].astype(str).str.replace("R$ ", "").str.replace(",", "."), errors="coerce"
    ).fillna(0)
    # Filtra apenas ativos (situacao = 'A'). Remove Inativos, Excluídos e sem situação.
    dados["produtos"] = df[df["situacao"] == "A"].copy()

    # --- Estoque ---
    df = todas_abas["EstoqueV3"].copy()
    df = df.dropna(subset=["ID_produto"])
    df["ID_deposito"] = df["ID_deposito"].apply(limpar_id)
    df["ID_produto"] = df["ID_produto"].apply(limpar_id)
    df["saldoFisico"] = pd.to_numeric(df["saldoFisico"], errors="coerce").fillna(0)
    dados["estoque"] = df

    # --- Produtos Detalhes ---
    df = todas_abas["Produtos_detalhes"].copy()
    df = df.dropna(subset=["ID_produto"])
    df["ID_produto"] = df["ID_produto"].apply(limpar_id)
    # Supabase pode ter múltiplas linhas de detalhe por produto; o restante do
    # código assume 1:1 (set_index/to_dict). Mantém o último registro.
    df = df.drop_duplicates(subset=["ID_produto"], keep="last")
    # Força todas as colunas de categorização para string (evita tipos misturados)
    for col in ["categoria", "Super_categoria", "Grupo", "Tamanho", "Marca_sku"]:
        if col in df.columns:
            df[col] = df[col].astype(str).replace("nan", "").str.strip()
    dados["detalhes"] = df

    # --- Vendedores ---
    df = todas_abas["Vendedores"].copy()
    df = df.dropna(subset=["ID"])
    df["ID"] = df["ID"].apply(limpar_id)
    dados["vendedores"] = df

    # --- Lojas ---
    df = todas_abas["Lojas"].copy()
    df = df.dropna(subset=["ID"])
    df["ID"] = df["ID"].apply(limpar_id)
    dados["lojas"] = df

    # --- Situações ---
    df = todas_abas["Situações"].copy()
    df = df.dropna(subset=["ID"])
    df["ID"] = pd.to_numeric(df["ID"], errors="coerce")
    dados["situacoes"] = df

    # --- Depósitos ---
    df = todas_abas["Depósitos"].copy()
    df = df.dropna(subset=["ID"])
    df["ID"] = df["ID"].apply(limpar_id)
    dados["depositos"] = df

    dados["carregado_em"] = todas_abas.get("_carregado_em")
    dados["validacao"] = {"ok": True, "erros": [], "avisos": avisos}
    return dados


def enriquecer_produtos(produtos: pd.DataFrame, detalhes: pd.DataFrame) -> pd.DataFrame:
    """
    Faz JOIN entre Produtos e Produtos_detalhes.
    Adiciona colunas: categoria, Super_categoria, Grupo, Tamanho, Marca_sku.

    Equivale ao carregarDetalhes() do Utils.gs.
    """
    colunas_detalhe = ["ID_produto", "categoria", "Super_categoria", "Grupo", "Tamanho", "Marca_sku"]
    det = detalhes[colunas_detalhe].copy()
    det = det.rename(columns={"ID_produto": "ID"})

    return produtos.merge(det, on="ID", how="left")
