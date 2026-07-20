# Documentação — AKU Dashboard (AK Uniformes)

Documentação de arquitetura, dados, regras de negócio e metodologia do dashboard
de gestão (estoque, PCP e vendas) da AK Uniformes — varejo de uniforme escolar
em Natal e Mossoró (RN), empresa do Grupo AK. A fábrica própria do grupo é a
Art Kamizetas (aparece nos contextos de produção/PCP).

> **Para quem é:** time de produto/negócio e desenvolvimento. É a fonte canônica
> "humana". O `CLAUDE.md` na raiz é a versão orientada a agente/IA (mais enxuta).

## Índice

| Documento | Conteúdo |
|---|---|
| [arquitetura.md](arquitetura.md) | Visão de processos, camadas, fluxo de dados ponta a ponta, como rodar |
| [dados.md](dados.md) | Fonte (Supabase), tabelas, DataFrames, IDs, tipagem, cache/custo |
| [regras-de-negocio.md](regras-de-negocio.md) | Regras de domínio (loja≠depósito, SKU, situações, alta temporada, colégios…) |
| [metodologia-pcp.md](metodologia-pcp.md) | **Metodologia de demanda + abastecimento** (order-up-to ancorado na alta) — o coração do sistema |
| [decisoes.md](decisoes.md) | Log cronológico de decisões (o "porquê" de cada escolha) |
| [migracao-supabase.md](migracao-supabase.md) | **Decisões congeladas** da futura migração das configs (persistência) para o Supabase — a implementar |
| [glossario.md](glossario.md) | Termos técnicos e de negócio |
| [bling-app-descricao.md](bling-app-descricao.md) | Texto de descrição do app no portal Bling (integração OAuth de Pedidos de Compra) |
| [requisitos/normalizacao-colegios.md](requisitos/normalizacao-colegios.md) | **Requisito (proposta)** — tabela configurável para normalizar o colégio (`Marca_sku`), jogando ruído em `Outros` |
| [requisitos/cobertura-alvo-rodada.md](requisitos/cobertura-alvo-rodada.md) | **Spec (implementada)** — Cobertura Alvo por rodada: antecipação deliberada em % da demanda anual; a rodada seguinte encolhe sozinha |
| [requisitos/posicao-estoque-on-order.md](requisitos/posicao-estoque-on-order.md) | **Exploração (não implementar ainda)** — on-order/em-trânsito na posição de estoque do motor + reconciliação com o Tiny |

## Mapa rápido do código

```
app.py                 Entrada — registra páginas e autenticação
config.yaml            TODAS as configurações (metas, IDs, parâmetros de PCP…)
etl/loader.py          Lê Supabase → dict de DataFrames
etl/demanda.py         Motor único de demanda + política order-up-to (PCP)
etl/fabrica.py         Sugestão tática de produção por SKU
etl/planejamento.py    Visão anual de rodadas (agrega o motor)
etl/vm_dinamico.py     VM (Visual Merchandising) — reposição de loja
etl/logistica.py       Reposição de loja (transferências)
etl/daily.py           Comercial / metas
pages/                 Telas Streamlit (Home, Daily, Logística, Simulador, Config)
```

## Manutenção desta documentação

- Atualize o doc relevante **junto com** a mudança de código (mesmo PR/commit).
- Toda decisão de negócio ou metodológica relevante entra em [decisoes.md](decisoes.md)
  com data e justificativa.
- Fórmulas e parâmetros: [metodologia-pcp.md](metodologia-pcp.md) é a referência.
