# Pedido à pipeline — Espelhar os pedidos de COMPRA do Bling

**Status:** 🔴 ABERTO — aguardando a equipe da pipeline Bling→Supabase
**Aberto em:** 2026-10-09
**Destinatário:** equipe da pipeline de ingestão Bling→Supabase
**Quem consome:** dashboard AKU-Hub — Reposição de Loja (já pronta para ler) e, depois, o
Simulador de Produção

---

## 1. O que precisamos

O espelho `public` traz os pedidos de **venda** do Bling, mas não os de **compra**. Sem eles o
dashboard não sabe o que já foi encomendado à fábrica e ainda não chegou ("em trânsito").

Hoje a Logística mostra "Sem estoque no CD" sem conseguir dizer se a reposição já está a
caminho, e o Simulador de Produção recalcula a próxima rodada sem enxergar a anterior.

## 2. Dado necessário

Pedidos de compra da conta **AK Uniformes** (`GET /pedidos/compras` da API v3), com os itens.

**Cabeçalho** (sugestão: `public.pedidos_compra`)

| Campo | Origem no Bling | Uso |
|---|---|---|
| `id_bling` | `id` | Chave |
| `numero` | `numero` | Exibição |
| `id_situacao_bling` + nome | `situacao` | **Filtro**: só "Em andamento" conta como em trânsito |
| `data` | `data` | Emissão |
| `data_prevista` | `dataPrevista` | Chegada prevista ao CD |
| `id_fornecedor_bling` | `fornecedor.id` | Separar Art Kamizetas de terceiros |
| `observacoes` | `observacoes` | Contém `ref: <uuid>` dos pedidos emitidos pelo dashboard |
| `updated_at` | — | Idade do dado |

**Itens** (sugestão: `public.pedidos_compra_itens`)

| Campo | Origem no Bling | Uso |
|---|---|---|
| `id_pedido_compra_bling` | id do pedido | Junção |
| `id_produto_bling` | `itens[].produto.id` | Junção com `produtos` |
| `quantidade` | `itens[].quantidade` | Peças encomendadas |

## 3. Regras que importam para nós

- **A situação é a reconciliação.** O em-trânsito tem de sair da conta no instante em que a
  mercadoria vira estoque físico. Para nós isso é a mudança de situação do pedido de compra
  (sai de "Em andamento") acompanhando o lançamento no estoque. Por isso a situação precisa
  estar **atualizada com a mesma frequência do estoque** — se o estoque subir e o pedido
  continuar "Em andamento", contamos a mesma peça duas vezes.
- **Recebimento parcial:** se o Bling registrar, precisamos da quantidade ainda pendente por
  item (ou de um jeito de derivá-la).
- **Tabela de situações de compra** (id → nome), como já existe `situacoes_vendas`.

## 4. Como o dashboard vai ler

`etl/logistica.py::processar_logistica` já aceita `em_transito`
(`ID_produto`, `Quantidade`, `DataPrevista`). Com as tabelas no ar, o `etl/loader.py` passa a
entregá-las e a tela exibe "Em trânsito" e "Chegada" sem outra mudança.
