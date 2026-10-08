-- =====================================================================
-- 007_app_item_manual.sql — Inclusão MANUAL de itens no pedido de compra
--
-- Até aqui todo item nascia do congelamento da rodada (o motor sugeria, o
-- gestor só ajustava a quantidade). Agora o gestor pode incluir no RASCUNHO
-- um produto que a simulação não trouxe (4_Pedidos → "Adicionar produto", ou
-- digitando numa célula vazia da grade).
--
-- 1. app.pedido_compra_item.origem ('SIMULACAO' | 'MANUAL') + quem/quando
--    incluiu. A auditoria passa a distinguir o que o motor sugeriu do que o
--    gestor acrescentou. O item manual entra com quantidade_sugerida = 0 e
--    memoria_sugerida = '{}' — o motor não o sugeriu.
--
-- 2. A trava de RASCUNHO passa a valer também no INSERT. O trigger era
--    `before update or delete`: enquanto nenhum código inseria item fora do
--    congelamento, bastava. Sem esta mudança o banco aceitaria um item novo
--    num pedido PRONTO ou já EMITIDO — que sairia do nosso registro sem nunca
--    ter ido para o Bling/Olist.
--
-- 3. Item da SIMULAÇÃO não se apaga, zera-se (quantidade_final = 0): a linha
--    é a prova do que o motor sugeriu. Só item MANUAL pode ser removido.
--    O CASCADE de pedido/rodada continua passando (pai já removido → st NULL).
--
-- Compatibilidade: default 'SIMULACAO' cobre todos os itens existentes. O
-- congelamento (repositorio.congelar_rodada) insere com o pedido ainda em
-- RASCUNHO (default da coluna status), então passa pela trava nova.
--
-- Grants: herdados do 001 (alter default privileges ... to service_role).
--
-- COMO APLICAR: python scripts/migrar.py aplicar   (ou SQL Editor → Run)
-- =====================================================================

alter table app.pedido_compra_item
  add column origem text not null default 'SIMULACAO'
    check (origem in ('SIMULACAO', 'MANUAL')),
  add column adicionado_por text,
  add column adicionado_em  timestamptz;

comment on column app.pedido_compra_item.origem is
  'SIMULACAO = nasceu do congelamento da rodada (quantidade_sugerida do motor); MANUAL = incluído pelo gestor no rascunho (quantidade_sugerida = 0, sem memória de cálculo).';
comment on column app.pedido_compra_item.adicionado_por is
  'E-mail de quem incluiu o item manualmente. NULL nos itens da simulação.';

-- ---------------------------------------------------------------------
-- Trava REAL no banco, agora nas três operações. Ramos explícitos por
-- tg_op: em INSERT não existe OLD e em DELETE não existe NEW.
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
  if st is not null and st is distinct from 'RASCUNHO' then
    raise exception 'pedido não está em RASCUNHO — itens bloqueados';
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

drop trigger tg_item_so_em_rascunho on app.pedido_compra_item;

create trigger tg_item_so_em_rascunho
  before insert or update or delete on app.pedido_compra_item
  for each row execute function app.fn_item_so_em_rascunho();
