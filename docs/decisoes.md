# Log de Decisões

Registro cronológico das decisões de arquitetura, metodologia e negócio — o
"porquê" de cada escolha. Formato leve inspirado em ADR (Architecture Decision
Records). Adicione no topo as mais recentes.

> Datas em AAAA-MM. Marque decisões revertidas com ~~tachado~~ e um ponteiro para a
> que a substituiu.

---

## 2026-07 · v1 vai ao ar com persistência via config.yaml (migração Supabase deferida)
Decisão consciente: subir a v1 com as configs no `config.yaml` **mesmo sabendo que no
Streamlit Cloud a escrita não persiste no redeploy** (filesystem efêmero). A migração
pro Supabase (retenção + cenários + auditoria) fica pra uma fase seguinte, para ir ao
ar mais rápido. **As decisões da migração estão CONGELADAS em
[migracao-supabase.md](migracao-supabase.md)** (3 categorias de dado, 3 tabelas, merge
no loader, plano em fases) — o plano de implementação futuro só executa, não
re-discute. **Por quê aceitar a persistência ruim agora:** o valor de ter o simulador
(demanda ancorada na alta, crescimento híbrido, order-up-to) rodando supera o
incômodo de re-configurar após um redeploy, que é raro. Impacto operacional enquanto
não migra: se o app reiniciar, os overrides do gestor (crescimento, proporção,
segmentos, exceções) voltam ao commitado — reaplicar pela tela.

## 2026-07 · Proporção da baixa: global + override manual (não por categoria)
A proporção da baixa saiu de **por Super_categoria** para **global** (Σbaixa/Σalta da
empresa, últimos 2 ciclos ≈ 0,43) + **override manual** por SKU
(`excecoes_sku[SKU].proporcao_baixa`) e por colégio (`colegios[COL].proporcao_baixa`),
cascata SKU→colégio→global (`proporcao_baixa_efetiva`). Chave obsoleta
`demanda.proporcao_baixa_override` (por categoria) removida. **Por quê — decidido por
backtest (2023-25), não por opinião:**
1. "Olhar só a baixa" vs "olhar o ano inteiro" são a **mesma conta** (proporção =
   peso_baixa/peso_alta) — WAPE 38,9% vs 38,2%. Falsa escolha.
2. Ancorar na alta **não é mais preciso** que usar a baixa própria do SKU — empatam
   (~38%), e nos gigantes a baixa própria até ganha. A vantagem da âncora é
   **robustez** (funciona p/ SKU novo/sem histórico), não precisão.
3. Categoria é o **pior** eixo (56% vs global 26% no teste de nível); mais
   granularidade **piora** (baixa esparsa → overfit).
4. Protótipo de híbrido por credibilidade (shrinkage baixa-própria⇄global): **não
   bate o global** — teto de erro ~48%, só redistribui erro entre faixas (ajuda os
   ~17 gigantes, piora o meio). Negativo útil: não construir a máquina; o override
   manual pega os mesmos gigantes de forma transparente.
- **Ruptura na baixa** reavaliada como **branda e auto-corrige** (falta 1 tamanho no
  fim da alta, volta em 2-3 meses; o dado do ano compensa) → não justifica modelo de
  cobertura/censura. E não é mensurável com o dado atual de qualquer forma.
- **Régua de esforço:** a baixa tem teto de erro alto **e** baixa alavanca (rola das
  sobras no calendário limpo) → parar de refinar aqui. Global + override e encerra.
- Editável na tela: coluna "Proporção baixa" no editor de Colégios (pré-preenchida com
  o global) + coluna `proporcao_baixa` no CSV de exceções (por SKU).

## 2026-07 · Segmento configurável + calendário de 5 rodadas + período histórico
Refinamentos após validação local com NEV e FAC:
- **Segmento como parâmetro** (`config.grupo_segmento`, editável na tela; default no
  código): o agrupamento grupo→segmento deixou de ser fixo. **EDF (Ed. Física, ~21%
  do volume) separado de Esporte** (ESP/EQP, times) — usos distintos que estavam num
  balde só. EFM→Médio, IDF→Infantil, FDF→Fundamental, EIF/TEI/DIA em baldes próprios.
- **Calendário explícito de 5 rodadas** (`rodadas_datas`): Jul/26, Out/26 · Mar/27,
  Jun/27, Out/27 (+ Mar/28 como fecho). Resolve o teto de cobertura: a rodada do pico
  (Out) passou a cobrir **5 meses (Nov→Mar)**, não 9 — as rodadas de Mar/Jun quebram
  o intervalo. Fecha a pendência do "teto por rodada".
- **Período histórico esclarecido**: define o FORMATO do ano (distribuição da baixa +
  base dos SKUs só-de-baixa), NÃO o tamanho do pico. O peso só é usado em 1 ponto
  (`distribuicao_mensal_baixa`); o peso do pico é só exibição. Deve ser janela larga
  (12+ meses); a estreita zera ~864 SKUs só-de-baixa.
- **Matriz de crescimento pré-preenchida** com o observado (coluna Observado + Origem):
  o sistema propõe, o planejador sobrescreve; célula igual ao observado fica viva.
- **Bugfix ruamel**: `pd.Timestamp` rejeita `DoubleQuotedScalarString` (a tela usa
  ruamel) → `str()` no editor de rodadas e em `_candidatas_rodadas`.

## 2026-07 · Crescimento híbrido: observado (colégio×segmento) + manual
O `+10% cego` (`fabrica.crescimento_pct` aplicado a todo SKU, tabela de colégios
vazia) punia quem cai e subestimava quem cresce. Adicionada uma **camada observada**
(`calcular_crescimento_observado`): mede o crescimento realizado nas ALTAS
(alta-sobre-alta — sinal limpo, a baixa tem ruptura de estoque) por colégio e por
**segmento** (nível intermediário `SEGMENTO_POR_GRUPO`: EF1·EF2·EFD→Fundamental,
EME·PRE·CUR→Médio, EDF·ESP·EQP→Esporte/EdFísica…), clamp [0.5,2.0], gate de volume
≥30 (senão vira ruído tipo DRM 22×). Entra na cascata de `taxa_crescimento_efetiva`
**abaixo dos overrides manuais** (manual do planejador sempre vence) e acima do
global. Flag `demanda.crescimento_observado_ativo`. **Por quê:** o crescimento é
centrado no colégio E na série (colégios expandem turmas em séries específicas — ex:
NEV Ensino Médio +51% com o colégio flat); e o dado não sabe de expansões futuras,
então precisa do override manual. **Efeito:** não muda o total da rede (~+11%),
redistribui para o mix certo. Validado nos dados: NEV Médio 1.10→1.51, LMN 1.10→0.71,
IBR Esporte 1.10→0.66. **Casos de borda:** OVD rescindiu (âncora=0 → demanda ~0
sozinho); colégios novos (CTE/DRM/SNC) sem histórico → caem no global até input
manual; previsão por alunos-por-turma p/ recém-contratados é FUTURO. Falta: pré-preencher
a matriz da UI (`5_Configuracoes`) com o observado + dashboard (protótipo feito fora do app).

## 2026-07 · Calendário explícito de rodadas + UI honesta do período
Refinamento da metodologia a partir do questionamento da diretoria ("por que a
sugestão dá 96 se vendi 62?"). Três mudanças:
1. **Calendário explícito** (`planejamento.rodadas_datas`, lista de datas ISO de
   disparo deste ano E do próximo): substitui a suposição de que as rodadas se
   repetem igual todo ano. Permite rodada atrasada este ano e antecipada no
   próximo; a última data só fecha o intervalo da penúltima. Vazio = fallback
   nos meses fixos (`rodadas`). Editor na página de Configurações.
2. **Split da demanda do período** no motor (`DemandaPeriodoAlta`/`Baixa` +
   `MesesIntervalo` + `data_chegada_seguinte`) e **UI honesta** na Sugestão por
   SKU: o cabeçalho da coluna diz o intervalo real coberto (ex: "Nov/26→Ago/27,
   9,0 meses") e separa a parcela do pico da parcela da baixa — a comparação
   "Demanda 81 vs Vendas Alta 62" comparava 9 meses com 3.
3. **Variação da Demanda medida dos dados** (2019→2026, razão real/âncora por
   SKU com crescimento agregado removido): CV por temporada ≈ 0,42–0,72 (bem
   acima dos 0,25 configurados), caindo com volume (banda A 0,60 / B 0,74 /
   C 0,85). Erro agregado da empresa é baixo (±8%) — o caos é no mix por SKU.
   Consequência estratégica: proteger SKU a 99% contra CV~0,6 via estoque é
   antieconômico; a resposta certa é encurtar a exposição (rodada reativa no
   meio do pico) e diferenciar NS. **Decisões de valor de CV e calendário
   definitivo: pendentes da diretoria** (cenários simulados em 2026-07).

## 2026-07 · Vocabulário único alta/baixa no motor de demanda
O código falava 3 dialetos para as mesmas duas estações (pico/manutenção/inglês).
Renomeado para o idioma do negócio: `pico_total`→`demanda_alta` (coluna
`PicoTotal`→`DemandaAlta`), `maint_total`→`demanda_baixa`,
`fator_manutencao()`→`calcular_proporcao_baixa()` (chave config
`fator_manutencao_override`→`proporcao_baixa_override`),
`shape_manutencao()`→`distribuicao_mensal_baixa()`, `pico_raw`→`vendas_ultima_alta`,
`tem_pico`→`vende_na_alta`, `vendas_manut_janela`→`vendas_baixa_recentes`. Chaves
`planejamento.sazonalidade_inicio/fim`→`periodo_historico_inicio/fim` (a UI já
chamava de "Período histórico"; a chave é que não). **Por quê:** a diretoria lia
`fator_manutencao`/`shape`/`maint_total` e não entendia sem tradutor; a fórmula
central agora lê `demanda_baixa = demanda_alta × proporcao_baixa`. Sem mudança de
metodologia — só nomenclatura (mesmo espírito do rename DI/SS/S/OH). De→Para
completo no [glossário](glossario.md). Aproveitado: removida linha morta em
`fabrica.py` (`pico_total` atribuído e nunca usado). **Atenção:** config antigo com
as chaves `sazonalidade_*`/`fator_manutencao_override` é ignorado silenciosamente
(leituras usam `.get` com default) — config.yaml renomeado junto no mesmo commit.

## 2026-07 · Limpeza de parâmetros obsoletos + UI por subsistema
Auditoria da página de Configurações contra o uso real no código antes da migração
para o Supabase. **Removidos 3 parâmetros mortos** (a UI mostrava, nenhum motor lia):
`planejamento.buffer_pct` (o order-up-to tira a margem do nível de serviço, não de um
buffer %), `logistica.dias_cobertura_minima` e as colunas `dias_analise`/`sazonalidade`
das exceções de SKU (resquício pré-order-up-to; só `vm` e `correcao` são consumidos).
O formulário da Aba 1 foi **reorganizado nos 3 subsistemas** da metodologia atual —
Comercial (Daily), Reposição de Loja (VM Dinâmico + fallback) e Produção (Demanda +
Planejamento + fallback da Fábrica) — no lugar das 6 seções soltas. `fonte.nome`
corrigido de "Google Sheets" para "Supabase". **Por quê:** não faz sentido carregar
knob morto para dentro do schema novo do Supabase; a reorganização espelha os 3 motores
reais (`daily`/`vm_dinamico`/`demanda`). Passo 1 da migração config.yaml → Supabase.

## 2026-07 · Termos do motor renomeados para português
As colunas/variáveis do motor order-up-to passaram de siglas em inglês para nomes
em português: `DI`→`DemandaPeriodo`, `SS`→`EstoqueSeguranca`, `S`→`EstoqueAlvo`,
`OH`→`EstoqueProjetado`; locais `z`→`fator_servico`, `cv`→`variacao_demanda`.
**Por quê:** a diretoria não entendia as siglas ao ler as fórmulas; o padrão do
projeto é código em português. Sem mudança de metodologia — só nomenclatura. A chave
de config `cv_demanda` também foi renomeada para `variacao_demanda` (config.yaml +
leitura/escrita na página de Configurações). Ver mapeamento no [glossário](glossario.md). Onde uma sigla curta ajuda (glossário), usa-se a inicial intuitiva do nome — **FS** (Fator de Serviço), **VD** (Variação da Demanda), **DP** (Desvio-Padrão) — no lugar dos símbolos de estatística `z`/`cv`/`σ`, cujas letras não têm relação com o nome.

## 2026-07 · Documentação de processo no repo (`docs/`)
Criada esta estrutura `docs/` para registrar arquitetura, dados, regras de negócio
e metodologia versionadas junto ao código. **Por quê:** decisões e metodologia
estavam só em memória de sessão da IA e no `CLAUDE.md`; o time precisa de uma fonte
canônica humana e durável.

## 2026-07 · Abatimento de estoque explícito na Visão Geral
Cada rodada mostra `Alvo (demanda+segurança) − estoque útil = Produção`. **Por
quê:** dúvida legítima da diretoria ("o número considera o estoque existente?").
Esclarecido que **sim, mas por SKU** — estoque no tamanho errado não abate, então
dos ~10k em estoque só ~5k reduzem o pedido do pico.

## 2026-07 · Auditoria de cálculos — 3 bugs corrigidos
Revisão minuciosa do motor após números suspeitos. Corrigidos:
1. **Estoque de segurança escalava linear** com o intervalo (`z×cv×DI`). Passou a
   `z×cv×DI/√meses` (Silver-Pyke-Peterson) — sem isso a rodada de 9 meses inflava
   3× (R2 de 24k→18k).
2. **Detecção da temporada quebrava** se `janela_alta` viesse fora de ordem da UI.
   Corrigido com ordenação cronológica robusta (`_ordenar_janela_cronologica`).
3. **186 SKUs que só vendem na baixa recebiam demanda 0** (âncora só no pico).
   Corrigido com fallback por vendas de manutenção recentes.

## 2026-07 · Metodologia de PCP: order-up-to ancorado na alta
Substituído o dimensionamento por "sazonalidade-peso por mês + % manual por rodada"
pela política **order-up-to (R,S) com projeção forward**, ancorando a demanda na
última temporada de alta. Ver [metodologia-pcp.md](metodologia-pcp.md). **Por quê:**
o modelo anterior misturava conceitos (venda mensal, cobertura por tempo, % em alta)
que não se comunicavam; a diretoria queria uma tela que servisse tanto para
planejar quanto para emitir o pedido, com a mesma conta. Decisões de negócio que
fundamentaram: nível de serviço alta 99% / baixa 92%; fábrica própria (LT ~4 sem);
alta = Dez-Jan-Fev; capacidade não é gargalo; uniforme não perece.
- **Sliders de % por rodada REMOVIDOS** — o tamanho de cada rodada emerge da
  projeção, não de um % manual.
- **ESTE ANO: 2 rodadas** (`[7, 10]`), 3 no futuro.

## 2026-07 · Crescimento por (colégio × grupo) com toggle
`taxa_crescimento` deixou de ser só por colégio e passou a aceitar override por
grupo/série (matriz colégio×grupo na UI). Vale para fábrica e VM de logística, com
toggle liga/desliga em cada um. **Por quê:** alguns colégios só crescem produtos de
uma série (ex: ensino médio); o toggle permite comparar cenário com/sem crescimento.

## 2026-07 · VM Dinâmico migrado do Excel para config.yaml
Os parâmetros do VM (Visual Merchandising / reposição de loja) saíram do
`data/Parametros_VM.xlsx` (arquivo local, fora do git, ausente em deploy) para o
`config.yaml`, editáveis pela página de Configurações. **Por quê:** sem o Excel, a
Logística caía para um VM fixo de 10 unidades igual para todo SKU — o motor de
cálculo por venda já existia, só faltava a fonte dos parâmetros.

## 2026-06 · Migração Google Sheets → Supabase
A fonte de dados passou de Google Sheets (via Apps Script) para Supabase (espelho
do Bling via PostgREST). **Por quê:** confiabilidade e performance; o dashboard
substitui o fluxo Apps Script + Looker Studio.

---

## Pendências (decisões em aberto)

- **Teto de cobertura da última rodada (2 rodadas):** com 2 rodadas, o modelo faz a
  R2 (Out) cobrir ~9 meses (até a rodada do ano seguinte). A diretoria quer que ela
  cubra só "pico + gordura até fim de março". Opções: (a) usar 3 rodadas mesmo este
  ano; (b) implementar um parâmetro de horizonte máximo por rodada (deixaria
  Abr-Jul descoberto, por conta da sobra). **Não decidido / não implementado.**
- **Calibração de `variacao_demanda`** (0,25) e do fator de manutenção (~20% acima do
  real) — ajuste fino, não bug.
