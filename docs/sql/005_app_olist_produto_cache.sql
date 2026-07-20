-- =====================================================================
-- 005_app_olist_produto_cache.sql — Cache SKU → id interno do Olist
--
-- O POST /pedidos do Olist exige `produto.id` (o id interno do Tiny), NÃO
-- aceita SKU/código. Descobrir esse id custa chamadas à API de produtos, e
-- o Olist v3 limita a 60 req/min (plano básico) — uma emissão em lote de
-- muitos pedidos estourava o rate limit (HTTP 429).
--
-- Como o id de um produto é IMUTÁVEL, guardamos cada `sku → olist_id` que já
-- resolvemos. Nas próximas emissões só vamos à API para SKUs NOVOS; depois
-- de uma ou duas rodadas de um colégio, o lote inteiro sai do cache.
--
-- É também a PONTE para a fonte definitiva (um espelho do catálogo do Tiny):
-- basta popular/atualizar esta tabela por fora que a emissão nem toca a API.
--
-- Cache é OTIMIZAÇÃO, não fonte da verdade: se esta tabela não existir (DDL
-- não aplicado), a leitura degrada para "vazio" e a escrita é ignorada — a
-- emissão continua funcionando, só resolve tudo pela API a cada vez.
--
-- Grants: herdados do 001 (alter default privileges ... to service_role).
--
-- COMO APLICAR: python scripts/migrar.py aplicar   (ou SQL Editor → Run)
-- =====================================================================

create table app.olist_produto_cache (
  sku           text primary key,
  olist_id      bigint not null,
  atualizado_em timestamptz not null default now()
);

comment on table app.olist_produto_cache is
  'Cache imutável SKU → id interno do Olist/Tiny (produto.id exigido pelo POST /pedidos). Otimização do mapeamento na emissão da venda: evita varrer/consultar a API de produtos a cada lote (rate limit 429). Populável por fora a partir de um espelho do catálogo do Tiny.';

comment on column app.olist_produto_cache.olist_id is
  'id interno do produto no Olist (imutável). SKUs são idênticos aos do Bling (confirmado pelo negócio).';
