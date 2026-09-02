# Especificação — Carregamento de dados: velocidade e feedback ao usuário

**Status:** ✅ IMPLEMENTADA (set/2026) · **Autor:** diretoria (Diogo) + assistente · **Alvo:** `etl/loader.py`, `ui_carga.py` (novo), `pages/0_Home.py`, `pages/1_Daily.py`, `pages/2_Logistica.py`, `pages/3_Fabrica.py`, `pages/5_Configuracoes.py`

> **Nota de implementação (02/set/2026).** Resultado medido ponta a ponta:
> `carregar_dados()` **117,5 s → 10,8 s**, com os mesmos DataFrames. As 6
> páginas foram exercitadas em runtime real (`streamlit.testing.v1.AppTest`)
> sem exceção. Três desvios do plano original, todos documentados nas seções
> correspondentes: (1) a barra de progresso por página **não foi possível** —
> element replay do `st.cache_data`, §4.2; (2) entrou a vetorização da conversão
> de datas, que virou o gargalo assim que a rede encolheu, §3.4; (3) entrou
> `fingerprint_config()`, exigido pela Etapa 3, §5.2. Testes em
> `tests/test_loader_paginacao.py` (21 casos).

> **Medições desta spec** foram feitas em 02/set/2026 contra o Supabase de
> produção, a partir do ambiente de dev (WSL). Números do Streamlit Cloud podem
> diferir pela latência da rede — o *shape* do problema (serial × paralelo) não.

## 1. Problema

O usuário relata que "várias telas demoram e não fica claro se o sistema travou".
A hipótese natural era que os motores de cálculo (demanda / order-up-to /
metas) fossem pesados. **A medição mostra o contrário:**

| Etapa | Tempo | Onde |
|---|---|---|
| **`carregar_dados()` — leitura do Supabase** | **117,5 s** | **todas as páginas** |
| `processar_daily()` | 5,0 s | 1_Daily, 5_Configuracoes |
| `simular_rodadas()` ×2 | 3,4 s | 3_Fabrica (Visão Geral) |
| `processar_fabrica()` | 1,8 s | 3_Fabrica (Sugestão por SKU) |
| `carregar_config()` | 1,2 s | todas |
| `calcular_vm_por_sku()` + `processar_logistica()` | 0,5 s | 2_Logistica |
| `calcular_sazonalidade()` | 0,02 s | 3_Fabrica |

**99% do tempo é I/O, não CPU.** Portanto: melhorar só o feedback trataria o
sintoma quando a causa tem correção barata e medida. Esta spec faz as duas
coisas, nesta ordem — primeiro o loader (que também é o que *habilita* o
progresso honesto), depois a UI.

### 1.1. Por que 117,5 s

`_ler_tabela` ([etl/loader.py:212](../../etl/loader.py#L212)) pagina **em série**,
1.000 linhas por request. `_ler_supabase`
([etl/loader.py:254](../../etl/loader.py#L254)) paraleliza **entre tabelas**
(4 workers), mas não **entre páginas** — então o relógio de parede é a corrente
serial da maior tabela:

- `itens` = 165.775 linhas → **166 idas e voltas sequenciais** (~0,7 s cada)
- `pedidos` = 44.707 linhas → 45 requests sequenciais

Aumentar a página **não funciona**: o Supabase corta em 1.000 linhas
(`max-rows`). Medido: `range(0,49999)` devolve 1.000 linhas.

### 1.2. Dois desperdícios confirmados

1. **41% de `itens` é baixado e jogado fora.** Das 165.775 linhas, **68.710 têm
   `id_pedido_bling` nulo** e morrem no `dropna(subset=["ID_pedido"])` de
   [etl/loader.py:375](../../etl/loader.py#L375). Um filtro server-side derruba
   de 166 para 98 páginas — **mesmo DataFrame final**.
2. **O teto de conexões do cliente HTTP é 10**
   ([etl/loader.py:202](../../etl/loader.py#L202)), o que limita qualquer
   paralelização futura.

### 1.3. O agravante: `st.cache_data.clear()` global

`st.cache_data.clear()` aparece **9× na 5_Configuracoes** e 1× na
[3_Fabrica.py:428](../../pages/3_Fabrica.py#L428). É **global**: derruba junto o
cache de 1 h do `carregar_dados`. Ou seja, **todo "Salvar" de parâmetro condena a
próxima navegação a pagar a carga fria inteira** — sem nenhuma pista de que foi o
salvamento que causou. O [auth.py:165](../../auth.py#L165) já documenta essa
armadilha para o cache da allowlist; a lição não foi aplicada aos parâmetros.

### 1.4. Buracos de feedback (revisão do front)

| Tela | Situação hoje |
|---|---|
| 0_Home, 1_Daily, 2_Logistica, 3_Fabrica | `st.spinner("Carregando dados...")` genérico, **~2 min**, sem progresso nem expectativa |
| 1_Daily | `processar_daily` (5 s) **sem spinner e sem cache** ([1_Daily.py:158](../../pages/1_Daily.py#L158)) — trocar a Competência congela a tela em silêncio |
| 3_Fabrica | `simular_rodadas` ×2 (3,4 s) e `_processar` (1,8 s) **sem spinner** ([:236](../../pages/3_Fabrica.py#L236), [:613](../../pages/3_Fabrica.py#L613)) — toggle de crescimento / calendário / cobertura dão cache miss silencioso |
| 5_Configuracoes | `_realizado_mensal()` roda `processar_daily` (5 s) com `show_spinner=False` deliberado ([:446](../../pages/5_Configuracoes.py#L446)) |
| 2_Logistica | spinner para uma operação de **0,5 s** — ruído onde não precisa |
| 4_Pedidos | ✅ **referência**: `st.status` com progresso nas emissões. É o padrão a replicar |

## 2. Resultado alvo (medido em protótipo)

| Cenário | Hoje | Alvo | Validação |
|---|---|---|---|
| Carga fria completa (9 tabelas) | 117,5 s | **10,5 s** | protótipo executado, 16 workers |
| Páginas buscadas | 230 | **162** | filtro server-side em `itens` |
| Linhas de `itens` entregues | 97.058 | **97.065** | idênticas (Δ7 = escrita da pipeline durante o teste) |
| Duplicatas por paginação | risco | **0** | `.order("id")`, `id.nunique() == len(df)` |

Curva de paralelismo medida (só `itens`, 98 páginas):

| workers / max_connections | tempo |
|---|---|
| 8 / 10 | 8,85 s |
| **16 / 20** | **5,38 s** ← escolhido |
| 24 / 30 | 5,15 s |
| 32 / 40 | 4,89 s |

Retorno decrescente após 16. Escolha: **16 workers, `max_connections=20`** —
pega ~90% do ganho sem abusar do pool do PostgREST (que é compartilhado com a
pipeline externa de escrita).

---

## 3. Etapa 1 — Loader paralelo e instrumentado (`etl/loader.py`)

**Insight que define o desenho:** paralelizar e reportar progresso são a **mesma
refatoração**. Instrumentar o loader serial entregaria uma barra que leva 117 s;
o desenho abaixo entrega uma barra honesta de 10 s. Por isso não são fases
separadas.

### 3.1. Fila plana de páginas

Substituir "cada worker lê uma tabela inteira em série" por
"**contar tudo → montar a lista completa de páginas → uma pool só**":

```python
_PAGE_SIZE = 1000        # teto do PostgREST (max-rows) — não aumentar, é servidor
_MAX_WORKERS = 16
_TENTATIVAS_PAGINA = 3

# Colunas que o loader descarta logo depois (dropna) — empurradas p/ o servidor.
# A MESMA condição vale para o count e para as páginas, senão os offsets
# desalinham e a leitura fica furada.
FILTROS_NAO_NULOS = {"itens": ("id_pedido_bling",)}
```

Fluxo de `_ler_supabase`:

1. **Contar** as 9 tabelas em paralelo (`select("id", count="exact").limit(1)`,
   com o filtro aplicado). Medido: **0,4–1,5 s** no total.
2. **Planejar** a lista de jobs `[(tabela, offset), ...]` — 162 no estado atual.
3. **Buscar** todos os jobs numa `ThreadPoolExecutor(16)`, consumindo com
   `as_completed()` **na thread principal**.
4. **Drenar a cauda**: se a página de maior offset de uma tabela voltou com
   `_PAGE_SIZE` linhas exatas, continuar sequencialmente a partir de `count` até
   uma página curta (cobre linhas inseridas pela pipeline entre o count e a
   leitura).
5. **Concatenar** e `drop_duplicates(subset=["id"])` como rede de segurança.

Cada página usa `.order("id")` — **obrigatório**. Sem `ORDER BY` explícito o
PostgREST não garante ordenação estável entre requests, e páginas concorrentes
podem duplicar ou pular linhas. Validado: todas as 9 tabelas têm coluna `id`.

### 3.2. Resiliência (risco NOVO introduzido pelo fan-out)

Hoje uma falha de rede quebra 1 de 9 requests grandes. Com 162 páginas, a
probabilidade de um 5xx transitório sobe. **Cada página deve ter retry próprio**
(`_TENTATIVAS_PAGINA = 3`, backoff 0,5 s → 1,5 s) antes de propagar. Só depois de
esgotadas as tentativas o erro sobe para o `try/except` de `carregar_dados`
([etl/loader.py:326](../../etl/loader.py#L326)), que já degrada com `st.error`.

Elevar `max_connections` de 10 → 20 e `max_keepalive_connections` de 5 → 20 em
[`_conn_supabase`](../../etl/loader.py#L177). `http2=False` permanece — o
comentário existente sobre race em HTTP/2 vale ainda mais com 16 threads.

### 3.3. Contrato de progresso

```python
# etl/loader.py
def carregar_dados(_progresso=None) -> dict: ...
def _ler_supabase(_progresso=None) -> dict: ...
```

- **Assinatura do callback:** `_progresso(feitas: int, total: int, etapa: str)`.
  `etapa` é rótulo humano em pt-BR (`"Itens"`, `"Pedidos"`, `"Contando registros"`).
- **O prefixo `_` é obrigatório**: é a convenção do Streamlit para excluir um
  argumento do hash da cache key. Sem ele, cada callback novo (objeto diferente a
  cada rerun) seria uma chave nova e **o cache nunca acertaria** — trocaríamos
  2 min ocasionais por 2 min sempre.
- **Só a thread principal chama o callback**, dentro do laço `as_completed()`. Os
  workers nunca tocam em `st.*` — chamada de Streamlit fora do
  `ScriptRunContext` não renderiza e polui o log com warning.
- **Cache hit não emite progresso**: o corpo da função não executa, o callback
  nunca é chamado. Correto — não há o que reportar quando é instantâneo.
- `_progresso=None` (default) mantém o loader utilizável fora do Streamlit
  (`scripts/*.py`, testes) sem nenhuma mudança de chamada.

### 3.4. Vetorização da conversão de datas (não estava no plano)

Com a rede em ~11 s, a transformação virou o gargalo seguinte: 6,2 s, dos quais
**5,8 s eram `converter_data_flexivel` aplicado linha a linha** nas 140k linhas
de Pedidos+Itens. Entrou `converter_serie_data(serie)`: uma passada vetorizada
em `%Y-%m-%d` (formato de 100% do espelho) e fallback elemento a elemento só no
que falhar — semântica idêntica, coberta por teste de equivalência. 6,2 s → 0,4 s.

`converter_data_flexivel` fica intacta: é a referência do fallback e já tinha
testes próprios.

### 3.5. Critérios de aceitação — Etapa 1

- [x] Mesmas colunas e contagens: `itens` 97.058 → 97.076, `pedidos` 44.706 →
      44.709, demais idênticas (a diferença é escrita da pipeline no intervalo).
- [x] Carga fria **10,78 s** (era 117,5 s).
- [x] `dados["itens"]["ID_pedido"]` sem nulos; `id.nunique() == len(df)`.
- [x] `carregar_dados()` sem `_progresso` funciona idêntico (scripts CLI).
- [x] Nenhum warning de `ScriptRunContext` vindo de thread worker (verificado
      capturando o logger por nome de thread num run de `AppTest`).
- [x] `pytest` verde — 451 testes, 21 novos.

---

## 4. Etapa 2 — UI de carregamento (`ui_carga.py` + 4 páginas)

### 4.1. Módulo compartilhado

Novo módulo **na raiz**, seguindo a convenção de `auth.py` / `auth_store.py`:

```python
# ui_carga.py
def carregar_com_feedback() -> tuple[dict, dict]:
    """Config + dados com st.status de progresso. Substitui o bloco
    `_carregar()` + `st.spinner` duplicado nas 4 páginas."""
```

Isso **elimina a duplicação atual**: as 4 páginas repetem a mesma função
`_carregar()` e o mesmo `with st.spinner("Carregando dados...")`. Passam a fazer:

```python
from ui_carga import carregar_com_feedback
dados, config = carregar_com_feedback()
```

### 4.2. O que a tela mostra — e por que NÃO é uma barra de progresso

**O plano previa barra de progresso por página. Não foi possível.** O Streamlit
fecha as duas portas:

- desenhar **dentro** da função cacheada funciona na carga fria, mas o
  `st.cache_data` grava esses elementos e os **reproduz no cache hit**
  (*element replay*): a barra reaparecia pronta em toda carga quente de 0,12 s —
  exatamente o "piscar" que o critério de aceite proibia. Confirmado em
  `AppTest`: run quente vinha com `progress: 1 | status: 1` e o texto
  `**Finalizando** · bloco 162 de 162`;
- desenhar num bloco criado **fora** e preenchido de dentro é **proibido** —
  `CacheReplayClosureError: a streamlit element is called on some layout block
  created outside the function`.

A saída restante seria carregar numa thread e fazer *polling* do progresso na
thread principal. Isso é máquina demais para uma carga que passou a levar ~10 s:
a decisão é **spinner com cronômetro**, criado fora do cache, imune a replay.

```
⏳ Lendo dados do Bling (Supabase) — primeira carga da hora, ~15s.
   As próximas são instantâneas.                              (0:07)
```

Duas decisões de conteúdo sobrevivem intactas do plano, e são o que de fato
converte "travou" em "está trabalhando":

1. **Dizer que é a primeira carga da hora** — o usuário não sabe que há cache.
2. **Dizer quanto costuma levar** — expectativa numérica vale mais que animação.
   `SEGUNDOS_ESPERADOS = 15` é arredondado para cima de propósito: prometer
   menos do que se entrega é o que mantém a mensagem confiável.

O `_progresso` do loader **continua existindo e testado** — serve a chamadores
fora do Streamlit (scripts CLI), onde replay não existe.

### 4.3. Frescor dos dados

Rodapé discreto em cada página (`rodape_frescor`):

```
Dados lidos às 14:22 · releitura automática às 15:22        [↻ Recarregar]
```

Torna o cache **visível** em vez de mágico — sem isso o usuário não tem como
saber se o número na tela é de agora ou de 50 minutos atrás, o que é uma questão
de confiança no dado, não só de conforto.

`_ler_supabase` grava `todas_abas["_carregado_em"]` e `carregar_dados` o repassa
em `dados["carregado_em"]`. A chave tem prefixo `_` no dict interno para ficar
fora do laço de validação, que itera sobre `SCHEMA`.

O botão chama **`invalidar_cache_dados()`**, não `carregar_dados.clear()`: são
DOIS caches encadeados (`carregar_dados` → `_ler_supabase`), e limpar só o de
fora releria o cache de dentro — a "recarga" não recarregaria nada.

### 4.4. Critérios de aceitação — Etapa 2

- [x] As 4 páginas usam `carregar_com_feedback()`; nenhuma tem `_carregar()` próprio.
- [x] Em cache frio o spinner aparece com cronômetro e a expectativa de tempo.
- [x] Em cache quente **nada pisca** — verificado em `AppTest`: run quente tem
      `progress: 0 | status: 0` e nenhum markdown residual.
- [x] O rodapé mostra hora coerente; clicar em ↻ Recarregar não levanta exceção.

---

## 5. Etapa 3 — Invalidação cirúrgica de cache

Trocar os **10 `st.cache_data.clear()` globais** por invalidação da função certa:

| Local | Hoje | Deve ser |
|---|---|---|
| 5_Configuracoes (9×) — salvar parâmetros/metas/colégios/exceções | `st.cache_data.clear()` | `carregar_config.clear()` |
| 3_Fabrica:428 — salvar plano de rodadas | `st.cache_data.clear()` | `carregar_config.clear()` |
| 0_Home:76 — "Forçar recarga de cache" | `st.cache_data.clear()` | **manter global** (é a intenção explícita do botão) |

Salvar um parâmetro deixa de custar uma carga fria inteira na próxima tela.

**Cuidado a verificar caso a caso:** algum dos 9 pontos pode depender de derrubar
um cache *derivado* junto (ex.: `_realizado_mensal` da própria 5_Configuracoes,
que tem TTL de 600 s e consome `carregar_dados`). Onde houver essa dependência,
invalidar as duas explicitamente (`carregar_config.clear()` +
`_realizado_mensal.clear()`) — nunca voltar ao global.

Complemento: após qualquer `clear()`, `st.toast("Parâmetros salvos — a próxima
tela vai reler os dados")`, para a lentidão deixar de parecer aleatória.

### 5.1. Efeito colateral obrigatório: `fingerprint_config()`

A Etapa 3 **quebraria** os caches de página se aplicada sozinha. `_processar`
(2_Logistica, 3_Fabrica) e o novo `_processar_daily` (1_Daily) recebem o config
como `_config` — fora do hash, porque dict não é hashável. Enquanto a
invalidação era global, isso não fazia falta: salvar derrubava tudo junto. Com o
clear cirúrgico, o resultado ficaria preso ao config **antigo** até o TTL — o
gestor salvaria uma meta e não veria efeito nenhum.

Entrou `loader.fingerprint_config(config)`: sha256 curto do config efetivo,
passado como argumento **sem** underscore (é o único que ENTRA na cache key).
Salvar → `carregar_config.clear()` → próximo read gera assinatura nova → os
caches de página perdem e recalculam. Semântica exata, e sem derrubar a leitura
do Supabase junto.

### 5.2. Critérios de aceitação — Etapa 3

- [x] 9 pontos de "Salvar" trocados; 2 botões "Forçar recarga" seguem globais.
- [x] Salvar parâmetro e navegar **não** dispara carga fria (o cache de dados
      de 1 h não é mais tocado).
- [x] O valor salvo aparece no rerun seguinte — garantido por
      `fingerprint_config`, não mais pelo efeito colateral do clear global.


## 6. Etapa 4 — Spinners nos cálculos

Depois da Etapa 1, os cálculos passam a ser a maior espera restante. Regra:
**acima de ~1 s, feedback nominal; abaixo, silêncio.**

| Local | Ação |
|---|---|
| [1_Daily.py:158](../../pages/1_Daily.py#L158) `processar_daily` (5 s) | `st.spinner(f"Recalculando metas de {rotulo_comp}…", show_time=True)` **e** envolver num `@st.cache_data` com chave `(competencia)` — hoje recalcula do zero a cada troca de mês |
| [3_Fabrica.py:236](../../pages/3_Fabrica.py#L236) `simular_rodadas` ×2 (3,4 s) | `st.spinner(f"Simulando {len(datas_rodadas)} rodadas…", show_time=True)` |
| [3_Fabrica.py:613](../../pages/3_Fabrica.py#L613) `_processar` (1,8 s) | `st.spinner("Calculando sugestão da rodada…", show_time=True)` |
| [5_Configuracoes.py:446](../../pages/5_Configuracoes.py#L446) `_realizado_mensal` (5 s) | trocar `show_spinner=False` por `show_spinner="Levantando o realizado dos últimos meses…"` |
| [2_Logistica.py:37](../../pages/2_Logistica.py#L37) `_processar` (0,5 s) | **remover** o spinner — pisca e gera ruído |

**Resultado medido** (2ª renderização da mesma página, cache de dados quente):

| Página | Antes | Depois |
|---|---|---|
| 1_Daily | 7,16 s | **0,32 s** ← o cache por competência |
| 2_Logistica | 0,92 s | **0,33 s** |
| 3_Fabrica | 7,64 s | **5,69 s** |

A 3_Fabrica melhora pouco porque `simular_rodadas` (×2, ~3,4 s) **não é
cacheável de forma trivial**: depende do preview de sessão (datas e coberturas
em edição), e o `cobertura_override` é um dict — não hashável. Ganhou spinner
nomeado, que era o escopo desta etapa. Cachear a simulação fica como
oportunidade medida e separada.

`show_time=True` está disponível no Streamlit 1.59 (versão do venv) e mostra
cronômetro — o sinal mais barato de "vivo".

> **Ganho extra do cache em `processar_daily`:** a 1_Daily hoje paga 5 s a cada
> mudança de Competência *e* a cada rerun de qualquer widget da página. Com
> cache por competência, só a primeira visita a cada mês custa.

---

## 7. Testes

Toda a lógica nova de paginação é **pura** (dado `count` e `_PAGE_SIZE`, produzir
a lista de jobs) e deve ser extraída para poder ser testada sem rede, no espírito
de `tests/test_loader_utils.py` (que já testa helpers puros do loader).

**`tests/test_loader_paginacao.py`** (novo):

- `planejar_paginas({"itens": 165775}, tamanho=1000)` → 166 jobs, offsets
  `0, 1000, …, 165000`.
- Contagem exata múltipla da página (ex.: 2000) → 2 jobs, **sem** job vazio extra.
- Contagem 0 → 1 job (a tabela pode ter sido esvaziada; a leitura confirma).
- Ordem dos resultados **não** importa: alimentar o montador com os lotes
  embaralhados e conferir que o DataFrame final tem as linhas certas
  (`as_completed` não preserva ordem — este é o teste que protege a mudança).
- Cauda: último lote com `_PAGE_SIZE` linhas exatas → dispara a drenagem
  sequencial.
- Dedup: lotes com `id` repetido (simulando drift) → uma linha por `id`.
- `FILTROS_NAO_NULOS` aplicado igual no count e na página (guarda contra o
  desalinhamento de offset, que é o modo de falha mais perigoso).

**Fake de cliente PostgREST** no espírito dos `RepoFake` / `HttpFake` já usados
na suíte: um objeto que devolve fatias de uma lista em memória e conta requests
— **zero rede, zero Supabase**, como o resto da suíte.

O callback de progresso também é testável sem Streamlit: passar um `list.append`
como `_progresso` e verificar que `feitas` é monotônica e termina em `total`.

---

## 8. Fora de escopo (v1)

- **Pré-aquecer o cache no login** (carregar enquanto o usuário lê a tela de
  boas-vindas). Bom ganho percebido, mas exige repensar onde a carga dispara —
  vale como v2, depois de medir se 10 s ainda incomodam.
- **`@st.fragment` nos blocos caros da 3_Fabrica** para o toggle de crescimento
  não rerodar a página inteira. Depende do resultado da Etapa 4.
- **Cache em disco / materialização** das agregações pesadas no Supabase.
  Desnecessário: com 10 s de carga, o problema deixa de existir.
- **Corrida de duas sessões no cache frio** (dois usuários entram juntos, ambos
  calculam). Não medido nesta rodada — a se verificar se o `st.cache_data` já
  serializa; hoje custa 2 min ×2, depois custará 10 s ×2, o que reduz muito a
  urgência.
- **Filtrar `produtos` por `situacao='A'` no servidor.** São 3 páginas
  (desprezível) e mudaria a semântica coberta por `tests/test_ativos_filtro.py`.

---

## 9. Impacto / risco

| Mudança | Risco | Mitigação |
|---|---|---|
| Paginação paralela | Duplicar/pular linhas | `.order("id")` + `drop_duplicates` + teste de ordem embaralhada |
| Fan-out 162 requests | 5xx transitório derruba a carga | retry por página (3×, backoff) |
| Filtro server-side em `itens` | Filtro no count ≠ filtro na página → offsets furados | constante única `FILTROS_NAO_NULOS` usada nos dois pontos + teste dedicado |
| `max_connections` 10 → 20 | Pressão no pool compartilhado com a pipeline de escrita | 16 workers (não 32); só leitura; monitorar |
| `_progresso` no cache | Argumento entrar no hash e matar o cache | prefixo `_` (convenção Streamlit) + critério de aceite explícito |
| `clear()` cirúrgico | Esquecer um cache derivado → tela mostra valor velho | critério de aceite "o valor salvo aparece"; revisar os 9 pontos um a um |
| `dados["carregado_em"]` | Chave nova quebrar validação/consumidores | chave prefixada, fora do laço do `SCHEMA`; rodar `pytest` |

**Nada aqui muda** o schema `app`, o `requirements.txt`, a estrutura de retorno
dos DataFrames de negócio ou nomes de colunas — as quatro coisas que o
`CLAUDE.md` marca como "não faça sem perguntar". A única adição ao dict de
`carregar_dados()` é a chave auxiliar `carregado_em`.

## 10. Ordem de execução sugerida

1. **Etapa 1** (loader) — maior ganho, isolada em um arquivo, coberta por testes novos.
2. **Etapa 3** (invalidação cirúrgica) — barata e independente; tira a pior armadilha.
3. **Etapa 2** (UI de carregamento) — depende do contrato de progresso da Etapa 1.
4. **Etapa 4** (spinners de cálculo) — cosmética depois que o resto encolheu.

Etapas 1 e 3 juntas já resolvem a queixa original; 2 e 4 são o acabamento.
