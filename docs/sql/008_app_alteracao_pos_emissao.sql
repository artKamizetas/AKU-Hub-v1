-- =====================================================================
-- 008_app_alteracao_pos_emissao.sql — ALTERAR e CANCELAR pedido já emitido
--
-- Até aqui o pedido ficava só-leitura assim que a compra ia ao Bling: ajuste
-- pós-emissão era feito à mão nos dois ERPs e o dashboard não ficava sabendo.
-- Agora o gestor altera (ou cancela) pela 4_Pedidos e o app sobrepõe o Bling
-- e o Olist — somente enquanto o pedido está em aberto nos DOIS (o status
-- nativo de cada ERP é o filtro; a checagem é do app, em pedidos/emissor.py).
--
-- 1. Três estados novos em app.pedido_compra.status:
--      EM_ALTERACAO          — repouso editável: quantidade_final é rascunho
--                              da alteração; os ERPs seguem na versão anterior
--      ALTERACAO_ENVIANDO    — lock CAS do envio da alteração
--      CANCELAMENTO_ENVIANDO — lock CAS do cancelamento nos ERPs
--
-- 2. A trava dos itens passa a aceitar RASCUNHO **ou** EM_ALTERACAO. O nome
--    da função fica (fn_item_so_em_rascunho): renomear exigiria recriar o
--    trigger sem ganho nenhum. Demais regras intactas (quantidade_sugerida e
--    origem imutáveis; item da simulação não se apaga).
--
-- 3. app.pedido_compra_revisao — o histórico do que foi aos ERPs. Uma linha
--    por versão: a emissão (nº 1), cada alteração e o cancelamento. `itens` é
--    o retrato das quantidades daquela versão. A última revisão CONCLUÍDA
--    (que não seja cancelamento) é a LINHA DE BASE: "o que está nos ERPs
--    agora". É ela que alimenta o "emitido × novo" da tela, o descarte da
--    alteração e a detecção de edição feita direto no ERP.
--      bling_ok_em / olist_ok_em — quando cada ERP confirmou aquela versão
--      concluida_em             — a operação inteira fechou (os dois lados)
--    Revisão com carimbo de um ERP e sem `concluida_em` = envio PARCIAL.
--
-- 4. Carga inicial: revisão 1 para todo pedido já emitido, a partir dos itens
--    atuais (estavam travados desde a emissão, logo SÃO o que foi emitido).
--    O app também se cura sozinho: pedido emitido sem revisão ganha a nº 1 ao
--    abrir a primeira alteração (repositorio.abrir_alteracao).
--
-- Compatibilidade: sem este DDL a tela segue funcionando em leitura e na
-- emissão; abrir alteração/cancelamento responde MigracaoPendente.
--
-- Grants: herdados do 001 (alter default privileges ... to service_role).
--
-- COMO APLICAR: python scripts/migrar.py aplicar   (ou SQL Editor → Run)
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. Estados novos
-- ---------------------------------------------------------------------
alter table app.pedido_compra drop constraint pedido_compra_status_check;
alter table app.pedido_compra add constraint pedido_compra_status_check
  check (status in ('RASCUNHO','PRONTO','COMPRA_EMITINDO','COMPRA_EMITIDA',
                    'VENDA_EMITINDO','EMITIDO','EM_ALTERACAO','ALTERACAO_ENVIANDO',
                    'CANCELAMENTO_ENVIANDO','SINCRONIZADO','CANCELADO'));

-- ---------------------------------------------------------------------
-- 2. Trava dos itens: editável em RASCUNHO ou EM_ALTERACAO
-- ---------------------------------------------------------------------
create or replace function app.fn_item_so_em_rascunho() returns trigger
language plpgsql as $$
declare
  st  text;
  pid uuid;
begin
  if tg_op = 'DELETE' then
    pid := old.pedido_id;
  else
    pid := new.pedido_id;
  end if;

  select status into st from app.pedido_compra where id = pid;

  -- st NULL = pai já removido → é o CASCADE de um delete de pedido/rodada
  -- (delete compensatório ou limpeza de congelamento abortado) — deixa passar.
  -- (No INSERT, pai inexistente é barrado logo depois pela FK.)
  if st is not null and st not in ('RASCUNHO', 'EM_ALTERACAO') then
    raise exception 'pedido não está em RASCUNHO nem EM_ALTERACAO — itens bloqueados';
  end if;

  if tg_op = 'UPDATE' then
    if new.quantidade_sugerida is distinct from old.quantidade_sugerida then
      raise exception 'quantidade_sugerida é imutável (snapshot)';
    end if;
    if new.origem is distinct from old.origem then
      raise exception 'origem do item é imutável';
    end if;
  end if;

  if tg_op = 'DELETE' and st is not null and old.origem <> 'MANUAL' then
    raise exception 'item da simulação não pode ser removido — zere a quantidade_final';
  end if;

  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end $$;

-- ---------------------------------------------------------------------
-- 3. Revisões: o que foi aos ERPs, versão a versão
-- ---------------------------------------------------------------------
create table app.pedido_compra_revisao (
  id            uuid primary key default gen_random_uuid(),
  pedido_id     uuid not null references app.pedido_compra(id) on delete cascade,
  numero        integer not null check (numero >= 1),
  tipo          text not null check (tipo in ('EMISSAO','ALTERACAO','CANCELAMENTO')),
  motivo        text,
  itens         jsonb not null default '[]',
    -- [{"item_id","sku","id_produto_bling","quantidade","custo_unit"}] — TODOS
    -- os itens do pedido naquela versão, zerados inclusive
  criado_em     timestamptz not null default now(),
  criado_por    text not null,
  bling_ok_em   timestamptz,
  olist_ok_em   timestamptz,
  concluida_em  timestamptz,

  unique (pedido_id, numero)
);

create index ix_pedido_compra_revisao_pedido on app.pedido_compra_revisao (pedido_id);

comment on table app.pedido_compra_revisao is
  'Histórico do que foi aos ERPs por pedido: emissão (nº 1), alterações e cancelamento. A última revisão concluída que não seja cancelamento é a linha de base ("o que está no Bling/Olist agora").';
comment on column app.pedido_compra_revisao.concluida_em is
  'Operação fechada nos ERPs que se aplicam. NULL com bling_ok_em/olist_ok_em preenchido = envio parcial (um ERP recebeu, o outro não).';

-- ---------------------------------------------------------------------
-- 4. Carga inicial — revisão 1 dos pedidos já emitidos
-- ---------------------------------------------------------------------
insert into app.pedido_compra_revisao
  (pedido_id, numero, tipo, motivo, itens, criado_em, criado_por,
   bling_ok_em, olist_ok_em, concluida_em)
select
  p.id, 1, 'EMISSAO', 'Carga inicial (DDL 008)',
  coalesce((
    select jsonb_agg(jsonb_build_object(
             'item_id', i.id, 'sku', i.sku, 'id_produto_bling', i.id_produto_bling,
             'quantidade', i.quantidade_final, 'custo_unit', i.custo_unit)
           order by i.sku)
      from app.pedido_compra_item i
     where i.pedido_id = p.id), '[]'::jsonb),
  p.atualizado_em, coalesce(p.atualizado_por, p.criado_por),
  case when nullif(p.bling_id, '') is not null then p.atualizado_em end,
  case when nullif(p.olist_id, '') is not null then p.atualizado_em end,
  p.atualizado_em
from app.pedido_compra p
where p.status in ('COMPRA_EMITIDA','VENDA_EMITINDO','EMITIDO','SINCRONIZADO')
on conflict (pedido_id, numero) do nothing;
