# Arquitetura

## Visão geral

Dashboard **Streamlit** (Python/pandas) que substituiu o fluxo antigo de Google
Apps Script + Looker Studio. Lê dados do **Supabase** (Postgres espelhado da
pipeline Bling→Supabase, mantida por outra equipe), processa em memória com
pandas e apresenta análises de estoque, PCP e vendas.

Não há banco próprio nem escrita de dados de negócio: o app é **read-only** sobre
o Supabase. A única escrita é em `config.yaml` (parâmetros), feita pela página de
Configurações.

## Camadas

```
┌─────────────────────────────────────────────────────────────┐
│  APRESENTAÇÃO — pages/*.py (Streamlit)                        │
│  Home · Daily · Logística · Simulador de Produção · Config    │
└───────────────▲──────────────────────────▲──────────────────┘
                │ DataFrames + config        │ lê/escreve
┌───────────────┴──────────────┐   ┌─────────┴──────────────────┐
│  LÓGICA — etl/*.py            │   │  CONFIG — config.yaml       │
│  demanda, fabrica,            │   │  metas, IDs, parâmetros PCP │
│  planejamento, vm_dinamico,   │   │  (editável via UI, admin)   │
│  logistica, daily             │   └─────────────────────────────┘
└───────────────▲──────────────┘
                │ dict de DataFrames
┌───────────────┴──────────────────────────────────────────────┐
│  DADOS — etl/loader.py                                         │
│  Lê Supabase (PostgREST), valida, tipa → dict de DataFrames    │
└───────────────▲──────────────────────────────────────────────┘
                │ HTTP (PostgREST)
        ┌───────┴────────┐
        │   SUPABASE      │  (Postgres, espelho do Bling ERP)
        └─────────────────┘
```

**Princípios:**
- Módulos `etl/` recebem DataFrames + `config` e retornam DataFrames/dicts. **Não
  abrem arquivos nem acessam o Supabase diretamente** (só o `loader.py` acessa).
- Páginas nunca leem o Supabase direto — sempre via `loader.carregar_dados()`.
- Configuração sempre de `config.yaml`, nunca hardcoded nas páginas.

## Fluxo de dados ponta a ponta

1. `loader.carregar_dados()` lê o Supabase via PostgREST, valida schema e tipa →
   `dict` de DataFrames (`pedidos`, `itens`, `produtos`, `estoque`, `detalhes`, …).
   Cacheado por 1h (`@st.cache_data(ttl=3600)`).
2. A página importa funções dos módulos `etl/` e passa os DataFrames + `config`.
3. O módulo calcula (ex: `demanda.simular_politica_reabastecimento`, `fabrica.processar_fabrica`).
4. A página renderiza tabelas/gráficos (Plotly).

## Processos de negócio suportados (por página)

| Página | Processo | Módulos |
|---|---|---|
| **Daily** | Comercial / acompanhamento de metas | `etl/daily.py` |
| **Logística** | Reposição de loja (CD → lojas) | `etl/logistica.py`, `etl/vm_dinamico.py` |
| **Simulador de Produção** | PCP — planejamento anual de rodadas + emissão de pedido de fábrica por SKU | `etl/demanda.py`, `etl/fabrica.py`, `etl/planejamento.py` |
| **Configurações** | Edição de parâmetros (admin) | `pages/5_Configuracoes.py` |

O **Simulador de Produção** é o processo mais complexo — ver
[metodologia-pcp.md](metodologia-pcp.md).

## Autenticação e perfis

`auth.py` (streamlit-authenticator), credenciais em `st.secrets["auth_config"]`.
Perfis (roles) controlam quais páginas cada usuário vê (`app.py`):

| Role | Acesso |
|---|---|
| `admin` | Tudo (inclui Configurações) |
| `supervisor` | Daily, Logística |
| `vendedor` | Home, Daily |
| `estoque` | Logística |

`app.py` chama `verificar_acesso()` uma vez por execução (reautentica pelo cookie,
inclusive na sessão nova pós-redirect OAuth). As páginas usam o guard leve
`exigir_login()`; a **`5_Configuracoes.py` usa `identidade_atual()`** (lê
`name`/`username`/`role` do `session_state`, sem instanciar um 2º
`Authenticate`/`CookieManager`) — chamar `verificar_acesso()` de novo na página
criava dois `CookieManager` com a mesma `key="init"` e estourava
`StreamlitDuplicateElementKey` no retorno do OAuth.

## Como rodar

```bash
# venv (Windows)
venv\Scripts\activate
streamlit run app.py
```

Requer `.streamlit/secrets.toml` com as credenciais do Supabase e da autenticação
(ver `.streamlit/secrets.toml.example`).

## Dependências principais

`streamlit`, `pandas`, `plotly`, `postgrest`, `pyyaml`
(preserva comentários ao salvar `config.yaml`), `streamlit-authenticator`,
`openpyxl` (só para exportar `data/VM_Calculado.xlsx`).
