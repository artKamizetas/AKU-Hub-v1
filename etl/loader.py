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

from concurrent.futures import ThreadPoolExecutor

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

_PAGE_SIZE = 1000  # limite default do PostgREST por request


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
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    )
    return SyncPostgrestClient(
        f"{cfg['url'].rstrip('/')}/rest/v1",
        schema=schema,
        headers=headers,
        http_client=http_client,
    )


def _ler_tabela(client, tabela: str) -> pd.DataFrame:
    """Lê tabela inteira paginando em blocos de _PAGE_SIZE (limite PostgREST)."""
    linhas = []
    inicio = 0
    while True:
        resp = (
            client.from_(tabela)
            .select("*")
            .range(inicio, inicio + _PAGE_SIZE - 1)
            .execute()
        )
        lote = resp.data or []
        linhas.extend(lote)
        if len(lote) < _PAGE_SIZE:
            break
        inicio += _PAGE_SIZE
    return pd.DataFrame(linhas)


def _ler_e_renomear(client, aba: str, tabela: str):
    """Lê uma tabela e aplica rename — unidade paralelizável por _ler_supabase."""
    if not tabela:
        return aba, None  # aba não mapeada — validação acusa aba ausente
    df = _ler_tabela(client, tabela)

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
    return aba, df.replace("", pd.NA)


@st.cache_data(ttl=3600)
def _ler_supabase() -> dict:
    """
    Lê cada tabela do Supabase (via supabase-py) e retorna
    {nome_aba_SCHEMA: DataFrame}. Cacheado por 1 hora. Tabelas via
    TABELAS_SUPABASE; colunas renomeadas via COLUNAS_SUPABASE quando necessário.

    9 tabelas lidas em paralelo (ThreadPoolExecutor). SyncPostgrestClient usa
    httpx.Client (thread-safe); cliente cacheado por sessão em _conn_supabase.
    """
    client = _conn_supabase()

    todas_abas = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futuros = [
            pool.submit(_ler_e_renomear, client, aba, tabela)
            for aba, tabela in TABELAS_SUPABASE.items()
        ]
        for f in futuros:
            aba, df = f.result()
            if df is not None:
                todas_abas[aba] = df

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

    return todas_abas


@st.cache_data(ttl=3600)
def carregar_dados() -> dict:
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
            "validacao": {"ok": bool, "erros": list, "avisos": list}
        }
    """
    erros = []
    avisos = []

    try:
        todas_abas = _ler_supabase()
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
    df["Data"] = df["Data"].apply(converter_data_flexivel)
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
    df["Data"] = df["Data"].apply(converter_data_flexivel)
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
