# Especificação — Cobertura Alvo por Rodada ("engordar" uma rodada)

**Status:** ✅ IMPLEMENTADA (jul/2026) · **Autor:** planejamento (Diogo) + assistente · **Alvo:** `etl/demanda.py`, `etl/planejamento.py`, `pages/3_Fabrica.py`

> Nota de implementação: a persistência saiu do `config.yaml` e foi para o
> Supabase (`app.parametros`, chave `planejamento.cobertura_override`) — a
> migração de parâmetros (docs/migracao-supabase.md) foi executada junto desta
> feature. Testes em `tests/test_cobertura_alvo.py`. Smoke com dados reais:
> R1 11%→39% (cobre até 12/Jan/27), R2 75%→47%, R3-R5 intactas, Σ produção
> conservada (43.452 pçs).

## 1. Problema

A política *order-up-to* atual dimensiona cada rodada exatamente para cobrir seu
intervalo de proteção `[chegada_r, chegada_{r+1})` + estoque de segurança
(`simular_politica_reabastecimento`, [etl/demanda.py:860](../../etl/demanda.py#L860)).
O planejador **não tem como antecipar produção de propósito** — trazer volume de
uma rodada futura para uma rodada mais próxima — sem mexer nas datas das rodadas
(`planejamento.rodadas_datas`) ou inflar o crescimento global (afeta tudo).

Caso de uso real: *"Estou atrasado no abastecimento. Quero engordar a Rodada 1
para que, quando a Rodada 2 for disparada, o estoque já esteja mais alto e eu não
precise fabricar tanto."*

## 2. Conceito

### 2.1. O botão é um alvo de **cobertura**, não um "+N peças"

O planejador define, por rodada, um **alvo de cobertura em % da demanda anual da
rede** — a mesma unidade já exibida na coluna "% da demanda anual" da Visão Geral
(`pct_anual = producao / demanda_anual`, [etl/planejamento.py:82](../../etl/planejamento.py#L82)).

Internamente isso **estende a janela de proteção** daquela rodada para frente no
tempo, até que a demanda acumulada da rede a partir da chegada da rodada atinja o
alvo. Não é um acréscimo cru de peças: é cobertura, o que garante duas coisas:

1. **Mix correto puxado para frente.** Estender a janela puxa a demanda que
   *realmente* cai no período estendido (ex: a alta de Dez–Fev), respeitando a
   grade de tamanhos e a sazonalidade — e não um % achatado de todo SKU.
2. **Estoque de segurança coerente.** Se a janela estendida passa a conter a
   alta, o flag `contem_alta` vira `True` e o `estoque_seguranca`
   ([etl/demanda.py:29](../../etl/demanda.py#L29)) é redimensionado para o nível
   de serviço da alta (~99%) automaticamente.

### 2.2. Invariante: antecipar **redistribui**, não **infla** o total

Numa política *order-up-to*, a produção total no horizonte é conservada
(≈ demanda anual + segurança final − estoque inicial). Engordar a Rodada 1 **não
cria produção nova** — move *quando* se fabrica. No exemplo real (R1 11%→40%): a
soma R1+R2 permanece ~86%; a R2 cai de 75% para ~46%. O único custo adicional é
carregar estoque por mais tempo.

### 2.3. Absorção: o efeito é local, não propaga

Toda rodada, ao chegar, recompõe o estoque até o seu **próprio** `EstoqueAlvo`
([etl/demanda.py:941](../../etl/demanda.py#L941): `stock = estoque_projetado + pedido`).
Como a Rodada 2 tem alvo próprio (função só da janela dela), ela **absorve
integralmente** a gordura da Rodada 1: produz menos, mas termina no mesmo nível de
estoque de sempre. Da chegada da Rodada 2 em diante, a projeção é **idêntica** à
de antes — Rodadas 3, 4, 5 não mudam.

**Exceção (cascata):** se o override for tão grande que o excedente na chegada da
R2 já ultrapassa o alvo dela (R2 → produção 0 e ainda sobra), o resto escorre para
a R3, que também encolhe, até ser absorvido. Comportamento correto e esperado.

## 3. Modelo de dados (`config.yaml`)

Novo bloco em `planejamento`, **keyed pela data de disparo ISO** (estável no
tempo — sobrevive à renumeração das rodadas conforme as datas passam; casa 1:1 com
`rodadas_datas`):

```yaml
planejamento:
  rodadas_datas:
  - '2026-07-01'
  - '2026-10-01'
  # ...
  # Antecipação deliberada: alvo de cobertura por rodada, em FRAÇÃO da demanda
  # anual da rede (0-1). Ausente ou <= cobertura natural = rodada normal.
  # A rodada seguinte encolhe sozinha (política order-up-to).
  cobertura_override:
    '2026-07-01': 0.40    # engorda a Rodada 1 para cobrir 40% da demanda anual
```

- **Chave:** string ISO idêntica a uma entrada de `rodadas_datas`.
- **Valor:** fração `0 < pct <= 1` (aceitar também `> 1` para pré-build extremo? **Não** nesta v1; clamp em 1.0).
- **Default:** bloco ausente ⇒ comportamento atual, byte-idêntico.

## 4. Mudança no motor (`etl/demanda.py`)

### 4.1. Nova assinatura

```python
def simular_politica_reabastecimento(dados, config, ativo_crescimento=None,
                                     data_hoje=None, dem=None,
                                     cobertura_override: dict = None) -> pd.DataFrame:
```

`cobertura_override=None` ⇒ lê `config["planejamento"]["cobertura_override"]`.
Passar um dict explícito permite **preview ao vivo** na UI sem salvar.

### 4.2. Pré-cálculo (antes do loop de SKUs)

```python
override = cobertura_override
if override is None:
    override = (config.get("planejamento", {}) or {}).get("cobertura_override", {}) or {}

# Curva de demanda mensal da REDE (soma dos SKUs por mês) e total anual
demanda_rede_mes = defaultdict(float)
for dpm in demanda_por_sku.values():
    for m, v in dpm.items():
        demanda_rede_mes[m] += v
demanda_anual_rede = sum(demanda_rede_mes.values())

# Fim da janela de PROTEÇÃO por rodada (estendido se houver override)
fim_protecao = {}
for r in seq:
    B_natural = r["data_chegada_seguinte"]
    pct = float(override.get(r["data_disparo"].strftime("%Y-%m-%d"), 0) or 0)
    pct = min(pct, 1.0)
    if pct <= 0 or demanda_anual_rede <= 0:
        fim_protecao[r["numero"]] = B_natural
        continue
    alvo_unid = pct * demanda_anual_rede
    natural_unid = sum(demanda_rede_mes.get(m, 0) * f
                       for m, f in fracionar_janela_por_mes(r["data_chegada"], B_natural))
    if alvo_unid <= natural_unid:
        fim_protecao[r["numero"]] = B_natural      # clamp: nunca abaixo do natural
    else:
        fim_protecao[r["numero"]] = _data_por_demanda_acumulada(
            r["data_chegada"], alvo_unid, demanda_rede_mes)
```

Helper novo:

```python
def _data_por_demanda_acumulada(inicio, alvo_unid, demanda_rede_mes,
                                horizonte_meses=24):
    """Menor data >= inicio tal que a demanda acumulada da rede desde `inicio`
    atinja `alvo_unid`. Caminha mês a mês (fração linear no mês de corte).
    Cap em `horizonte_meses` para evitar runaway."""
    acc, cursor = 0.0, pd.Timestamp(inicio)
    for _ in range(horizonte_meses):
        fim_mes = (cursor + pd.offsets.MonthEnd(0)).normalize() + pd.Timedelta(days=1)
        dem_mes = demanda_rede_mes.get(cursor.month, 0)
        # ... fração do mês entre cursor e fim_mes, interpola quando cruza o alvo
    return data_alvo
```

*(Implementação exata reaproveitando `fracionar_janela_por_mes` para consistência
com o resto do motor.)*

### 4.3. Uso no loop de SKUs

Uma única troca: o fim do intervalo de proteção passa a ser `fim_protecao`, **não**
`data_chegada_seguinte`. A **consumação entre rodadas continua usando a chegada
real** (`prev → A`) — a rodada seguinte ainda chega na data dela; só o *alvo* da
rodada atual é dimensionado sobre a janela estendida.

```python
        # Intervalo de proteção: da chegada até o fim de cobertura (estendido)
        fim_int = fim_protecao[r["numero"]]
        meses_int = fracionar_janela_por_mes(A, fim_int)      # era r["data_chegada_seguinte"]
        # ... demanda_periodo_alta/baixa, contem_alta, seguranca, estoque_alvo — IDÊNTICOS
```

Nova coluna no DataFrame de retorno: `FimCobertura` (data) e opcionalmente
`CoberturaPct` (o pct aplicado), para a UI exibir/depurar. `data_chegada_seguinte`
permanece (é a chegada real da próxima, usada nos rótulos "cobre até").

> **Nota de acoplamento:** como a mudança é só no cálculo de `estoque_alvo`, a
> auto-absorção da seção 2.3 emerge de graça — a R2 vê `estoque_projetado` mais
> alto e pede menos. Nenhuma lógica extra de "reduzir a próxima".

## 5. Mudança na UI

### 5.1. `etl/planejamento.py::simular_rodadas`

Aceitar e repassar `cobertura_override` para `simular_politica_reabastecimento`
(hoje já aceita `pct_por_rodada` morto na assinatura — reaproveitar/renomear).

### 5.2. Visão Geral (`pages/3_Fabrica.py`)

Na tabela "Rodadas de Produção":

- **Coluna editável "Cobertura alvo (%)"**, pré-preenchida com a cobertura
  **natural** de cada rodada (`pct_anual` atual). `st.data_editor` com
  `column_config.NumberColumn(min_value=<natural>, max_value=100, step=1)`.
- Ao editar, montar `cobertura_override = {data_disparo_iso: pct/100}` (só entradas
  acima do natural) e **re-rodar `simular_rodadas` ao vivo** — a tabela mostra a
  R2 encolhendo e o Investimento migrando de linha, sem salvar ainda.
- **Curva de estoque projetado** (`resultado["estoque_projetado"]`): mostrar a
  curva com override sobreposta à natural — o platô sobe após a chegada da R1 e
  **reconverge exatamente na chegada da R2** (visualiza a absorção).
- Botão **"💾 Salvar plano"** (admin): grava `rodadas_datas` + `cobertura_override`
  juntos em `app.parametros` (Supabase) via `etl/config_store.py` +
  `st.cache_data.clear()`. Preview de sessão vence o salvo até então. (A gravação
  migrou de `config.yaml`/ruamel para o Supabase — ver nota no topo.)

### 5.3. Sugestão por SKU

**Zero mudança na lógica de cálculo** — `processar_fabrica` chama o mesmo
`simular_politica_reabastecimento` sem passar `cobertura_override`, então o motor
lê o do `config` e a aba **reflete o override SALVO automaticamente** (verificado
com dados reais — SES015CAMDIA-PP, R1: **56 → 292** pares após salvar 50%; o NS
sobe de 92% para 99% porque a janela estendida passa a conter a alta).

**Aviso de preview não salvo (jul/2026):** esta aba lê o plano **salvo** (é a
superfície de "Congelar rodada"), então um preview de antecipação/calendário ainda
**não salvo** na Visão Geral não aparece aqui — o que surpreendia o planejador
("mudei a cobertura e a sugestão não reagiu"). Um banner `st.warning` no topo da
aba avisa quando há alterações pendentes (flags `_tem_preview*` hoisted da Visão
Geral para ficarem sempre definidos), instruindo a **Salvar plano** para refleti-las
na sugestão **e** no congelamento. O preview **não** vaza para cá de propósito: o
congelamento recalcula do `config` salvo, então exibir o preview divergiria do que
seria efetivamente congelado.

## 6. Regras e casos-limite

| Caso | Comportamento |
|---|---|
| `pct` ausente / 0 | Rodada normal (byte-idêntico ao atual) |
| `pct` ≤ cobertura natural | Clamp para o natural (no-op) — uma rodada nunca cobre menos que seu próprio intervalo |
| `pct` moderado | R seguinte absorve; rodadas posteriores idênticas |
| `pct` grande (cascata) | Excedente escorre para R+2, R+3… até absorver; produção total conservada |
| `pct` > 1.0 | Clamp em 1.0 (não faz sentido cobrir mais que a demanda anual numa rodada) |
| Janela estendida cruza a alta | `contem_alta=True` ⇒ SS no nível de serviço da alta (correto) |
| Override numa data que não está em `rodadas_datas` | Ignorado (chave órfã); logar aviso na página |

## 7. Testes (`tests/`)

Adicionar a `tests/test_demanda.py` (fixtures sintéticas de `conftest.py`):

1. **Conservação:** `Σ Pedido` no horizonte com override na R1 ≈ sem override
   (tolerância = diferença de SS + arredondamento par).
2. **Absorção local:** override moderado na R1 ⇒ `Pedido_R1` sobe, `Pedido_R2`
   cai, `Pedido_R3+` **inalterados**; estoque projetado pós-R2 idêntico.
3. **Clamp:** `pct` abaixo do natural ⇒ resultado idêntico ao sem override.
4. **Cascata:** `pct` alto ⇒ R2 vai a 0 e R3 encolhe.
5. **SS da alta:** override que estende a janela da R1 até a alta ⇒ `contem_alta`
   e `EstoqueSeguranca` da R1 sobem para o patamar de alta.
6. **Mix:** SKU só-de-alta tem sua demanda puxada para a R1 (janela estendida a
   inclui), não um % achatado.

## 8. Fora de escopo (v1)

- **Override por SKU/colégio** — v1 é global por rodada. Peculiaridades finas
  ficam para depois (poderiam entrar em `excecoes_sku`).
- **Pedidos em trânsito (on-order):** hoje `stock = estoque − backlog`
  ([etl/demanda.py:914](../../etl/demanda.py#L914)), sem termo de pedido já
  disparado e não chegado. Isso é ortogonal a esta feature, mas **necessário para
  que a "hora da verdade" da R2 enxergue a gordura já em produção da R1** quando
  as rodadas se sobrepõem no tempo real. Tratar em spec separada (posição de
  estoque = on-hand + on-order − backorder).
- **Persistência em banco** — segue em `config.yaml` (decisão congelada em
  `docs/migracao-supabase.md`).

## 9. Impacto / risco

- **Backward-compatible:** sem o bloco `cobertura_override`, saída idêntica à atual
  (garantido por teste). Nova coluna no DataFrame é aditiva.
- **Superfície pequena:** ~1 helper + ~25 linhas no motor, 1 param repassado, 1
  coluna editável + 1 botão na UI. Sugestão por SKU não muda.
- **Risco principal:** confundir "cobertura alvo" (nível de estoque pós-chegada)
  com "% de produção" (o que a coluna exibe hoje). Mitigar deixando claro na UI que
  a produção resultante é **≤** o alvo (abate o estoque já projetado).
