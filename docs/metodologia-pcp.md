# Metodologia de PCP — Demanda + Abastecimento

Documento de referência da lógica do **Simulador de Produção** (`pages/3_Fabrica.py`,
motor em `etl/demanda.py`, `etl/fabrica.py`, `etl/planejamento.py`).

> **Uma frase:** demanda **ancorada na alta temporada** + política de reposição
> **order-up-to (R,S) com projeção forward de estoque**, dimensionando margem por
> **nível de serviço** — de modo que **planejar (Visão Geral) e emitir o pedido
> (Sugestão por SKU) são a mesma conta**, em dois níveis.

Fundamentação: *Silver, Pyke & Peterson — Inventory Management and Production
Planning*; *Cachon & Terwiesch — Matching Supply with Demand* (newsvendor);
*Nahmias — Production and Operations Analysis* (aggregate planning); *Fisher —
Accurate Response* (rodada reativa).

---

## 0. As duas camadas

O motor separa **quanto vou vender** de **quanto produzir em cada disparo**:

| Camada | Pergunta | Onde |
|---|---|---|
| **Demanda** (§1) | quanto cada SKU vende, mês a mês? | `calcular_demanda_mensal_por_sku` |
| **Abastecimento** (§2) | quanto pedir em cada rodada? | `simular_politica_reabastecimento` |

A camada 2 só **consome** a curva da camada 1 — nunca inventa demanda.

---

## 1. Estimativa de demanda (ancorada na alta)

Princípio: **a alta define forma e magnitude; a baixa só adiciona volume.** O dado
esparso da baixa nunca define o nível de um SKU (e ainda sofre ruptura de estoque —
ver §6).

Para cada **SKU** (`calcular_demanda_mensal_por_sku`):

```
g(sku) = taxa_crescimento_efetiva(colégio, grupo)         # crescimento híbrido (§4)

Meses de alta (config.demanda.janela_alta, ex Dez/Jan/Fev):
    demanda_alta(sku) = Σ vendas_reais_da_última_alta(sku, mês) × g
    D(sku, mês)       = vendas_reais(sku, mês) × g

Meses de baixa:
    demanda_baixa(sku) = demanda_alta(sku) × proporcao_baixa(super_categoria)
    D(sku, mês)        = demanda_baixa(sku) × distribuicao_baixa(mês)
```

As **três demandas** (colunas do DataFrame: `DemandaAlta`, `DemandaMensalProjetada`):

| Conceito | Fórmula | O que é |
|---|---|---|
| **Demanda de alta** | Σ vendas reais da última alta × g | o topo (dado duro, por SKU) |
| **Demanda de baixa** | demanda_alta × proporção da baixa | total do ano fora do pico |
| **Demanda total** | demanda_alta × (1 + proporção) | soma anual |

- **`última alta`** = última temporada de alta **completa** (toda no passado),
  detectada por `_ultima_temporada_alta` (robusta à ordem da `janela_alta`).
- **`proporcao_baixa`** = quanto a baixa representa da alta (Σbaixa ÷ Σalta), medida
  **global** (empresa inteira, últimos 2 ciclos ≈ 0,43) por `calcular_proporcao_baixa`.
  Cascata de override manual (`proporcao_baixa_efetiva`): `excecoes_sku[SKU] →
  colegios[COL] → global`. **Por que global e não por categoria/SKU:** backtest
  (2023-25) — a baixa tem teto de erro ~48% em qualquer eixo; fatiar mais só
  redistribui o erro (ver §10). O override pega os poucos gigantes de cauda curta
  (tamanho central concentrado no pico, ex: NEV009 real ~0,15 vs global 0,43).
  Ruptura de estoque na baixa é branda e auto-corrige (produto volta em 2-3 meses),
  então o dado do ano compensa — não vale modelo de censura.
- **`distribuicao_baixa`** = fração da demanda de baixa que cai em cada mês
  (`distribuicao_mensal_baixa`), derivada do **peso do período histórico** (§6).
- **Fallback (SKU só de baixa, sem venda no pico):** usa as vendas de baixa recentes
  do SKU como base (senão receberia demanda 0). São **~864 SKUs (~2,9k pçs/ano)** —
  a janela histórica precisa conter meses de baixa, senão eles zeram.

**Validação:** o pico do modelo bate quase exato com a venda real (13.255 vs 13.294
na última alta); a demanda de baixa fica ~20% acima (conservador, volume pequeno).

---

## 2. Política de reposição order-up-to (R,S)

Modelo-texto para quem repõe em **datas fixas** (as rodadas).
`simular_politica_reabastecimento` — motor comum. Por SKU, caminha as rodadas em
ordem cronológica mantendo o **estoque projetado**:

```
Para cada rodada r (chegada A_r, próxima chegada A_{r+1}):

  DemandaPeriodo_r   = demanda no intervalo [A_r, A_{r+1})     # "intervalo de proteção"
      = DemandaPeriodoAlta_r + DemandaPeriodoBaixa_r           # split pico/baixa (exposto na UI)
  EstoqueSeguranca_r = Fator de Serviço × Variação da Demanda × DemandaPeriodo_r / √(meses)
  EstoqueAlvo_r      = DemandaPeriodo_r + EstoqueSeguranca_r   # nível-alvo (order-up-to)

  EstoqueProjetado_r = estoque_atual − consumo(hoje→A_r) + chegadas_anteriores   # projeção forward
  Pedido_r = par_ceil( max(EstoqueAlvo_r − EstoqueProjetado_r, 0) )
```

- **Projeção forward (`EstoqueProjetado_r`)**: o estoque é depletado mês a mês pela
  demanda e reabastecido a cada chegada. Garante que o pedido conte o que as rodadas
  anteriores já entregam — **não pede a mais** (nem duplica entre rodadas). Por isso
  o pedido usa o estoque **projetado na chegada**, não o estoque de hoje.
- **Split alta/baixa** (`DemandaPeriodoAlta`/`Baixa`): a UI mostra quanto da demanda
  do intervalo é pico (dado duro) e quanto é baixa (estimativa macia) — a
  comparação "demanda do período vs vendas da alta" é honesta só sabendo o intervalo.
- **Sugestão por SKU** = `Pedido_r` da rodada selecionada.
- **Visão Geral** = soma bottom-up de `Pedido_r` por rodada.
- Mês parcial na borda é ponderado por dias (`fracionar_janela_por_mes`).

### Estoque de segurança — cuidado com o √tempo

A segurança cresce com a **raiz do tempo**, não linearmente: demanda de N meses é
proporcionalmente mais previsível (pooling). **Sem o `/√meses`**, uma rodada que
cobre 9 meses infla a segurança em ~√9 = 3× (bug corrigido — ver [decisoes.md](decisoes.md)).

### Nível de serviço (a margem)

`config.demanda.nivel_servico_alta` (~99%, "não pode faltar na alta") e
`nivel_servico_baixa` (~92%). O **Fator de Serviço** vem da criticidade do intervalo:
se cobre **qualquer** mês de alta → 99% para o intervalo inteiro. A **Variação da
Demanda** (`config.demanda.variacao_demanda`) é a alavanca de calibração (§7).

**Lente newsvendor:** a compra do pico é quase um compromisso único (não dá pra
repor no meio de Janeiro). Uniforme não perece (sobra barata) e faltar custa venda +
contrato → fractil crítico altíssimo → comprar generoso no pico é correto.

---

## 3. Abatimento de estoque — por SKU, não agregado

O pedido **desconta o estoque existente**, mas **SKU a SKU** (item × tamanho).
Estoque no **tamanho/item errado não abate**. Exemplo real (rodada do pico): ~10k
peças na rede, mas só ~5k abatem o pedido (o resto está preso em SKUs que já excedem
o alvo, ou é consumido na baixa antes da chegada). Exibido explícito: **Est. Projetado**
por SKU (a coluna **Est. Rede** mostra o estoque de hoje, pré-projeção).

---

## 4. Crescimento — híbrido (observado + manual), por colégio × segmento

O crescimento é **centrado no colégio E na série** (colégios expandem turmas em
séries específicas). Cascata de `taxa_crescimento_efetiva(colégio, config, grupo,
ativo, observado)` — **manual do planejador SEMPRE vence os dados**:

```
1. config.colegios[colégio].crescimento_grupos[grupo]   (manual, grupo)
2. config.colegios[colégio].taxa_crescimento            (manual, colégio)
3. observado[colégio].segmentos[segmento(grupo)]        (dados, colégio×segmento)
4. observado[colégio].__geral__                         (dados, colégio)
5. 1 + config.fabrica.crescimento_pct/100               (fallback global)
```

### Camada observada (`calcular_crescimento_observado`)

Mede o crescimento realizado **nas altas** (alta-sobre-alta — sinal limpo, a baixa
tem ruptura), por colégio e por **segmento**. Última transição de alta (ciclo N /
ciclo N−1), **clamp [0,5–2,0]**, **gate de volume ≥ 30 pçs** (senão vira ruído — ex:
DRM Esporte 22× de base 1). Desligável em `config.demanda.crescimento_observado_ativo`
(→ volta ao +10% cego).

### Segmento — nível intermediário configurável

O campo Grupo é fragmentado (EF1·EF2·EFD são todos "fundamental"). O **segmento**
agrupa as siglas em células mais gordas/estáveis. Mapa em `config.grupo_segmento`
(default no código `SEGMENTO_POR_GRUPO`, sobrescrito pelo config; editável na tela):

| Segmento | Grupos |
|---|---|
| Infantil | BER, EIN, IDF |
| Inf+Fund | EIF |
| Fundamental | EF1, EF2, EFD, FDF |
| Médio | EME, PRE, CUR, EFM |
| Tempo Integral | TEI |
| Diário | DIA *(legado SES)* |
| Ed. Física | EDF *(uniforme de todo aluno, ~21% do volume)* |
| Esporte | ESP, EQP *(times/equipe, opcional)* |
| Outros | OPC + não classificados |

### Comportamento

- **Efeito = redistribuição, não inflação.** O total da rede quase não muda (~+11%),
  mas vai pro mix certo. Validado: NEV Médio +10%→**+51%**, LMN +10%→**−29%**,
  IBR Esporte +10%→−34%.
- **Manual só onde você sabe** de algo que os dados não sabem (expansão futura,
  colégio novo). Na matriz da tela, célula deixada **igual ao observado fica viva**
  (re-mede a cada temporada); só o que muda vira override fixo.
- **Casos de borda:** colégio novo com 1ª temporada → mede a partir dela; colégio
  recém-contratado sem histórico → previsão por alunos-por-turma (**futuro, não
  implementado**); OVD rescindiu contrato → âncora vira 0 (demanda ~0 sozinho).
- Vale p/ **fábrica** e **VM de logística**, cada um com toggle liga/desliga.

---

## 5. Rodadas de produção

Cada rodada dispara um pedido que cobre da sua chegada até a **próxima** chegar.
Dois modos de calendário (`_candidatas_rodadas` / `_sequencia_rodadas`):

- **Calendário explícito (recomendado)** — `config.planejamento.rodadas_datas`:
  lista de **datas ISO reais**, deste ano E do próximo, **sem repetição
  automática**. Permite rodada atrasada este ano e antecipada no próximo. A **última
  data só fecha o intervalo da penúltima** (não gera pedido próprio). Chegada =
  disparo + `lead_time_semanas` × 7 dias.
- **Fallback mensal** — `config.planejamento.rodadas` (meses 1–12 que repetem todo
  ano). Só usado se `rodadas_datas` estiver vazio. Chegada em meses.

### Conceito-chave: mais rodadas fatiam o mesmo bolo

A **demanda anual é fixa** (ancorada na alta), independe do nº de rodadas. Mais
rodadas = pedaços menores; a rodada do pico **encolhe** porque deixa de carregar a
demanda de baixa pós-pico — e o intervalo curto paga menos √tempo de segurança.

**Calendário atual** (`rodadas_datas`): Jul/26, Out/26 · Mar/27, Jun/27, Out/27.
A rodada do pico (Out) cobre **Nov→Mar (5 meses)** — as rodadas de Mar/Jun quebram o
antigo intervalo de 9 meses. É o *accurate response*: a exposição do pico encurta,
e uma rodada reativa (disparo no meio do pico, com sell-through real) troca estoque
por informação — o antídoto certo pra alta variação por SKU (§7).

---

## 6. Período histórico — o "formato do ano"

`config.planejamento.periodo_historico_inicio`/`fim` = janela de vendas passadas que
ensina o **FORMATO** do ano, **não o tamanho do pico** (esse vem sempre da última
alta real × crescimento).

Levanta duas coisas, ambas **da baixa**:
1. **Peso sazonal de cada mês** (`calcular_sazonalidade_empresa` → `PesoNormalizado`,
   1,0 = mês médio).
2. **Base dos SKUs só-de-baixa** (`vendas_baixa_recentes`).

**Onde o peso é usado no cálculo:** em **um único ponto** — `distribuicao_mensal_baixa`
descarta os meses de alta, pega só a baixa e re-normaliza para somar 1
→ `distribuicao_baixa`, consumido em `D(sku, mês de baixa) = demanda_baixa ×
distribuicao_baixa[mês]`. O **peso do pico é só exibição** (gráfico de sazonalidade,
coluna "Peso" da Visão Geral) — nunca multiplica demanda. Só as **proporções entre
os meses de baixa** importam.

**Regra prática:** use uma janela **larga (12+ meses, incluindo baixa)**. Janela
estreita (só Dez-Fev) colapsa a distribuição pra uniforme e **zera os ~864 SKUs
só-de-baixa**. (`calcular_sazonalidade_por_colegio` existe mas é legado — sem
chamador no modelo atual.)

---

## 7. Variação da Demanda (o CV da segurança)

`config.demanda.variacao_demanda` calibra o estoque de segurança. **Medido dos dados**
(erro real da âncora "alta seguinte = alta anterior × crescimento", 2019→2026):

| Nível | CV medido |
|---|---|
| Empresa (agregado) | ±8% |
| SKU banda A (alto giro) | ~0,60 |
| SKU banda B | ~0,74 |
| SKU banda C (baixo giro) | ~0,85 |

O agregado é previsível; **o caos está no mix por SKU** (0,25 no config está
subestimado). Conclusão estratégica: proteger CV ~0,6 a 99% via estoque é
antieconômico — a resposta é **encurtar a exposição** (rodada reativa, §5), não
inflar segurança.

---

## 8. Parâmetros (config.yaml)

```yaml
demanda:
  janela_alta: [12, 1, 2]              # meses da alta (ordem cronológica)
  nivel_servico_alta: 99               # %
  nivel_servico_baixa: 92              # %
  variacao_demanda: 0.25               # CV → tamanho da segurança (calibrar, ver §7)
  aplicar_crescimento_fabrica: true
  crescimento_observado_ativo: true    # camada observada; false = +10% cego
  proporcao_baixa_override: {}         # {super_categoria: valor}; vazio = medir

grupo_segmento:                        # mapa grupo→segmento (editável na tela)
  EFD: Fundamental
  EME: Médio
  EDF: Ed. Física
  # …

planejamento:
  rodadas_datas: ["2026-07-01","2026-10-01","2027-03-01","2027-06-01","2027-10-01","2028-03-01"]
  rodadas: [7, 10]                     # fallback mensal (só se rodadas_datas vazio)
  lead_time_semanas: 4
  periodo_historico_inicio: "2025-03-01"   # janela LARGA (formato do ano, §6)
  periodo_historico_fim:    "2026-02-28"

colegios:                              # overrides MANUAIS (vencem o observado)
  NEV:
    taxa_crescimento: 1.10
    nivel_servico: 95
    crescimento_grupos: {EME: 1.6}     # ex: expansão de turma no médio
```

## 9. Auditoria / rastreabilidade

`python scripts/memoria_calculo_fabrica.py <SKU>` imprime o passo a passo order-up-to de um
SKU (âncora na alta → demanda mensal → DemandaPeriodo / EstoqueSeguranca /
EstoqueAlvo / EstoqueProjetado → pedido por rodada).

## 10. Pendências conhecidas

- **Calibração da Variação da Demanda** (0,25 → medido ~0,6 por SKU). Ajuste fino.
- **Proporção da baixa** resolvida: global + override manual (backtest fechou que
  granularidade não melhora — teto de erro ~48% em qualquer eixo, e a baixa rola das
  sobras no calendário limpo, baixa alavanca). Ver [decisoes.md](decisoes.md).
- **Previsão por alunos-por-turma** para colégios recém-contratados — não implementado.
- **Pipeline** (ordens em produção) sempre 0 — não alimentado.
