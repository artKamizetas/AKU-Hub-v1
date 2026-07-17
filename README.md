# AKU Dashboard — AK Uniformes

Dashboard interativo de estoque, PCP e vendas da AK Uniformes.
Lê os dados do **Supabase** (Postgres espelhado da pipeline Bling → Supabase, mantida por outra equipe), processa via pandas e roda no Streamlit — local ou no Streamlit Community Cloud.

> Documentação técnica detalhada em [`docs/`](docs/README.md) (arquitetura, dados, metodologia de PCP e log de decisões) e resumo para agentes em [`CLAUDE.md`](CLAUDE.md).

---

## Sumário

1. [Rodar localmente](#rodar-localmente)
2. [Configuração dos Secrets](#configuração-dos-secrets)
3. [Configuração do Sistema (UI)](#configuração-do-sistema-ui)
   - [Aba 1 — Parâmetros Gerais](#aba-1--parâmetros-gerais)
   - [Aba 2 — Exceções de SKU](#aba-2--exceções-de-sku)
   - [Aba 3 — Sistema](#aba-3--sistema)
4. [Deploy no Streamlit Cloud](#deploy-no-streamlit-community-cloud)
5. [Estrutura do projeto](#estrutura-do-projeto)
6. [Atualização dos dados](#atualização-dos-dados)
7. [Utilitários de linha de comando](#utilitários-de-linha-de-comando)
8. [Dúvidas comuns](#dúvidas-comuns)

---

## Rodar localmente

**Pré-requisito:** Python 3.10+

```bash
# 1. Criar e ativar o ambiente virtual (Windows)
python -m venv venv
venv\Scripts\activate

# 2. Instalar dependências (só precisa fazer uma vez)
pip install -r requirements.txt

# 3. Configurar os secrets (ver seção abaixo)
#    Copie .streamlit/secrets.toml.example → .streamlit/secrets.toml e preencha

# 4. Rodar
streamlit run app.py
```

No Windows, `run.bat` faz os passos 1 (ativação) e 4 automaticamente.

Os dados vêm do Supabase — não há mais leitura de Excel local nem de Google Sheets. Basta que os secrets do Supabase estejam configurados.

### Testes

A suíte cobre o motor de demanda / PCP (`etl/demanda.py`) e os utilitários do loader. Não depende do Supabase nem de secrets.

```bash
# Instalar dependências de desenvolvimento (inclui pytest)
uv pip install -r requirements-dev.txt   # ou: pip install -r requirements-dev.txt

# Rodar a suíte (a partir da raiz)
pytest
```

---

## Configuração dos Secrets

As credenciais ficam em `.streamlit/secrets.toml` (local) ou em **App Settings → Secrets** (Streamlit Cloud). O arquivo **nunca** é commitado — já está no `.gitignore`. Use [`.streamlit/secrets.toml.example`](.streamlit/secrets.toml.example) como modelo.

```toml
# Supabase (Project Settings → API). O service_key ignora RLS — uso server-side.
[supabase]
url = "https://SEU_PROJECT_REF.supabase.co"
service_key = "SEU_SERVICE_ROLE_KEY"
schema = "public"           # opcional (default: public)

# Autenticação do dashboard
[auth_config]
cookie_name = "bling_dashboard_auth"
cookie_key = "SEU_COOKIE_SECRET"
cookie_expiry_days = 7

[auth_config.credentials.usernames.admin]
name = "Admin"
password = "BCRYPT_HASH_AQUI"   # hash bcrypt, não a senha em texto puro
role = "admin"
```

**Perfis de acesso** (`role`) controlam quais páginas cada usuário vê: `admin` (tudo), `supervisor` (Daily, Logística), `vendedor` (Home, Daily), `estoque` (Logística).

---

## Configuração do Sistema (UI)

A partir da versão 2.0, os parâmetros operacionais podem ser alterados **pela própria interface**, sem editar `config.yaml` na mão. Acesse **⚙️ Configurações** (disponível apenas para `role="admin"`). Ao salvar, o `config.yaml` é atualizado preservando comentários (`ruamel.yaml`) e o cache do Streamlit é limpo.

### Aba 1 — Parâmetros Gerais

Formulário organizado pelos três subsistemas da metodologia:
- **Comercial (Daily):** metas (Natal, Mossoró) + IDs de status de pedido
- **Reposição de Loja:** VM Dinâmico (cobertura, alta temporada, VM mínimo, lead time, nível de serviço) + fallback fixo
- **Produção (Simulador):** Demanda / order-up-to (níveis de serviço alta/baixa, variação, janela da alta), Planejamento (rodadas, lead time, período histórico) e fallback da Fábrica

Abaixo do formulário, um editor de **Colégios** permite ajustar taxa de crescimento e nível de serviço por colégio (valores sempre manuais, descobertos dinamicamente dos dados).

### Aba 2 — Exceções de SKU

CSV para sobrescrever parâmetros globais por SKU específico. Colunas:
- `sku` (obrigatório): código do produto
- `vm_override`: unidades de exposição (opcional)
- `correcao_manual`: fator de correção do VM/pedido (opcional)

Baixe o template atual, edite e faça o upload para aplicar. Salvo em `config.yaml["excecoes_sku"]`.

### Aba 3 — Sistema

- Versões (Python, Streamlit, Pandas) e fonte de dados (Supabase)
- Data da última modificação do `config.yaml`
- Botão de forçar recarga de cache
- Download de backup do `config.yaml`

> **Nota:** As alterações são feitas em `config.yaml`, que é versionado no git. Para produção, revise as mudanças antes de fazer `git push`.

---

## Deploy no Streamlit Community Cloud

Deploy gratuito no [share.streamlit.io](https://share.streamlit.io) — o Supabase está no free tier.

1. Suba o código para um repositório no GitHub (pode ser privado):
   ```bash
   git push -u origin main
   ```
2. Em [share.streamlit.io](https://share.streamlit.io), clique em **New app** e preencha:
   - **Repository:** `SEU_USUARIO/AKU-Hub-v1`
   - **Branch:** `main`
   - **Main file path:** `app.py`
3. Em **Advanced settings → Secrets**, cole o conteúdo do seu `secrets.toml` (bloco `[supabase]` + `[auth_config]` da seção acima).
4. Clique em **Deploy!** — o Streamlit instala o `requirements.txt` e sobe o app em alguns minutos.

Para atualizar o app após mudanças no código, basta `git push` — o redeploy é automático. Para restringir acesso, use **Share → Invite viewers** no painel do Streamlit Cloud (além da autenticação própria do dashboard).

---

## Estrutura do projeto

```
AKU-Hub-v1/
├── app.py                         # Ponto de entrada do Streamlit
├── auth.py                        # Autenticação e controle de acesso por perfil
├── config.yaml                    # Metas, IDs e configurações operacionais
├── requirements.txt               # Dependências Python (produção)
├── requirements-dev.txt           # Dependências de desenvolvimento (pytest)
├── pytest.ini                     # Configuração da suíte de testes
├── run.bat                        # Atalho Windows: ativa venv e roda o app
├── .gitignore                     # Arquivos excluídos do repositório
├── README.md                      # Este arquivo
├── CLAUDE.md                      # Resumo do projeto para agentes
├── docs/                          # Documentação humana (arquitetura, dados, PCP, decisões)
├── .streamlit/
│   ├── config.toml                # Tema / configurações do Streamlit
│   ├── secrets.toml               # Credenciais reais (NÃO commitar — está no .gitignore)
│   └── secrets.toml.example       # Modelo de secrets (commitar este)
├── etl/
│   ├── loader.py                  # Leitura dos dados do Supabase (via PostgREST)
│   ├── daily.py                   # Lógica de metas comerciais
│   ├── logistica.py               # Lógica de reposição de loja
│   ├── demanda.py                 # Motor único de demanda por SKU (base de fábrica + planejamento)
│   ├── fabrica.py                 # Lógica de PCP (sugestão tática por SKU)
│   ├── planejamento.py            # Planejamento anual de rodadas
│   └── vm_dinamico.py             # Cálculo de Visual Merchandising
├── pages/
│   ├── 0_Home.py                  # Visão geral / status
│   ├── 1_Daily.py                 # Dashboard comercial
│   ├── 2_Logistica.py             # Reposição de loja
│   ├── 3_Fabrica.py               # PCP / Simulador de produção
│   └── 5_Configuracoes.py         # ⭐ UI de configuração (admin only)
├── scripts/                       # Utilitários CLI (rodar da raiz: python scripts/<nome>.py)
│   ├── exportar_vm.py             # Exporta data/VM_Calculado.xlsx
│   ├── memoria_calculo.py         # Memória de cálculo do VM Dinâmico (por SKU)
│   └── memoria_calculo_fabrica.py # Memória de cálculo do PCP (por SKU)
├── tests/                         # Suíte pytest (motor de demanda + utilitários)
│   ├── conftest.py                # Fixtures (config + dados sintéticos)
│   ├── test_demanda_helpers.py    # Funções puras do motor
│   ├── test_demanda_engine.py     # Demanda por SKU + política order-up-to
│   ├── test_rodadas.py            # Calendário de rodadas de produção
│   └── test_loader_utils.py       # limpar_id / converter_data_flexivel
├── assets/                        # Estáticos (favicon)
└── data/                          # Saídas locais (ex: VM_Calculado.xlsx — não versionado)
```

---

## Atualização dos dados

Os dados do Bling são espelhados no Supabase por uma **pipeline externa** (mantida por outra equipe). O dashboard apenas **lê** o Supabase via PostgREST — não escreve nem faz upload de arquivos.

O `loader.py` usa `st.cache_data` com **TTL de 1 hora** (3600 s). Para forçar a releitura antes disso, use **⚙️ Configurações → Sistema → Forçar recarga de cache** (ou o botão de recarregar na página inicial).

---

## Utilitários de linha de comando

Scripts de auditoria/exportação em `scripts/`, executados a partir da raiz do projeto (precisam dos secrets do Supabase configurados):

```bash
python scripts/exportar_vm.py                          # gera data/VM_Calculado.xlsx (VM + Pulmão de todos os SKUs)
python scripts/memoria_calculo.py <SKU>                # passo a passo do VM Dinâmico de um SKU
python scripts/memoria_calculo_fabrica.py <SKU>        # passo a passo do PCP (order-up-to) de um SKU
```

---

## Dúvidas comuns

**"Aba ausente" ou erro de validação no carregamento**
→ A pipeline Bling → Supabase pode não ter populado alguma tabela. Verifique com a equipe responsável pela pipeline; o `loader.py` valida o schema esperado antes de exibir os dados.

**"Erro ao conectar ao Supabase"**
→ Verifique se `[supabase].url` e `[supabase].service_key` nos secrets estão corretos e se o projeto Supabase está ativo.

**Acesso negado / página não aparece**
→ Confira o `role` do usuário em `[auth_config]`. Cada perfil vê um subconjunto das páginas (ver [Configuração dos Secrets](#configuração-dos-secrets)).

**"No module named ..."**
→ O ambiente virtual não está ativado ou as dependências não foram instaladas. Rode `venv\Scripts\activate` e `pip install -r requirements.txt`.
