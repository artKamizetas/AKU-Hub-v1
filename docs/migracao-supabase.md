# Migração das Configurações para Supabase — Decisões

> **STATUS: ✅ IMPLEMENTADO (jul/2026) — Fases 0 e 1.** O plano congelado abaixo
> foi executado como especificado, com três ajustes registrados em
> [decisoes.md](decisoes.md):
> 1. **Tabelas no schema `app`** (criado pela Fase 0 de Pedidos de Compra), não
>    no `public` — DDL em `docs/sql/002_app_parametros.sql`, seed em
>    `scripts/seed_parametros.py`.
> 2. **`planejamentos` (Categoria C) não foi criada** — `app.rodada_congelada`
>    (001) já cumpre o papel de cenário salvo. Fase 2 considerada entregue.
> 3. **Merge com substituição de coleções**: `colegios`, `colegios_alias`,
>    `grupo_segmento`, `excecoes_sku` e `planejamento.cobertura_override`
>    substituem o bloco inteiro no merge (item apagado na UI não ressuscita do
>    yaml). Implementação: `etl/config_store.py` + `loader.carregar_config()`.
>
> O texto original segue abaixo como registro das decisões.

---

## 1. Por que migrar (o motivo é retenção)

O app roda no **Streamlit Community Cloud**, onde o **filesystem é efêmero**: toda
escrita no `config.yaml` (feita pela página de Configurações) funciona até o app
reiniciar/redeployar — aí **volta pro `config.yaml` do git e as mudanças do gestor
somem**. Hoje o gestor salva, vê "✅ salvo", e perde no próximo deploy.

Isso **invalida toda a camada "gestor decide"** que foi construída (crescimento,
proporção da baixa, segmentos, exceções). A migração é o que dá chão pra isso.

**A migração é 3 coisas** (em ordem de prioridade):
1. **Retenção** dos parâmetros operacionais (o principal) — sobreviver a restart.
2. **Cenários/planejamentos salvos** (feature nova pedida) — histórico, planejado×realizado.
3. **Auditoria** — quem mudou o quê, quando.

---

## 2. Decisões já tomadas (NÃO re-discutir)

| Questão | Decisão |
|---|---|
| Onde roda | Streamlit Community Cloud (filesystem efêmero) |
| Quer cenários nomeados? | **Sim** |
| Quer auditoria? | **Sim** (poucos admins editando) |
| Arquitetura | **Híbrida**: estrutura no `config.yaml` (git) + parâmetros no Supabase |
| Nº de tabelas | **3** (ver §4) |
| Como o app lê | `loader` faz **deep-merge**: `config.yaml` (defaults) ← Supabase (overrides) |
| Impacto nas páginas | **Nenhum** — o `loader` devolve o mesmo dict `config`; só muda a origem |
| Concorrência | poucos admins → last-write-wins aceitável; histórico recupera. Optimistic-lock (`versao`) fica pra depois se incomodar |

---

## 3. Classificação dos dados (o que vai pra onde)

### Categoria A — Estrutural / referência → **fica no `config.yaml` (git)**
Muda quase nunca, anda com o código, serve de **default** do merge:
- `fonte.nome`
- `depositos.*` (IDs de loja/depósito)
- `daily.situacoes_venda` `[9]`, `fabrica.situacoes_backlog` `[6,15]`
- `demanda.min_vendas_colegio`, `demanda.min_meses_colegio` (thresholds, sem UI)
- `daily.status_ids` (IDs do Bling; editáveis na UI mas praticamente estruturais — pode ir pro Supabase junto se preferir)

### Categoria B — Parâmetros operacionais do gestor → **Supabase (tabela `parametros`)**
É o "estado vivo" que a tela edita e que **precisa persistir**:
- `daily.metas` (Natal, Mossoró)
- `logistica.vm_padrao`, `logistica.dias_analise_giro`
- `vm.*` (dias_cobertura, inicio/fim_alta, mult_pa, vm_minimo, lead_time, nivel_servico_default, aplicar_crescimento)
- `fabrica.crescimento_pct`, `cobertura_meses`, `correcao_manual`
- `demanda.*` (nivel_servico_alta/baixa, variacao_demanda, janela_alta, aplicar_crescimento_fabrica, crescimento_observado_ativo)
- `planejamento.rodadas_datas`, `lead_time_semanas`, `periodo_historico_inicio/fim`
- **`colegios.*`** (taxa_crescimento, nivel_servico, `crescimento_grupos`, `proporcao_baixa`) — **cresce com o tempo**, é o maior candidato: são os overrides que o gestor acumula
- **`grupo_segmento`** (mapa grupo→segmento)
- **`excecoes_sku.*`** (vm, correcao, proporcao_baixa)

> Nota: tudo em Categoria B hoje é escrito no `config.yaml` via `5_Configuracoes.py`
> com `ruamel.yaml`. É exatamente o que evapora no redeploy.

### Categoria C — Cenários salvos → **Supabase (tabela `planejamentos`)** (feature nova)
Não existe hoje. Documento imutável: "o plano que aprovei em jul/2026", com os
parâmetros da época + (opcional) o resultado calculado congelado.

---

## 4. Schema proposto (3 tabelas)

```sql
-- B: estado vivo (1 linha)
parametros (
  id             text primary key default 'default',
  dados          jsonb,          -- o bloco mutável do config (Categoria B)
  atualizado_em  timestamptz,
  atualizado_por text
)

-- auditoria: append a cada save
parametros_historico (
  id             uuid primary key,
  dados          jsonb,
  criado_em      timestamptz,
  criado_por     text
)

-- C: cenários nomeados
planejamentos (
  id                  uuid primary key,
  nome                text,
  criado_em           timestamptz,
  criado_por          text,
  snapshot_parametros jsonb,      -- params usados na hora (reprodutibilidade)
  resultado           jsonb,      -- opcional: congela o cálculo p/ planejado×realizado
  observacao          text
)
```

`dados` como **JSONB único** (não key-value) porque espelha o dict que os módulos
`etl/` já consomem — nenhuma página muda. Ao salvar na tela: `UPDATE parametros` +
`INSERT parametros_historico` (auditoria de graça).

---

## 5. Plano em fases (cada uma entrega sozinha)

- **Fase 0 — Tabelas + seed.** Criar as 3 tabelas; popular `parametros.dados` com os
  valores da Categoria B do `config.yaml` atual (script único).
- **Fase 1 — Persistência (conserta o bug).** `loader` mescla Supabase sobre o yaml;
  `5_Configuracoes.py` grava no Supabase (+ histórico) em vez do `config.yaml`.
  **Aqui o problema imediato acaba.** Dá pra parar aqui.
- **Fase 2 — Cenários.** Botão "Salvar cenário" (snapshot params + resultado) e tela
  de listar/carregar/comparar.

---

## 6. Como fica o loader (o único ponto que muda)

Um `carregar_config()` no `loader.py`:
```
config = deep_merge(config_yaml_defaults, supabase_parametros_dados)
```
- Lê `config.yaml` (Categoria A + defaults).
- Sobrescreve com `parametros.dados` do Supabase (Categoria B, se existir a chave).
- `st.cache_data` + invalidação no save.
- Se o Supabase estiver indisponível → cai no `config.yaml` puro (degradação graciosa).

O `config.yaml` continua sendo a **fonte da verdade dos defaults** — facilita
introduzir parâmetro novo (adiciona no yaml; o merge preenche se o Supabase não tiver).

---

## 7. Em aberto (decidir no plano de implementação, não antes)

- **Margem de folga da baixa** (parâmetro opcional, default 1,0) — se um dia quiser
  blindar a baixa contra a ruptura branda. Não implementado; low-priority.
- **Optimistic-lock** (`versao` int) — só se a edição concorrente incomodar.
- **`status_ids`**: Categoria A ou B? (estrutural vs editável). Tanto faz; decidir na hora.
- **Secrets**: já existe `st.secrets["supabase"]` (o `loader` já lê o Bling de lá) —
  reusar a mesma conexão pra ler/gravar as tabelas novas.

---

Ver também: [decisoes.md](decisoes.md) (log cronológico) e [arquitetura.md](arquitetura.md).
