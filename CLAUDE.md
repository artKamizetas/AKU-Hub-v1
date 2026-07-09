# Bling Dashboard — Contexto do Projeto

## O que é este projeto
Dashboard Streamlit que substitui o fluxo Google Apps Script + Looker Studio da AK Uniformes.
Lê dados do **Supabase** (Postgres espelhado da pipeline Bling→Supabase, mantida por outra equipe), processa via pandas e exibe análises de estoque, PCP e vendas.
Empresa: **AK Uniformes** (varejo, do Grupo AK) — lojas em Natal e Mossoró (RN). A fábrica própria do grupo é a **Art Kamizetas** (referida quando o assunto é produção/PCP).

> **Documentação humana detalhada em [`docs/`](docs/README.md)**: arquitetura, dados, regras de negócio, metodologia de PCP (order-up-to) e log de decisões. Este CLAUDE.md é o resumo orientado a agente; ao mudar metodologia/arquitetura, atualize também o doc correspondente e registre em `docs/decisoes.md`.

## Como rodar
```bash
# Ativar venv (Windows)
venv\Scripts\activate

# Rodar o dashboard
streamlit run app.py
```

**Origem dos dados:** Supabase via PostgREST (`postgrest`), credenciais em `st.secrets["supabase"]` (`url` + `service_key`).

## Arquitetura

```
app.py                      # Ponto de entrada — registra as páginas
config.yaml                 # Todas as configurações (metas, IDs, janelas de tempo, exceções SKU)
auth.py                     # Autenticação Streamlit (role-based access control)
data/                       # Saídas locais opcionais (ex: VM_Calculado.xlsx via scripts/exportar_vm.py), não sincronizado com Bling
etl/
  loader.py                 # Lê Supabase (via PostgREST) e valida → retorna dict de DataFrames
                            # Mapas TABELAS_SUPABASE / COLUNAS_SUPABASE convertem nomes para o SCHEMA
  daily.py                  # Lógica de Comercial / Metas diárias
  logistica.py              # Lógica de Reposição de Loja
  demanda.py                 # Motor único de demanda por SKU × Colégio × Mês (sazonalidade, crescimento, janela de cobertura) — usado por fabrica.py e planejamento.py
  fabrica.py                # Sugestão tática de produção por SKU (PCP)
  planejamento.py           # Simulação estratégica de rodadas de produção anuais (bottom-up, a partir de demanda.py)
  vm_dinamico.py            # Cálculo de VM (Visual Merchandising) dinâmico — reposição de loja
pages/
  0_Home.py                 # Tela inicial / status do sistema
  1_Daily.py                # Dashboard Comercial (metas, vendedores, lojas)
  2_Logistica.py            # Reposição de Loja (sugestões de transferência)
  3_Fabrica.py              # "Simulador de Produção" — PCP tático (Sugestão por SKU) + planejamento anual (Visão Geral/rodadas), ambos usando etl/demanda.py como base comum
  5_Configuracoes.py        # (Admin) Configuração de parâmetros, exceções SKU e sistema
scripts/                    # Utilitários de linha de comando (rodar da raiz: python scripts/<nome>.py)
  exportar_vm.py            # Exporta data/VM_Calculado.xlsx (VM + Pulmão de todos os SKUs)
  memoria_calculo.py        # Memória de cálculo passo a passo do VM Dinâmico p/ um SKU
  memoria_calculo_fabrica.py# Memória de cálculo passo a passo do PCP (order-up-to) p/ um SKU
```

## Fluxo de dados padrão
1. `loader.py::carregar_dados()` lê **Supabase** via PostgREST e retorna um `dict` de DataFrames com chaves/colunas no formato do `SCHEMA`
2. `TABELAS_SUPABASE` mapeia aba do SCHEMA → tabela real do Supabase; `COLUNAS_SUPABASE` renomeia colunas (IDs usados são os `*_bling`, não os surrogate `id` UUID)
3. Páginas importam funções dos módulos `etl/` e passam os DataFrames
4. Configurações são sempre lidas do `config.yaml` via `yaml.safe_load()` ou via `ruamel.yaml` (para preservar comentários)
5. **Nunca** leia tabelas Supabase diretamente nas páginas — sempre passe pelo `loader.py()`

## DataFrames disponíveis após `carregar_dados()`
- `dados["pedidos"]` — Pedidos (Loja ID, Data, Total Venda, id_situacao)
- `dados["itens"]` — Itens dos pedidos (ID_pedido, ID_produto, Quantidade)
- `dados["produtos"]` — Produtos ATIVOS (situacao == "A")
- `dados["estoque"]` — Saldo físico por depósito (ID_deposito, ID_produto, saldoFisico)
- `dados["detalhes"]` — Detalhes do produto (categoria, Super_categoria, Grupo, Tamanho)
- `dados["vendedores"]`, `dados["lojas"]`, `dados["situacoes"]`, `dados["depositos"]`

## IDs importantes (config.yaml)
- Lojas (usado em Pedidos): Natal = `203379922`, Mossoró = `203575032`
- Depósitos (usado em EstoqueV3): Natal = `7011018386`, Mossoró = `14887086441`, CD = `11105614627`
- Situações de venda efetiva: `[9]` (Atendido)
- Situações de backlog: `[6, 15]`

## Regras de negócio críticas
- **Loja ID ≠ Depósito ID**: loja aparece em Pedidos, depósito em EstoqueV3
- **SKU format**: código alfanumérico sem padrão fixo, mas normalmente `CATEGORIA-TAMANHO`
- **Produtos**: só trabalhar com `dados["produtos"]` (ativos)
- **IDs como string**: IDs do Bling são sempre tratados como string após `limpar_id()` — nunca compare como int
- **Datas**: coluna `Data` já convertida para datetime no loader

## Convenções de código
- Pandas para toda manipulação de dados
- `st.cache_data` nos carregamentos pesados (leitura do Supabase via `loader.py`, TTL=3600)
- **IDs entre tabelas Supabase**: sempre usar `limpar_id()` ANTES de comparar/joinar — postgrest devolve colunas int com NULL como `float64`, e `astype(str)` direto gera `'123.0'` que quebra merges com IDs vindos como int64 puros
- Configurações sempre de `config.yaml`, nunca hardcoded nas páginas
- **Autenticação:** `auth.py` com role-based access (admin, user) via `st.secrets["auth_config"]`
- **Configuração dinâmica:** página `5_Configuracoes.py` permite salvar alterações em `config.yaml` via UI (admin only)
- Nomes de variáveis e comentários em português (padrão do projeto)
- Cada módulo ETL recebe DataFrames e o dicionário `config` — não abre arquivos diretamente
- **YAML:** usar `ruamel.yaml` (não `yaml`) quando precisar salvar config.yaml preservando comentários

## Motor de Demanda + Abastecimento (etl/demanda.py)

Fonte única usada tanto pela aba tática ("Sugestão por SKU") quanto pela estratégica ("Visão Geral"/rodadas) do Simulador de Produção. Metodologia: **demanda ancorada na alta + política order-up-to (R,S) com projeção forward** (ref. Silver-Pyke-Peterson; newsvendor; aggregate planning). Fundamentação e decisões da diretoria no plano `~/.claude/plans/ethereal-whistling-prism.md`.

- **Demanda ancorada na ALTA** (`calcular_demanda_mensal_por_sku`): a alta define forma e magnitude, a baixa só adiciona volume — o dado esparso da baixa nunca entra no nível do SKU.
  - Meses de alta (`config["demanda"]["janela_alta"]`, ex: [12,1,2]): `vendas reais da última temporada de alta completa × crescimento` (a grade de tamanhos é preservada porque cada SKU é um tamanho).
  - Meses de baixa: `demanda de baixa` = demanda de alta × `proporção da baixa`, espalhada pela `distribuicao_mensal_baixa()` agregada. A proporção é **global** (`calcular_proporcao_baixa()` = Σbaixa/Σalta da empresa, últimos 2 ciclos ≈ 0,43) com **cascata de override manual** (`proporcao_baixa_efetiva(sku, colegio, config, base)`): `excecoes_sku[sku].proporcao_baixa → colegios[COL].proporcao_baixa → global`. Backtest (2023-25): fatiar por categoria/SKU não melhora (teto ~48%); global + override nos poucos gigantes de cauda curta é o que sustenta. Editável na tela (coluna no editor de Colégios + coluna no CSV de exceções).
- **Crescimento por (colégio × série)** (`taxa_crescimento_efetiva(colegio, config, grupo, ativo, observado)`): cascata híbrida (manual do planejador SEMPRE vence os dados): `crescimento_grupos[grupo] (manual) → taxa_crescimento colégio (manual) → observado colégio×segmento → observado colégio → 1+fabrica.crescimento_pct/100`. A **camada observada** (`calcular_crescimento_observado`) mede o crescimento realizado nas ALTAS (alta-sobre-alta, sinal limpo — a baixa tem ruptura), por colégio e por segmento, clamp [0.5,2.0], gate de volume ≥30. O mapa grupo→segmento (`mapa_grupo_segmento(config)`) tem default no código (`SEGMENTO_POR_GRUPO`) sobrescrito por `config["grupo_segmento"]` — editável na página de Configurações (baldes atuais: Infantil, Inf+Fund, Fundamental, Médio, Tempo Integral, Diário, Ed. Física, Esporte, Outros). Desligável em `config["demanda"]["crescimento_observado_ativo"]` (→ volta ao +10% cego). `ativo=False` desliga tudo (toggle p/ comparar). Vale p/ fábrica e VM logística. Não muda o total da rede (~+11%), **redistribui** para o mix certo (ex: NEV Médio +51% vs LMN −29%).
- **Política order-up-to** (`simular_politica_reabastecimento`): motor comum. Por SKU, caminha as rodadas mantendo estoque projetado (`estoque − backlog`, consumido mês a mês, reabastecido a cada chegada). Em cada rodada r: `DemandaPeriodo` = demanda até a próxima chegar; `EstoqueSeguranca = estoque_seguranca(DemandaPeriodo, contém_alta, config)` (Fator de Serviço × Variação da Demanda × DemandaPeriodo); `EstoqueAlvo = DemandaPeriodo + EstoqueSeguranca`; `Pedido = par_ceil(EstoqueAlvo − EstoqueProjetado_na_chegada)`. As colunas do DataFrame retornado usam esses nomes (`DemandaPeriodo`/`EstoqueSeguranca`/`EstoqueAlvo`/`EstoqueProjetado`; antes eram `DI`/`SS`/`S`/`OH`). Sugestão por SKU = Pedido da rodada selecionada; Visão Geral = soma por rodada.
- **Nível de serviço** (`config["demanda"]["nivel_servico_alta"/"nivel_servico_baixa"/"variacao_demanda"]`): alta ~99% ("não pode faltar"), baixa ~92%. Fator de Serviço pela criticidade do intervalo.
- `planejamento.periodo_historico_inicio`/`fim` = período histórico único (sazonalidade agregada + distribuição mensal da baixa + base dos SKUs só-de-baixa). Define o FORMATO do ano, não o tamanho do pico. Calendário de rodadas: `planejamento.rodadas_datas` (datas ISO explícitas, este ano + próximo, SEM repetição anual — a última data só fecha o intervalo da penúltima; recomendado) com fallback em `planejamento.rodadas` (meses fixos que repetem todo ano). Override p/ cenários aceita meses e/ou datas em `rodadas_meses`. A simulação expõe `DemandaPeriodoAlta`/`DemandaPeriodoBaixa`/`MesesIntervalo`/`data_chegada_seguinte` (split pico/baixa usado pela UI da Sugestão por SKU).

## Página de Configuração (5_Configuracoes.py)

**Admin only** — acesso controlado por role em `st.secrets["auth_config"]`.

### Aba 1: Parâmetros Gerais
Formulário organizado pelos **3 subsistemas** da metodologia atual:
- **Comercial (Daily):** metas (Natal, Mossoró) + status IDs de pedido (em_aberto, em_andamento, pronto_retirada)
- **Reposição de Loja:** VM Dinâmico (`config.yaml["vm"]` — cobertura, alta temporada, multiplicador PA, VM mínimo, lead time, nível de serviço, toggle de crescimento) + *fallback* fixo (`logistica.vm_padrao`, `logistica.dias_analise_giro`) usado só quando o SKU não tem giro
- **Produção (Simulador):** Demanda/order-up-to (`config.yaml["demanda"]` — níveis de serviço alta/baixa, variação, janela da alta, toggle de crescimento), Planejamento (rodadas, lead time, período histórico único) e *fallback* da Fábrica (crescimento, cobertura, correção manual global)

Logo abaixo do formulário, editor independente de **Colégios** (`config.yaml["colegios"]`): tabela com taxa de crescimento e nível de serviço por colégio, descobertos dinamicamente a partir de `detalhes["Marca_sku"]` — valores são sempre input manual do usuário, nunca calculados a partir das vendas.

Ao salvar:
1. Valida estrutura mínima e valores numéricos
2. Salva em `config.yaml` usando `ruamel.yaml` (preserva comentários)
3. Limpa cache do Streamlit (`st.cache_data.clear()`)

### Aba 2: Exceções de SKU
CSV template para sobrescrever parâmetros globais por SKU:
- Columns: `sku`, `vm_override`, `correcao_manual` (as antigas `dias_analise`/`sazonalidade` foram removidas — nenhum motor as lia)
- Download: template atual (ou exemplo padrão se nenhuma exceção existe)
- Upload: aplicar novas exceções via CSV
- Salva em `config.yaml["excecoes_sku"]`
- O campo `correcao_manual` (salvo como chave `correcao`) também é lido por `vm_dinamico.calcular_vm_por_sku()` como fator de correção do VM dinâmico

### Aba 3: Sistema
Informações do sistema:
- Versões (Python, Streamlit, Pandas)
- Fonte de dados: Supabase (Bling ERP via pipeline externa)
- Data de última modificação do `config.yaml`
- Botão: Forçar recarga de cache
- Botão: Backup do `config.yaml` (download)

---

## Não faça sem perguntar
- Alterar `config.yaml` manualmente (use a página de Configurações, ou pergunte ao usuário)
- Alterar a estrutura de retorno de `loader.py::carregar_dados()` (quebra todas as páginas)
- Renomear colunas dos DataFrames
- Adicionar dependências ao `requirements.txt`

---

## Dependências (requirements.txt)
```
streamlit
pandas
openpyxl
pyyaml
ruamel.yaml
plotly
postgrest
streamlit-authenticator
```

`openpyxl` continua porque `scripts/exportar_vm.py` exporta `data/VM_Calculado.xlsx` (não é mais usado para ler parâmetros de entrada — o VM Dinâmico lê tudo de `config.yaml`).

---

## Secrets esperados (streamlit/secrets.toml)
```toml
# Supabase (Project Settings → API)
[supabase]
url = "https://<PROJECT_REF>.supabase.co"
service_key = "<SERVICE_ROLE_KEY>"
schema = "public"

# Autenticação
[auth_config]
cookie_name = "bling_dashboard_auth"
cookie_key = "<COOKIE_SECRET>"
cookie_expiry_days = 7
[auth_config.credentials.usernames.admin]
name = "Admin"
password = "<BCRYPT_HASH>"
role = "admin"
```
