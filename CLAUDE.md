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
config.yaml                 # DEFAULTS estruturais (Categoria A: IDs, situações, thresholds) — os parâmetros do gestor vivem no Supabase (app.parametros)
auth.py                     # Autenticação Streamlit (role-based access control)
data/                       # Saídas locais opcionais (ex: VM_Calculado.xlsx via scripts/exportar_vm.py), não sincronizado com Bling
etl/
  loader.py                 # Lê Supabase (via PostgREST) e valida → retorna dict de DataFrames
                            # Mapas TABELAS_SUPABASE / COLUNAS_SUPABASE convertem nomes para o SCHEMA
                            # carregar_config() = ponto ÚNICO de leitura de config (yaml ← app.parametros)
  config_store.py           # Persistência dos parâmetros no Supabase (app.parametros + historico); deep_merge/extrair_parametros
  daily.py                  # Lógica de Comercial / Metas diárias
  logistica.py              # Lógica de Reposição de Loja
  demanda.py                 # Motor único de demanda por SKU × Colégio × Mês (sazonalidade, crescimento, janela de cobertura) — usado por fabrica.py e planejamento.py
  fabrica.py                # Sugestão tática de produção por SKU (PCP)
  planejamento.py           # Simulação estratégica de rodadas de produção anuais (bottom-up, a partir de demanda.py)
  vm_dinamico.py            # Cálculo de VM (Visual Merchandising) dinâmico — reposição de loja
pedidos/                    # Domínio TRANSACIONAL de Pedidos de Compra (etl/ segue analítico read-only)
  estados.py                # Máquina de estados pura (RASCUNHO→PRONTO→COMPRA_EMITINDO→COMPRA_EMITIDA→VENDA_EMITINDO→EMITIDO, badges)
  builder.py                # Puro: DataFrame do processar_fabrica → snapshot + grupos Colégio×SuperCategoria
  repositorio.py            # ÚNICA porta de escrita/leitura do schema `app` do Supabase (gravável)
  emissor.py                # Casos de uso da emissão (compra Bling → venda Olist), locks CAS + rollback
  integracoes/
    repositorio.py          # app.integracao (credenciais/tokens OAuth) + app.integracao_evento (auditoria)
    oauth.py                # Fluxo OAuth2 genérico Bling/Olist (httpx injetável; state no banco)
    bling.py                # Cliente API v3 Bling — POST /pedidos/compras (payload PURO + HTTP)
    olist.py                # Cliente API v3 Olist/Tiny — POST /pedidos + mapeamento SKU→id
pages/
  0_Home.py                 # Tela inicial / status do sistema
  1_Daily.py                # Dashboard Comercial (metas, vendedores, lojas)
  2_Logistica.py            # Reposição de Loja (sugestões de transferência)
  3_Fabrica.py              # "Simulador de Produção" — PCP tático (Sugestão por SKU) + planejamento anual (Visão Geral/rodadas), ambos usando etl/demanda.py como base comum; botão admin "Congelar rodada" → pedidos/
  4_Pedidos.py              # (Admin) Pedidos de Compra — rodadas congeladas, rascunhos, edição, PRONTO, emissão Bling+Olist
  5_Configuracoes.py        # (Admin) Parâmetros, exceções SKU, Integrações (chaves OAuth), sistema
scripts/                    # Utilitários de linha de comando (rodar da raiz: python scripts/<nome>.py)
  exportar_vm.py            # Exporta data/VM_Calculado.xlsx (VM + Pulmão de todos os SKUs)
  memoria_calculo.py        # Memória de cálculo passo a passo do VM Dinâmico p/ um SKU
  memoria_calculo_fabrica.py# Memória de cálculo passo a passo do PCP (order-up-to) p/ um SKU
  seed_parametros.py        # Semeia app.parametros com a Categoria B do config.yaml (rodar 1x pós-DDL 002)
  migrar.py                 # Aplica docs/sql/*.sql no Supabase via Management API (PAT em .env/env SUPABASE_ACCESS_TOKEN, NÃO em secrets); subcomandos status/aplicar/marcar; ledger app.schema_migrations
docs/sql/                   # DDL versionada do schema `app` (aplicar com `python scripts/migrar.py aplicar`, fora do runtime do app)
```

## Fluxo de dados padrão
1. `loader.py::carregar_dados()` lê **Supabase** via PostgREST e retorna um `dict` de DataFrames com chaves/colunas no formato do `SCHEMA`
2. `TABELAS_SUPABASE` mapeia aba do SCHEMA → tabela real do Supabase; `COLUNAS_SUPABASE` renomeia colunas (IDs usados são os `*_bling`, não os surrogate `id` UUID)
3. Páginas importam funções dos módulos `etl/` e passam os DataFrames
4. Config é sempre lido via `loader.carregar_config()` = `deep_merge(config.yaml ← app.parametros do Supabase)`, cache 5 min, degradação graciosa (Supabase fora → yaml puro). **Nunca** abra `config.yaml` direto em páginas/scripts
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
- Configurações sempre de `carregar_config()` (yaml defaults + Supabase), nunca hardcoded nas páginas
- **Autenticação:** `auth.py` com role-based access (admin, user) via `st.secrets["auth_config"]`
- **Configuração dinâmica:** página `5_Configuracoes.py` salva no Supabase via `etl/config_store.py` (`app.parametros` + histórico de auditoria; admin only). No merge, coleções (`colegios`, `colegios_alias`, `grupo_segmento`, `excecoes_sku`, `planejamento.cobertura_override`) substituem o bloco inteiro
- Nomes de variáveis e comentários em português (padrão do projeto)
- Cada módulo ETL recebe DataFrames e o dicionário `config` — não abre arquivos diretamente

## Motor de Demanda + Abastecimento (etl/demanda.py)

Fonte única usada tanto pela aba tática ("Sugestão por SKU") quanto pela estratégica ("Visão Geral"/rodadas) do Simulador de Produção. Metodologia: **demanda ancorada na alta + política order-up-to (R,S) com projeção forward** (ref. Silver-Pyke-Peterson; newsvendor; aggregate planning). Fundamentação e decisões da diretoria no plano `~/.claude/plans/ethereal-whistling-prism.md`.

- **Demanda ancorada na ALTA** (`calcular_demanda_mensal_por_sku`): a alta define forma e magnitude, a baixa só adiciona volume — o dado esparso da baixa nunca entra no nível do SKU.
  - Meses de alta (`config["demanda"]["janela_alta"]`, ex: [12,1,2]): `vendas reais da última temporada de alta completa × crescimento` (a grade de tamanhos é preservada porque cada SKU é um tamanho).
  - Meses de baixa: `demanda de baixa` = demanda de alta × `proporção da baixa`, espalhada pela `distribuicao_mensal_baixa()` agregada. A proporção é **global** (`calcular_proporcao_baixa()` = Σbaixa/Σalta da empresa, últimos 2 ciclos ≈ 0,43) com **cascata de override manual** (`proporcao_baixa_efetiva(sku, colegio, config, base)`): `excecoes_sku[sku].proporcao_baixa → colegios[COL].proporcao_baixa → global`. Backtest (2023-25): fatiar por categoria/SKU não melhora (teto ~48%); global + override nos poucos gigantes de cauda curta é o que sustenta. Editável na tela (coluna no editor de Colégios + coluna no CSV de exceções).
- **Crescimento por (colégio × série)** (`taxa_crescimento_efetiva(colegio, config, grupo, ativo, observado)`): cascata híbrida (manual do planejador SEMPRE vence os dados): `crescimento_grupos[grupo] (manual) → taxa_crescimento colégio (manual) → observado colégio×segmento → observado colégio → 1+fabrica.crescimento_pct/100`. A **camada observada** (`calcular_crescimento_observado`) mede o crescimento realizado nas ALTAS (alta-sobre-alta, sinal limpo — a baixa tem ruptura), por colégio e por segmento, clamp [0.5,2.0], gate de volume ≥30. O mapa grupo→segmento (`mapa_grupo_segmento(config)`) tem default no código (`SEGMENTO_POR_GRUPO`) sobrescrito por `config["grupo_segmento"]` — editável na página de Configurações (baldes atuais: Infantil, Inf+Fund, Fundamental, Médio, Tempo Integral, Diário, Ed. Física, Esporte, Outros). Desligável em `config["demanda"]["crescimento_observado_ativo"]` (→ volta ao +10% cego). `ativo=False` desliga tudo (toggle p/ comparar). Vale p/ fábrica e VM logística. Não muda o total da rede (~+11%), **redistribui** para o mix certo (ex: NEV Médio +51% vs LMN −29%).
- **Política order-up-to** (`simular_politica_reabastecimento`): motor comum. Por SKU, caminha as rodadas mantendo estoque projetado (`estoque − backlog`, consumido mês a mês, reabastecido a cada chegada). Em cada rodada r: `DemandaPeriodo` = demanda até a próxima chegar; `EstoqueSeguranca = estoque_seguranca(DemandaPeriodo, contém_alta, config)` (Fator de Serviço × Variação da Demanda × DemandaPeriodo); `EstoqueAlvo = DemandaPeriodo + EstoqueSeguranca`; `Pedido = par_ceil(EstoqueAlvo − EstoqueProjetado_na_chegada)`. As colunas do DataFrame retornado usam esses nomes (`DemandaPeriodo`/`EstoqueSeguranca`/`EstoqueAlvo`/`EstoqueProjetado`; antes eram `DI`/`SS`/`S`/`OH`). Sugestão por SKU = Pedido da rodada selecionada; Visão Geral = soma por rodada. **Cobertura Alvo** (antecipação deliberada, `planejamento.cobertura_override` = {data_disparo ISO → fração 0-1 da demanda anual da rede}): estende o fim de proteção da rodada até a demanda acumulada da rede atingir o alvo (`_data_por_demanda_acumulada`; clamp piso=natural, teto=1.0) — a rodada seguinte encolhe SOZINHA (order-up-to é auto-liquidante; Σ produção do horizonte se conserva). Colunas `FimCobertura`/`CoberturaPct` no retorno. A **Visão Geral é o cockpit único do plano de rodadas**: edita o calendário de disparos (`rodadas_datas`, **multiselect de mês/ano** — pills; disparos são sempre 1º-de-mês) E as coberturas na mesma tela, onde o efeito é visível ao vivo (config de simulação usa as datas do preview antes de salvar). A tabela tem DUAS colunas de cobertura — "Cobertura natural (%)" read-only (piso) + "Cobertura alvo (%)" editável e **vazia quando não há antecipação** (preenchida = intenção deliberada). Preview de sessão para datas e coberturas; um botão "Salvar plano" (admin) persiste `rodadas_datas` + `cobertura_override` juntos. Spec: `docs/requisitos/cobertura-alvo-rodada.md`.
- **Nível de serviço** (`config["demanda"]["nivel_servico_alta"/"nivel_servico_baixa"/"variacao_demanda"]`): alta ~99% ("não pode faltar"), baixa ~92%. Fator de Serviço pela criticidade do intervalo.
- `planejamento.periodo_historico_inicio`/`fim` = período histórico único (sazonalidade agregada + distribuição mensal da baixa + base dos SKUs só-de-baixa). Define o FORMATO do ano, não o tamanho do pico. Calendário de rodadas: `planejamento.rodadas_datas` (datas ISO explícitas de disparo, este ano + próximo, SEM repetição anual — a última data só fecha o intervalo da penúltima; 2+ datas obrigatórias) é a **fonte única**. O antigo fallback mensal (`planejamento.rodadas`, meses fixos que repetiam todo ano) e o override `rodadas_meses` foram removidos — havia duas metodologias divergindo na UI (Visão Geral por meses × Sugestão por SKU por datas). Sem `rodadas_datas`, a Visão Geral só avisa e a Sugestão por SKU cai na cobertura fixa (`fabrica.cobertura_meses`). A simulação expõe `DemandaPeriodoAlta`/`DemandaPeriodoBaixa`/`MesesIntervalo`/`data_chegada_seguinte` (split pico/baixa usado pela UI da Sugestão por SKU).

## Pedidos de Compra (pedidos/ + pages/4_Pedidos.py)

Ponte Simulador → ERPs. Fluxo: na 3_Fabrica (admin), **"Congelar rodada"** recalcula
`processar_fabrica` FRESCO (nunca o cache — o motor ancora em `Timestamp.now()`),
tira **snapshot imutável** (resultado integral por SKU + config completo + data de
referência) e gera **pedidos rascunho por Colégio × SuperCategoria** (1 pedido nosso ↔
1 pedido de compra no Bling ↔ 1 pedido de venda no Olist). Revisão/edição na 4_Pedidos:
`quantidade_sugerida` (imutável, snapshot) vs `quantidade_final` (editável só em
RASCUNHO — trigger no banco é a trava real), depois PRONTO → emissão.

- **Persistência**: schema **`app`** do Supabase (gravável) — `rodada_congelada`
  (snapshot, jsonb), `pedido_compra`, `pedido_compra_item`, `integracao`
  (credenciais/tokens OAuth), `integracao_evento` (auditoria). DDL numerada em
  `docs/sql/00N_*.sql` (aplicar manual no SQL Editor; `app` já exposto na Data API).
  O `public` segue 100% espelho read-only da pipeline externa.
- **Escrita no Supabase SÓ via portas** `pedidos/repositorio.py` (pedidos) e
  `pedidos/integracoes/repositorio.py` (integrações) — ambas reusam `_conn_app`.
  Consistência sem transação: unique parcial = 1 congelamento vivo por rodada
  (23505 → `RodadaJaCongelada`); `CONGELANDO`→`ABERTA` = commit lógico; transições
  por compare-and-swap (`transicionar_pedido` retorna False = corrida perdida).
- **Emissão em DOIS momentos** (`pedidos/emissor.py`): compra no **Bling** (conta AK
  Uniformes, `POST /pedidos/compras`) → depois venda no **Olist/Tiny** (conta Art
  Kamizetas, `POST /pedidos`; `numeroOrdemCompra` = nº do Bling — ordem obrigatória).
  Estados: RASCUNHO→PRONTO→COMPRA_EMITINDO→COMPRA_EMITIDA→VENDA_EMITINDO→EMITIDO
  (`*_EMITINDO` = lock CAS anti duplo-clique, igual ao CONGELANDO). Falha ANTES do
  POST → rollback ao estado anterior; falha DEPOIS → fica travado em `*_EMITINDO`, UI
  oferece "Destravar" com aviso de conferir no ERP. Idempotência por `bling_id`/
  `olist_id` já gravado. SKUs idênticos nos 2 sistemas → mapa SKU→id Olist via
  `GET /produtos`; pré-validação lista faltantes antes de habilitar a venda.
- **Integrações** (`pedidos/integracoes/`): OAuth2 authorization_code; chaves e tokens
  vivem em `app.integracao` (NÃO em secrets.toml — filesystem do Cloud é efêmero),
  geridas na aba Integrações de 5_Configuracoes. `state` anti-CSRF no banco (a sessão
  Streamlit morre no redirect); callback no topo da 5_Configuracoes lê `?code&state`.
  Redirect URI = URL do app + `/configuracoes` (`url_path` fixo em app.py). Clientes
  HTTP com `http` injetável (testes sem rede). App no portal Bling e no Olist a
  registrar (pré-requisito; ver `docs/decisoes.md`).
- **Observações internas padronizadas**: `pedido_compra.titulo`
  (`AKU-PC · COLÉGIO · SUPERCAT · Rmm/aaaa`) persistido; bloco completo SEMPRE
  recomposto por `builder.montar_observacoes_bling` no momento do uso (nunca
  pré-gravado — envelheceria ao editar quantidades). `ref: <uuid>` = chave de
  reconciliação com o espelho futuro. Vai em `observacoes`/`observacoesInternas` dos
  dois ERPs.

## Página de Configuração (5_Configuracoes.py)

**Admin only** — acesso controlado por role em `st.secrets["auth_config"]`.

### Aba 1: Parâmetros Gerais
Formulário organizado pelos **3 subsistemas** da metodologia atual:
- **Comercial (Daily):** metas (Natal, Mossoró) + status IDs de pedido (em_aberto, em_andamento, pronto_retirada)
- **Reposição de Loja:** VM Dinâmico (`config.yaml["vm"]` — cobertura, alta temporada, multiplicador PA, VM mínimo, lead time, nível de serviço, toggle de crescimento) + *fallback* fixo (`logistica.vm_padrao`, `logistica.dias_analise_giro`) usado só quando o SKU não tem giro
- **Produção (Simulador):** Demanda/order-up-to (`config.yaml["demanda"]` — níveis de serviço alta/baixa, variação, janela da alta, toggle de crescimento), Planejamento (lead time, período histórico único — o **calendário de rodadas migrou para o Simulador → Visão Geral**, ao lado das coberturas) e *fallback* da Fábrica (crescimento, cobertura, correção manual global)

Logo abaixo do formulário, editor independente de **Colégios** (`config.yaml["colegios"]`): tabela com taxa de crescimento e nível de serviço por colégio, descobertos dinamicamente a partir de `detalhes["Marca_sku"]` — valores são sempre input manual do usuário, nunca calculados a partir das vendas.

Ao salvar:
1. Valida estrutura mínima e valores numéricos
2. Grava a Categoria B no **Supabase** via `etl/config_store.py` (`app.parametros` UPDATE + `app.parametros_historico` INSERT — auditoria de quem/quando)
3. Limpa cache do Streamlit (`st.cache_data.clear()` — invalida também o `carregar_config`)

O `config.yaml` do git NÃO é mais escrito pela UI — é só a fonte dos defaults (Categoria A). Falha no Supabase → erro na tela, nada é salvo.

### Aba 2: Exceções de SKU
CSV template para sobrescrever parâmetros globais por SKU:
- Columns: `sku`, `vm_override`, `correcao_manual` (as antigas `dias_analise`/`sazonalidade` foram removidas — nenhum motor as lia)
- Download: template atual (ou exemplo padrão se nenhuma exceção existe)
- Upload: aplicar novas exceções via CSV
- Salva em `app.parametros` (chave `excecoes_sku`) via config_store
- O campo `correcao_manual` (salvo como chave `correcao`) também é lido por `vm_dinamico.calcular_vm_por_sku()` como fator de correção do VM dinâmico

### Aba Integrações
Um card por plataforma (**Bling** = compra AK · **Olist** = venda Art Kamizetas):
credenciais OAuth (client_id/secret `type="password"` — vazio mantém o salvo,
redirect_uri), botão Conectar (link OAuth via `montar_authorize_url`), status do token,
Testar conexão (GET leve), IDs de negócio (fornecedor / contato+vendedor+depósito+situação)
salvos no jsonb `config` da `app.integracao`, e (só Bling) "Validar contrato" via GET num
pedido existente. O callback OAuth (`?code&state`) é tratado no topo da página, antes das
abas. Tudo via `pedidos/integracoes/repositorio.py` — nada em secrets.toml.

### Aba 3: Sistema
Informações do sistema:
- Versões (Python, Streamlit, Pandas)
- Fonte de dados: Supabase (Bling ERP via pipeline externa)
- Última gravação de parâmetros no Supabase (quando/por quem)
- Botão: Forçar recarga de cache
- Botão: Backup do config EFETIVO (yaml + Supabase mesclados, download)

---

## Não faça sem perguntar
- Alterar `config.yaml` manualmente (use a página de Configurações, ou pergunte ao usuário)
- Alterar a estrutura de retorno de `loader.py::carregar_dados()` (quebra todas as páginas)
- Renomear colunas dos DataFrames
- Adicionar dependências ao `requirements.txt`
- Escrever no Supabase fora das portas: `pedidos/repositorio.py` (pedidos), `pedidos/integracoes/repositorio.py` (integrações/tokens) e `etl/config_store.py` (parâmetros); o schema `public` é read-only SEMPRE
- Alterar o DDL do schema `app` sem criar um novo arquivo numerado em `docs/sql/` (migrations versionadas; aplicadas por `python scripts/migrar.py aplicar`, que registra no ledger `app.schema_migrations`)

---

## Dependências (requirements.txt)
```
streamlit
pandas
openpyxl
pyyaml
plotly
postgrest
streamlit-authenticator
```

`openpyxl` continua porque `scripts/exportar_vm.py` exporta `data/VM_Calculado.xlsx` (não é mais usado para ler parâmetros de entrada — o VM Dinâmico lê tudo de `config.yaml`).

---

## Testes (`tests/`)
Suíte `pytest` focada no **motor de demanda/PCP** (`etl/demanda.py`), nos utilitários do `loader.py`, no **domínio de pedidos** (`pedidos/` — builder e estados puros; repositório testado com fake do gateway `_inserir/_atualizar/_selecionar/_deletar`, sem Supabase) e nas **integrações/emissão** (`pedidos/integracoes/` + `emissor.py` — payloads puros, OAuth e clientes HTTP com fake injetável, emissor com repo+cliente fakes; zero rede). Sem Supabase/secrets. Instalar dev deps com `uv pip install -r requirements-dev.txt` (o venv usa **uv**, não pip) e rodar `pytest` na raiz. `tests/conftest.py` monta `config` e `dados` sintéticos; o determinismo vem de ancorar as altas em `now()` de forma constante e desligar o crescimento (ver docstring do conftest). Os fakes reusam os `RepoFake` de `test_pedidos_repositorio`/`test_integracoes_repositorio` e o `HttpFake` de `test_integracoes_payloads`. Ao mexer no motor, rode a suíte e atualize os testes junto.

---

## Secrets esperados (streamlit/secrets.toml)
```toml
# Supabase (Project Settings → API)
[supabase]
url = "https://<PROJECT_REF>.supabase.co"
service_key = "<SERVICE_ROLE_KEY>"
schema = "public"
schema_app = "app"   # schema gravável (pedidos de compra) — requer DDL aplicado + schema exposto na Data API

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

**Migrações (`scripts/migrar.py`) NÃO usam secrets.toml.** O Personal Access Token
que aplica DDL é lido de `SUPABASE_ACCESS_TOKEN` — do **arquivo `.env` na raiz**
(git-ignored; `cp .env.example .env` e preencha) ou do ambiente (a env do sistema
vence). Fica fora do runtime do app — o dashboard só carrega a `service_key`
CRUD-only, e nada lê o `.env` além do script. O project ref sai de
`SUPABASE_PROJECT_REF` ou é extraído de `[supabase].url`. Nunca ponha o PAT no
secrets.toml: ele iria para o app e ampliaria o raio de dano (pode `DROP TABLE`).
