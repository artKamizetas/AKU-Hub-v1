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
auth.py                     # Login Google (OIDC nativo do Streamlit) + autorização por role
auth_store.py               # Porta de app.usuario (allowlist de acesso); PURO exceto o cache
ui_carga.py                 # Porta ÚNICA de carregamento das páginas (spinner + rodapé de frescor)
data/                       # Saídas locais opcionais (ex: VM_Calculado.xlsx via scripts/exportar_vm.py), não sincronizado com Bling
etl/
  loader.py                 # Lê Supabase (via PostgREST) e valida → retorna dict de DataFrames
                            # Paginação PARALELA em fila plana (117s → 11s); fingerprint_config()
                            # Mapas TABELAS_SUPABASE / COLUNAS_SUPABASE convertem nomes para o SCHEMA
                            # carregar_config() = ponto ÚNICO de leitura de config (yaml ← app.parametros)
  config_store.py           # Persistência dos parâmetros no Supabase (app.parametros + historico); deep_merge/extrair_parametros
  daily.py                  # Comercial: monta detalhado + metas por Loja e por Vendedor (aceita `competencia`)
  metas.py                  # Motor PURO das metas escalonadas (níveis, rateio vendedor←loja, agregação)
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
  1_Daily.py                # Dashboard Comercial — metas escalonadas (competência) + análise livre (período)
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
- **Páginas carregam por `ui_carga.carregar_com_feedback()`**, nunca chamando
  `carregar_dados()`+`carregar_config()` na mão (era o `_carregar()` duplicado em 4 telas)
- **Nunca `st.cache_data.clear()` global** ao salvar: use `carregar_config.clear()`
  (parâmetros) ou `loader.invalidar_cache_dados()` (dados). O global levava junto a
  leitura de 1h do Supabase e cada "Salvar" custava uma carga fria. Só os botões
  "Forçar recarga" usam o global, que é a intenção deles
- **Função de página cacheada que recebe `_config`** deve receber TAMBÉM
  `fingerprint_config(config)` como argumento sem underscore — é o que entra na cache
  key. Sem ele o resultado fica preso ao config antigo (o clear global mascarava isso)
- **IDs entre tabelas Supabase**: sempre usar `limpar_id()` ANTES de comparar/joinar — postgrest devolve colunas int com NULL como `float64`, e `astype(str)` direto gera `'123.0'` que quebra merges com IDs vindos como int64 puros
- Configurações sempre de `carregar_config()` (yaml defaults + Supabase), nunca hardcoded nas páginas
- **Autenticação:** `auth.py` — login Google via `st.login()`/`st.user` (OIDC nativo, secrets
  `[auth]`+`[auth.google]`); a allowlist `app.usuario` decide quem entra e com qual role.
  Páginas usam `exigir_login()`, `identidade_atual()`, `e_admin()` ou `exigir_admin()` —
  **nunca** releiam role de secrets/session_state (era a duplicação que existia em 3 páginas)
- **Configuração dinâmica:** página `5_Configuracoes.py` salva no Supabase via `etl/config_store.py` (`app.parametros` + histórico de auditoria; admin only). No merge, coleções (`colegios`, `colegios_alias`, `grupo_segmento`, `excecoes_sku`, `planejamento.cobertura_override`) substituem o bloco inteiro
- Nomes de variáveis e comentários em português (padrão do projeto)
- Cada módulo ETL recebe DataFrames e o dicionário `config` — não abre arquivos diretamente

## Metas Escalonadas — Comercial (etl/metas.py + etl/daily.py + pages/1_Daily.py)

Meta comercial em **três níveis** (Prata < Ouro < Diamante) por **loja × mês**, em
**Faturamento e PA**. Spec: `docs/requisitos/metas-escalonadas.md`.

- **`etl/metas.py` é PURO** (sem streamlit/pandas — só dicts): `chave_competencia`,
  `metas_da_loja`, `atribuicao_vendedores`, `metas_do_vendedor`, `classificar_nivel`,
  `resumo_faturamento`/`resumo_pa`, `agregar_faturamento`/`agregar_pa`,
  `validar_metas_mensais`, `competencias_anteriores`/`historico_atingimento`
  (série de 12 meses) e as duas de ESCRITA — `aplicar_edicao_metas` /
  `aplicar_edicao_vendedores`, que transformam o que sai do `data_editor` no dict
  gravado em `app.parametros`. Essas duas vivem aqui, e não na página, porque um
  bug nelas corrompe a configuração de todo mundo: são o caminho de escrita e
  precisam de teste sem Streamlit nem Supabase (casos que APAGAM dado — mês
  limpo, loja removida, competência órfã — têm cobertura dedicada).
  **Nunca duplique essa regra na página** — foi exatamente a
  duplicação ETL↔página que a refatoração eliminou.
- **Meta é só por loja.** Colégio é recorte de leitura do realizado, nunca meta
  cadastrada. **Vendedor não digita meta**: é rateada da loja —
  `meta(v) = meta(loja) × peso(v) / Σ pesos dos ativos da loja`; a soma dos vendedores
  fecha a meta da loja por construção. **PA não se rateia** (razão): a meta de PA do
  vendedor é a mesma da loja.
- **PA não é aditivo**: agregar é `Σpeças / Σpedidos` (média ponderada), nunca a média
  dos PAs; e PA não tem run rate — o PA parcial do mês já é a projeção.
- **Persistência em `app.parametros`** (`daily.metas_mensais` + `daily.vendedores_loja`),
  ambos em `CHAVES_PARAMETROS["daily"]` **e** em `CAMINHOS_SUBSTITUICAO` — sem a
  substituição, um mês/vendedor apagado na UI ressuscitaria do yaml no `deep_merge`.
  Chave de competência = `"AAAA-MM"`.
- **Herança assimétrica, de propósito:** meta **nunca** herda entre meses (mês sem
  cadastro é lacuna sinalizada; Julho herdar Janeiro seria pior que nada). A
  **atribuição vendedor→loja herda** da competência editada mais recente ≤ a pedida —
  o gestor só edita quando alguém entra/sai/troca, e revisar Março não sofre com uma
  contratação de Agosto. Fallback do formato antigo (`daily.metas`): vira o Ouro de
  faturamento (Prata 0,85× / Diamante 1,20×), rotulado `origem="estimada"` na tela.
- **`processar_daily(dados, config, competencia=None)`** retorna
  `(df_detalhado, df_metas_loja, df_metas_vendedor)` — 3 valores, não 2. Colunas de
  TEXTO saem como `""` (nunca None: o pandas as converteria em NaN numa coluna string);
  colunas numéricas sem meta saem NaN (célula vazia, correto).
- **A tela separa dois regimes temporais**: `Competência` (mês/ano, permite reabrir
  meses fechados) rege as metas; `Período` rege a análise livre. Antes se misturavam
  sem aviso — os KPIs eram sempre do mês corrente enquanto o filtro de Período mandava
  no resto da página.
- **Gráficos:** bullet chart com faixas em **rampa neutra sequencial** (prata/ouro/
  diamante é escala ordinal, não identidade — a identidade vem do emoji 🥈🥇💎, nunca
  da cor sozinha) e **nada de eixo duplo** (o histórico usa seletor de métrica em eixo
  único; R$ e peças em escalas diferentes no mesmo gráfico distorcem a comparação).
- **Caveat de dados (RESOLVIDO set/2026):** o espelho tinha o histórico até fev/2026
  em códigos de situação órfãos (`1` = 95% da base) enquanto `daily.situacoes_venda`
  é `[9]` — a coluna "Realizado ano anterior" ficava vazia e o *Propor* desabilitado.
  A pipeline aplicou o backfill (`1 → 9`): zero órfãos, 12 meses de histórico reais.
  **Nada mudou no dashboard** — a degradação levantou sozinha, que era o desenho.
  Registro em `docs/requisitos/backfill-situacao-pedidos.md`.

## Motor de Demanda + Abastecimento (etl/demanda.py)

Fonte única usada tanto pela aba tática ("Sugestão por SKU") quanto pela estratégica ("Visão Geral"/rodadas) do Simulador de Produção. Metodologia: **demanda ancorada na alta + política order-up-to (R,S) com projeção forward** (ref. Silver-Pyke-Peterson; newsvendor; aggregate planning). Fundamentação e decisões da diretoria no plano `~/.claude/plans/ethereal-whistling-prism.md`.

- **Demanda ancorada na ALTA** (`calcular_demanda_mensal_por_sku`): a alta define forma e magnitude, a baixa só adiciona volume — o dado esparso da baixa nunca entra no nível do SKU.
  - Meses de alta (`config["demanda"]["janela_alta"]`, ex: [12,1,2]): `vendas reais da última temporada de alta completa × crescimento` (a grade de tamanhos é preservada porque cada SKU é um tamanho).
  - Meses de baixa: `demanda de baixa` = demanda de alta × `proporção da baixa`, espalhada pela `distribuicao_mensal_baixa()` agregada. A proporção é **global** (`calcular_proporcao_baixa()` = Σbaixa/Σalta da empresa, últimos 2 ciclos ≈ 0,43) com **cascata de override manual** (`proporcao_baixa_efetiva(sku, colegio, config, base)`): `excecoes_sku[sku].proporcao_baixa → colegios[COL].proporcao_baixa → global`. Backtest (2023-25): fatiar por categoria/SKU não melhora (teto ~48%); global + override nos poucos gigantes de cauda curta é o que sustenta. Editável na tela (coluna no editor de Colégios + coluna no CSV de exceções).
- **Crescimento por (colégio × série)** (`taxa_crescimento_efetiva(colegio, config, grupo, ativo, observado)`): cascata híbrida (manual do planejador SEMPRE vence os dados): `crescimento_grupos[grupo] (manual) → taxa_crescimento colégio (manual) → observado colégio×segmento → observado colégio → 1+fabrica.crescimento_pct/100`. A **camada observada** (`calcular_crescimento_observado`) mede o crescimento realizado nas ALTAS (alta-sobre-alta, sinal limpo — a baixa tem ruptura), por colégio e por segmento, clamp [0.5,2.0], gate de volume ≥30. O mapa grupo→segmento (`mapa_grupo_segmento(config)`) tem default no código (`SEGMENTO_POR_GRUPO`) sobrescrito por `config["grupo_segmento"]` — editável na página de Configurações (baldes atuais: Infantil, Inf+Fund, Fundamental, Médio, Tempo Integral, Diário, Ed. Física, Esporte, Outros). Desligável em `config["demanda"]["crescimento_observado_ativo"]` (→ volta ao +10% cego). `ativo=False` desliga tudo (toggle p/ comparar). Vale p/ fábrica e VM logística. Não muda o total da rede (~+11%), **redistribui** para o mix certo (ex: NEV Médio +51% vs LMN −29%).
- **Política order-up-to** (`simular_politica_reabastecimento`): motor comum. Por SKU, caminha as rodadas mantendo estoque projetado (`estoque − backlog`, consumido mês a mês, reabastecido a cada chegada). Em cada rodada r: `DemandaPeriodo` = demanda até a próxima chegar; `EstoqueSeguranca = estoque_seguranca(DemandaPeriodo, contém_alta, config)` (Fator de Serviço × Variação da Demanda × DemandaPeriodo); `EstoqueAlvo = DemandaPeriodo + EstoqueSeguranca`; `Pedido = par_ceil(EstoqueAlvo − EstoqueProjetado_na_chegada)`. As colunas do DataFrame retornado usam esses nomes (`DemandaPeriodo`/`EstoqueSeguranca`/`EstoqueAlvo`/`EstoqueProjetado`; antes eram `DI`/`SS`/`S`/`OH`). Sugestão por SKU = Pedido da rodada selecionada; Visão Geral = soma por rodada. **Cobertura Alvo** (antecipação deliberada, `planejamento.cobertura_override` = {data_disparo ISO → fração 0-1 da demanda anual da rede}): estende o fim de proteção da rodada até a demanda acumulada da rede atingir o alvo (`_data_por_demanda_acumulada`; clamp piso=natural, teto=1.0) — a rodada seguinte encolhe SOZINHA (order-up-to é auto-liquidante; Σ produção do horizonte se conserva). Colunas `FimCobertura`/`CoberturaPct` no retorno. A **Visão Geral é o cockpit único do plano de rodadas**: edita o calendário de disparos (`rodadas_datas`, **multiselect de mês/ano** — pills; disparos são sempre 1º-de-mês) E as coberturas na mesma tela, onde o efeito é visível ao vivo (config de simulação usa as datas do preview antes de salvar). A tabela tem DUAS colunas de cobertura — "Cobertura natural (%)" read-only (piso) + "Cobertura alvo (%)" editável e **vazia quando não há antecipação** (preenchida = intenção deliberada). Preview de sessão para datas e coberturas; um botão "Salvar plano" (admin) persiste `rodadas_datas` + `cobertura_override` juntos. Spec: `docs/requisitos/cobertura-alvo-rodada.md`. **On-order/em-trânsito** (pendência conhecida, EM DISCUSSÃO — não implementar ainda): hoje o motor abre em `estoque − backlog` e recalcula o pedido a cada chegada, então a decisão manual do gestor (congelou 65 no lugar de 70) não retroalimenta a próxima rodada. A spec `docs/requisitos/posicao-estoque-on-order.md` explora somar o termo em-trânsito à posição de partida, com o Tiny/Olist como fonte da verdade da execução (qtd/data reais deslizam na fábrica) e a regra de ouro da reconciliação (baixar o on-order quando vira estoque físico).
- **Nível de serviço** (`config["demanda"]["nivel_servico_alta"/"nivel_servico_baixa"/"variacao_demanda"]`): alta ~99% ("não pode faltar"), baixa ~92%. Fator de Serviço pela criticidade do intervalo.
- `planejamento.periodo_historico_inicio`/`fim` = período histórico único (sazonalidade agregada + distribuição mensal da baixa + base dos SKUs só-de-baixa). Define o FORMATO do ano, não o tamanho do pico. Calendário de rodadas: `planejamento.rodadas_datas` (datas ISO explícitas de disparo, este ano + próximo, SEM repetição anual — a última data só fecha o intervalo da penúltima; 2+ datas obrigatórias) é a **fonte única**. O antigo fallback mensal (`planejamento.rodadas`, meses fixos que repetiam todo ano) e o override `rodadas_meses` foram removidos — havia duas metodologias divergindo na UI (Visão Geral por meses × Sugestão por SKU por datas). Sem `rodadas_datas`, a Visão Geral só avisa e a Sugestão por SKU cai na cobertura fixa (`fabrica.cobertura_meses`). A simulação expõe `DemandaPeriodoAlta`/`DemandaPeriodoBaixa`/`MesesIntervalo`/`data_chegada_seguinte` (split pico/baixa usado pela UI da Sugestão por SKU).

## Pedidos de Compra (pedidos/ + pages/4_Pedidos.py)

Ponte Simulador → ERPs. Fluxo: na 3_Fabrica (admin), **"Congelar rodada"** recalcula
`processar_fabrica` FRESCO (nunca o cache — o motor ancora em `Timestamp.now()`),
tira **snapshot imutável** (resultado integral por SKU + config completo + data de
referência) e gera **pedidos rascunho por Colégio × SuperCategoria** (1 pedido nosso ↔
1 pedido de compra no Bling ↔ 1 pedido de venda no Olist). Revisão/edição na 4_Pedidos:
`quantidade_sugerida` (imutável, snapshot) vs `quantidade_final` (editável só em
RASCUNHO — trigger no banco é a trava real), depois PRONTO → emissão. Cada item
carrega também `memoria_sugerida` (jsonb, DDL 004) — cópia CURADA e enxuta dos
drivers order-up-to que geraram a sugestão (demanda do período alta/baixa,
estoque de segurança, estoque-alvo, estoque da rede, backlog, projetado na
chegada, nível de serviço). É o "por que essa quantidade" exibido na revisão do
rascunho SEM carregar o `resultado_skus` pesado da rede; congelada no INSERT,
nunca atualizada.

- **Persistência**: schema **`app`** do Supabase (gravável) — `rodada_congelada`
  (snapshot, jsonb), `pedido_compra`, `pedido_compra_item`, `integracao`
  (credenciais/tokens OAuth), `integracao_evento` (auditoria), `olist_produto_cache`
  (SKU→id do Olist, DDL 005 — otimização da emissão, degrada se ausente). DDL numerada em
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
  `olist_id` já gravado. SKUs idênticos nos 2 sistemas → mapa SKU→id Olist
  resolvido por `emissor.resolver_ids_olist` em CAMADAS (mais barato → mais caro):
  **(1) cache** `app.olist_produto_cache` (DDL 005, id imutável — SKU já visto = 0
  chamadas; degrada se o DDL não existe); **(2) por família**
  (`olist.mapear_por_familia`): deriva o SKU pai (corta o `-TAMANHO`), `?codigo=`
  acha o pai e `GET /produtos/{id}` traz a grade INTEIRA (`variacoes[]`), ~2
  chamadas por família de ~7 tamanhos e aquece os irmãos no cache; **(3) fallback
  exato** `?codigo=<SKU>` (match EXATO local, o filtro pode ser parcial) para o
  que sobrou. NUNCA varre o catálogo — a varredura estourava o rate limit (Olist
  v3 = 60 req/min → 429). Todo GET/POST passa por `_requisitar`, que em 429
  respeita `Retry-After` e reemite (backoff limitado). No modo LOTE, a 4_Pedidos
  resolve todos os SKUs UMA vez e passa o superset a cada `emitir_venda_olist`
  (mapa_sku=None refazia por pedido). O cache é a ponte para a fonte definitiva
  (espelho do catálogo do Tiny): popule a tabela por fora e a API nem é tocada.
  Pré-validação lista faltantes antes de habilitar a venda.
  **Pré-validação simétrica** (`validar_pre_emissao_bling`/`_olist` em `emissor.py`):
  checks puros (config incompleta — `fornecedor_id`/`contato_id` etc, nada a emitir,
  itens sem id de produto) que dão o feedback ANTES do clique — lista vazia = pode
  emitir. A UI da 4_Pedidos oferece **dois modos** num `st.segmented_control`
  (editar UM pedido × ação em LOTE), ambos dentro de um único `@st.fragment`
  (`_area_trabalho`) para trocar de modo sem re-ler o topo (sem cache) e sem o
  "piscar"; NÃO em `st.tabs` (fragment-que-rerroda dentro de tabs vaza conteúdo,
  bug #9158/#9313 do Streamlit). Emissões usam `st.status` com progresso.
- **Integrações** (`pedidos/integracoes/`): OAuth2 authorization_code; chaves e tokens
  vivem em `app.integracao` (NÃO em secrets.toml — filesystem do Cloud é efêmero),
  geridas na aba Integrações de 5_Configuracoes. `state` anti-CSRF no banco (a sessão
  Streamlit morre no redirect); callback no topo da 5_Configuracoes lê `?code&state`.
  Redirect URI = URL do app + `/configuracoes` (`url_path` fixo em app.py). Clientes
  HTTP com `http` injetável (testes sem rede). App no portal Bling e no Olist a
  registrar (pré-requisito; ver `docs/decisoes.md`).
- **Observações padronizadas**: `pedido_compra.titulo`
  (`COLÉGIO - SUPERCAT - Rmm/aaaa`) persistido; bloco completo SEMPRE
  recomposto por `builder.montar_observacoes_bling` no momento do uso (nunca
  pré-gravado — envelheceria ao editar quantidades). `ref: <uuid>` = chave de
  reconciliação com o espelho futuro. **Mapeamento nos dois ERPs**: o título
  curto vai em `observacoesInternas` (é a coluna "Observação interna" da
  listagem de compras do Bling e alimenta a busca por pedido); o bloco completo
  vai em `observacoes`. A 1ª linha do bloco repete o título de propósito, para
  ficar autocontido. Por ITEM, `descricaoDetalhada` recebe a memória de cálculo
  em uma linha (`builder.montar_descricao_item`, derivada da `memoria_sugerida`):
  `Alvo 42 = demanda 30 + segurança 12 - projetado 0 | final 40 | R08/2026`.
- **Campos de compra do Bling**: `codigoFornecedor` = nosso SKU; `unidade` vem de
  `unidade_padrao` no config da integração (default `PÇ` — o espelho não traz
  unidade do cadastro); pagamento = parcela única com `dataVencimento` = emissão +
  `prazo_pagamento_dias` (default 30) e `formaPagamento.id` (`forma_pagamento_id`,
  selectbox por nome via `GET /formas-pagamentos`). Os três moram na aba
  Integrações — são fixos do acordo comercial, não decisão por rodada.
- **Campos de venda do Olist**: `dataPrevista` = a MESMA `rodada.data_chegada` que
  vai no Bling (os dois ERPs não podem divergir sobre quando a mercadoria chega);
  `produto.tipo = "P"` nos itens; `infoAdicional` = SKU (a memória de cálculo é
  só do Bling — a fábrica não precisa dela). O pagamento espelha a parcela única
  do Bling, mas o SHAPE é OUTRO: objeto aninhado `pagamento.{formaRecebimento,
  meioPagamento, parcelas[]}` — no Olist é forma de **recebimento**, não
  `formaPagamento` no topo. `forma_recebimento_id` (obrigatório p/ gerar o bloco),
  `meio_pagamento_id` (opcional) moram na aba Integrações. A forma de recebimento
  é um **selectbox por nome** (`olist.listar_formas_recebimento` = `GET
  /formas-recebimento`, só ativas, id por conta — igual ao selectbox de forma de
  pagamento do Bling), com degradação para text_input se desconectado/GET falhar;
  o id inválido digitado à mão foi o que fez a 1ª venda reprovar ("Forma de
  recebimento não encontrada"). O `meio_pagamento_id` segue digitado — a API v3
  NÃO lista os meios e a numeração de id é própria do Olist. **O prazo NÃO se
  configura no Olist**: a compra e a venda são
  o mesmo acordo, então `emissor.py` injeta o `prazo_pagamento_dias` do config do
  **Bling** em `montar_payload_venda(prazo_dias=...)` e os dois vencimentos batem
  por construção — um campo editável em dois lugares divergiria.

## Carregamento de dados (etl/loader.py + ui_carga.py)

A leitura do Supabase era **117 s** e dominava 99% de toda espera do app (os
motores de cálculo somam 0,02–5 s). Hoje são **~11 s**. Spec:
`docs/requisitos/carregamento-e-feedback.md`.

- **Fila plana de páginas**: `_ler_supabase` conta as 9 tabelas em paralelo,
  monta `planejar_paginas()` = `[(tabela, offset)…]` (162 hoje) e busca TODAS
  numa `ThreadPoolExecutor(16)`, consumindo com `as_completed()` **na thread
  principal**. Antes paralelizava entre TABELAS e paginava em série dentro de
  cada uma — o relógio virava a corrente serial de `itens` (166 requests).
  Aumentar a página não adianta: o Supabase corta em 1.000 (`max-rows`).
- **`.order("id")` é obrigatório** em toda página. Sem ORDER BY explícito o
  PostgREST não garante ordem estável entre requests e páginas paralelas podem
  duplicar/pular linhas. `montar_dataframe` ainda ordena e deduplica: a ordem
  precisa ser determinística porque `Produtos_detalhes` faz
  `drop_duplicates(keep="last")`.
- **`FILTROS_NAO_NULOS`** empurra para o servidor o que o loader descartaria no
  `dropna` (`itens.id_pedido_bling`: 41% das linhas). A MESMA condição vale no
  count e nas páginas — divergir desalinha os offsets e some com dados **sem
  erro nenhum**. É o modo de falha mais perigoso da mudança; tem teste dedicado.
- **Retry por página** (3×): o fan-out foi de ~9 para ~162 requests, e um 5xx
  transitório que antes era raro derrubaria a carga inteira.
- **`converter_serie_data`** vetoriza a conversão de datas (era 5,8 s de `apply`
  linha a linha em 140k linhas). `converter_data_flexivel` continua sendo o
  fallback por elemento.
- **`carregar_dados(_progresso=…)`**: callback opcional `(feitas, total, etapa)`,
  chamado só da thread principal. O prefixo `_` o mantém fora do hash da cache
  key. **A UI não o usa** — desenhar dentro de função cacheada faz o
  `st.cache_data` reproduzir os elementos no cache hit (element replay), e
  desenhar em bloco criado fora é proibido (`CacheReplayClosureError`). Serve a
  chamadores fora do Streamlit.
- **`ui_carga.carregar_com_feedback()`** é a porta das páginas: spinner com
  cronômetro (fora do cache, imune a replay) dizendo que é a primeira carga da
  hora e quanto leva. `rodape_frescor(dados)` mostra a idade do dado e recarrega
  via `invalidar_cache_dados()` — que limpa os DOIS caches encadeados
  (`carregar_dados` → `_ler_supabase`); limpar só o de fora não recarregaria nada.

## Autenticação (auth.py + auth_store.py + app.usuario)

Login **Google** pelo OIDC nativo do Streamlit (`st.login("google")`/`st.user`) —
não há fluxo OAuth escrito à mão. O callback é a rota `/oauth2callback`, servida
pelo servidor do Streamlit; **não** colide com o `/configuracoes?code&state` das
integrações Bling/Olist (paths diferentes, e aquele bloco no topo da página segue
intacto). Quem autoriza é a allowlist **`app.usuario`** (DDL 006), não o secrets.

- **`auth.py` tem um núcleo PURO** (`resolver_acesso`, `paginas_do_role`,
  `validar_edicao_usuarios`) testável sem Streamlit nem Supabase, e uma casca que
  fala com `st.user`. Estados: `AUTORIZADO` / `INATIVO` / `NAO_AUTORIZADO` /
  `INDISPONIVEL`, cada um com sua tela (sempre mostrando o e-mail logado e um
  botão Sair — o caso comum é ter entrado com a conta Google errada e ficar preso
  ao cookie).
- **Sem auto-cadastro.** E-mail fora da tabela vê "peça acesso ao administrador"
  e **nada é escrito no banco**. O formulário da aba Usuários é a única entrada.
- **Fail-closed com break-glass.** Se a allowlist não puder ser lida
  (`fonte_ok=False`), ninguém entra — exceto os e-mails de
  `st.secrets["acesso"]["admins"]`, que passam antes de qualquer consulta. Essa
  lista é SOBERANA: tirar alguém dela faz parte de revogar o acesso. Dentro de uma
  sessão já autorizada há fail-soft (usa o último acesso conhecido), porque a
  5_Configuracoes chama `st.cache_data.clear()` em vários pontos e um piscar do
  Supabase logo após um "Salvar" expulsaria o próprio admin no meio da edição.
- **Cache da allowlist inteira** (`@st.cache_data(ttl=300)`), não por e-mail: são
  poucas linhas, o `cache_data` é global entre sessões (1 query/5 min para o app
  todo) e a invalidação vira global — `invalidar_cache_usuarios()` ao salvar faz
  o próximo rerun de QUALQUER sessão ler o estado novo, sem esperar o TTL. Use-a,
  nunca o `st.cache_data.clear()` global (levaria o cache de dados de 1h junto).
- **`PAGINAS_POR_ROLE` mora aqui**, e `paginas_do_role()` devolve tupla VAZIA para
  role desconhecido — nunca `None`. O `.get()` que existia no app.py devolvia
  `None` para role fora do mapa, e `None` significa "libera tudo".
- **A tupla de identidade é `(nome, email, role)`** — o 2º elemento é o E-MAIL
  (antes era o `username` do streamlit-authenticator). É ele que vai para
  `atualizado_por`/`congelada_por`/`criado_por` em todas as escritas.
- **Por que não `auth.users` do Supabase:** o app conecta com a `service_key`
  (ignora RLS) e autoriza em Python, então o benefício do GoTrue não seria usado;
  o schema `auth` não é exposto no PostgREST (exigiria a Admin API); e o GoTrue
  não guarda perfil — a doc do próprio Supabase manda criar uma tabela
  companheira, ou seja, `app.usuario` existiria de todo jeito. Ver `docs/decisoes.md`.

## Página de Configuração (5_Configuracoes.py)

**Admin only** — `auth.exigir_admin()` no topo da página.

### Aba 1: Parâmetros Gerais
Formulário organizado pelos **3 subsistemas** da metodologia atual:
- **Comercial (Daily):** status IDs de pedido (em_aberto, em_andamento, pronto_retirada). As **metas saíram do form** — viraram a seção *Metas Mensais* (abaixo)
- **Reposição de Loja:** VM Dinâmico (`config.yaml["vm"]` — cobertura, alta temporada, multiplicador PA, VM mínimo, lead time, nível de serviço, toggle de crescimento) + *fallback* fixo (`logistica.vm_padrao`, `logistica.dias_analise_giro`) usado só quando o SKU não tem giro
- **Produção (Simulador):** Demanda/order-up-to (`config.yaml["demanda"]` — níveis de serviço alta/baixa, variação, janela da alta, toggle de crescimento), Planejamento (lead time, período histórico único — o **calendário de rodadas migrou para o Simulador → Visão Geral**, ao lado das coberturas) e *fallback* da Fábrica (crescimento, cobertura, correção manual global)

Logo abaixo do formulário, seção **🎯 Metas Mensais** (fora do `st.form`, padrão
`data_editor` + botão próprio): pills de Ano + `segmented_control` de Loja (alimentado
por `config["depositos"]["lojas"]` — acabou o hardcode dos nomes), grade 12 meses × 6
colunas (Prata/Ouro/Diamante × Faturamento/PA) com "Realizado ano anterior" read-only
como âncora, atalho *Propor a partir do realizado* e preview de sessão antes de salvar.
Sub-seção **👥 Vendedores por Loja**: atribuição com vigência mensal (`Vendedor · Loja ·
Peso · Ativo`), pré-carregada de `dados["vendedores"]` filtrado a `situacao='A'` e pelo
`id_loja_bling`, com prévia do rateio antes de gravar.

Em seguida, editor independente de **Colégios** (`config.yaml["colegios"]`): tabela com taxa de crescimento e nível de serviço por colégio, descobertos dinamicamente a partir de `detalhes["Marca_sku"]` — valores são sempre input manual do usuário, nunca calculados a partir das vendas.

Ao salvar:
1. Valida estrutura mínima e valores numéricos
2. Grava a Categoria B no **Supabase** via `etl/config_store.py` (`app.parametros` UPDATE + `app.parametros_historico` INSERT — auditoria de quem/quando)
3. Invalida só o cache de config (`carregar_config.clear()`) — **não** o `clear()` global,
   que derrubaria junto a leitura de 1h do Supabase

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

### Aba Usuários
Allowlist de acesso (`app.usuario` via `auth_store.py`). Formulário **➕ Adicionar
usuário** (e-mail Google + nome + perfil) — a única porta de entrada, já que não há
auto-cadastro. Abaixo, `data_editor` em `st.form` com `num_rows="fixed"` (usuário
novo nasce no formulário, nunca no grid: e-mail com typo viraria linha morta que
nunca loga), coluna read-only "Vê hoje" traduzindo o role em páginas, e
`ultimo_acesso` para identificar conta morta. Salvar grava só o **diff**;
`auth.validar_edicao_usuarios` barra antes de gravar as duas formas de lockout
(zero admins ativos; o admin logado se auto-rebaixar/desativar/remover).

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
- Escrever no Supabase fora das portas: `pedidos/repositorio.py` (pedidos), `pedidos/integracoes/repositorio.py` (integrações/tokens), `etl/config_store.py` (parâmetros) e `auth_store.py` (usuários/allowlist); o schema `public` é read-only SEMPRE
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
Authlib
```

`openpyxl` continua porque `scripts/exportar_vm.py` exporta `data/VM_Calculado.xlsx` (não é mais usado para ler parâmetros de entrada — o VM Dinâmico lê tudo de `config.yaml`).

---

## Testes (`tests/`)
Suíte `pytest` focada no **motor de demanda/PCP** (`etl/demanda.py`), nos utilitários e na **paginação paralela** do `loader.py` (`tests/test_loader_paginacao.py` — planejamento de páginas, montagem a partir de lotes fora de ordem, filtro simétrico count/página, retry e cauda, com fake do cliente PostgREST), no **Comercial/metas** (`etl/metas.py` puro + `etl/daily.py` com competência fixa via fixtures `config_daily`/`dados_daily` do conftest — o default é o mês corrente, que não serviria para teste determinístico), no **domínio de pedidos** (`pedidos/` — builder e estados puros; repositório testado com fake do gateway `_inserir/_atualizar/_selecionar/_deletar`, sem Supabase) e nas **integrações/emissão** (`pedidos/integracoes/` + `emissor.py` — payloads puros, OAuth e clientes HTTP com fake injetável, emissor com repo+cliente fakes; zero rede). Sem Supabase/secrets. Instalar dev deps com `uv pip install -r requirements-dev.txt` (o venv usa **uv**, não pip) e rodar `pytest` na raiz. `tests/conftest.py` monta `config` e `dados` sintéticos; o determinismo vem de ancorar as altas em `now()` de forma constante e desligar o crescimento (ver docstring do conftest). Os fakes reusam os `RepoFake` de `test_pedidos_repositorio`/`test_integracoes_repositorio` e o `HttpFake` de `test_integracoes_payloads`. Ao mexer no motor, rode a suíte e atualize os testes junto.

---

## Secrets esperados (streamlit/secrets.toml)
```toml
# Supabase (Project Settings → API)
[supabase]
url = "https://<PROJECT_REF>.supabase.co"
service_key = "<SERVICE_ROLE_KEY>"
schema = "public"
schema_app = "app"   # schema gravável (pedidos de compra) — requer DDL aplicado + schema exposto na Data API

# Autenticação — login Google (OIDC nativo do Streamlit)
# App em console.cloud.google.com → Credentials → OAuth client ID → Web application.
# O redirect_uri TEM de terminar em /oauth2callback e estar cadastrado lá.
[auth]
redirect_uri = "http://localhost:8501/oauth2callback"  # Cloud: https://<app>.streamlit.app/oauth2callback
cookie_secret = "<32+ BYTES ALEATÓRIOS>"               # fixo: trocar desloga todo mundo

[auth.google]
client_id = "<...apps.googleusercontent.com>"
client_secret = "<GOCSPX-...>"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"

# Break-glass: entra como admin mesmo com o Supabase fora. Lista SOBERANA —
# tirar alguém daqui faz parte de revogar o acesso.
[acesso]
admins = ["<email-do-admin>"]
```

**Migrações (`scripts/migrar.py`) NÃO usam secrets.toml.** O Personal Access Token
que aplica DDL é lido de `SUPABASE_ACCESS_TOKEN` — do **arquivo `.env` na raiz**
(git-ignored; `cp .env.example .env` e preencha) ou do ambiente (a env do sistema
vence). Fica fora do runtime do app — o dashboard só carrega a `service_key`
CRUD-only, e nada lê o `.env` além do script. O project ref sai de
`SUPABASE_PROJECT_REF` ou é extraído de `[supabase].url`. Nunca ponha o PAT no
secrets.toml: ele iria para o app e ampliaria o raio de dano (pode `DROP TABLE`).
