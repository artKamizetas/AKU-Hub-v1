# Dados

## Fonte

**Supabase** (Postgres) acessado via **PostgREST** (biblioteca `postgrest`).
Credenciais em `st.secrets["supabase"]` (`url` + `service_key` + `schema`).
O Supabase é um **espelho do Bling ERP**, alimentado por uma pipeline mantida por
outra equipe — este projeto **não escreve** dados de negócio, só lê.

## Tabelas → SCHEMA (etl/loader.py)

`TABELAS_SUPABASE` mapeia o nome da "aba" lógica (SCHEMA) para a tabela real no
Supabase; `COLUNAS_SUPABASE` renomeia colunas. **IDs usados são os do Bling
(`*_bling`), não o `id` surrogate (UUID) do Supabase** — os joins entre tabelas
usam os IDs Bling.

| Aba (SCHEMA) | Tabela Supabase | Papel |
|---|---|---|
| Pedidos | `pedidos` | Cabeçalho de venda (loja, data, valor, situação) |
| Itens | `itens` | Itens dos pedidos (produto, quantidade) |
| Produtos | `produtos` | Cadastro (só ativos, `situacao == "A"`) |
| EstoqueV3 | `estoque` | Saldo físico por depósito |
| Produtos_detalhes | `produto_detalhes` | Categoria, tamanho, colégio, grupo |
| Vendedores / Lojas / Situações / Depósitos | idem | Dimensões |

## DataFrames após `carregar_dados()`

| Chave | Conteúdo (colunas-chave) |
|---|---|
| `dados["pedidos"]` | `ID`, `Loja ID`, `Data`, `Total Venda`, `id_situacao` |
| `dados["itens"]` | `ID_pedido`, `ID_produto`, `Quantidade`, `Data` (enriquecida do pedido), `Valor Unidade` |
| `dados["produtos"]` | `ID`, `codigo` (SKU), `Descricao`, `situacao`, `preco_custo` — **só ativos** |
| `dados["estoque"]` | `ID_deposito`, `ID_produto`, `saldoFisico` |
| `dados["detalhes"]` | `ID_produto`, `categoria`, `Super_categoria`, `Grupo`, `Tamanho`, `Marca_sku` (=colégio) |
| `dados["vendedores"]`, `["lojas"]`, `["situacoes"]`, `["depositos"]` | Dimensões |
| `dados["validacao"]` | `{ok, erros, avisos}` |

## IDs importantes (config.yaml)

- **Lojas** (aparecem em Pedidos): Natal = `203379922`, Mossoró = `203575032`
- **Depósitos** (aparecem em EstoqueV3): Natal = `7011018386`, Mossoró = `14887086441`, CD/Central = `11105614627`
- **Situações de venda efetiva**: `[9]` (Atendido)
- **Situações de backlog**: `[6, 15]` (em aberto / em andamento)

> ⚠️ **Loja ID ≠ Depósito ID.** A mesma loja física tem os dois. Loja aparece em
> Pedidos; depósito em EstoqueV3. Ver [regras-de-negocio.md](regras-de-negocio.md).

## Campos de `detalhes` (atenção)

- `Marca_sku` = **colégio** (ex: NEV, SES, OVD, FAC…). Nome vem do Bling.
- `categoria` = subtipo granular (CAI, CMI, POI…), com alguns códigos numéricos de
  lixo ('1','2','89'). **Para agrupamentos robustos use `Super_categoria`**
  (Camiseta, Calça, Moletom… — 14 valores limpos).
- `Grupo` = **mistura série/ensino** (EF1, EF2, EFM, EIN, EME…) **e linha de
  produto** (DIA, ESP, OPC, TOR…). Não é "série pura". Usado na matriz de
  crescimento colégio×grupo.

## Tipagem e convenções

- **IDs sempre como string** após `limpar_id()`. Nunca comparar como int.
  PostgREST devolve colunas int com NULL como `float64`; `astype(str)` direto gera
  `'123.0'` e quebra merges. **Sempre `limpar_id()` antes de comparar/joinar.**
- **Datas** já convertidas para `datetime` no loader (`converter_data_flexivel`,
  aceita ISO e BR). `itens` não tem data própria — é enriquecida via join com Pedidos.
- **Produtos**: só trabalhar com `dados["produtos"]` (ativos).

## Custo / egress (importante)

Cada `carregar_dados()` completo faz **~227 requisições HTTP** (paginação de 1000
linhas) e transfere **~80 MB** (dominado por `itens` ~165k linhas e `pedidos` ~44k).
Mitigado por `@st.cache_data(ttl=3600)` — múltiplos usuários navegando na mesma
hora reusam o cache. Dispara nova carga completa: expiração do cache, botão
"Forçar recarga", salvar config (limpa cache), ou o app "dormir/acordar" no
Streamlit Cloud. Atenção à cota de **bandwidth** do plano Supabase.
