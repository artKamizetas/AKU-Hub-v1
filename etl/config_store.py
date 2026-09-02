"""
config_store.py — Persistência dos parâmetros operacionais no Supabase.

Porta única de leitura/escrita de app.parametros + app.parametros_historico
(DDL: docs/sql/002_app_parametros.sql). Conserta o bug de retenção do
Streamlit Cloud: o que a página de Configurações edita vive no banco; o
config.yaml do git é só a fonte dos DEFAULTS. Decisões congeladas em
docs/migracao-supabase.md.

Arquitetura híbrida (merge feito por loader.carregar_config):
    config = deep_merge(config.yaml, app.parametros.dados)

- Categoria A (estrutural — IDs de depósito, situações de venda/backlog,
  thresholds sem UI) fica SÓ no yaml: nunca é extraída nem salva no banco.
- Categoria B (operacional do gestor) é o subconjunto CHAVES_PARAMETROS:
  é o que extrair_parametros() recorta e salvar() grava.
- Subárvores "de coleção" (CAMINHOS_SUBSTITUICAO) são substituídas por
  inteiro no merge — sem isso, um colégio/exceção APAGADO na UI
  ressuscitaria do default do yaml.
"""

import json

import pandas as pd
import streamlit as st


TAB_PARAMETROS = "parametros"
TAB_HISTORICO = "parametros_historico"
ID_LINHA = "default"          # app.parametros tem 1 linha; o app só faz UPDATE

# Categoria B — o que vive no Supabase. bloco → None (bloco inteiro) ou
# lista de chaves (subconjunto; o resto do bloco é Categoria A / yaml).
CHAVES_PARAMETROS = {
    "logistica": None,
    "vm": None,
    "colegios": None,
    "colegios_alias": None,
    "daily": ["metas", "metas_mensais", "vendedores_loja", "status_ids"],  # situacoes_venda é estrutural
    "fabrica": ["crescimento_pct", "cobertura_meses", "correcao_manual"],
    "demanda": ["janela_alta", "nivel_servico_alta", "nivel_servico_baixa",
                "variacao_demanda", "aplicar_crescimento_fabrica",
                "crescimento_observado_ativo"],       # min_* são estruturais
    "grupo_segmento": None,
    "planejamento": None,
    "excecoes_sku": None,
}

# Caminhos (pontuados) onde o valor do Supabase SUBSTITUI o bloco inteiro
# em vez de mesclar chave a chave — coleções que o gestor possui por completo.
CAMINHOS_SUBSTITUICAO = {
    "colegios", "colegios_alias", "grupo_segmento", "excecoes_sku",
    "planejamento.cobertura_override",
    "daily.metas_mensais", "daily.vendedores_loja",
}


def _normalizar_json(valor):
    """Converte tipos ruamel/numpy/Timestamp para tipos JSON puros."""
    return json.loads(json.dumps(valor, default=str))


def extrair_parametros(config: dict) -> dict:
    """
    Recorta do dict de config completo só a Categoria B (CHAVES_PARAMETROS)
    — o payload de app.parametros.dados. Usado pelo seed e pelo save.
    """
    dados = {}
    for bloco, chaves in CHAVES_PARAMETROS.items():
        if bloco not in config or config[bloco] is None:
            continue
        if chaves is None:
            dados[bloco] = config[bloco]
        else:
            sub = {k: config[bloco][k] for k in chaves if k in config[bloco]}
            if sub:
                dados[bloco] = sub
    return _normalizar_json(dados)


def deep_merge(base: dict, override: dict, _caminho: str = "") -> dict:
    """
    Mescla `override` (Supabase) sobre `base` (config.yaml), recursiva em
    dicts — EXCETO nos CAMINHOS_SUBSTITUICAO, onde o override substitui o
    bloco inteiro. Escalares e listas sempre substituem. Não muta os args.
    """
    resultado = dict(base or {})
    for chave, valor in (override or {}).items():
        caminho = f"{_caminho}.{chave}" if _caminho else chave
        if (caminho not in CAMINHOS_SUBSTITUICAO
                and isinstance(resultado.get(chave), dict) and isinstance(valor, dict)):
            resultado[chave] = deep_merge(resultado[chave], valor, caminho)
        else:
            resultado[chave] = valor
    return resultado


def obter_repositorio_parametros() -> "RepositorioParametros":
    """Fábrica usada pelas páginas (client PostgREST do schema `app`)."""
    from pedidos.repositorio import _conn_app
    return RepositorioParametros(_conn_app())


class RepositorioParametros:
    """Acesso a app.parametros. Client injetado — testável com fake
    (mesmo padrão de gateway do pedidos.RepositorioPedidos)."""

    def __init__(self, client):
        self._client = client

    # ------------------------------------------------------------------
    # Gateway interno (o que os testes falsificam)
    # ------------------------------------------------------------------
    def _selecionar(self, tabela: str, filtros: dict, colunas: str = "*") -> list:
        q = self._client.from_(tabela).select(colunas)
        for col, val in filtros.items():
            q = q.eq(col, val)
        return q.execute().data or []

    def _atualizar(self, tabela: str, filtros: dict, valores: dict) -> list:
        q = self._client.from_(tabela).update(valores)
        for col, val in filtros.items():
            q = q.eq(col, val)
        return q.execute().data or []

    def _inserir(self, tabela: str, linha: dict) -> list:
        return self._client.from_(tabela).insert(linha).execute().data or []

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    def ler(self) -> dict:
        """dados (Categoria B) da linha default; {} se vazia/inexistente."""
        linhas = self._selecionar(TAB_PARAMETROS, {"id": ID_LINHA}, "dados")
        return (linhas[0].get("dados") or {}) if linhas else {}

    def ler_metadados(self) -> dict:
        """{atualizado_em, atualizado_por} da linha default; {} se ausente."""
        linhas = self._selecionar(
            TAB_PARAMETROS, {"id": ID_LINHA}, "atualizado_em,atualizado_por")
        return linhas[0] if linhas else {}

    def salvar(self, dados: dict, usuario: str) -> None:
        """
        UPDATE app.parametros (estado vivo) + INSERT app.parametros_historico
        (auditoria). O histórico guarda o estado completo salvo, não o diff.
        """
        dados = _normalizar_json(dados)
        agora = pd.Timestamp.now(tz="UTC").isoformat()
        self._atualizar(TAB_PARAMETROS, {"id": ID_LINHA},
                        {"dados": dados, "atualizado_em": agora,
                         "atualizado_por": usuario})
        self._inserir(TAB_HISTORICO, {"dados": dados, "criado_por": usuario})
