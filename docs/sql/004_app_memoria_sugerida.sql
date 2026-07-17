-- =====================================================================
-- 004_app_memoria_sugerida.sql — Memória de cálculo por item do pedido
--
-- Adiciona app.pedido_compra_item.memoria_sugerida (jsonb): os principais
-- drivers que definiram a quantidade_sugerida no motor order-up-to —
-- demanda do período (alta/baixa), estoque da rede, backlog, estoque de
-- segurança, estoque-alvo, estoque projetado na chegada e nível de serviço.
--
-- MOTIVO: na revisão do rascunho o gestor precisa ver POR QUE a sugestão é
-- aquela, ao lado da quantidade — sem carregar o jsonb pesado
-- rodada_congelada.resultado_skus (todos os SKUs da rede). Esta é uma cópia
-- CURADA e enxuta, congelada no momento do congelamento (imutável, como
-- quantidade_sugerida — só é escrita no INSERT; atualizar_quantidades toca
-- exclusivamente quantidade_final).
--
-- Compatibilidade: default '{}' cobre os itens já existentes (rodadas
-- congeladas antes desta migração ficam sem memória — a UI degrada avisando).
--
-- Grants: herdados do 001 (alter default privileges ... to service_role).
-- Trigger de itens intacto — o campo nunca é atualizado.
--
-- COMO APLICAR: python scripts/migrar.py aplicar   (ou SQL Editor → Run)
-- =====================================================================

alter table app.pedido_compra_item
  add column memoria_sugerida jsonb not null default '{}'::jsonb;

comment on column app.pedido_compra_item.memoria_sugerida is
  'Cópia congelada dos drivers da SugestaoProducao (order-up-to): demanda do período (total/alta/baixa), estoque da rede, backlog, estoque de segurança, estoque-alvo, estoque projetado na chegada e nível de serviço. Enxuto p/ a revisão do rascunho sem carregar resultado_skus.';

-- ---------------------------------------------------------------------
-- Backfill dos itens já congelados: a feature nasceu depois que rodadas
-- foram congeladas. A memória vem de rodada_congelada.resultado_skus
-- (que já guarda a memória integral por SKU), casada por SKU. As chaves
-- espelham exatamente o pedidos/builder._memoria_sugerida.
--
-- A trigger de itens (tg_item_so_em_rascunho) bloqueia UPDATE de itens de
-- pedidos fora de RASCUNHO (ex.: CANCELADOS) — é desligada só durante o
-- backfill. memoria_sugerida não é editável pela UI nem por
-- atualizar_quantidades, então popular o histórico aqui é seguro.
-- ---------------------------------------------------------------------
alter table app.pedido_compra_item disable trigger tg_item_so_em_rascunho;

-- CTE achata cada resultado_skus em (rodada, sku, memória) — evita o LATERAL
-- referenciar a tabela-alvo do UPDATE (não permitido no Postgres). SKUs são
-- únicos por rodada (validar_pre_congelamento barra duplicados), então o join
-- é 1:1.
with mem as (
  select rc.id as rodada_id,
         e->>'SKU' as sku,
         jsonb_build_object(
           'vendas_hist',           e->'VendasHist',
           'demanda_periodo',       e->'DemandaProjetada',
           'demanda_periodo_alta',  e->'DemandaPeriodoAlta',
           'demanda_periodo_baixa', e->'DemandaPeriodoBaixa',
           'estoque_rede',          e->'EstoqueRede',
           'backlog',               e->'Backlog',
           'estoque_seguranca',     e->'EstoqueSeguranca',
           'estoque_meta',          e->'EstoqueMeta',
           'estoque_projetado',     e->'EstoqueProjetado',
           'nivel_servico',         e->'NivelServico',
           'janela_label',          e->'JanelaLabel'
         ) as memoria
  from app.rodada_congelada rc
  cross join lateral jsonb_array_elements(rc.resultado_skus) e
)
update app.pedido_compra_item i
set memoria_sugerida = mem.memoria
from app.pedido_compra p, mem
where i.pedido_id = p.id
  and mem.rodada_id = p.rodada_id
  and mem.sku = i.sku
  and i.memoria_sugerida = '{}'::jsonb;

alter table app.pedido_compra_item enable trigger tg_item_so_em_rascunho;
