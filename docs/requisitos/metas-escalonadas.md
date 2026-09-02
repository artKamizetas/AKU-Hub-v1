# Especificação — Metas Escalonadas (Prata/Ouro/Diamante) por Loja

**Status:** ✅ IMPLEMENTADA (ago/2026) · **Autor:** diretoria (Diogo) + assistente
**Alvo:** `etl/metas.py` (novo), `etl/daily.py`, `pages/1_Daily.py`, `pages/5_Configuracoes.py`, `etl/config_store.py`, `config.yaml`

> **Nota de implementação:** testes em `tests/test_metas.py` (23), `tests/test_daily.py`
> (15) e a classe `TestMetasEscalonadas` de `tests/test_config_store.py`. Validado com
> `streamlit.testing.v1.AppTest` contra o Supabase real: fallback legado e caminho
> completo (metas cadastradas → níveis, rateio e projeção) sem exceções; troca de
> competência para mês fechado confirma projeção = realizado. **Pendência de dados descoberta na
> validação e RESOLVIDA em set/2026:** ver §11.

## 1. Problema

A meta comercial hoje é um único número por loja (`config["daily"]["metas"]`),
igual o ano inteiro, editado por dois `st.number_input` com os nomes das lojas
**hardcoded** no código (`meta_natal`, `meta_mossoró`). Não há como:

- diferenciar a meta de Janeiro (pico) da de Julho (baixa);
- revisar contra que meta um mês *fechado* foi medido (o motor só conhece
  "mês atual", ancorado em `datetime.now()`);
- enxergar a distância entre "bateu a meta" e "estourou a meta" — hoje é
  binário (atingiu ou não).

A tela do Daily também mistura, sem aviso, dois escopos temporais diferentes:
os 6 cards do topo são sempre do mês corrente, mas o filtro de Período logo
acima controla o resto — escolher "Últimos 90 dias" não muda os KPIs de meta,
uma fonte de leitura errada.

## 2. Conceito

### 2.1. Três níveis, não um teto

Cada loja, em cada mês, tem três metas crescentes: **Prata < Ouro < Diamante**.
O "nível conquistado" no mês é o maior nível cujo valor foi ultrapassado
(`—` → Prata → Ouro → Diamante). "Falta" é sempre a distância até o
**próximo** nível, não até o topo — é o número acionável.

### 2.2. Meta é só por loja; colégio e vendedor são leitura, não cadastro

- **Colégio** é puramente um recorte do *realizado* ("dos R$ 70k vendidos em
  Natal, R$ 22k foram NEV") — nunca uma meta cadastrada. Cadastrar meta por
  colégio multiplicaria o volume por ~9 (as marcas distintas em
  `produto_detalhes.marca`) sem ganho proporcional.
- **Vendedor** não tem meta própria digitada — ela é **derivada por rateio**
  da meta da loja (§2.4), porque manter os dois em sincronia manualmente
  (meta da loja = soma das metas dos vendedores) é fonte garantida de
  divergência.

### 2.3. Duas métricas: Faturamento e PA

Faturamento tem os três níveis em R$; PA (Peças por Atendimento) tem os três
níveis em unidades fracionárias (ex: 2,0 / 2,5 / 3,0). Peças e Ticket Médio
continuam existindo como **indicadores exibidos, sem meta própria** — uma
tela com 5 métricas × 3 níveis × 12 meses por loja não se preenche na
prática.

PA **não é aditivo**: agregar PA de duas lojas/vendedores é `Σ peças / Σ
pedidos` (média ponderada), nunca a média dos PAs individuais. E não tem
"run rate" no sentido do faturamento — o PA do mês parcial já *é* a projeção
do PA (a razão não "acelera" como uma soma acumulada acelera).

### 2.4. Atribuição vendedor→loja com vigência mensal e rateio por peso

```
meta_faturamento(vendedor) = meta_faturamento(loja) × peso(vendedor)
                              ────────────────────────────────────────
                              Σ peso(vendedores ativos da loja no mês)

meta_pa(vendedor) = meta_pa(loja)     # razão não se rateia
```

A soma das metas de faturamento dos vendedores de uma loja fecha exatamente
a meta da loja, por construção. `peso` (default 1,0) resolve meio-período —
alguém que entrou dia 15 recebe peso 0,5 sem precisar de um segundo cadastro
de metas.

A atribuição é gravada **por competência** (mês em que o vendedor entrou,
saiu ou trocou de loja) e **herda da competência anterior mais recente**
quando o mês não tem edição — o gestor só mexe quando algo muda; revisar
Março não é afetado por uma contratação de Agosto porque a leitura de Março
busca a última atribuição **≤ Março**, nunca uma futura.

## 3. Modelo de dados

**Decisão de persistência: `app.parametros` (JSONB existente), não tabela
nova.** Dimensionado antes de decidir: meta só por loja em 2 métricas dá
`2 lojas × 12 meses × 2 métricas × 3 níveis` ≈ **144 valores/ano** (~10 KB) —
isso é configuração de baixa frequência de escrita (editada em rodadas, não
o tempo todo), o mesmo perfil de `colegios`/`grupo_segmento` que já vivem
ali. Uma tabela dedicada (DDL + porta de escrita + fakes de teste) custaria
~1,5 dia extra sem ganho proporcional para esse volume. Se o escopo crescer
no futuro (meta por vendedor digitada à mão, por exemplo), reavaliar.

```yaml
daily:
  metas:                        # LEGADO — mantido como fallback
    Natal: 70000.0
    Mossoró: 50000.0

  metas_mensais:                # NOVO — fonte primária
    "2026-01":
      Natal:
        faturamento: {prata: 120000, ouro: 150000, diamante: 180000}
        pa:          {prata: 2.0,    ouro: 2.5,    diamante: 3.0}
      Mossoró:
        faturamento: {prata: 80000,  ouro: 100000, diamante: 125000}
        pa:          {prata: 1.8,    ouro: 2.2,    diamante: 2.6}
    "2026-08":
      Natal:
        faturamento: {prata: 55000, ouro: 70000, diamante: 85000}
        pa:          {prata: 2.0,   ouro: 2.4,   diamante: 2.8}

  vendedores_loja:              # NOVO — atribuição com vigência mensal
    "2026-01":
      "203400111": {loja: "Natal",   peso: 1.0, ativo: true}
      "203400222": {loja: "Mossoró", peso: 0.5, ativo: true}
```

- **Chave de competência:** `"AAAA-MM"` — string ordenável, sem ambiguidade
  de fuso, chave de dict JSON válida.
- **Mês sem entrada em `metas_mensais`** ⇒ **lacuna sinalizada** na tela
  (`origem: "ausente"`), nunca herdada de outro mês — herdar a meta de
  Janeiro em Julho seria pior que não ter meta.
- **`vendedores_loja` herda** da competência anterior mais recente (§2.4) —
  única cascata deste modelo, e é sobre atribuição, não sobre valor de meta.
- Ambos os blocos entram em `CHAVES_PARAMETROS["daily"]` e em
  `CAMINHOS_SUBSTITUICAO` de `etl/config_store.py` — sem isso, apagar um mês
  na UI o ressuscitaria do yaml no próximo `deep_merge`.

### Fallback de compatibilidade

Mês ausente em `metas_mensais` mas presente em `daily.metas` (formato
antigo) usa esse valor como o nível **Ouro** de faturamento, derivando Prata
= 0,85× e Diamante = 1,20×; PA fica sem meta (origem `"ausente"`). Rotulado
como `origem: "estimada"` na tela — não há PA legado para estimar de nada.
Garante que nada quebra no dia do deploy, antes do gestor cadastrar o
calendário de metas novo.

## 4. Motor (`etl/metas.py`, novo módulo puro)

Sem streamlit, sem pandas — só dicts e `datetime`, para ser testável sem
fixtures pesadas.

| Função | Papel |
|---|---|
| `chave_competencia(ano, mes)` | `(2026, 1) -> "2026-01"` |
| `metas_da_loja(config, loja, competencia)` | `{faturamento: {...}, pa: {...}, origem}` |
| `atribuicao_vendedores(config, competencia)` | resolve a herança do mês anterior → `{vendedor_id: {loja, peso, ativo}}` |
| `metas_do_vendedor(config, vendedor_id, loja, competencia)` | aplica o rateio da §2.4 |
| `classificar_nivel(realizado, metas)` | `{nivel, proximo_nivel, falta, pct_do_proximo}` |
| `resumo_faturamento(vendido, metas, dia_atual, dias_no_mes)` | + `run_rate`, `nivel_projetado`, `ritmo_necessario` (`falta ÷ dias_restantes`) |
| `resumo_pa(pecas, pedidos, metas)` | PA = `pecas/pedidos`; sem run rate (§2.3) |
| `agregar_faturamento(lista_de_resumos)` | soma direta |
| `agregar_pa(lista_de_pecas_pedidos)` | `Σpeças / Σpedidos`, reclassifica no fim |
| `competencias_anteriores(competencia, n)` | série de `n` competências até a dada, em ordem |
| `historico_atingimento(config, lojas, realizado, competencias)` | nível conquistado mês a mês; `origem` = `configurada` só quando TODAS as lojas do recorte têm cadastro |
| `aplicar_edicao_metas(metas_mensais, ano, loja, linhas)` | **escrita** — editor → dict; `(novo, n_meses)` |
| `aplicar_edicao_vendedores(vendedores_loja, competencia, linhas, sem_atribuicao)` | **escrita** — editor → dict |
| `copiar_do_ano_anterior(linhas, metas_mensais, ano, loja, fator)` | atalho — traz as metas de `ano-1` × fator |
| `replicar_nos_vazios(linhas)` | atalho — 1ª linha preenchida → todos os meses vazios |

As duas últimas são o **caminho de escrita** em `app.parametros`. Ficam no motor
puro (e não na página) porque um bug nelas corrompe a configuração de todos os
subsistemas — o `salvar_parametros` grava o documento inteiro. A cobertura mira
o que **destrói** dado: mês com as células limpas remove a meta; competência que
ficou sem loja é apagada para não deixar chave órfã; as demais lojas e os demais
anos passam intactos; `NaN` do `data_editor` é célula vazia, mas **zero é meta
válida**.

## 5. Página de Configuração (`5_Configuracoes.py`)

Nova seção **"🎯 Metas Mensais"**, fora do `st.form` principal (mesmo padrão
do editor de Colégios: `st.data_editor` + botão de salvar próprio):

- Pills de **Ano** + `segmented_control` de **Loja**, alimentado por
  `config["depositos"]["lojas"]` (acaba o hardcode de nomes de loja no
  código).
- Grade 12 linhas (Jan…Dez) × 6 colunas (Prata/Ouro/Diamante ×
  Faturamento/PA), com coluna read-only **"Realizado ano anterior"** ao
  lado — a âncora de quem decide o número.
- Atalhos (os três, no motor puro — a página só desenha o botão):
  *✨ Propor a partir do realizado* (realizado de `ano-1` × crescimento),
  *📋 Copiar metas de `ano-1`* (metas já cadastradas × crescimento) e
  *⤵️ Replicar nos meses vazios* (1ª linha preenchida → meses vazios; não
  sobrescreve preenchidos). O campo **Crescimento (%)** alimenta os dois
  primeiros; `0` faz cópia literal.
  **Por que os dois últimos importam:** *Propor* depende do realizado do ano
  anterior, que hoje vem vazio por causa da pendência de `id_situacao_bling`
  (§11). Sem *Copiar* e *Replicar*, o gestor digitaria 72 números à mão.
- Validação: `prata ≤ ouro ≤ diamante`, valores ≥ 0; célula vazia = mês não
  configurado (não grava zero disfarçado de meta).

Sub-seção **"👥 Vendedores por Loja"**: seletor de competência + grade
`Vendedor · Loja · Peso · Ativo`, pré-carregada de `dados["vendedores"]`
filtrado a `situacao == "A"` (10 dos 52 cadastrados) e por `id_loja_bling`
quando existente (20 apontam Natal, 4 Mossoró, 26 vêm sem loja — o editor
avisa quem precisa de atribuição manual). Rodapé mostra a meta individual
resultante do rateio antes de salvar.

## 6. ETL (`etl/daily.py`)

`processar_daily(dados, config, competencia=None)` — `competencia=None` usa
o mês corrente; passar `"2026-03"` reprocessa um mês fechado. Toda a conta
de meta delega para `etl/metas.py`. Retorno passa a ser
`(df_detalhado, df_metas_loja, df_metas_vendedor)`:

- `df_metas_loja`: Loja · Vendido · Meta Prata/Ouro/Diamante · Nível ·
  Próximo Nível · Falta · Run Rate · Nível Projetado · Ritmo Necessário ·
  (mesmas colunas para PA) · Peças · Desconto.
- `df_metas_vendedor`: Vendedor · Loja · mesmas colunas de faturamento e PA.

## 7. Página do Daily (`pages/1_Daily.py`)

Separação explícita dos dois regimes temporais (a maior fonte de leitura
errada hoje):

- **Competência** (mês/ano, permite reabrir meses fechados) rege o bloco de
  metas.
- **Período** (como hoje) rege a análise livre — histórico diário, ranking
  por vendedor/colégio.

Layout:

1. **🎯 Metas da Loja** — bullet chart contínuo por loja + Total (um para
   Faturamento, um para PA): barra do realizado sobre trilha com as três
   faixas Prata/Ouro/Diamante e threshold no nível-alvo. Substitui o gauge
   circular atual (não comporta três metas). KPIs: Vendido · Nível
   conquistado · Falta para o próximo (+ ritmo diário necessário) ·
   Projeção do mês.
2. **📈 Ritmo** — acumulado realizado (área) vs. três linhas de meta
   acumulada ao longo dos dias do mês.
3. **👤 Vendedores** — tabela: Vendedor · Loja · Meta · Vendido · % ·
   Nível · Falta · PA meta · PA real · Nível PA · Peças · Ticket · Pedidos.
4. **🏫 Colégios** — quebra do realizado do mês por colégio (R$ e peças,
   participação %); disponível para a loja e, num expander, por vendedor.
5. **📊 Análise livre (Período)** — status de pedidos, histórico diário,
   rankings — como hoje, com os filtros de loja migrando de checkbox para
   `st.pills` multi.

Paleta: prata `#9AA5B1`, ouro `#D4A017`, diamante `#00B4D8` (validada em
claro e escuro).

## 8. Testes

- `tests/test_metas.py`: classificação nos limites exatos (realizado ==
  ouro), fallback legado, mês ausente, rateio por peso, herança de
  atribuição, agregação de PA por média ponderada (não pela média simples),
  `prata > ouro` rejeitado na validação.
- `tests/test_daily.py` (novo — hoje o Comercial tem zero cobertura):
  `processar_daily` com competência fixa sobre o `dados` sintético de
  `conftest.py`.

## 9. Fora de escopo (v1)

- Meta cadastrada por colégio (§2.2) — só recorte de leitura por ora.
- Meta digitada manualmente por vendedor (é sempre derivada por rateio).

### 9.1. Histórico de atingimento — IMPLEMENTADO (fase 6)

Estava adiado, mas saiu junto: expander **"📅 Histórico de atingimento
(12 meses)"** no bloco de Metas do Daily. Barras do realizado com o emoji do
nível conquistado, sobre as três linhas de meta na rampa neutra.

- **Custo baixo por construção:** um `groupby` no `df_detalhado` (que já traz
  TODOS os pedidos), nunca um `processar_daily` por mês.
- **Lacuna é explícita:** mês sem meta sai com `nivel=None` e falha na linha
  (`connectgaps=False`) — nunca um zero, que pareceria desempenho ruim.
- **Não passa fallback por cadastro:** o painel só compara quando
  `origem == "configurada"`. Com apenas o `daily.metas` legado, a meta seria a
  MESMA nos 12 meses (o valor único não tem competência) e a linha chapada
  passaria por histórico real — então a tela avisa e não desenha. Foi um achado
  da validação com dados reais, não do desenho no papel.

## 10. Impacto / risco

- **Backward-compatible:** sem `metas_mensais` cadastrado, cai no fallback
  do `daily.metas` legado — nada quebra no deploy.
- **Superfície:** 1 módulo novo (~150 linhas), 2 blocos novos em
  `config_store.py`, 1 seção nova de config, refatoração de `daily.py` e
  reformulação de `1_Daily.py`.
- **Risco principal:** confundir "Ouro" com "teto" — a UI precisa deixar
  claro que Diamante é o nível mais alto, não um limite, e que passar dele
  também é celebrado (o bullet não deve "cortar" no topo).

## 11. Pendência de dados — `situacoes_venda` × histórico do espelho

> ✅ **RESOLVIDA em set/2026.** A pipeline aplicou o backfill: zero códigos
> órfãos no espelho e `id_situacao_bling = 9` saltou de 1.371 para 44.088
> linhas (42.540 dos 43.135 pedidos anteriores a mar/2026), confirmando o
> de-para `1 → 9`. **Nenhuma linha de código mudou aqui** — a coluna
> "Realizado ano anterior" voltou a popular, o *Propor* destravou e o aviso
> sumiu, que era o comportamento desenhado para a degradação. O histórico de
> atingimento passou a ter 12 meses reais, com a sazonalidade visível
> (jan/2026 R$ 936k contra jun/2026 R$ 31k). O texto abaixo fica como registro
> do diagnóstico. Fechamento em
> [backfill-situacao-pedidos.md](backfill-situacao-pedidos.md).


Descoberto ao validar a coluna "Realizado ano anterior": o espelho tem o
histórico **até 2025 quase todo em `id_situacao = 1`**, enquanto
`config["daily"]["situacoes_venda"]` é `[9]` (Atendido) — código que só
aparece a partir de 2026.

```
Pedidos por ano × situação (crosstab do espelho, ago/2026)
ano     1      9     outros
2023  8372     0       127
2024  7942     0        43
2025  8327     9        90
2026  4090  1362       114
```

Efeito: a coluna de referência e o botão *Propor a partir do realizado*
ficam sem base para qualquer ano ≤ 2025. Tratado na UI com uma legenda que
**explica a causa** e o botão desabilitado — vazio silencioso enganaria quem
está definindo a meta.

A investigação completa do achado — evidências, hipótese de causa raiz, consultas
para reproduzir e proposta de backfill — está em
**[backfill-situacao-pedidos.md](backfill-situacao-pedidos.md)**, endereçada à
equipe da pipeline Bling→Supabase. Resumo: o código `1` (95% da base) não existe
na tabela de domínio `situacoes_vendas`; ele vem da rotina de **carga em massa**,
enquanto o incremental diário grava os códigos corretos da API v3 — o corte é em
2026-03.

**Do lado do dashboard não há nada a mudar**: `daily.situacoes_venda = [9]` já é o
filtro certo, e a coluna de referência volta a funcionar sozinha quando o backfill
for concluído. Ajustar `situacoes_venda` para incluir `1` seria tratar o sintoma —
e contaminaria também o motor de demanda, que lê a mesma chave.
