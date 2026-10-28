-- =====================================================================
-- 010_app_data_prevista_erp.sql — data prevista escolhida para os ERPs
--
-- Até aqui a `dataPrevista` do pedido de compra (Bling) e da venda (Olist)
-- era sempre a `data_chegada` do snapshot (disparo + lead time do motor).
-- Agora o gestor escolhe, por RODADA, a data que vai gravada nos dois ERPs.
--
-- Por que uma coluna nova e não editar `data_chegada`: ela é a base do
-- cálculo (início da janela de cobertura que gerou as quantidades) e o
-- snapshot é imutável. A data dos ERPs é decisão de execução.
--
-- NULL = usar a data_chegada (rodadas já congeladas seguem iguais). A regra
-- de leitura é única: pedidos.builder.data_prevista(rodada).
--
-- Pedidos JÁ emitidos não mudam sozinhos: recebem a data nova no próximo
-- envio de alteração (o lote "Reenviar sem mudar quantidades" cobre isso).
--
-- Compatibilidade: sem este DDL a tela lê normalmente (cai na data_chegada)
-- e salvar a data responde MigracaoPendente.
--
-- COMO APLICAR: python scripts/migrar.py aplicar   (ou SQL Editor → Run)
-- =====================================================================

alter table app.rodada_congelada
  add column if not exists data_prevista_erp    date,
  add column if not exists data_prevista_erp_por text,
  add column if not exists data_prevista_erp_em  timestamptz;

comment on column app.rodada_congelada.data_prevista_erp is
  'dataPrevista gravada no Bling e no Olist. NULL = data_chegada (cálculo do motor).';
