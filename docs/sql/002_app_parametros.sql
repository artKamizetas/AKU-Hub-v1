-- =====================================================================
-- 002_app_parametros.sql — Persistência dos parâmetros operacionais
--
-- Conserta o bug de retenção do Streamlit Cloud (filesystem efêmero):
-- tudo que a página de Configurações edita passa a viver aqui, e o
-- config.yaml do git vira só a fonte dos DEFAULTS (Categoria A +
-- fallback). O loader faz deep-merge: yaml ← app.parametros.dados.
-- Decisões congeladas em docs/migracao-supabase.md (§2-§4).
--
-- A tabela `planejamentos` (Categoria C / cenários) do plano original NÃO
-- é criada: app.rodada_congelada (001) já cumpre esse papel.
--
-- COMO APLICAR (manual, uma vez):
--   1. Supabase Dashboard → SQL Editor → colar este arquivo → Run
--   2. (pré-requisito do 001 já atendido: schema `app` exposto na Data API)
--   3. Semear com os valores atuais: python scripts/seed_parametros.py
-- =====================================================================

-- ---------------------------------------------------------------------
-- Estado vivo: UMA linha ('default') com o bloco mutável do config
-- (Categoria B) como JSONB único — espelha o dict que os módulos etl/
-- consomem, então nenhuma página muda de estrutura.
-- ---------------------------------------------------------------------
create table app.parametros (
  id             text primary key default 'default',
  dados          jsonb not null default '{}'::jsonb,
  atualizado_em  timestamptz not null default now(),
  atualizado_por text
);

comment on table app.parametros is
  'Parâmetros operacionais do gestor (Categoria B do config). 1 linha (id=default); o loader mescla dados sobre o config.yaml.';

-- Garante a linha única desde já — o app só faz UPDATE, nunca INSERT.
insert into app.parametros (id, dados) values ('default', '{}'::jsonb)
on conflict (id) do nothing;

-- ---------------------------------------------------------------------
-- Auditoria: append a cada save (quem mudou o quê, quando).
-- O histórico guarda o estado COMPLETO salvo (não o diff) — restaurar é
-- copiar dados de volta pra app.parametros.
-- ---------------------------------------------------------------------
create table app.parametros_historico (
  id        uuid primary key default gen_random_uuid(),
  dados     jsonb not null,
  criado_em timestamptz not null default now(),
  criado_por text not null
);

create index ix_parametros_historico_criado_em
  on app.parametros_historico (criado_em desc);

comment on table app.parametros_historico is
  'Snapshot completo de app.parametros.dados a cada save da página de Configurações (auditoria/restauração).';
