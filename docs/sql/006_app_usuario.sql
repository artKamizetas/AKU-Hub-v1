-- =====================================================================
-- 006_app_usuario.sql — Allowlist de acesso ao dashboard (login Google)
--
-- Substitui as credenciais de st.secrets["auth_config"] (usuário/senha do
-- streamlit-authenticator) pela identidade do Google: o login passa a ser
-- o OIDC nativo do Streamlit (st.login/st.user), e QUEM entra e COM QUAL
-- perfil vive aqui — editável pela aba Usuários de Configurações.
--
-- Mesmo motivo do 002 (parâmetros): cadastro em secrets.toml exige editar
-- dois arquivos (local + painel do Cloud) e redeploy a cada admissão, não
-- tem auditoria, e o filesystem do Streamlit Cloud é efêmero.
--
-- NÃO usamos auth.users (Supabase Auth/GoTrue): o app conecta com a
-- service_key, que ignora RLS, e faz toda a autorização em Python — o
-- benefício do GoTrue (RLS por usuário) não seria usado. Além disso o
-- schema `auth` não é exposto no PostgREST (exigiria a Admin API por
-- HTTP) e o GoTrue também não guarda perfil: a doc do próprio Supabase
-- manda criar uma tabela companheira. Ou seja, esta tabela existiria de
-- todo jeito. Ver docs/decisoes.md.
--
-- NÃO há auto-cadastro: e-mail fora desta tabela é barrado na tela de
-- login e NADA é escrito. Convite pelo admin é a única porta de entrada.
--
-- Grants: herdados do 001 (alter default privileges ... to service_role;
-- anon/authenticated não têm nem USAGE no schema).
--
-- COMO APLICAR: python scripts/migrar.py aplicar   (ou SQL Editor → Run)
-- =====================================================================

create table app.usuario (
  -- E-mail é a PK: é a identidade que o Google devolve e a que o admin
  -- digita ao convidar. O CHECK garante no banco a normalização que o
  -- app faz em normalizar_email() — sem isso, "Ana@x.com" e "ana@x.com"
  -- seriam duas linhas e a resolução de acesso viraria loteria.
  email          text primary key
                   check (email = lower(email) and position('@' in email) > 1),
  nome           text        not null default '',

  -- role diz O QUE a pessoa vê (auth.PAGINAS_POR_ROLE); `ativo` é a chave
  -- geral. São ORTOGONAIS de propósito: desativar sem perder o perfil
  -- (afastamento, férias) e reativar depois sem redigitar nada.
  role           text        not null
                   check (role in ('admin','supervisor','vendedor','estoque')),
  ativo          boolean     not null default true,

  criado_em      timestamptz not null default now(),
  criado_por     text,                    -- null = veio do seed desta migração
  ultimo_acesso  timestamptz,             -- best-effort, 1x por sessão
  atualizado_em  timestamptz not null default now(),
  atualizado_por text
);

comment on table app.usuario is
  'Allowlist de acesso ao dashboard. O login é Google (OIDC nativo do Streamlit); esta tabela decide SE entra (ativo) e O QUE vê (role). Porta de escrita: auth_store.py. Sem auto-cadastro — só o admin insere.';
comment on column app.usuario.email is
  'E-mail da conta Google, sempre minúsculo (CHECK). É a chave de resolução do acesso no login.';
comment on column app.usuario.role is
  'admin | supervisor | vendedor | estoque. Traduzido em páginas visíveis por auth.paginas_do_role().';
comment on column app.usuario.ativo is
  'Chave geral, independente do role — desativa o login sem perder o perfil cadastrado.';
comment on column app.usuario.ultimo_acesso is
  'Gravado uma vez por sessão, best-effort (falha nunca derruba o login). Serve ao admin para identificar conta morta.';

-- Defesa em profundidade: esta é a tabela que decide quem é admin. Hoje o
-- isolamento já vem dos grants do 001 (anon/authenticated não enxergam o
-- schema) e o service_role IGNORA RLS por definição — então isto não muda
-- nada no runtime do app, mas faz uma exposição futura acidental do schema
-- falhar fechada em vez de aberta.
alter table app.usuario enable row level security;

-- ---------------------------------------------------------------------
-- SEED — o administrador, para que exista alguém capaz de cadastrar o
-- resto pela aba Usuários no primeiro login. Os outros 4 usuários do
-- secrets.toml antigo estavam com `email` VAZIO, então não há o que
-- semear: entram pelo formulário "Adicionar usuário".
--
-- ANTES DE APLICAR: confirme que este é o e-mail da conta GOOGLE que o
-- admin usa para logar. Se for outra, troque aqui — e mantenha a mesma
-- em [acesso] admins no secrets.toml (break-glass).
--
-- `on conflict do nothing`: reaplicar é inócuo e não rebaixa quem já foi
-- promovido pela UI.
-- ---------------------------------------------------------------------
insert into app.usuario (email, nome, role, ativo) values
  ('diretoria@vistaak.com.br', 'Diogo', 'admin', true)
on conflict (email) do nothing;
