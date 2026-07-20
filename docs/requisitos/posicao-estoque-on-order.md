# Especificação (EXPLORAÇÃO) — Posição de Estoque com On-Order + Reconciliação com o Tiny

**Status:** 🟡 EM DISCUSSÃO — não implementar ainda · **Autor:** diretoria (Diogo) + assistente · **Alvo provável:** `etl/demanda.py`, `etl/loader.py`, `pedidos/`, `pages/3_Fabrica.py` + `pages/4_Pedidos.py`

> Documento de aprofundamento, não spec congelada. Nasce da pergunta da diretoria
> (jul/2026): *"a rodada sugere 70 mas eu faço 65; a projeção nunca fica sabendo
> disso — não deveria?"*. Fecha a pendência aberta na
> [Cobertura Alvo, §8](cobertura-alvo-rodada.md) ("tratar on-order em spec
> separada"). Várias decisões aqui são **em aberto** — ver §9.

---

## 1. Problema

O motor order-up-to (`simular_politica_reabastecimento`,
[etl/demanda.py:926](../../etl/demanda.py#L926)) calcula a posição de partida de
cada SKU como **estoque físico − backlog de venda**:

```python
stock = float(est.get(idp, 0)) - float(backlog.get(idp, 0))   # etl/demanda.py:1024
```

e, ao caminhar as rodadas, soma sempre a quantidade **recalculada** na chegada:

```python
stock = estoque_projetado + pedido   # etl/demanda.py:1053  (pedido = SUGESTÃO recalculada)
```

Faltam **dois** termos que a teoria de revisão periódica (R,S) exige. A política
correta opera sobre a **posição de estoque**, não sobre o estoque em mãos:

```
posição = em mãos (físico) − backorders + em trânsito (on-order comprometido)
```

O termo **em trânsito** — o que já foi decidido/emitido e ainda não virou estoque
físico — hoje simplesmente não existe no motor. Consequências (as duas já
demonstradas em análise):

1. **A decisão manual do gestor evapora.** Congelou 65 no lugar de 70 → a próxima
   rodada continua projetando os 70 recalculados. A caneta do gestor não
   retroalimenta o plano.
2. **Viés que troca de sinal conforme o tempo** (mesma raiz — sem on-order):
   - **Antes da chegada** da rodada congelada: o motor a recalcula como "futura"
     (~70) e projeta 70 chegando → a rodada seguinte acha que tem mais estoque a
     caminho do que foi comprometido → **pede de menos**.
   - **Depois da data nominal de chegada, mas antes do lançamento no estoque:**
     [_sequencia_rodadas](../../etl/demanda.py#L864) descarta a rodada
     (`if c["data_chegada"] < data_hoje: continue`) → ela some da conta, nem
     on-order nem chegada → **pede de mais**.

**Atenuante conhecido:** order-up-to é auto-liquidante — um desvio pontual se
dilui na rodada seguinte. Não é erro que compõe geração após geração; é viés
temporário na janela "em trânsito". Por isso a plataforma funciona sem isso hoje.
Mas é imprecisão evitável, e piora quanto mais rodadas se sobrepõem no tempo real.

---

## 2. Conceito central

Adicionar o termo **on-order** (em trânsito, comprometido e ainda não recebido) à
posição de partida do motor. Isso, sozinho, faz a decisão do gestor propagar e
elimina os dois vieses do §1 — **desde que** a baixa do on-order (quando a
mercadoria vira estoque físico) seja limpa, senão conta-se duas vezes.

Duas invariantes:

- **Consistência de fonte:** on-order e físico têm que sair de fontes que se
  conversam. Se o físico sobe quando a mercadoria entra, o on-order tem que cair
  no mesmo instante. É a **regra de ouro da reconciliação** (§5).
- **Não infla o total:** on-order é reconhecimento de produção já comprometida,
  não produção nova. Como a Cobertura Alvo, redistribui *quando* se enxerga o
  volume — não cria volume.

---

## 3. A camada nova é onde a discussão fica interessante: **a fonte da verdade é o Tiny**

Aqui o problema deixa de ser "somar um número" e vira uma questão de arquitetura
de dados, porque o dado do "em trânsito" **não é nosso** — ele vive nos ERPs, e a
autoridade sobre a execução (fábrica) é o **Tiny/Olist** (conta Art Kamizetas),
não o nosso banco nem o Bling.

Fluxo relembrado (ver [decisões · Emissão](../decisoes.md)): congelamos a rodada
→ emitimos **compra no Bling** (AK Uniformes compra) + **venda no Tiny** (Art
Kamizetas produz). Quem fabrica trabalha no Tiny. Logo:

- A **quantidade realmente produzida** pode diferir da emitida (produção parcial,
  quebra de lote) — verdade no Tiny.
- A **data de entrega** que congelamos (`data_chegada`) é um **plano**; a data
  real desliza conforme a fila da fábrica — verdade no Tiny.
- O **status** (em espera / em produção / faturado / enviado) só existe no Tiny.

### 3.1. A tensão: intenção (nosso banco) × realidade (ERP)

A direção que o Diogo colocou — *"enviar os dados para o Tiny e ficar só com o id
no nosso banco"* — obriga a nomear, sem ambiguidade, **do que cada lado é fonte
da verdade**:

| Dado | Fonte da verdade | Natureza |
|---|---|---|
| Snapshot da rodada (sugestão por SKU, config, `data_referencia`) | **Nosso banco** (`app.rodada_congelada`) | Intenção, imutável, auditoria |
| `quantidade_final` decidida na emissão | **Nosso banco** (`app.pedido_compra_item`) | Intenção no ato da emissão |
| Identidade da rodada (`ref: <uuid>`, IDs Bling/Tiny) | **Nosso banco** | Chaves de junção |
| Quantidade **produzida/enviada** agora | **Tiny** | Execução, viva |
| Data de entrega **atual** | **Tiny** | Execução, viva (desliza) |
| Status de produção | **Tiny** | Execução, viva |

Conclusão de projeto (a discutir, mas é a hipótese forte): **o on-order do motor
deve ler a EXECUÇÃO (Tiny), não a INTENÇÃO congelada** — precisamente por causa do
problema das datas (§3.2). O nosso banco guarda o "o que eu quis", o ERP guarda "o
que está acontecendo"; o `ref: <uuid>` costura os dois.

### 3.2. O problema que o Diogo levantou: e se a data muda na fábrica?

Se a fábrica reprograma a entrega (a rodada que ia chegar em Ago vai pra Set), o
nosso `data_chegada` congelado no snapshot fica **mentindo**. Um on-order baseado
na data congelada colocaria estoque "chegando" num mês em que ele não vai chegar —
e a rodada seguinte seria dimensionada errado. Ou seja: **o passo 1 barato (usar a
data/qtd congelada localmente) resolve o caso comum, mas fica cego a desvios da
fábrica**. Fechar isso de verdade exige ler a data/qtd **atual** do Tiny.

Isso é, de novo, um argumento a favor de tratar o Tiny como fonte do on-order — e
liga diretamente com a feature de visibilidade (§6, passo 3).

### 3.3. Opções de fonte do on-order (a decidir)

| Opção | Como | Prós | Contras |
|---|---|---|---|
| **(a) Intenção local** | usa `quantidade_final` + `data_chegada` congelados | zero I/O de ERP; trivial | cego a mudança de qtd/data na fábrica (§3.2) |
| **(b) Read-back Tiny ao vivo** | `GET /pedidos/{id}` por ID guardado, na hora da simulação | verdade viva | latência/limite de API no caminho quente; acopla motor a rede (fere "etl/ puro") |
| **(c) Espelho em `public`** | pipeline externa espelha pedidos de compra/status em `public.pedidos_compra`; loader lê como o resto | consistente com físico (mesma fonte); motor segue lendo só `public` | depende do outro time; prazo incerto |
| **(d) Sync próprio → `app`** | job nosso poll no Tiny (temos os IDs) upserta qtd/data/status numa tabela `app`; loader lê | sob nosso controle; tira rede do caminho quente; alimenta a UI do gestor | mais um processo pra manter; janela de defasagem do poll |

**Inclinação atual:** **(d) como ponte** (rápido, sob nosso controle, já entrega a
visão do gestor) convergindo para **(c) no fim** (quando o espelho `public` de
pedidos de compra existir — já previsto no roadmap do outro time e no `ref:<uuid>`).
**Evitar (b)** no caminho quente do motor. **(a)** só como fallback quando não há
leitura do ERP (melhor que nada, mas com o caveat da §3.2 anotado na UI).

---

## 4. Faseamento proposto

### Passo 1 — Travar a rodada futura já comprometida *(baixo risco, recomendado 1º)*

Para uma rodada **futura** (`data_chegada ≥ hoje`) que já foi congelada e
comprometida (≥ PRONTO), o motor injeta na chegada a **quantidade comprometida** em
vez do pedido recalculado, e reporta essa rodada como *travada*, não como sugestão
viva.

- **Sem risco de contagem dupla:** a mercadoria ainda não entrou no físico, então
  somá-la na chegada não duplica nada.
- **Não precisa de read-back:** na janela "ainda vai chegar", a qtd/data congeladas
  bastam (o desvio da fábrica só morde depois — §3.2). Pode começar pela **opção
  (a)** e trocar para (c)/(d) depois sem mudar o motor.
- **Resolve o caso mais comum** do §1 (a próxima rodada enxerga 65, não 70) e a UI
  fica honesta (rodada congelada mostra o número congelado, não um recalculado que
  "escorrega" a cada `now()`).

### Passo 2 — On-order atrasado na posição inicial *(precisa de fonte viva + reconciliação)*

Pedidos comprometidos, **ainda não recebidos**, cuja chegada já passou (o caso que
o `_sequencia_rodadas` descarta hoje), somam na posição de abertura:

```python
stock = físico − backlog + em_trânsito_não_recebido   # etl/demanda.py:1024
```

- **Aqui mora o risco de contar duas vezes** → depende da regra de ouro (§5) e,
  portanto, de uma fonte que saiba "recebido" (opção (c)/(d), não (a)).

### Passo 3 — Painel "em trânsito" para o gestor da AKU *(UI, mesma fonte do passo 2)*

Uma visão (provável em `4_Pedidos` ou uma aba nova) do que **está para chegar e
ainda não é estoque real**: pedidos de compra em andamento, em espera/produção na
fábrica, com qtd, data esperada **atual** (do Tiny) e status. Requisito do Diogo,
e **reusa exatamente o dado do passo 2** — uma coleta, dois usos (o motor consome
o on-order; o gestor vê a lista). Sequenciar passo 2 e 3 juntos maximiza o retorno
do investimento na coleta.

---

## 5. Reconciliação e a regra de ouro (evitar contagem dupla)

O on-order tem que **sair da conta no exato instante em que a mercadoria entra no
estoque físico** — senão o físico sobe e o on-order ainda conta o mesmo volume.

- **Baixa por recebimento (correto):** o on-order de um pedido = `emitido −
  recebido`. Quando a fonte (Tiny/espelho) marca recebido/faturado e o Bling lança
  no estoque, o termo zera naturalmente. Exige uma fonte com noção de "recebido"
  (opção (c)/(d)).
- **Baixa por data (aproximação frágil):** contar até `data_chegada` e depois
  parar. Simples, mas na virada gera buraco (se atrasa, some antes de entrar) ou
  duplicata (se adianta). **Não recomendada** como solução final — é o mesmo tipo
  de viés que estamos tentando remover.
- **Chave de junção:** `ref: <uuid>` (gravado nas observações do Bling/Tiny,
  [builder.py](../../pedidos/builder.py#L277)) + IDs Bling/Tiny guardados. É o que
  amarra "nosso pedido" ↔ "pedido no ERP" ↔ "linha no espelho".

---

## 6. Encaixe na arquitetura (respeitando as regras do projeto)

- **Motor puro.** `etl/` não pode ler `app` nem falar com ERP direto. O seam: a
  **página/serviço** carrega o on-order (via `pedidos/repositorio.py` para a
  intenção, e via loader para o espelho `public`) e passa pro motor como argumento
  novo, ex.:
  ```python
  def simular_politica_reabastecimento(dados, config, ..., on_order: dict = None):
      # on_order = {(data_chegada_efetiva, SKU): qtd_em_transito}
  ```
  `on_order=None` ⇒ comportamento atual, byte-idêntico (backward-compatible).
- **Se a fonte for espelho `public`** (opção c): entra no `loader.carregar_dados()`
  como mais um DataFrame (`dados["em_transito"]`), e o motor segue lendo só de
  `dados` — coerente com tudo o mais.
- **Se for sync próprio** (opção d): tabela nova em `app` (DDL numerada em
  `docs/sql/`), escrita só pela porta `pedidos/`, lida pela página.
- **Qual estado conta como comprometido?** (knob) — recomendação: **≥ PRONTO** (é
  quando `quantidade_final` fica imutável, trava no banco garante). RASCUNHO **não**
  conta (fluido, pode ser descartado → viraria fantasma). Alternativa conservadora:
  só ≥ COMPRA_EMITIDA (existe PO real), ao custo de ignorar a decisão no intervalo
  PRONTO→emissão.

---

## 7. Impacto esperado

- **As sugestões vão mudar de número.** Em geral, a rodada seguinte a um corte
  manual passa a sugerir um pouco mais (deixa de assumir estoque a caminho que não
  virá). Comunicar isso à diretoria antes de ligar.
- **Auto-liquidação fica mais tight:** a correção acontece na hora certa, não com
  atraso de uma rodada.
- **Superfície:** aditiva no motor (1 arg + termo na posição inicial + trava na
  chegada), 1 fonte de dado nova (a decisão pesada), 1 painel de UI. Precisa de
  testes novos (o `conftest` passaria a montar `on_order`/`em_transito` sintético).

---

## 8. Relação com outras specs / decisões

- **Fecha** a pendência da [Cobertura Alvo §8](cobertura-alvo-rodada.md) (on-order
  era pré-requisito para a "hora da verdade" da rodada seguinte enxergar a gordura
  já em produção).
- **Continua** a arquitetura da [Pedidos Fase 0](../decisoes.md) — `app.pedido_compra`
  (intenção) × `public.pedidos_compra` (realidade, espelho futuro) já foi desenhada
  justamente para este acerto de contas; o `ref:<uuid>` já existe.

---

## 9. Questões em aberto (a discutir)

1. **Fonte do on-order:** ponte (d) própria agora, ou esperar o espelho (c) do
   outro time? Qual o prazo real do espelho `public.pedidos_compra`?
2. **"Guardar só o ID":** confirmamos que a `quantidade_final` e a `data_chegada`
   locais passam a ser **intenção de emissão** (auditoria) e que a **verdade viva**
   é sempre relida do Tiny? Ou mantemos a intenção local como fallback autoritativo
   quando o Tiny estiver indisponível?
3. **Mudança de quantidade na fábrica:** se o Tiny disser 60 produzidos onde
   pedimos 65, o motor usa 60 (execução) — e a diferença de 5 vira o quê? Backlog
   de compra? Alerta pro gestor? Nova rodada?
4. **Baixa do on-order:** dá pra confiar num sinal de "recebido" do Tiny/espelho, ou
   a baixa tem que casar com o lançamento de estoque no Bling (que é o que move o
   físico)? Qual chega primeiro?
5. **Granularidade da data:** o motor consome em resolução de mês; a data do Tiny é
   diária e desliza. Reprojetar on-order para o mês da data esperada atual — com que
   frequência recalcular?
6. **Passo 1 isolado já vale a pena** (mesmo sem tocar a fonte viva), como entrega
   de baixo risco enquanto (1)-(5) são decididas?
