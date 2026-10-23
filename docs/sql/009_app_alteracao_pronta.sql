-- =====================================================================
-- 009_app_alteracao_pronta.sql — estado ALTERACAO_PRONTA
--
-- O PRONTO do pós-emissão. Até aqui a alteração de um pedido emitido ficava
-- em EM_ALTERACAO até ser enviada, e nada sinalizava "a digitação acabou,
-- pode enviar". Agora:
--      EM_ALTERACAO → ALTERACAO_PRONTA → ALTERACAO_ENVIANDO → estado emitido
-- e o envio SÓ sai de ALTERACAO_PRONTA (mesmo desenho de RASCUNHO → PRONTO →
-- emitir). Reabrir a alteração volta a EM_ALTERACAO.
--
-- Itens TRAVADOS em ALTERACAO_PRONTA: o trigger tg_item_so_em_rascunho (DDL
-- 008) só libera RASCUNHO/EM_ALTERACAO, então não muda — o estado novo já
-- nasce travado, como o PRONTO.
--
-- (A reabertura de pedido CANCELADO → RASCUNHO, que entrou junto, não precisa
-- de DDL: os dois estados já existem; a guarda é do app.)
--
-- Compatibilidade: sem este DDL, "Marcar alteração pronta" responde
-- MigracaoPendente (check_violation 23514) e o pedido segue em EM_ALTERACAO.
--
-- COMO APLICAR: python scripts/migrar.py aplicar   (ou SQL Editor → Run)
-- =====================================================================

alter table app.pedido_compra drop constraint pedido_compra_status_check;
alter table app.pedido_compra add constraint pedido_compra_status_check
  check (status in ('RASCUNHO','PRONTO','COMPRA_EMITINDO','COMPRA_EMITIDA',
                    'VENDA_EMITINDO','EMITIDO','EM_ALTERACAO','ALTERACAO_PRONTA',
                    'ALTERACAO_ENVIANDO','CANCELAMENTO_ENVIANDO','SINCRONIZADO',
                    'CANCELADO'));
