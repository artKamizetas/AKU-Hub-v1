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

Login **Google** pelo OIDC nativo do Streamlit (`st.login()`/`st.user`), configurado
em `st.secrets["auth"]` + `["auth.google"]`. Quem pode entrar e com qual perfil vive
na tabela `app.usuario` (DDL 006), editável na aba **Usuários** de Configurações —
não no secrets. **Não há auto-cadastro**: e-mail fora da allowlist é barrado e nada
é gravado.

| Role | Acesso |
|---|---|
| `admin` | Tudo (inclui Configurações) |
| `supervisor` | Daily, Logística |
| `vendedor` | Home, Daily |
| `estoque` | Logística |

`app.py` chama `auth.verificar_acesso()` uma vez por execução, antes da navegação;
o mapa role→páginas (`PAGINAS_POR_ROLE`) e a resolução vivem no `auth.py`. As
páginas usam `exigir_login()` (defesa em profundidade), `identidade_atual()`,
`e_admin()` (gate não bloqueante) ou `exigir_admin()` (gate de página inteira) —
nenhuma relê role de secrets ou `session_state`.

A allowlist é lida inteira e cacheada por 5 min (`@st.cache_data`), global entre
sessões; `auth.invalidar_cache_usuarios()` ao salvar faz a revogação valer no rerun
seguinte. Se a allowlist não puder ser lida, o login falha **fechado** — exceto os
e-mails de `st.secrets["acesso"]["admins"]` (break-glass contra lockout).

## Como rodar

```bash
# venv (Windows)
venv\Scripts\activate
streamlit run app.py
```

Requer `.streamlit/secrets.toml` com as credenciais do Supabase e do login Google
(ver `.streamlit/secrets.toml.example`).

## Dependências principais

`streamlit`, `pandas`, `plotly`, `postgrest`, `pyyaml`
(preserva comentários ao salvar `config.yaml`), `Authlib` (exigido pelo `st.login()`),
`openpyxl` (só para exportar `data/VM_Calculado.xlsx`).
