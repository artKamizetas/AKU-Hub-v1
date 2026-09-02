# Achado de Dados — `pedidos.id_situacao_bling` com duas codificações incompatíveis

**Status:** ✅ **RESOLVIDO** (set/2026) — backfill executado pela equipe da pipeline

> **Fechamento (2026-09).** A pipeline aplicou o backfill. Verificado no espelho:
> **zero códigos órfãos** (eram 43.089), `id_situacao_bling = 9` passou de 1.371
> para 44.088 linhas, e 42.540 dos 43.135 pedidos anteriores a mar/2026 agora
> respondem pelo código correto — confirmando o mapeamento `1 → 9` que a §3
> levantava como hipótese. Do lado do dashboard **nada precisou mudar**: a coluna
> "Realizado ano anterior" voltou a popular, o atalho *Propor a partir do
> realizado* destravou e o aviso de degradação sumiu sozinho. O documento fica
> como registro da investigação e do de-para aplicado.

**Status original:** 🔴 ABERTO — requer investigação e backfill na pipeline Bling→Supabase
**Descoberto em:** 2026-08-05, durante a implementação das metas escalonadas do dashboard
**Destinatário:** equipe da pipeline de ingestão Bling→Supabase
**Escopo:** tabela `public.pedidos`, coluna `id_situacao_bling` (e a FK derivada `id_situacao`)

---

## 1. Resumo executivo

A coluna `pedidos.id_situacao_bling` contém **duas codificações de status
diferentes e incompatíveis**, separadas no tempo:

| Período | Codificação | Códigos observados | Existe em `situacoes_vendas`? |
|---|---|---|---|
| 2019-12 → **2026-02** | **Legada (órfã)** | `0`, `1`, `2`, `3`, `11` | ❌ **Não** |
| 2026-03 → hoje | **Bling v3 (válida)** | `6`, `9`, `12`, `24`, `28488` | ✅ Sim |

**42.514 pedidos (95,5% da base, R$ 9,71 milhões em `valor_total`) usam o código
`1`, que não existe na tabela de domínio `situacoes_vendas`.** No total são
**43.089 pedidos órfãos** (96,8% das 44.514 linhas da tabela):

```
situação |     n | primeira    | última      | valor_total
---------+-------+-------------+-------------+---------------
       1 | 42514 | 2019-12-03  | 2026-02-28  | R$ 9.710.465,92
       2 |   456 | 2019-12-13  | 2026-02-25  | R$   106.585,23
       0 |    75 | 2022-12-29  | 2026-01-31  | R$    15.676,23
       3 |    33 | 2022-02-10  | 2026-01-09  | R$     8.432,98
      11 |    11 | 2022-01-12  | 2026-02-02  | R$     2.469,92
```

Consequência prática: qualquer consulta que filtre "venda efetiva" pelo código
correto (`id_situacao_bling = 9`, *Atendido*) **enxerga apenas dados de março de
2026 em diante**. Todo o histórico 2019–2025 fica invisível — não porque falta,
mas porque está rotulado com um código que nenhuma tabela de domínio explica.

---

## 2. Evidência

### 2.1. A tabela de domínio não conhece os códigos legados

`public.situacoes_vendas` tem 10 linhas:

```
id_bling | descricao
---------+----------------------
       6 | Em aberto
       9 | Atendido
      12 | Cancelado
      15 | Em andamento
      18 | Venda Agenciada
      21 | Em digitação
      24 | Verificado
   28487 | Enviado
   28488 | Pronto para Retirada
   28506 | Embalado
```

Os códigos `0`, `1`, `2`, `3` e `11` presentes em `pedidos` **não aparecem aqui**.
São referências órfãs.

### 2.2. O corte é abrupto entre fev/2026 e mar/2026

Contagem de pedidos por mês × situação na janela da transição:

```
mês       |   0     1     2     3     6     9    11    12    24  28488
----------+--------------------------------------------------------------
2025-09   |   1   241     0     0     0     0     0     0     0      0
2025-10   |   1   127     0     0     0     8     0     0     0      0
2025-11   |   0   180    11     0     0     0     0     0     0      0
2025-12   |  18   462     3     1     0     1     0     0     0      0
2026-01   |  34  3173    14     1     2    17     2     3     6      1
2026-02   |   0   917     9     0     0     0     1     1     6      0
2026-03   |   0     0     0     0     6   361     0     1     0      0
2026-04   |   0     0     0     0     2   256     0     6     0      2
```

O código `1` some completamente após **2026-02-28**; o `9` passa a dominar a
partir de **2026-03**. Não há sobreposição relevante — é uma troca de regime, não
uma convivência.

### 2.3. Cada codificação vem de uma rota de ingestão diferente

Agrupando por `created_at` (quando a linha entrou no Supabase):

```
situação |     n | primeira ingestão        | última ingestão
---------+-------+--------------------------+--------------------------
       0 |    75 | 2025-05-20 21:18         | 2026-03-01 15:08
       1 | 42514 | 2025-05-20 21:18         | 2026-03-01 15:08
       2 |   456 | 2025-05-20 21:18         | 2026-03-01 15:08
       3 |    33 | 2025-05-20 21:18         | 2026-03-01 15:08
      11 |    11 | 2025-05-20 21:18         | 2026-02-25 15:40
      24 |    13 | 2025-05-20 21:18         | 2026-03-01 15:08
       9 |  1371 | 2025-10-31 20:00         | 2026-08-04 18:25
       6 |    17 | 2026-01-08 00:01         | 2026-08-04 18:18
      12 |    20 | 2026-02-11 18:34         | 2026-06-03 16:20
   28488 |     4 | 2026-03-01 15:08         | 2026-08-04 18:16
```

Volume de ingestão por dia (maiores lotes):

```
2025-05-20 -> 36.749 linhas     ← carga histórica inicial
2026-03-01 ->  3.792 linhas     ← segunda carga / re-sincronização
2025-05-21 ->    141 linhas
2026-08-04 ->    116 linhas     ← ritmo normal do incremental
```

**Leitura:** os códigos legados chegam pelos **lotes grandes** (carga histórica de
2025-05-20 e re-sync de 2026-03-01); os códigos válidos chegam pelo **incremental
diário**. São dois caminhos de código com mapeamentos de status divergentes.

> ⚠️ **Atenção:** o lote de **2026-03-01 ainda gravou códigos legados**. Isso sugere
> que a rotina de carga em massa **continuava com o mapeamento antigo naquela data**.
> Se essa rotina for reexecutada hoje sem correção, ela reintroduz o problema.

### 2.4. `1` tem o perfil de uma venda concluída

Comparando o comportamento financeiro das situações (saída literal da
consulta §5.5, executada em 2026-08-05):

```
situação |     n | ticket médio | % com valor_pago > 0 | % valor_total <= 0
---------+-------+--------------+----------------------+--------------------
       1 | 42514 |    R$ 229,22 |               90,9 % |              0,4 %
       9 |  1371 |    R$ 194,85 |               99,8 % |              0,0 %
       2 |   456 |    R$ 272,60 |               82,0 % |             14,3 %
       0 |    75 |    R$ 214,74 |               38,7 % |              2,7 %
```

O código `1` tem praticamente o mesmo perfil do `9` (*Atendido*): quase tudo pago,
quase nada com valor zero. O `2` tem 14,3% de valor zero e menos pagamento —
compatível com cancelamento/estorno. O `0` tem só 38,7% pago — compatível com
pedido em aberto.

---

## 3. Hipótese de causa raiz

**A carga histórica usou a enumeração de status da API v2 do Bling (ou um enum
interno do Apps Script), enquanto o incremental usa os `id` de situação da API
v3.** O campo é o mesmo, mas o significado dos números mudou, e ninguém traduziu
o histórico.

Mapeamento **provável** (a confirmar contra a fonte — ver §5):

| Código legado | Significado provável | Código v3 correspondente |
|---|---|---|
| `1` | Atendido / concluído | `9` |
| `0` | Em aberto | `6` |
| `2` | Cancelado | `12` |
| `3` | ? (33 linhas) | ? |
| `11` | ? (11 linhas) | ? |

⚠️ **Este mapeamento é inferência estatística, não confirmação.** Ele é
consistente com os perfis da §2.4, mas precisa ser verificado contra o Bling
antes de qualquer `UPDATE` — ver §5.

---

## 4. Impacto

### Já observado
- **Dashboard AKU (`etl/daily.py`, `pages/1_Daily.py`)**: a configuração
  `daily.situacoes_venda = [9]` faz o acompanhamento comercial enxergar só de
  mar/2026 em diante. A tela de definição de metas perde a coluna "Realizado ano
  anterior" — a referência que o gestor usa para calibrar a meta. Já **mitigado na
  UI** com aviso explícito e o atalho desabilitado, mas é mitigação, não correção.
- **Qualquer análise ano-contra-ano** de faturamento, ticket médio ou PA que
  filtre por situação está comprometida.

### Risco latente (ainda não observado)
- **Motor de demanda / PCP (`etl/demanda.py`)** usa `fabrica.situacoes_backlog =
  [6, 15]` para identificar pedidos que consomem estoque sem faturar. Com o
  histórico em codificação legada, o backlog histórico também não é reconhecido.
  Hoje isso é inofensivo porque o motor ancora na alta recente, mas **qualquer
  mudança de janela histórica pode expor o problema**.
- **Reprocessamento**: se a rotina de carga em massa de 2026-03-01 rodar de novo,
  reintroduz códigos legados em linhas hoje corretas.

---

## 5. O que investigar (perguntas para a pipeline)

1. **Quais são as duas rotinas de ingestão?** Identificar o job da carga em massa
   (2025-05-20 e 2026-03-01) e o do incremental diário. Confirmar que usam
   endpoints ou versões de API diferentes.
2. **De onde vem o número gravado em `id_situacao_bling` na carga em massa?**
   É o campo `situacao` da API v2? Um enum interno? Um export de planilha do
   Apps Script antigo?
3. **Qual o mapeamento oficial?** Puxar do Bling a lista de situações da API v2
   (ou da fonte que a carga usou) e cruzar com `situacoes_vendas`. **Não aceitar
   a inferência da §3 sem essa confirmação** — um `UPDATE` errado em 42 mil linhas
   é pior que o problema atual.
4. **O que são `3` (33 linhas) e `11` (11 linhas)?** Volume irrelevante, mas
   precisam de destino definido (mapear ou marcar como desconhecido — nunca
   adivinhar).
5. **A carga em massa ainda está com o mapeamento antigo?** Se sim, corrigir a
   rotina **antes** do backfill, senão o próximo re-sync desfaz o trabalho.

### Consultas para reproduzir

> Todas verificadas contra o banco em 2026-08-05 (Supabase → SQL Editor).
> A consulta 1 devolve as 5 linhas órfãs da §1; a 5 reproduz a tabela da §2.4.

```sql
-- 1. Códigos órfãos (sem correspondência na tabela de domínio)
select p.id_situacao_bling, count(*) as n,
       min(p.data) as primeira, max(p.data) as ultima,
       sum(p.valor_total) as valor_total
  from public.pedidos p
  left join public.situacoes_vendas s on s.id_bling = p.id_situacao_bling
 where s.id_bling is null
 group by 1 order by n desc;

-- 2. A transição no tempo
select date_trunc('month', data) as mes, id_situacao_bling, count(*)
  from public.pedidos
 where data >= '2025-09-01' and data < '2026-05-01'
 group by 1, 2 order by 1, 2;

-- 3. Qual rota de ingestão gravou cada código
select id_situacao_bling, count(*) as n,
       min(created_at) as primeira_ingestao, max(created_at) as ultima_ingestao
  from public.pedidos group by 1 order by n desc;

-- 4. Os lotes de carga em massa
select created_at::date as dia, count(*)
  from public.pedidos group by 1 order by 2 desc limit 10;

-- 5. Perfil financeiro (evidência do significado de cada código)
select id_situacao_bling, count(*) as n,
       round(avg(valor_total)::numeric, 2) as ticket_medio,
       round(100.0 * avg((coalesce(valor_pago,0) > 0)::int), 1) as pct_pago,
       round(100.0 * avg((coalesce(valor_total,0) <= 0)::int), 1) as pct_valor_zero
  from public.pedidos group by 1 order by n desc;
```

---

## 6. Proposta de correção

### Princípios
- **Confirmar o mapeamento antes de escrever** (§5.3). Sem confirmação, não há
  backfill.
- **Corrigir a rotina antes de corrigir os dados** (§5.5), senão o próximo
  re-sync desfaz.
- **Preservar o valor original.** O backfill não pode ser destrutivo — sem o
  valor de origem, um mapeamento errado se torna irreversível.

### Passos sugeridos

1. **Corrigir a rotina de carga em massa** para emitir os `id` da API v3, iguais
   aos do incremental.
2. **Preservar a origem**: adicionar `id_situacao_bling_origem` (ou uma tabela de
   auditoria do backfill) e copiar o valor atual antes de qualquer `UPDATE`.
3. **Criar a tabela de-para** com o mapeamento confirmado, versionada no repositório
   da pipeline — não como constante embutida no script.
4. **Backfill em transação**, restrito à faixa afetada, com contagem antes/depois:

   ```sql
   -- ILUSTRATIVO — só executar com o de-para CONFIRMADO na etapa 3
   begin;
   alter table public.pedidos add column if not exists id_situacao_bling_origem int;
   update public.pedidos set id_situacao_bling_origem = id_situacao_bling
    where id_situacao_bling_origem is null
      and id_situacao_bling in (0, 1, 2, 3, 11);
   update public.pedidos p set id_situacao_bling = m.id_v3
     from de_para_situacao m
    where p.id_situacao_bling = m.id_legado
      and p.data < '2026-03-01';
   -- conferir a contagem esperada ANTES de confirmar
   commit;
   ```

5. **Reconstruir a FK derivada `id_situacao`** (o UUID) após o backfill.
6. **Adicionar uma checagem contínua** que alerte quando entrar em `pedidos` um
   `id_situacao_bling` sem correspondência em `situacoes_vendas` — é o teste que
   teria pego isso em 2025.

### Critérios de aceite

- [ ] A consulta §5.1 (códigos órfãos) retorna **zero linhas**.
- [ ] `select count(*) from pedidos where id_situacao_bling = 9 and data < '2026-03-01'`
      retorna um volume compatível com a §2.2 (ordem de 40 mil, não 9).
- [ ] Faturamento anual por situação, reprocessado, bate com o fechamento contábil
      de cada ano — **este é o teste que realmente valida o mapeamento**.
- [ ] Um re-sync da carga em massa **não** reintroduz códigos legados.
- [ ] `id_situacao_bling_origem` preenchido em 100% das linhas alteradas.

---

## 7. Contato / contexto do lado do dashboard

O achado saiu da implementação das metas escalonadas
(`docs/requisitos/metas-escalonadas.md`, §11). Do lado do dashboard **nada precisa
mudar** quando o backfill for concluído: `daily.situacoes_venda = [9]` já é o
filtro correto, e a coluna "Realizado ano anterior" e o botão *Propor a partir do
realizado* voltam a funcionar sozinhos assim que os dados históricos passarem a
responder pelo código `9`.

Enquanto o backfill não acontece, o dashboard exibe o aviso explicando a lacuna —
nenhuma decisão é tomada sobre dado silenciosamente vazio.
