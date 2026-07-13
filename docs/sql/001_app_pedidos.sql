-- =====================================================================
-- 001_app_pedidos.sql — Fase 0 de Pedidos de Compra
--
-- Cria o schema `app` (estado GRAVÁVEL do dashboard) com as tabelas de
-- congelamento de rodada e pedidos de compra em rascunho.
--
-- O schema `public` continua sendo o espelho READ-ONLY do Bling mantido
-- pela pipeline externa — nunca criar tabelas nossas lá. Futuramente
-- `public.pedidos_compra` será o espelho real do Bling e
-- `app.pedido_compra` a nossa intenção (par reconciliado pelo
-- sincronizador em fase futura).
--
-- COMO APLICAR (manual, uma vez):
--   1. Supabase Dashboard → SQL Editor → colar este arquivo → Run
--   2. Settings → API → Exposed schemas → adicionar "app"
--   3. (opcional) secrets.toml: schema_app = "app" em [supabase]
-- =====================================================================

create schema if not exists app;

-- Segurança: schema exposto na Data API, mas SÓ a service_role acessa
-- (anon/authenticated NÃO recebem grant — PostgREST nega o acesso).
-- O gating de quem pode escrever é o role `admin` do dashboard.
grant usage on schema app to service_role;
alter default privileges in schema app grant all on tables to service_role;
alter default privileges in schema app grant all on sequences to service_role;

-- ---------------------------------------------------------------------
-- Agregado pai: snapshot imutável de uma rodada no momento do congelamento
-- ---------------------------------------------------------------------
create table app.rodada_congelada (
  id                    uuid primary key default gen_random_uuid(),

  -- Identidade ABSOLUTA da rodada (o nº "rodada" da UI não é estável —
  -- é índice em planejamento.rodadas_datas e muda se o calendário mudar)
  mes_disparo           smallint    not null check (mes_disparo between 1 and 12),
  ano_disparo           smallint    not null check (ano_disparo >= 2024),
  data_disparo          date        not null,
  data_chegada          date        not null,
  data_chegada_seguinte date        not null,   -- fecho do intervalo de cobertura
  rodada_numero         smallint,               -- informativo (posição no calendário da época)
  janela_label          text,                   -- rótulo humano da época

  -- Snapshot (única fonte de conferência posterior: o motor ancora em
  -- Timestamp.now(), recalcular depois dá números diferentes)
  data_referencia       date        not null,   -- o "hoje" que ancorou o motor
  congelada_em          timestamptz not null default now(),
  congelada_por         text        not null,   -- st.session_state["username"]
  ativo_crescimento     boolean     not null,
  config_snapshot       jsonb       not null,   -- config.yaml completo usado no cálculo
  resultado_skus        jsonb       not null,   -- saída integral do processar_fabrica (records)

  -- CONGELANDO = inserção multi-request em andamento/abortada;
  -- ABERTA = congelamento concluído; CANCELADA = descartada (auditável).
  -- "Concluída" NÃO é persistida: é derivada (todos os pedidos ≥ PRONTO).
  status                text        not null default 'CONGELANDO'
                        check (status in ('CONGELANDO','ABERTA','CANCELADA')),
  observacao            text
);

comment on table app.rodada_congelada is
  'Snapshot imutável de uma rodada do Simulador de Produção no momento em que virou pedidos.';

-- IDEMPOTÊNCIA: uma rodada (mês×ano de disparo) só pode ter UM congelamento
-- vivo. Canceladas ficam para auditoria e liberam novo congelamento.
create unique index uq_rodada_congelada_ativa
  on app.rodada_congelada (ano_disparo, mes_disparo)
  where status <> 'CANCELADA';

-- ---------------------------------------------------------------------
-- Pedido de compra (1 por Colégio × SuperCategoria da rodada congelada;
-- relação 1:1 com futuro pedido de compra no Bling)
-- ---------------------------------------------------------------------
create table app.pedido_compra (
  id              uuid primary key default gen_random_uuid(),
  rodada_id       uuid not null references app.rodada_congelada(id) on delete cascade,
  colegio         text not null,   -- vazio normalizado p/ '(sem colégio)'
  super_categoria text not null,   -- idem
  titulo          text not null,   -- título padronizado p/ observação interna do Bling
                                   -- ex: 'AKU-PC · NEVES · CALÇAS · R08/2026'

  -- Máquina de estados: RASCUNHO → PRONTO → (futuro: EMITINDO → EMITIDO →
  -- SINCRONIZADO). CANCELADO a partir de RASCUNHO/PRONTO. Estados futuros
  -- já reservados no CHECK para não exigir DDL depois.
  status          text not null default 'RASCUNHO'
                  check (status in ('RASCUNHO','PRONTO','EMITINDO','EMITIDO',
                                    'SINCRONIZADO','CANCELADO')),

  criado_em       timestamptz not null default now(),
  criado_por      text not null,
  atualizado_em   timestamptz not null default now(),
  atualizado_por  text,
  pronto_em       timestamptz,
  pronto_por      text,

  -- Reservado para fases futuras (emissor/sincronizador) — sempre NULL na Fase 0
  bling_id        text,
  bling_numero    text,

  observacao      text,

  -- IDEMPOTÊNCIA: um grupo por rodada congelada
  unique (rodada_id, colegio, super_categoria)
);

create index ix_pedido_compra_rodada on app.pedido_compra (rodada_id);

comment on column app.pedido_compra.bling_id is
  'ID do pedido de compra espelho no Bling (public.pedidos_compra futuro). NULL até a fase de emissão.';
comment on column app.pedido_compra.titulo is
  'Título padronizado que abre o campo de observações internas do Bling na emissão.';

-- ---------------------------------------------------------------------
-- Itens do pedido (por SKU)
-- ---------------------------------------------------------------------
create table app.pedido_compra_item (
  id                  uuid primary key default gen_random_uuid(),
  pedido_id           uuid not null references app.pedido_compra(id) on delete cascade,
  sku                 text not null,
  id_produto_bling    text not null,   -- congelado aqui (produto pode inativar depois)
  produto             text,            -- descrição p/ exibição sem join no public
  tamanho             text,
  categoria           text,

  quantidade_sugerida integer not null check (quantidade_sugerida >= 0),  -- IMUTÁVEL (snapshot)
  quantidade_final    integer not null check (quantidade_final >= 0),     -- editável no RASCUNHO
  custo_unit          numeric(12,2) not null default 0,

  atualizado_em       timestamptz not null default now(),
  atualizado_por      text,

  unique (pedido_id, sku)
);

create index ix_pedido_compra_item_pedido on app.pedido_compra_item (pedido_id);

comment on column app.pedido_compra_item.quantidade_sugerida is
  'Cópia congelada de SugestaoProducao — nunca é alterada; o delta p/ quantidade_final é a auditoria da edição.';

-- ---------------------------------------------------------------------
-- Trava REAL no banco: itens só podem mudar enquanto o pedido é RASCUNHO
-- (Streamlit reroda o script a cada clique — a UI não é confiável como trava).
-- Também protege quantidade_sugerida contra UPDATE.
-- ---------------------------------------------------------------------
create or replace function app.fn_item_so_em_rascunho() returns trigger
language plpgsql as $$
declare
  st text;
begin
  select status into st from app.pedido_compra
   where id = coalesce(new.pedido_id, old.pedido_id);
  -- st NULL = pai já removido → é o CASCADE de um delete de pedido/rodada
  -- (delete compensatório ou limpeza de congelamento abortado) — deixa passar.
  if st is not null and st is distinct from 'RASCUNHO' then
    raise exception 'pedido não está em RASCUNHO — itens bloqueados';
  end if;
  if tg_op = 'UPDATE' and new.quantidade_sugerida is distinct from old.quantidade_sugerida then
    raise exception 'quantidade_sugerida é imutável (snapshot)';
  end if;
  return coalesce(new, old);
end $$;

create trigger tg_item_so_em_rascunho
  before update or delete on app.pedido_compra_item
  for each row execute function app.fn_item_so_em_rascunho();
