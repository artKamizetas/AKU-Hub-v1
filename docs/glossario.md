# Glossário

Termos técnicos e de negócio usados no sistema e na documentação.

## Negócio

| Termo | Significado |
|---|---|
| **Colégio** | Escola atendida com exclusividade. Vem de `detalhes["Marca_sku"]` (ex: NEV, SES, OVD, FAC). |
| **Alta temporada** | Dez-Jan-Fev (Jan é o pico, ~48% do ano; Fev declínio). Volta às aulas. |
| **Baixa** (antes "manutenção") | Meses fora da alta — venda esparsa e pontual. |
| **Grade (de tamanhos)** | Proporção de venda por tamanho de um produto (ex: 40P/100M/60G/20GG). Estrutural e estável. |
| **Rodada** | Uma leva de produção disparada à fábrica em um mês definido. |
| **Backlog** | Peças já vendidas mas não faturadas (situações 6, 15) — ocupam estoque mas estão comprometidas. |
| **Pipeline** | Peças já em produção na fábrica (chegando). Hoje sempre 0 — não alimentado. |
| **VM (Visual Merchandising)** | Quantidade-alvo de exposição de um SKU na prateleira da loja. |
| **Pulmão** | Estoque de segurança do VM (armário da loja) = Fator de Serviço × Desvio-Padrão × √lead time. |
| **CD / Central** | Depósito central (estoque que abastece as lojas). |

## PCP / metodologia (ver [metodologia-pcp.md](metodologia-pcp.md))

| Termo | Significado |
|---|---|
| **Order-up-to (R,S)** | Política de reposição periódica: em cada revisão, repõe até o **Estoque-Alvo**. |
| **DemandaPeriodo** (antes `DI`) | Demanda projetada no intervalo de proteção de uma rodada (da chegada dela até a próxima chegar). Era `DI` — *demand in interval*. |
| **EstoqueSeguranca** (antes `SS`) | Estoque de segurança = `Fator de Serviço × Variação da Demanda × DemandaPeriodo / √meses`. Era `SS` — *safety stock*. |
| **EstoqueAlvo** (antes `S`) | Nível-alvo order-up-to = `DemandaPeriodo + EstoqueSeguranca`. Quanto o SKU **deveria ter** para cobrir o intervalo com margem. |
| **EstoqueProjetado** (antes `OH`) | Estoque projetado do SKU na chegada da rodada (após consumir a demanda até lá). Coluna "Est. Projetado". Era `OH` — *on-hand*. |
| **Pedido** | `par_ceil(max(EstoqueAlvo − EstoqueProjetado, 0))` — o que produzir, já descontado o estoque projetado. |
| **Projeção forward** | Simular o estoque mês a mês pra frente, contando as chegadas das rodadas, para não pedir a mais. |
| **Nível de serviço** | Probabilidade-alvo de não faltar (alta ~99%, baixa ~92%). Define o Fator de Serviço. |
| **Fator de Serviço** (sigla `FS`) | Multiplicador do nível de serviço (99% → 2,33; 95% → 1,65; 92% → 1,41…). |
| **Variação da Demanda** (sigla `VD`) | Coeficiente de variação da demanda mensal (`config.demanda.variacao_demanda`) — alavanca de margem. |
| **Desvio-Padrão** (sigla `DP`) | Dispersão da demanda **diária** em torno da média (medido dos dados). Usado no Pulmão da logística. |
| **lead time** | Tempo entre disparar a reposição/produção e a peça chegar. Na fábrica é a rodada; na logística, o pulmão. |
| **Demanda de alta** (antes `pico_total`) | Vendas reais do SKU na última alta × crescimento — o "topo" que ancora tudo. Coluna `DemandaAlta`. |
| **Demanda de baixa** (antes `maint_total`) | Total do ano fora do pico = `DemandaAlta × proporção da baixa`. |
| **Proporção da baixa** (antes "fator de manutenção") | Quanto a baixa representa da alta (Σbaixa ÷ Σalta). Base **global** (empresa, últimos 2 ciclos ≈ 0,43) + override manual por SKU/colégio (`proporcao_baixa_efetiva`). Ex: 0,43 = baixa vende 43% da alta. |
| **Distribuição da baixa** (antes `shape`) | Que fração da demanda de baixa cai em cada mês (soma = 1). Vem do período histórico. |
| **Período histórico** (chaves `periodo_historico_inicio/fim`) | Janela de vendas passadas que ensina o FORMATO do ano (sazonalidade + base dos SKUs só-de-baixa). Não define o tamanho do pico. |
| **DemandaPeriodoAlta / Baixa** | Split da `DemandaPeriodo` de uma rodada: quanto cai nos meses de pico (dado duro) vs meses de baixa (estimativa). Exposto na Sugestão por SKU. |
| **Crescimento observado** | Camada do crescimento medida dos dados (alta-sobre-alta) por colégio×segmento; baseline sob os overrides manuais (`calcular_crescimento_observado`). |
| **Crescimento híbrido** | Cascata: manual do planejador (vence) → observado dos dados → global. Manual só onde os dados não sabem (expansão futura). |
| **Segmento** | Nível intermediário que agrupa siglas de Grupo (EF1·EF2·EFD → Fundamental; EDF → Ed. Física) para medir crescimento em células estáveis. Mapa em `config.grupo_segmento`. |
| **Calendário explícito** (`rodadas_datas`) | Datas ISO reais de disparo, este ano + próximo, sem repetição automática. A última data só fecha o intervalo da penúltima. |
| **Âncora na alta** | Estimar a demanda a partir das vendas reais da última alta (sinal limpo), não da baixa esparsa. |
| **Ruptura na baixa** | A venda medida na baixa é subestimada por falta de estoque — não é demanda confiável; a alta é o sinal limpo. |
| **Newsvendor** | Modelo de compra única sob incerteza — justifica comprar generoso no pico (uniforme não perece). |
| **Accurate response** | Aposta antecipada menor + rodada reativa no meio do pico (com venda real) — troca estoque por informação (Fisher / Sport Obermeyer). |

## De → Para (siglas antigas → nomes atuais)

Renomeação de jul/2026 (ver [decisoes.md](decisoes.md)). Os **nomes atuais** valem em código, telas e docs; as siglas sobrevivem só como *símbolo estatístico* (rotulado nas tabelas acima) e neste changelog.

| Sigla antiga | Nome atual | Motor |
|---|---|---|
| `DI` | **DemandaPeriodo** | Fábrica / PCP |
| `SS` | **EstoqueSeguranca** | Fábrica / PCP |
| `S` | **EstoqueAlvo** | Fábrica / PCP |
| `OH` | **EstoqueProjetado** | Fábrica / PCP |
| `cv` | **Variação da Demanda** | Fábrica / PCP |
| `z` | **Fator de Serviço** | Fábrica **e** Logística |
| `σ` (sigma) | **Desvio-Padrão** | Logística / VM |
| `LT` | **lead time** | Logística / VM |
| `pico_total` / `PicoTotal` | **demanda_alta** / **DemandaAlta** | Fábrica / PCP |
| `maint_total` | **demanda_baixa** | Fábrica / PCP |
| `fator_manutencao` | **proporcao_baixa** (`calcular_proporcao_baixa`) | Fábrica / PCP |
| `shape_manutencao` / `shape` | **distribuicao_mensal_baixa** / **distribuicao_baixa** | Fábrica / PCP |
| `pico_raw` | **vendas_ultima_alta** | Fábrica / PCP |
| `sazonalidade_inicio/fim` | **periodo_historico_inicio/fim** (config) | Fábrica / PCP |

## Dados / técnico

| Termo | Significado |
|---|---|
| **SKU** | `codigo` do produto = produto × tamanho (ex: FAC103CAIEFM-XGG). |
| **Loja ID** | ID da loja (aparece em Pedidos). ≠ Depósito ID. |
| **Depósito ID** | ID do depósito (aparece em EstoqueV3). |
| **Super_categoria** | Tipo de peça limpo (Camiseta, Calça, Moletom…). Preferir a `categoria` para agrupar. |
| **Grupo** | Campo que mistura série/ensino (EF1, EFM, EME…) e linha de produto (DIA, ESP…). |
| **`limpar_id()`** | Normaliza IDs para string antes de joins (evita `'123.0'`). |
| **PostgREST** | API REST do Supabase usada pelo `loader.py`. |
