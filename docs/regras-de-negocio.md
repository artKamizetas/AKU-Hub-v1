# Regras de Negócio

Regras de domínio da AK Uniformes que o sistema precisa respeitar. Parâmetros
concretos vivem em `config.yaml`; aqui está o "porquê".

## O negócio

Varejo de **uniforme escolar** com **contrato de exclusividade** por colégio
(cada colégio explorado só vende naquela loja). Lojas em Natal e Mossoró (RN),
com **fábrica própria** dentro do grupo empresarial — a **AK Uniformes** (varejo)
e a **Art Kamizetas** (fábrica) fazem parte do **Grupo AK**.

Consequências importantes:
- O **perfil de venda de cada produto é estável** — muda pouco de ano a ano,
  geralmente só **cresce** conforme o crescimento de matrícula do colégio. Isso
  torna a demanda relativamente previsível (base da metodologia de PCP).
- **Uniforme não perece**: sobra de estoque vira estoque inicial da temporada
  seguinte (baixo custo de excesso → comprar generoso no pico é economicamente
  ótimo).

## Loja ≠ Depósito

No Bling, "Loja" e "Depósito" são entidades diferentes com IDs diferentes. A mesma
loja física tem **ambos**:
- **Loja ID** → aparece nos **Pedidos** (onde a venda aconteceu).
- **Depósito ID** → aparece no **EstoqueV3** (onde o estoque está).

Nunca cruzar Loja ID com Depósito ID. IDs em [dados.md](dados.md).

## SKU

- **SKU = produto × tamanho** (ex: `FAC103CAIEFM-XGG`). Cada tamanho é um SKU
  próprio, com estoque e demanda próprios.
- Formato alfanumérico sem padrão rígido, normalmente `CATEGORIA-TAMANHO`.
- A **grade de tamanhos** (ex: uma camiseta que vende 40P/100M/60G/20GG) é uma
  propriedade estrutural do produto×colégio. Como cada tamanho é um SKU, a grade é
  preservada automaticamente ao ancorar cada SKU na sua própria venda.

## Situações de pedido

- **Venda efetiva**: situação `9` (Atendido). É o que conta como venda realizada.
- **Backlog**: situações `[6, 15]` (em aberto / em andamento). São peças **já
  vendidas mas não faturadas** — ocupam saldo físico mas estão comprometidas, então
  são **descontadas** do estoque disponível.

## Alta temporada (crítico para o PCP)

- **Alta = Dezembro, Janeiro, Fevereiro** — Janeiro é o pico (~48% da venda anual,
  volta às aulas), Fevereiro é o declínio.
- Demais meses = **manutenção**: venda esparsa e pontual.
- A alta é o **sinal limpo** de demanda; a baixa é ruidosa. Por isso a metodologia
  **ancora a demanda na alta** e trata a baixa só como acréscimo de volume
  (ver [metodologia-pcp.md](metodologia-pcp.md)).
- Parâmetro: `config["demanda"]["janela_alta"]` (ex: `[12, 1, 2]`, ordem cronológica).

## Colégios

- Descobertos dinamicamente de `detalhes["Marca_sku"]` (não há cadastro fixo).
- Cada colégio tem parâmetros manuais em `config["colegios"][nome]`:
  - `taxa_crescimento` — crescimento base (matrícula), **input manual**.
  - `crescimento_grupos` — override por grupo/série (ex: crescer só o ensino médio).
  - `nivel_servico` — nível de serviço do VM de loja.
- Crescimento **nunca** é calculado dos dados — é sempre decisão do usuário.

## Produção (PCP)

- **Fábrica própria**, lead time ~4 semanas (controlável; até 6-7 só em incidente).
- Capacidade ~20k peças/mês **não é gargalo** (demanda anual ~20k). O desafio é
  **timing, margem e concentração de risco**, não capacidade.
- Produção é planejada em **rodadas** (`config["planejamento"]["rodadas_datas"]`, datas
  reais de disparo). Cada rodada dispara um pedido à fábrica.
- Regra de arredondamento: pedidos são arredondados para cima e forçados a **número
  par** (produção em pares).
- **Pipeline** (ordens já em produção) não é alimentado — sempre 0 (pendência conhecida).

## Produtos ativos

Só trabalhar com produtos ativos (`situacao == "A"`). Inativos/excluídos são
filtrados no `loader`.
