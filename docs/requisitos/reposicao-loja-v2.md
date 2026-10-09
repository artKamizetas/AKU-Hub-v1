# Especificação — Reposição de Loja v2 (Estoque-Alvo por loja)

**Status:** ✅ implementada (out/2026) · **Autor:** diretoria (Diogo) + assistente ·
**Código:** `etl/reposicao.py` (regras puras), `etl/logistica.py` (orquestrador),
`etl/relatorio_separacao.py`, `pages/2_Logistica.py`, `pages/5_Configuracoes.py` (seção
Reposição de Loja), `etl/config_edicao.py`

Substitui o "VM Dinâmico + Pulmão" (`etl/vm_dinamico.py`, removido).

---

## 1. Por que mudou

O motor antigo calculava, por SKU, um VM (prateleira) e um Pulmão (armário) e mandava repor
a diferença para o estoque da loja. Não servia ao negócio:

| Problema | Efeito |
|---|---|
| Somava **todas** as altas da base (desde 2019) e dividia por 180 dias | Demanda inflada várias vezes |
| Alvo **estático** o ano inteiro | Curto no pico (5 semanas fazem ~70% da alta), gordo na baixa |
| Alvo da **rede** aplicado igual às duas lojas, sobre todos os SKUs ativos | Mossoró "precisava" de uniforme de colégio de Natal; a venda institucional inflava o alvo de balcão |
| Cada loja recebia `min(gap, estoque do CD)` de forma independente | O mesmo estoque do CD prometido duas vezes |
| VM e Pulmão separados | Dois nomes para o mesmo estoque: a loja expõe 2 por tamanho e guarda o resto em gavetas |
| Sem noção de espaço, excesso ou lista de separação | A tela não virava operação |

**Em linguagem simples:** cada loja passa a ter uma "gaveta ideal" por tamanho, calculada com
a venda *daquela loja* no último pico, olhando os próximos dias do calendário. O ideal é
limitado pelo espaço real (arara + gavetas). O caminhão sai com a diferença, e quando o CD não
tem para as duas lojas, recebe quem ficaria sem primeiro.

## 2. A conta

Um único número por **loja × SKU**: o **Estoque-Alvo**.

```
Demanda da loja (SKU, mês)  = demanda da rede (etl/demanda.py) × participação da loja no SKU
Janela de proteção (dias)   = cobertura da fase (alta | baixa) + prazo de entrega da loja
Demanda da janela           = Σ demanda da loja nos dias [hoje, hoje + janela)
Segurança                   = Fator de Serviço × √(Demanda da janela × PA do SKU)
Alvo ideal                  = max(exposição mínima, ⌈Demanda da janela + Segurança⌉)
Alvo                        = Alvo ideal limitado pelo espaço (arara + gavetas do modelo)
Necessidade                 = max(Alvo − estoque da loja, 0)
Separar                     = Necessidade, rateada pelo saldo do CD
Falta                       = Necessidade − Separar
Excesso                     = estoque da loja − max(Alvo, demanda da loja no horizonte)
```

### 2.1 Demanda por loja
- **Rede:** `demanda.calcular_demanda_mensal_por_sku` — a mesma do Simulador (última alta
  completa × crescimento; baixa pela proporção global). Loja e fábrica falam a mesma língua.
- **Participação da loja** (`reposicao.participacao_por_loja`): vendas do SKU na loja ÷ vendas
  do SKU na rede, na janela em que o motor ancorou (última alta; SKU só-de-baixa usa o período
  histórico). O denominador é a rede inteira, então a **venda institucional** (loja
  "Encomendas") não cai em loja nenhuma — sai sozinha.
- **Fase:** alta se o mês de hoje está em `demanda.janela_alta` (Dez/Jan/Fev). Não existe mais
  uma "temporada da loja" separada.
- **Janela para frente** (`demanda.fracionar_janela_por_mes`): no fim de dezembro a janela já
  pega janeiro — a pré-carga do pico aparece sem regra especial.

### 2.2 Segurança
A venda de loja é **intermitente** (o SKU mediano sai 1×/semana no pico) e em **pacotes** (um
cliente leva várias peças). É um Poisson composto: variância ≈ demanda × peças por atendimento
(PA). Daí `Fator de Serviço × √(demanda × PA)`. O Fator de Serviço vem do nível de serviço do
colégio (`colegios[COL].nivel_servico`) ou do padrão da Reposição, pela mesma tabela do
Simulador (`demanda._nivel_para_z`).

### 2.3 Sortimento (quais colégios cada loja atende)
- **Cadastro manual** loja × colégio em `reposicao.sortimento` (Configurações → Reposição de
  Loja). Colégio desmarcado → alvo 0; o estoque que a loja tiver dele é excesso.
- **Sem cadastro**, a loja usa o derivado das vendas: colégio com **30+ peças** vendidas na
  loja em 12 meses (`MIN_PECAS_SORTIMENTO`). A tela avisa que está no derivado.
- **Produto sem colégio** (revenda, acessório) não cabe no cadastro por colégio: fica onde a
  própria loja o vende.
- **Exposição só para produto vivo:** SKU sem demanda projetada na rede não ganha os 2 de
  exposição (o cadastro do Bling mantém produto morto como ativo).

### 2.4 Espaço (gavetas)
A loja expõe `exposicao_minima` peças por tamanho na arara e guarda o resto em gavetas.
- Parâmetros: gavetas por loja (`reposicao.lojas[loja].gavetas`) e peças por gaveta por super
  categoria (`reposicao.capacidade_gaveta`, com `_padrao`).
- **Modelo** = SKU pai (`pedidos.grade.identificar`). **Fundo** do modelo = Σ tamanhos de
  `alvo ideal − exposição`.
- `reposicao.distribuir_gavetas`: as gavetas vão, uma a uma, para o modelo com mais fundo ainda
  descoberto. Um campeão pode levar mais de uma.
- `reposicao.aplicar_teto_espaco`: fundo todo coberto → alvo = ideal; coberto em parte → cada
  tamanho recebe a sua proporção; sem gaveta → fica na exposição. `LimitadoPorEspaco` marca
  quem ficou abaixo do ideal (depende de reposição frequente).
- Loja **sem** `gavetas` cadastrado = sem teto. `gavetas: 0` = só a arara.

### 2.5 Rateio do CD — "quem vai zerar primeiro"
Só entra quando Σ necessidade > saldo do CD. A peça vai, uma a uma, para a loja com menos dias
de cobertura `(estoque + já alocado) ÷ venda diária`. Loja sem venda prevista fica por último.
Garante **Σ Separar ≤ saldo do CD**.

### 2.6 Excesso (recolher)
Só é excesso o que a loja não vende no horizonte (`recolher_horizonte_dias`, default 120) —
com horizonte curto, outubro mandaria recolher o que janeiro vende. Fora do sortimento, todo o
estoque é excesso. A tela só **indica**; nada é movimentado.

### 2.7 Ações
`🚫 Corrigir estoque` (saldo negativo na loja ou no CD — sem sugestão até corrigir) ·
`🚨 Sem estoque no CD` · `⚠️ Repor parcial` · `✨ Repor` · `↩️ Recolher` · `✅ OK`.

## 3. Parâmetros (`config["reposicao"]`, gravados em `app.parametros`)

| Chave | Default | O que é |
|---|---|---|
| `exposicao_minima` | 2 | Peças por tamanho × modelo na arara (piso do alvo) |
| `cobertura_dias_alta` | 3 | Dias de venda que a loja guarda na alta (reposição diária) |
| `cobertura_dias_baixa` | 15 | Idem na baixa (reposição semanal) |
| `nivel_servico_default` | 95 | Nível de serviço do colégio sem valor próprio |
| `aplicar_crescimento` | true | Liga o crescimento na demanda da loja (editado em Colégios e Crescimento) |
| `recolher_horizonte_dias` | 120 | Horizonte do excesso |
| `lojas[loja].prazo_entrega_dias` | Natal 1 · Mossoró 2 | Dias entre separar no CD e estar na loja |
| `lojas[loja].gavetas` | 20 | Gavetas de fundo |
| `capacidade_gaveta` | `_padrao: 50` | Peças por gaveta, por super categoria |
| `sortimento` | `{}` | `{loja: [colégios]}` |

`lojas`, `capacidade_gaveta` e `sortimento` são coleções substituídas inteiras no merge
(`CAMINHOS_SUBSTITUICAO`). Os defaults de cobertura e prazo são ponto de partida — calibrar com
a operação. O bloco antigo `vm.*` e `logistica.vm_padrao` não têm mais leitor; `vm` segue em
`CHAVES_PARAMETROS` só para o que já estava gravado não se perder até a seção ser salva.

## 4. Tela e papel

- **Tela (`pages/2_Logistica.py`)** — decidir: KPIs (separar, falta no CD, recolher, limitados
  por espaço) e uma fila por vez (`Separar` · `Falta no CD` · `Recolher` · `Corrigir estoque` ·
  `Tudo`), com memória de cálculo e ocupação das gavetas em expanders.
- **Papel (`etl/relatorio_separacao.py`)** — separar: documento A4, uma página por loja,
  agrupado por Colégio → Modelo, tamanhos lado a lado, total e caixa de conferência. Segue a
  loja e os filtros da tela. Imprimir (iframe que chama a impressão do navegador) ou baixar o HTML.

## 5. Em trânsito

`processar_logistica(..., em_transito=None)` aceita um DataFrame
`[ID_produto, Quantidade, DataPrevista]` do que já foi comprado e não chegou ao CD; as colunas
`EmTransito`/`ChegadaPrevista` aparecem na fila "Falta no CD". **Hoje o dado não existe** no
espelho: a fonte decidida é a pipeline (ver
[espelho-pedidos-compra.md](espelho-pedidos-compra.md)). Quando existir, basta o loader
entregar a tabela. O Simulador de Produção **não** muda nesta entrega
([posicao-estoque-on-order.md](posicao-estoque-on-order.md)).

## 6. Fora do escopo / pendências

- Perfil **semanal** do pico: a demanda é mensal, então a 1ª semana de janeiro usa a média do mês.
- **Produto novo** (sem histórico) não tem demanda: fica sem alvo e, se tiver saldo na loja,
  aparece em Recolher com o motivo "Sem venda na rede (produto novo ou parado)".
- Transferência **entre lojas**; movimentar estoque a partir da tela.
- Filtro por situação do pedido (o motor de demanda não filtra; nas lojas os não-atendidos são <0,3%).
