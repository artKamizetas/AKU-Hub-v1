-- =====================================================================
-- 003_app_integracoes.sql — Fase de Emissão: integrações Bling + Olist
--
-- Cria a infraestrutura de credenciais/tokens OAuth2 e auditoria das
-- integrações (Bling = pedido de COMPRA da AK Uniformes; Olist/Tiny =
-- pedido de VENDA da Art Kamizetas), e evolui app.pedido_compra para a
-- emissão em DOIS momentos (compra primeiro, venda depois).
--
-- Grants: herdados do 001 (alter default privileges ... to service_role).
--
-- COMO APLICAR (manual, uma vez):
--   1. Supabase Dashboard → SQL Editor → colar este arquivo → Run
--   2. Nada a expor: o schema `app` já está na Data API (feito no 001)
-- =====================================================================

-- ---------------------------------------------------------------------
-- Credenciais + tokens + config de negócio, 1 linha por plataforma.
-- As chaves vivem AQUI (não em secrets.toml): são editadas pela UI de
-- Configurações e o filesystem do Streamlit Cloud é efêmero.
-- ---------------------------------------------------------------------
create table app.integracao (
  id              text primary key check (id in ('bling','olist')),
  client_id       text,
  client_secret   text,            -- schema acessível só via service_role (risco aceito v1;
                                   -- alternativa futura: pgcrypto)
  redirect_uri    text,
  oauth_state     text,            -- state anti-CSRF do fluxo OAuth em curso — persistido
                                   -- no banco porque a sessão do Streamlit morre no redirect
  access_token    text,
  refresh_token   text,
  token_expira_em timestamptz,
  conectado_em    timestamptz,
  conectado_por   text,
  config          jsonb not null default '{}',
    -- bling: {"fornecedor_id": "..."}                          (Art Kamizetas no Bling da AK)
    -- olist: {"contato_id":..., "vendedor_id":..., "deposito_id":..., "situacao": 0}
  atualizado_em   timestamptz not null default now(),
  atualizado_por  text
);

insert into app.integracao (id) values ('bling'), ('olist') on conflict do nothing;

comment on table app.integracao is
  'Credenciais OAuth2 + tokens + config de negócio das integrações (bling = compra AK Uniformes; olist = venda Art Kamizetas).';

-- ---------------------------------------------------------------------
-- Auditoria de toda interação com os ERPs.
-- REGRA: `detalhe` NUNCA contém tokens/secrets — só resumos legíveis.
-- ---------------------------------------------------------------------
create table app.integracao_evento (
  id         uuid primary key default gen_random_uuid(),
  plataforma text not null,
  pedido_id  uuid references app.pedido_compra(id) on delete set null,
  acao       text not null,   -- 'oauth_conectar','oauth_refresh','testar_conexao',
                              -- 'contrato_get','emitir_compra','emitir_venda','destravar'
  sucesso    boolean not null,
  detalhe    jsonb,
  criado_em  timestamptz not null default now(),
  criado_por text
);

create index ix_integracao_evento_pedido on app.integracao_evento (pedido_id);
create index ix_integracao_evento_criado on app.integracao_evento (criado_em);

comment on table app.integracao_evento is
  'Log de auditoria das chamadas às APIs externas (Bling/Olist). Nunca gravar tokens no detalhe.';

-- ---------------------------------------------------------------------
-- Emissão no Olist + máquina de estados de 2 momentos.
-- Seguro alterar o CHECK: nenhuma linha existe em EMITINDO/EMITIDO
-- (estados reservados na Fase 0, nunca escritos). Trigger de itens
-- intacto (edição segue só em RASCUNHO).
-- ---------------------------------------------------------------------
alter table app.pedido_compra add column olist_id text;
alter table app.pedido_compra add column olist_numero text;

comment on column app.pedido_compra.olist_id is
  'ID do pedido de VENDA no Olist (Art Kamizetas). NULL até a emissão da venda.';
comment on column app.pedido_compra.olist_numero is
  'Número humano do pedido de venda no Olist. NULL até a emissão da venda.';

alter table app.pedido_compra drop constraint pedido_compra_status_check;
alter table app.pedido_compra add constraint pedido_compra_status_check
  check (status in ('RASCUNHO','PRONTO','COMPRA_EMITINDO','COMPRA_EMITIDA',
                    'VENDA_EMITINDO','EMITIDO','SINCRONIZADO','CANCELADO'));
