# Especificação — Alterar e cancelar pedido de compra depois da emissão

**Status:** ✅ IMPLEMENTADA e validada contra os ERPs reais (out/2026; DDL 008 aplicado) · **Autor:** diretoria (Diogo) + assistente · **Alvo:** `pedidos/` + `pages/4_Pedidos.py`

## 1. Problema

O pedido ficava só-leitura assim que a compra ia ao Bling: `editavel()` só
aceitava Rascunho, o trigger do banco travava os itens, a máquina de estados não
tinha caminho de volta e os clientes só sabiam criar (POST). Qualquer ajuste
pós-emissão era feito à mão nos dois ERPs — e o dashboard não ficava sabendo.

## 2. Regra de negócio

- Um pedido emitido pode ser **alterado** (quantidades, inclusão de item) ou
  **cancelado** pela 4_Pedidos, e o app **sobrepõe** o Bling e o Olist.
- **O filtro é o status nativo de cada ERP** (decisão da diretoria): compra
  **"Em aberto"** no Bling (`situacao.valor == 0`) **e** venda **"Aberta"** no
  Olist (`situacao == 0`). Nada além disso bloqueia. Qualquer outra situação
  (Em andamento, Atendido, Aprovada, Faturada…) impede, com mensagem dizendo o
  ERP e a situação.
- **Edição feita direto no ERP não bloqueia**: é mostrada, e o envio só segue
  com um "sobrepor" explícito.
- Vale também para o pedido que só tem a compra emitida (sem venda no Olist):
  aí só o Bling entra na conta.

## 3. Máquina de estados

"Estado emitido" = `EMITIDO` se o pedido tem `olist_id`, senão `COMPRA_EMITIDA`
(derivado por `estados.estado_emitido`, não gravado).

```
estado emitido ──abrir alteração──▶ EM_ALTERACAO ──descartar──▶ estado emitido
EM_ALTERACAO ──enviar──▶ ALTERACAO_ENVIANDO ──ok──▶ estado emitido
                              └─ falha antes de tocar ERP ──▶ EM_ALTERACAO
estado emitido ──cancelar──▶ CANCELAMENTO_ENVIANDO ──ok──▶ CANCELADO
                              └─ falha antes de tocar ERP ──▶ estado emitido
```

- `EM_ALTERACAO` é repouso **editável**: `quantidade_final` é rascunho da
  alteração; os ERPs seguem na versão anterior.
- Não se reusa `RASCUNHO`: ele significa "não existe nos ERPs" e libera cancelar
  pedido e rodada.
- Falha **depois** de tocar um ERP: o pedido fica no lock (`*_ENVIANDO`) e a
  tela oferece **Concluir**, que reexecuta. PUT/PATCH são idempotentes — não há
  o risco de duplicata do POST da emissão, por isso não é o mesmo "Destravar".
- Saída de emergência do lock: **Voltar a editar** (alteração) / **Destravar**
  (cancelamento) — `emissor.destravar`.

## 4. Linha de base e revisões (DDL 008)

`app.pedido_compra_revisao` guarda **o que foi aos ERPs**, versão a versão: a
emissão (nº 1), cada alteração e o cancelamento. Cada linha tem o retrato dos
itens, o motivo, quem/quando, e três carimbos: `bling_ok_em`, `olist_ok_em`,
`concluida_em`. Regras puras em `pedidos/revisoes.py`:

| Pergunta | Função | Resposta |
|---|---|---|
| O que está nos ERPs agora? | `linha_de_base` | última revisão concluída (não cancelamento) |
| O que AQUELE ERP recebeu por último? | `confirmada_no_erp` | última revisão carimbada naquele ERP |
| Os dois ERPs estão em versões diferentes? | `envio_parcial` | revisão pendente com carimbo de um só |
| O que a alteração muda? | `diferencas` | itens × linha de base |

A linha de base alimenta o "emitido × novo" da tela, o descarte e a detecção de
edição manual. Pedido emitido antes do DDL 008 ganha a revisão 1 pela carga
inicial do DDL — ou, se faltar, ao abrir a primeira alteração (os itens estavam
travados desde a emissão, logo são o que foi emitido).

## 5. Envio da alteração (`emissor.enviar_alteracao`)

1. Lock CAS `EM_ALTERACAO → ALTERACAO_ENVIANDO`.
2. Lê os dois ERPs; aplica o filtro (`avaliar_abertura`).
3. Compara cada ERP com o que nós enviamos a ele por último
   (`comparar_com_erp`); divergência sem `sobrepor=True` → `DivergenciaNoErp`.
4. Prepara **tudo** antes de tocar em ERP (tokens, payloads, SKU→id do Olist).
5. **Olist primeiro** (`PUT /pedidos/{id}/itens`, depois `PUT /pedidos/{id}`),
   **Bling depois** (`PUT /pedidos/compras/{id}`).
6. Carimba a revisão e volta ao estado emitido.

Olist primeiro porque é o lado da fábrica, onde a recusa importa: se ele
recusar, nada mudou em lugar nenhum.

O PUT do Bling substitui o pedido inteiro, então o payload **preserva**
(`bling.montar_payload_alteracao_compra`): número e data de emissão (sem o
número o Bling renumera e o `numeroOrdemCompra` do Olist fica órfão), as
parcelas (mesmas datas e formas; valores refeitos na proporção do total novo) e
o que não gerimos (frete, desconto, categoria, ordem de compra). A situação
**não** vai no PUT — ver "Conferido na conta", §9.

Zerar todos os itens é recusado: o caminho é cancelar.

## 6. Cancelamento (`emissor.cancelar_emitido`)

Mesmo filtro, mesma ordem (Olist, depois Bling). Por ERP: em aberto → cancela;
**já cancelado → conta como feito** (quem cancelou à mão no ERP só registra
aqui); qualquer outra situação → impedimento, nada é tocado.

- Muda a **situação** nos ERPs, não exclui: os pedidos ficam lá como registro e
  `bling_id`/`olist_id` ficam aqui como trilha.
- Olist: `PUT /pedidos/{id}/situacao` com `2`. Bling:
  `PATCH /pedidos/compras/{id}/situacoes/{idSituacao}` — o id é o da situação
  no módulo (por conta), escolhido em **Configurações → Integrações →
  Situação de cancelado** (`situacao_cancelado_id`). Depois do PATCH o app faz
  um GET e só aceita se o pedido ficou de fato Cancelado: um id errado moveria
  o pedido para outra situação sem erro nenhum.
- Motivo obrigatório. É **terminal**: o grupo Colégio × SuperCategoria não é
  refeito na mesma rodada (unique do banco). O motor não enxerga on-order hoje,
  então a rodada seguinte recompra sozinha.

## 7. Tela (4_Pedidos, só no modo de um pedido)

- **Pedido emitido:** "Verificar nos ERPs" (leitura sob demanda — chamada de
  API, não cabe em todo rerun) → `Bling nº X: Em aberto · Olist nº Y: Aberta` →
  habilita **Alterar pedido** e **Cancelar pedido emitido**.
- **Em alteração — como a diferença aparece:**
  1. *Lista:* coluna só-leitura **Emitida (pçs)** ao lado de **Nova (pçs)**,
     pintada em âmbar onde muda. *Grade:* SKU em âmbar + legenda
     `CAM-M 20→14`. Resumo acima da tabela: `−10 peças · −R$ 389,30 vs emitido`.
  2. *Conferência antes de enviar:* só as linhas que mudam — Emitida · Nova ·
     Δ pçs · Δ valor.
  3. *Edição manual no ERP:* aviso + tabela Enviado por nós · No Bling · No
     Olist · Vai ficar, com a célula divergente pintada, e a caixa "Sobrepor".
- **Lote** não ganha alteração nem cancelamento pós-emissão (é exceção, não rotina).

## 8. Limites assumidos

- O Olist não informa se a fábrica já gerou ordem de produção a partir do
  pedido: só a situação conta. Um pedido "Aberta" pode estar em produção.
- Entre a checagem e o envio há uma janela de segundos em que a situação pode mudar.
- Se o segundo ERP deixar de estar em aberto no meio de um envio, "Concluir"
  recusa e a saída é reabrir o pedido naquele ERP (ou "Voltar a editar").
- Enquanto o pedido está `EM_ALTERACAO`, `quantidade_final` **não** é o que
  está nos ERPs — quem precisar do emitido lê a linha de base. Vale para a
  futura posição on-order (`posicao-estoque-on-order.md`).

## 9. Validação do contrato contra os ERPs reais (09/10/2026)

Feita com UM pedido de teste, rodando o próprio `emissor` contra o Bling e o
Olist de produção. O pedido de teste viveu num repositório em memória (não
entrou em `app.pedido_compra`); os eventos ficaram em `app.integracao_evento`
com `detalhe.teste_contrato = true`. Sobrou nos ERPs, cancelados e rotulados
"TESTE AKU-HUB": **compra Bling nº 521** e **venda Olist nº 12351**.

| O que se queria saber | Resultado |
|---|---|
| PUT do Bling com o nosso payload é aceito? | ✅ Sim, sem alertas. |
| Número e data de emissão sobrevivem ao PUT? | ✅ Nº 521 e data mantidos nas 3 alterações. |
| PUT **sem** `situacao` mantém o pedido Em aberto? | ✅ Ficou `{id: 28, valor: 0}`. |
| Parcela: mesma data, valor refeito? | ✅ R$ 81,60 → 122,40 → 204,00, vencimento fixo, nos dois ERPs (no Olist o próprio ERP recalcula). |
| Trocar a grade (tirar um tamanho, incluir outro)? | ✅ Nos dois ERPs. |
| `PUT /pedidos/{id}` do Olist preserva o que não enviamos? | ✅ `numeroOrdemCompra`, cliente, vendedor, depósito e forma de recebimento intactos. |
| Alterar pedido que só tem a compra (sem venda)? | ✅ Só o Bling é chamado; a venda emitida depois já sai com os itens alterados. |
| O filtro barra fora de "Em aberto"? | ✅ Compra movida para Em andamento (37): alterar e cancelar recusados. Devolvida a Em aberto (28): liberados. |
| Edição feita direto no ERP é detectada? | ✅ Quantidade mexida no Olist (4→5) apareceu como divergência; com "sobrepor" o envio a corrigiu. |
| Situação 34 leva a Cancelado? | ✅ `{id: 34, valor: 2}` — o par deixou de ser inferido. |
| Cancelar a venda no Olist? | ✅ `situacao = 2`. |
| O app do Bling tem permissão de alterar e mudar situação da compra? | ✅ Sim. |

**Não testado, de propósito:**

- *Venda "Aprovada" no Olist barrando o envio.* Aprovar um pedido na conta da
  fábrica pode lançar estoque/contas; o caminho é o mesmo já exercitado com o
  Bling (o filtro lê `situacao != 0`) e está coberto nos testes automatizados.
- *Os ERPs recusarem sozinhos uma alteração fora de "em aberto".* Exigiria
  furar o nosso filtro de propósito; o app não depende disso.
- *Pedido com contas/estoque já lançados.* O pedido de teste não tinha.

**Achado lateral:** o token do Bling não tem o escopo de **Situações** —
`GET /situacoes/modulos` responde 403. Consequências: o botão "Testar conexão"
do Bling em Integrações falha (ele usa esse endpoint) e o seletor "Situação de
cancelado" cai no campo de texto. Nada disso impede emitir, alterar ou cancelar.
Para corrigir, incluir o escopo no app do portal do Bling e reconectar.

### Situações de compra da conta

Pedidos de Compra do Bling = módulo `575904`, só as quatro situações padrão:

| id | Situação | `situacao.valor` |
|---|---|---|
| 28 | Em aberto | 0 |
| 31 | Atendido | 1 |
| 34 | Cancelado | 2 |
| 37 | Em andamento | 3 |

- **"Situação de cancelado" em Integrações = 34** (salvo e validado).
- **A listagem devolve `situacao.id = 0` em 403 de 500 pedidos** — só o `valor`
  vem preenchido. Por isso o filtro decide **só pelo `valor`**, e o PUT de
  alteração não devolve a situação (mandaria um id que não existe).

Se o contrato mudar, o ajuste fica nas funções puras de payload
(`bling.montar_payload_alteracao_compra`, `olist.montar_itens_venda`).

## 10. Testes

`tests/test_pedidos_revisoes.py` (regras puras), `tests/test_pedidos_estados.py`,
`tests/test_pedidos_repositorio.py` (revisões, abrir/descartar),
`tests/test_integracoes_payloads.py` (payloads e clientes) e
`tests/test_emissor.py` (filtro, divergência, envio feliz / falha antes / falha
depois / concluir / voltar a editar, cancelamento feliz / já cancelado / parcial
/ id de situação errado). Sem rede e sem Supabase.
