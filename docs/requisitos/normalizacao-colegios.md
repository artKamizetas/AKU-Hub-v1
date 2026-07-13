# Requisito — Normalização configurável de Colégios

> Status: **implementado** (núcleo + UI). Falta só D4 (migração de parâmetros por
> colégio renomeado). Autor: engenharia. Data: 2026-07-10.
>
> **Decisões fechadas (2026-07-10):** **D1 = opção A** (identidade + exceções — só
> lista o que muda; ruído vira `Outros`). **D2 = helper central** `aplicar_alias_colegio`
> aplicado no topo de cada entrypoint ETL, **sem tocar no `loader.py`**, preservando
> `Marca_sku_raw`. **D3 = chave `colegios_alias`**. **D4 (migração)** adiada para a fase de UI.

## 1. Contexto e problema

O "colégio" de cada produto vive na coluna `detalhes["Marca_sku"]`. Ela **não é
calculada pelo dashboard** — chega pronta do Supabase (`Produtos_detalhes.marca`,
renomeada em [`etl/loader.py`](../../etl/loader.py) para `Marca_sku`). O valor é
extraído **por processamento de string da SKU** na pipeline externa
Bling→Supabase (mantida por outra equipe). O `loader.py` só limpa (`str`/`strip`),
não interpreta.

Consequência: SKUs fora do padrão produzem colégios **equivocados** — ex.: `"27"`,
tokens numéricos, siglas soltas. Esses valores viram "colégios" de primeira classe
e poluem tudo que agrupa/filtra/parametriza por colégio.

**Objetivo deste requisito:** uma **tabela configurável** que define como cada
valor cru de `Marca_sku` deve ser **exibido/agrupado** — ex.: `ADC` permanece
`ADC`; `27` e outros ruídos entram como `Outros` — **sem** tocar na extração
upstream (fora de escopo agora).

### Explicação simples
Hoje a etiqueta do colégio é "colada" automaticamente lendo o código do produto.
Quando o código é estranho, a etiqueta sai errada (`27`). Não vamos consertar a
máquina que cola etiqueta agora; vamos por um **de-para** na nossa mão: uma
tabelinha onde dizemos "esse valor cru chama-se assim" e jogamos o lixo todo num
balde `Outros`.

## 2. O que já existe na plataforma (reaproveitar)

O padrão pedido **já existe** para outra dimensão — é o molde a copiar:

- **`config.yaml["grupo_segmento"]`** + `mapa_grupo_segmento(config)` /
  `segmento_do_grupo(grupo, config)` em [`etl/demanda.py`](../../etl/demanda.py):
  dicionário `default no código` (`SEGMENTO_POR_GRUPO`) **sobrescrito** por
  `config`, **editável na UI** via `st.data_editor` (Configurações → Aba 1,
  "Agrupamento de Grupos em Segmentos", [`pages/5_Configuracoes.py`](../../pages/5_Configuracoes.py#L563)).
  Grupo sem mapa cai em `"Outros"`. **É exatamente a mecânica que queremos para colégio.**
- **`config.yaml["colegios"]`**: parâmetros *por colégio* (`taxa_crescimento`,
  `nivel_servico`, `proporcao_baixa`), com **descoberta dinâmica** dos colégios a
  partir de `Marca_sku` ([`5_Configuracoes.py:429`](../../pages/5_Configuracoes.py#L429)).
  Hoje `27` aparece aqui como colégio configurável — é um dos sintomas.

Ou seja: **não criar nada do zero** — estender o padrão `grupo_segmento` para os colégios.

## 3. Escopo

**Dentro:**
- Camada de normalização crua→canônica de colégio, configurável, com balde `Outros`.
- Aplicação em **todos** os consumidores (cálculo + filtros + descoberta na config).
- Editor na página de Configurações.

**Fora (não fazer agora):**
- Corrigir a extração de colégio na pipeline Bling→Supabase (upstream, outra equipe).
- Reescrever a lógica de VM/PCP em si — só trocamos a *fonte do nome do colégio*.
- Persistência em Supabase (segue o plano de [migracao-supabase.md](../migracao-supabase.md); por ora vive no `config.yaml`).

## 4. Onde o colégio é lido hoje (mapa de impacto)

Todo ponto abaixo lê `Marca_sku` como colégio. Todos precisam passar a ver o **nome canônico**.

| Arquivo | Ponto | Uso |
|---|---|---|
| `etl/demanda.py` | `calcular_sazonalidade_por_colegio` (~L139) | sazonalidade por colégio |
| `etl/demanda.py` | `calcular_crescimento_observado` (~L267) | crescimento alta-sobre-alta por colégio×segmento |
| `etl/demanda.py` | `calcular_demanda_mensal_por_sku` (~L491) | `taxa_crescimento_efetiva`, `proporcao_baixa_efetiva` |
| `etl/vm_dinamico.py` | `map_id_colegio` (L54), L115-118 | taxa de crescimento **e** nível de serviço por colégio → **afeta VM e Pulmão** |
| `etl/logistica.py` | L141 (`"Colegio"`) | coluna de saída → **filtro** na tela |
| `etl/fabrica.py` | L144 (`"Colegio"`) | coluna de saída → **filtro** na tela |
| `etl/daily.py` | L51/L70 | enriquece itens com `Colegio` |
| `pages/2_Logistica.py` | L81-84, L97 | **selectbox de filtro** "Colégio" |
| `pages/3_Fabrica.py` | L364-367, L384 | **selectbox de filtro** "Colégio" |
| `pages/5_Configuracoes.py` | L429-431 | **descoberta dinâmica** dos colégios p/ os editores de parâmetros |
| `config.yaml["colegios"]` | — | parâmetros por colégio, **chaveados pelo nome** |

## 5. Solução proposta

### 5.1 Modelo de dados (`config.yaml`)

Nova chave, análoga a `grupo_segmento` — **de-para do valor cru para o canônico**:

```yaml
# De-para de normalização de colégio (Marca_sku cru → nome de exibição).
# Só liste o que MUDA: valor ausente = mantém o cru (identidade).
# Use "Outros" para jogar ruído (SKU fora de padrão) num balde único.
colegios_alias:
  "27": "Outros"
  "31": "Outros"
  "NEV": "Nova Era"      # exemplo de renomear/unificar
```

Semântica recomendada: **identidade por default** (valor não listado é mantido
como está) + overrides explícitos. Motivo: o ruído é ilimitado e imprevisível
(`27`, `31`, …), mas os colégios reais são poucos e estáveis — listar só as
exceções é o menor esforço de manutenção e **não some silenciosamente** com um
colégio novo legítimo. Ver decisão em aberto **D1**.

### 5.2 Camada de normalização (helpers em `etl/demanda.py`)

Espelhar `mapa_grupo_segmento` / `segmento_do_grupo`:

```python
def mapa_colegio(config: dict = None) -> dict:
    """De-para efetivo Marca_sku(cru) → colégio canônico (config['colegios_alias'])."""
    m = {}
    if config and config.get("colegios_alias"):
        m.update({str(k).strip(): str(v).strip() for k, v in config["colegios_alias"].items()})
    return m

def colegio_efetivo(marca_sku, config: dict = None) -> str:
    """Nome canônico do colégio. Default = identidade (mantém o cru)."""
    raw = str(marca_sku or "").strip()
    return mapa_colegio(config).get(raw, raw)
```

(Opcional, item 5.4: heurística `parece_ruido(raw)` para *sugerir* `Outros` no editor.)

### 5.3 Ponto de aplicação (recomendado)

**Normalizar em um único ponto e cedo**: cada função ETL já recebe `config` e
`detalhes`. Adicionar, no topo dos consumidores, a normalização da coluna que
alimenta o colégio, roteando por `colegio_efetivo` — **exatamente como
`segmento_do_grupo` é chamado em cada consumidor** (precedente do repositório).

Recomendação concreta: **derivar uma coluna canônica `Colegio` em `detalhes`
uma vez**, preservando o cru:
- `detalhes["Marca_sku_raw"]` = valor cru (auditoria);
- `detalhes["Marca_sku"]` = `colegio_efetivo(cru, config)` (canônico).

Assim os `col_map`/`Colegio` já existentes passam a ler o canônico sem reescrita,
e os **filtros das telas** (que leem a coluna `Colegio` de saída do ETL) e a
**descoberta dinâmica** na página de Configurações herdam o nome certo de graça.
Como `loader.carregar_dados()` não recebe `config` (e mudar isso é item sensível —
ver CLAUDE.md), a normalização roda **nos entrypoints ETL** (`processar_logistica`,
`processar_fabrica`, funções do motor de demanda) ou num helper compartilhado
`aplicar_alias_colegio(dados, config)` chamado logo após o load em cada página.
Ver decisão em aberto **D2**.

### 5.4 Estratégia de correspondência / default — **decisão-chave (D1)**

| Opção | Como funciona | Prós | Contras |
|---|---|---|---|
| **A. Identidade + exceções** (recomendada) | lista só o que muda; resto mantém cru | menor manutenção; colégio novo não some | ruído novo aparece cru até ser mapeado |
| B. Whitelist estrita | só nomes listados sobrevivem; resto → `Outros` | telas sempre limpas | precisa cadastrar todo colégio real; novo colégio cai em `Outros` sem aviso |
| C. Identidade + heurística | como A, mas o editor **sugere** `Outros` p/ ruído (ex.: só dígitos, `len < 3`) | pouco trabalho manual; seguro | heurística é palpite; só sugere, não decide |

Recomendação: **A com a assistência de C** — default identidade; o editor
pré-sinaliza prováveis ruídos (coluna "Sugestão") para o gestor confirmar em 1 clique.

### 5.5 UI — editor em Configurações (Aba 1)

Novo bloco "**Normalização de Colégios**", espelhando o editor de segmentos:
- Lista **todos os `Marca_sku` crus distintos** encontrados nos dados, com
  **contagem de SKUs** (ordenar por volume desc — os grandes primeiro).
- Coluna editável "**Colégio (exibição)**"; default = valor cru; digitar `Outros`
  (ou outro nome) agrupa/renomeia. Coluna "Sugestão" (heurística) e "SKUs" só-leitura.
- Botão salvar → grava `config["colegios_alias"]` (só as linhas que diferem do cru),
  `salvar_config` (ruamel, preserva comentários) + `st.cache_data.clear()`.
- Os editores de **parâmetros por colégio** (taxa/nível/proporção) passam a
  descobrir os colégios **canônicos** (pós-alias), não os crus.

### 5.6 Interação com `config["colegios"]` (parâmetros) + migração

- Parâmetros por colégio passam a ser **chaveados pelo nome canônico**. Ex.: se
  `27`→`Outros`, os parâmetros de `Outros` valem para todo o ruído agrupado.
- **Migração única:** entradas existentes em `config["colegios"]` chaveadas por
  valor cru que foi renomeado devem ser remapeadas para o canônico (script ou
  aviso na UI). Baixo volume hoje (o editor está praticamente vazio) — provável
  migração trivial/manual.

### 5.7 Rastreabilidade
Preservar `Marca_sku_raw` garante que o gestor consiga **auditar** o que caiu em
`Outros` (ex.: uma coluna/expander "ver crus deste balde"). Sem isso, mapear
ruído vira caixa-preta.

## 6. Mudanças por arquivo (checklist de implementação)

- [ ] `etl/demanda.py`: `mapa_colegio`, `colegio_efetivo` (+ opcional `parece_ruido`).
- [ ] Ponto de aplicação (5.3): derivar `Colegio` canônico + `Marca_sku_raw` (helper único).
- [ ] `etl/vm_dinamico.py`, `etl/logistica.py`, `etl/fabrica.py`, `etl/daily.py`,
      `etl/demanda.py` (3 pontos): ler o colégio **canônico**.
- [ ] `pages/5_Configuracoes.py`: novo editor de alias; descoberta dos params por canônico; migração.
- [ ] `pages/2_Logistica.py`, `pages/3_Fabrica.py`: filtros herdam canônico (sem mudança se lerem a coluna de saída).
- [ ] `config.yaml`: chave `colegios_alias` documentada.
- [ ] `docs/`: registrar em [decisoes.md](../decisoes.md); nota em [regras-de-negocio.md](../regras-de-negocio.md) e [dados.md](../dados.md).

## 7. Casos de teste (pytest — estender a suíte)

- `colegio_efetivo("ADC", cfg) == "ADC"` (identidade sem mapa).
- `colegio_efetivo("27", cfg_com_27_outros) == "Outros"`.
- `colegio_efetivo("  27 ", cfg) == "Outros"` (strip).
- `colegio_efetivo("", cfg) == ""` (vazio segue vazio → fallback empresa, como hoje).
- `mapa_colegio` sobrescreve identidade só nas chaves listadas.
- Integração: dois crus (`27`,`31`)→`Outros` **somam** demanda/estoque no mesmo balde.
- Params por colégio resolvem pelo **canônico** (taxa de `Outros` aplicada ao ruído agrupado).
- Filtro de tela lista o canônico, sem duplicatas de ruído.

## 8. Decisões em aberto (precisam de sign-off)

- **D1 — Default de correspondência:** A (identidade+exceções, recomendada), B
  (whitelist estrita) ou C (identidade+heurística)? Define a UX do editor e o risco
  de colégio novo cair em `Outros`.
- **D2 — Ponto de aplicação:** derivar coluna canônica em `detalhes` via helper nos
  entrypoints ETL (recomendado) **vs.** aplicar `colegio_efetivo` pontualmente em
  cada consumidor. Trade-off: 1 ponto central vs. consistência com o precedente `grupo_segmento`.
- **D3 — Nome da chave:** `colegios_alias` (proposto) vs. `colegio_nomes` / `normalizacao_colegio`.
- **D4 — Migração de `config["colegios"]`:** script automático vs. aviso manual na UI (dado o baixo volume atual).

## 9. Faseamento e esforço

1. **Núcleo** (helpers + ponto de aplicação + testes) — ~0,5 dia. Já entrega
   normalização nos cálculos (VM/PCP/logística) e filtros.
2. **UI** (editor de alias + descoberta canônica dos params + rastreabilidade) — ~0,5–1 dia.
3. **Migração + docs** — ~0,25 dia.

Esforço total estimado: **~1,5–2 dias**. Sem novas dependências. Risco baixo
(camada aditiva; `Marca_sku_raw` preserva o dado original).
