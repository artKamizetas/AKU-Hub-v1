# Log de Decisões

Registro cronológico das decisões de arquitetura, metodologia e negócio — o
"porquê" de cada escolha. Formato leve inspirado em ADR (Architecture Decision
Records). Adicione no topo as mais recentes.

> Datas em AAAA-MM. Marque decisões revertidas com ~~tachado~~ e um ponteiro para a
> que a substituiu.

---

## 2026-08 · Login com Google (OIDC nativo) + allowlist em `app.usuario`
O acesso era usuário/senha (`streamlit-authenticator`) com as **pessoas cadastradas
no `secrets.toml`**: admitir alguém exigia editar dois arquivos (local + painel do
Cloud), gerar um hash bcrypt na mão e redeploy — sem auditoria e com senhas para
guardar. Agora o login é a **conta Google** e a lista de quem entra vive na tabela
`app.usuario`, editável na aba **👥 Usuários** de Configurações. Mesmo movimento dos
parâmetros em 2026-07: o secrets volta a ser credencial de infraestrutura, não
cadastro.

- **OIDC nativo do Streamlit (`st.login`/`st.user`), não fluxo próprio.** O 1.59 já
  traz tudo (redirect, PKCE, cookie assinado); só faltava a dependência `Authlib`.
  O callback é a rota `/oauth2callback`, servida pelo servidor do Streamlit, e por
  isso **não colide** com o `/configuracoes?code&state` das integrações Bling/Olist —
  são paths diferentes e aquele bloco no topo da página segue intacto. De quebra, o
  cookie de identidade é lido no handshake do websocket em qualquer path: a sessão
  nova pós-redirect do Bling já volta logada.
- **Tabela própria, não `auth.users` (Supabase Auth/GoTrue).** O GoTrue está
  provisionado no projeto, mas com 3 contas de e-mail/senha (a de serviço da pipeline
  externa e duas nossas) — nada a reaproveitar. E adotá-lo não pagaria: o app conecta
  com a `service_key`, que ignora RLS, e autoriza em Python — o benefício do GoTrue
  (RLS por usuário) não seria usado; o schema `auth` não é exposto no PostgREST
  (exigiria a Admin API por HTTP, com mais uma chave); o fluxo PKCE dele também volta
  com `?code=`, disputando o parâmetro com o OAuth das integrações; e o GoTrue **não
  guarda perfil** — a doc do próprio Supabase manda criar uma tabela companheira.
  Ou seja, `app.usuario` existiria de todo jeito, e o resto seria custo puro.
- **Sem auto-cadastro** (chegou a ser desenhado e foi cortado): e-mail desconhecido vê
  "peça acesso ao administrador" e **nada é escrito**. Deixar uma conta Google
  qualquer inserir linha no nosso banco exigiria teto de pendentes, kill-switch e
  disciplina de limpeza — complexidade para um fluxo de convite que acontece poucas
  vezes por ano.
- **Fail-closed com break-glass.** Allowlist ilegível = ninguém entra, exceto os
  e-mails de `[acesso] admins` no secrets, que passam antes de qualquer consulta ao
  banco. Essa lista é SOBERANA — tirar alguém dela faz parte de revogar o acesso.
  Dentro de uma sessão já autorizada há fail-soft (último acesso conhecido), porque a
  `5_Configuracoes` chama `st.cache_data.clear()` em vários pontos e um piscar do
  Supabase logo depois de um "Salvar" expulsaria o próprio admin no meio da edição.
- **Cache da allowlist INTEIRA** (`ttl=300`), não uma consulta por e-mail: são poucas
  linhas, o `cache_data` é global entre sessões (1 query/5 min para o app todo) e a
  invalidação vira global — `invalidar_cache_usuarios()` ao salvar faz a revogação
  valer no rerun seguinte de qualquer sessão, sem esperar o TTL.
- **Dois bugs latentes corrigidos junto.** (1) `PAGINAS_POR_ROLE.get(role)` devolvia
  `None` para role fora do mapa — o mesmo valor que significa "todas as páginas", ou
  seja, um perfil desconhecido **liberava o app inteiro**; `paginas_do_role()` agora
  devolve tupla vazia. (2) A leitura de role a partir do secrets estava **duplicada em
  3 páginas** (`4_Pedidos`, `3_Fabrica` ×2); virou `e_admin()`/`exigir_admin()`.
- **A identidade agora é o e-mail.** A tupla passou de `(nome, username, role)` para
  `(nome, email, role)` — é o e-mail que vai para `atualizado_por`/`congelada_por`/
  `criado_por`. Auditoria melhor, mas os valores históricos ficam em outro formato.
- **RLS em `app.usuario`**, mesmo o schema `app` já sendo service_role-only (verificado:
  `anon`/`authenticated` não têm nem USAGE). É a tabela que decide quem é admin —
  `service_role` ignora RLS, então não muda nada no runtime, mas faz uma exposição
  futura acidental do schema falhar fechada.

## 2026-08 · Metas escalonadas (Prata/Ouro/Diamante) por loja × mês
A meta comercial era **um número por loja**, igual o ano inteiro, com os nomes das
lojas hardcoded na página de Configurações. Passou a ser **três níveis por loja e por
mês**, em Faturamento e PA. **Por quê:** um número só é binário (bateu/não bateu) e
não distingue Janeiro (pico) de Julho (baixa); três níveis dão à equipe um próximo
degrau sempre visível, e a competência mensal permite revisar meses fechados.

Decisões de modelo (spec completa em `requisitos/metas-escalonadas.md`):
- **Meta só por loja.** Colégio é recorte de *leitura* do realizado, nunca meta
  cadastrada — cadastrar por colégio multiplicaria o volume por ~9 sem ganho
  proporcional. Vendedor não digita meta: é **derivada por rateio** da meta da loja
  pelo peso na atribuição vendedor→loja, então a soma dos vendedores fecha a meta da
  loja por construção (dois cadastros manuais divergiriam).
- **PA entra como meta; peças e ticket ficam como indicadores sem meta.** Uma tela de
  5 métricas × 3 níveis × 12 meses por loja não se preenche na prática. PA **não é
  aditivo**: agregar é Σpeças/Σpedidos, e o PA parcial do mês já é a própria projeção.
- **Persistência em `app.parametros`, não em tabela nova.** Dimensionado antes de
  decidir: 2 lojas × 12 meses × 2 métricas × 3 níveis ≈ **144 valores/ano** (~10 KB).
  É configuração de baixa frequência de escrita, mesmo perfil de `colegios`. Uma DDL +
  porta de escrita + fakes custaria ~1,5 dia sem ganho. *(A avaliação inicial recomendou
  tabela dedicada supondo meta por colégio e por vendedor — ~10.000 valores/ano; ao
  fechar o escopo em "meta só por loja", o volume caiu 70× e a decisão inverteu.)*
- **Meta não herda entre meses; atribuição de vendedor herda.** Mês sem meta é lacuna
  sinalizada na tela (Julho herdar a meta de Janeiro seria pior que não ter meta). Já a
  atribuição vendedor→loja resolve pela competência editada mais recente **≤** a pedida,
  então revisar Março não é afetado por uma contratação de Agosto.
- **Dois regimes temporais separados na tela** (competência × período). Antes os KPIs de
  meta eram sempre do mês corrente enquanto o filtro de Período controlava o resto da
  página, sem aviso — leitura errada garantida.
- **Bullet chart com faixas em rampa neutra**, não três matizes (prata/ouro/diamante é
  escala **ordinal**, não identidade); a identidade vem do emoji 🥈🥇💎, nunca da cor
  sozinha. O gráfico de **eixo duplo** do histórico (R$ em barra + peças em linha no y2)
  foi substituído por um seletor de métrica em eixo único — duas escalas diferentes num
  gráfico distorcem a comparação.

**Pendência de dados descoberta na validação:** `pedidos.id_situacao_bling` tem **duas
codificações incompatíveis** — o histórico até fev/2026 usa códigos órfãos (`0,1,2,3,11`,
ausentes de `situacoes_vendas`) vindos da rotina de **carga em massa**, e o incremental
diário grava os códigos corretos da API v3 (`6,9,12,24,28488`). O código `1` sozinho é 95%
da base (42.514 pedidos, R$ 9,71 M) e tem perfil financeiro idêntico ao `9`/Atendido.
Efeito no Daily: a coluna "Realizado ano anterior" fica vazia e o botão *Propor* é
desabilitado, com a causa explicada na tela — vazio silencioso enganaria quem define a
meta. Investigação completa e proposta de backfill (para a equipe da pipeline) em
`requisitos/backfill-situacao-pedidos.md`. **Do lado do dashboard nada muda:**
`situacoes_venda = [9]` já é o filtro certo; incluir `1` trataria o sintoma e
contaminaria o motor de demanda, que lê a mesma chave. **Aguardando a pipeline.**

## 2026-07 · Emissão da venda no Olist: 429 e a resolução SKU→id em camadas
A 1ª emissão de venda real quebrou com **HTTP 429** (rate limit). Causa: o mapeamento
SKU→id do Olist **varria o catálogo inteiro** (paginado 100/pág), dezenas de GETs por
pedido — e no lote, uma varredura POR pedido. O Olist v3 dá 60 req/min (plano básico):
um único pedido já estourava. O POST /pedidos exige `produto.id` (id interno do Tiny),
**não aceita SKU/código** — então o id tem de ser resolvido de algum jeito.

Testado ao vivo contra a API para decidir a estratégia:
- `?codigo=` é match **EXATO** — buscar o SKU pai retorna só o registro-pai, não os
  filhos; prefixos curtos retornam zero. (A ideia inicial de "buscar o pai e vir a
  grade" não funciona por aqui.)
- `?nome=` é parcial e traz a grade toda num GET, mas casa por **descrição** (texto não
  garantido idêntico entre Bling e Tiny) e é guloso — descartado como chave.
- `GET /produtos/{id_pai}` devolve `variacoes[]` com `{sku, id}` de todos os tamanhos —
  robusto porque chaveia por **SKU** (idêntico nos dois sistemas).

Resolução final em **3 camadas** (`emissor.resolver_ids_olist`), do mais barato ao mais
caro: **(1) cache** `app.olist_produto_cache` (DDL 005; id é imutável → SKU já visto
custa 0 chamadas); **(2) por família** (deriva o pai cortando o `-TAMANHO`, `?codigo=`
acha o pai, `/produtos/{id}` traz a grade — ~2 chamadas p/ ~7 tamanhos, e grava os
irmãos no cache); **(3) fallback exato** `?codigo=<SKU>` para o que sobrou. A derivação
do pai é ingênua de propósito (corta no último `-`): se errar, o SKU cai no fallback
exato — **nunca casa errado**. Todo GET/POST passa por `_requisitar`, que em 429 respeita
`Retry-After` e reemite. O cache é a **ponte para a fonte definitiva** (espelho do
catálogo do Tiny que o Diogo tem): popular a tabela por fora dispensa a API.

## 2026-07 · Payload de compra completo: código, unidade, pagamento e memória por item
A 1ª conferência de um pedido emitido no Bling mostrou campos vazios que a operação
preenchia à mão: **Código** (`codigoFornecedor` = nosso SKU — idêntico nos dois
sistemas), **Un** (`unidade`) e a **forma de pagamento**. Agora saem no payload:

- `unidade` vem de `unidade_padrao` no config da integração (default `PÇ`) — o
  espelho `public.produto_detalhes` **não traz** unidade do cadastro, e uma chamada
  extra ao Bling por item não se justifica para um valor que é o mesmo em tudo;
- pagamento = **parcela única**, `dataVencimento` = emissão + `prazo_pagamento_dias`
  (default 30), `formaPagamento.id` escolhido por nome num selectbox alimentado por
  `GET /formas-pagamentos`. Mora na **aba Integrações**, não na rodada congelada:
  é característica fixa do acordo com a Art Kamizetas, não decisão por rodada —
  colocá-la na rodada obrigaria a redecidir todo congelamento algo que nunca muda,
  e espalharia config de ERP para dentro do domínio de pedidos;
- `descricaoDetalhada` por item recebe a **memória de cálculo em uma linha**
  (`builder.montar_descricao_item`): `Alvo 42 = demanda 30 + segurança 12 -
  projetado 0 | final 40 | R08/2026`. Reaproveita a `memoria_sugerida` (DDL 004) que
  já era congelada e só era lida na revisão do rascunho — quem produz passa a ver o
  porquê da quantidade sem abrir o AKU-Hub. `| final X` só aparece quando divergiu
  da sugestão (sinal de intervenção manual); sem memória, campo vazio, sem placeholder.

`forma_pagamento_id` entrou como obrigatório em `validar_pre_emissao_bling` — a
falta barra o clique em vez de gerar um pedido sem vencimento. Nomes dos campos
conferidos contra o JSON de exemplo do POST `/pedidos/compras` (atenção:
`codigoFornecedor`, não `codigo`; parcela usa `observacao` no singular).

## 2026-07 · Spec (exploração) — on-order/em-trânsito na posição de estoque
Aberta a spec `requisitos/posicao-estoque-on-order.md` (🟡 EM DISCUSSÃO, não
implementar). Nasce da pergunta da diretoria: *"a rodada sugere 70 mas eu faço
65; a projeção nunca fica sabendo disso"*. O motor order-up-to abre em
`estoque − backlog` e recalcula o pedido a cada chegada — falta o termo
**em-trânsito** (comprometido e ainda não recebido), o que faz a decisão manual
do gestor evaporar e gera um viés que troca de sinal conforme o tempo. Fecha a
pendência da Cobertura Alvo (§8). Decisões de projeto ainda em aberto: fonte do
on-order (ponte própria em `app` × espelho `public` do outro time × read-back no
Tiny), a **regra de ouro da reconciliação** (baixar o on-order no instante em que
vira estoque físico, evitando contagem dupla), e o faseamento (passo 1 =
"travar rodada futura comprometida" é baixo risco e recomendado primeiro). O
Tiny/Olist é a fonte da verdade da execução (qtd/data reais deslizam na fábrica);
o nosso banco guarda a intenção; o `ref:<uuid>` costura os dois.

## 2026-07 · Guard de auth leve nas páginas (`identidade_atual`) + UI de Pedidos em 2 modos
- ~~**`auth.identidade_atual()`**: a `5_Configuracoes.py` passou a usar um guard leve
  que lê `name`/`username`/`role` do `session_state`, em vez de chamar
  `verificar_acesso()` de novo. O `app.py` já reautentica pelo cookie a cada
  execução (inclusive na sessão nova pós-redirect OAuth) ANTES de a página rodar;
  chamar `verificar_acesso()` de novo criava um 2º `CookieManager` com a mesma
  `key="init"` e estourava `StreamlitDuplicateElementKey` no retorno do OAuth.~~
  → O motivo desapareceu com a migração para o login Google (2026-08): `st.user` vem
  do cookie lido no handshake do websocket e não instancia widget nenhum. A função
  continua existindo, agora como leitura barata do `session_state`.
- **Pré-validação simétrica da emissão**: `validar_pre_emissao_bling` espelha o
  `_olist` — dá o feedback (config incompleta, nada a emitir, item sem id) ANTES
  do clique, em vez de o erro do payload sumir.
- **UI da 4_Pedidos** reorganizada: `st.segmented_control` com dois modos (editar
  UM pedido × ação em LOTE) dentro de um único `@st.fragment` (`_area_trabalho`),
  para trocar de modo sem re-ler o topo (sem cache) nem "piscar". NÃO em
  `st.tabs` — fragment-que-rerroda dentro de tabs vaza conteúdo (bug #9158/#9313
  do Streamlit). Resumo da rodada em KPIs no topo, emissões com `st.status`, e o
  log de integração ganhou coluna "Detalhe / erro" (resumo legível do jsonb).

## 2026-07 · Observações dos pedidos: título curto vai no campo "interno"
Dois ajustes no texto que os pedidos de compra levam aos ERPs (`builder.py` +
`integracoes/{bling,olist}.py`):
1. O prefixo `AKU-PC ·` saiu do título e o separador `·` virou ` - ` (fácil de
   digitar na busca do Bling). Formato agora: `COLÉGIO - SUPERCAT - Rmm/aaaa`.
2. **Inversão dos campos**: o Bling mostra a coluna "Observação interna" na
   *listagem* de compras e busca por ela; então o **título curto** passou a ir
   em `observacoesInternas` (escaneável/buscável) e o **bloco completo** em
   `observacoes` (detalhe da rodada, aberto ao entrar no pedido). Antes era o
   contrário. Espelhado no Olist para consistência. A 1ª linha do bloco repete
   o título de propósito (autocontido). Só afeta pedidos congelados a partir
   daqui — o `titulo` é persistido no congelamento.

## 2026-07 · Migrações DDL por script (Management API), não mais copiar-e-colar
As migrações do schema `app` (`docs/sql/00N_*.sql`) deixam de ser aplicadas à mão no
SQL Editor: `python scripts/migrar.py aplicar` roda as pendentes e registra num ledger
`app.schema_migrations` (idempotente). **Por que não a `service_key` que o app já tem:**
ela fala com o **PostgREST**, que faz CRUD e **não roda DDL** — não é falta de permissão,
o verbo `CREATE/ALTER TABLE` não existe nessa API. DDL exige credencial de outro plano.
Escolhida a **Management API** (`POST /v1/projects/{ref}/database/query`) com um **Personal
Access Token**, em vez de conexão Postgres direta (`psycopg`): zero dependência nova
(`httpx` já existe) e o token é literalmente uma chave de API. **Trade-off aceito:** o PAT
é de CONTA (alcança todos os projetos do dono), enquanto a senha do banco seria escopada
ao projeto — mitigado mantendo o PAT **só no ambiente de quem migra** (env
`SUPABASE_ACCESS_TOKEN`), nunca no secrets.toml. **Por que fora do runtime do app** (script,
não botão no dashboard): (1) **menor privilégio** — o processo que atende cliques não pode
carregar uma credencial capaz de `DROP TABLE`; (2) o Streamlit reexecuta o script a cada
rerun, o que exigiria guardas para não migrar em loop. O project ref (não é segredo) sai de
`SUPABASE_PROJECT_REF` ou da `[supabase].url`. Comando `marcar` faz **baseline** das
migrações já aplicadas à mão (001/002) — registra sem rodar, senão o runner tentaria
recriar objetos existentes. Migrações precisam ser transaction-safe (o runner envolve cada
uma em `begin/commit` junto do registro no ledger — atômico).

## 2026-07 · Emissão de pedidos: Bling (compra) + Olist (venda), dois momentos
A ponte Simulador → ERPs deixa de ser manual (CSV). Um pedido nosso PRONTO agora vira
**pedido de COMPRA no Bling** (conta AK Uniformes, API v3 `POST /pedidos/compras`,
fornecedor = Art Kamizetas) e, num **segundo momento explícito**, **pedido de VENDA no
Olist/Tiny** (conta Art Kamizetas — a fábrica NÃO usa Bling; `POST /pedidos`,
`numeroOrdemCompra` = nº do Bling, cliente = AK Uniformes). **Por quê dois momentos e
não um botão só:** decisão do usuário — a compra é aprovada/emitida primeiro (gera o nº
que amarra a venda) e a venda pode ser disparada depois, possivelmente por outra pessoa.
**Ordem obrigatória** Bling→Olist é natural (a amarração depende do nº do Bling).
Descartada a ideia de importação por planilha: o Bling não importa pedidos de COMPRA por
planilha, e o Olist tem API v3 boa. **Estados** (evolução dos reservados na Fase 0, DDL
`003`): RASCUNHO→PRONTO→COMPRA_EMITINDO→COMPRA_EMITIDA→VENDA_EMITINDO→EMITIDO; os
`*_EMITINDO` são locks CAS anti duplo-clique (mesmo padrão do CONGELANDO). **Falha ANTES
do POST** → rollback ao estado anterior; **falha DEPOIS** (POST ok, gravar id falhou) →
fica travado em `*_EMITINDO`, UI oferece "Destravar" + aviso de conferir no ERP (a
idempotência por `bling_id`/`olist_id` não cobre esse caso — o id não chegou a ser
gravado). **Chaves e tokens no Supabase** (`app.integracao`, NÃO secrets.toml — filesystem
do Cloud é efêmero), geridos na aba **Integrações** de Configurações; OAuth2
authorization_code com `state` anti-CSRF **persistido no banco** (a sessão do Streamlit
morre no redirect) e callback no topo da 5_Configuracoes (`?code&state`); redirect_uri =
URL do app + `/configuracoes` (`url_path` fixo). SKUs idênticos nos 2 sistemas → mapa
SKU→id Olist via `GET /produtos` (a API do Olist referencia produto por id interno, não
SKU), com pré-validação de faltantes. Módulos: `pedidos/integracoes/` (repositorio, oauth,
bling, olist — clientes com `http` injetável; payloads PUROS) + `pedidos/emissor.py`;
auditoria em `app.integracao_evento` (nunca grava tokens). **Verificação sem escrita nos
ERPs** (decisão do usuário): preview do JSON na UI + GET de contrato do Bling + CAS que
bloqueia a 2ª sessão antes do POST. **Pré-requisitos externos pendentes:** registrar o app
no portal developer do Bling e criar o aplicativo no Olist (ambos com o redirect real);
coletar os IDs de negócio. `httpx` virou dependência explícita.

## 2026-07 · Visão Geral vira o cockpit único do plano de rodadas
A edição do **calendário de rodadas** (`rodadas_datas`) saiu de Configurações →
Produção e passou para a **Visão Geral do Simulador**, ao lado das coberturas alvo;
em Configurações fica só um ponteiro read-only mostrando as datas atuais. **Por
quê:** ao adicionar a coluna editável de cobertura alvo, "o plano de rodadas" ficou
partido em duas telas (datas num lugar, coberturas noutro) — conflito de arquitetura
de informação levantado pelo usuário. O princípio decisivo foi **editar onde se vê o
efeito**: cobertura E data mudam a simulação inteira, então ambas pertencem à tela
que mostra o resultado ao vivo (tela de Configuração é para parâmetros sem feedback
imediato). Datas e coberturas agora têm **preview de sessão** e um único botão
**"Salvar plano"** (admin) que persiste `rodadas_datas` + `cobertura_override`
juntos via `config_store` (o merge trata `rodadas_datas` como lista→substitui e
`planejamento` como deep-merge por chave). `lead_time` e `período histórico` seguem
em Configurações (parâmetros do motor, não "o plano"). O calendário usa um
**`st.multiselect` de mês/ano** (pills), não uma tabela: os disparos são sempre
1º-de-mês (o motor consome em resolução de mês e o `cobertura_override` é keyed
pela ISO), então o conjunto de opções é finito e as datas ficam normalizadas ao
dia 1 — trocou a lista longa de date-pickers por uma linha de pills que cresce. Segundo ajuste no mesmo
passo: a coluna de cobertura deixou de **pré-preencher todas as rodadas com a
cobertura natural** (parecia que o usuário tinha antecipado todas) — virou DUAS
colunas: "Cobertura natural (%)" read-only + "Cobertura alvo (%)" editável e **vazia
por padrão**, preenchida só onde há antecipação deliberada. Não-admin vê tudo com
editores travados.

## 2026-07 · Cobertura Alvo por rodada (antecipação deliberada de produção)
O planejador pode "engordar" uma rodada além da sua cobertura natural: coluna
editável **Cobertura alvo (%)** na Visão Geral (% da demanda ANUAL da rede). O
motor converte o % em extensão da janela de proteção (`_data_por_demanda_acumulada`
caminha a curva de demanda da rede até acumular o alvo) e dimensiona o
`EstoqueAlvo` sobre a janela maior; a rodada SEGUINTE encolhe sozinha pela
projeção forward (order-up-to é auto-liquidante) — a produção total do horizonte
se conserva (antecipar redistribui, não infla). **Por quê:** caso real de
jul/2026 — abastecimento atrasado; a diretoria quis puxar volume da R2 (Out, 75%)
para a R1 (Jul, 11%) sem mexer nas datas das rodadas. Decisões: knob em **%** e
não em tempo (a sazonalidade torna tempo não-linear — o motor converte % → data);
piso na cobertura natural e teto em 100%; keyed por `data_disparo` ISO (estável à
renumeração das rodadas); persistido em `planejamento.cobertura_override` junto
dos demais parâmetros. Preview ao vivo na UI (tabela + curva de estoque com a
linha "sem antecipação" tracejada) antes de salvar (admin). Smoke com dados
reais: R1 11%→39% (cobre até 12/Jan/27 — entra na alta → SS sobe p/ 99%), R2
75%→47%, R3–R5 intactas, Σ produção idêntica (43.452). Spec:
[requisitos/cobertura-alvo-rodada.md](requisitos/cobertura-alvo-rodada.md).

## 2026-07 · Parâmetros migrados do config.yaml para o Supabase (app.parametros)
Executada a migração deferida (ver "v1 vai ao ar…"): tudo que a página de
Configurações edita (**Categoria B** — metas, vm, logistica, demanda, colégios,
alias, segmentos, exceções, planejamento) vive em **`app.parametros`** (JSONB, 1
linha `default`) com auditoria append-only em **`app.parametros_historico`**
(quem/quando/estado completo). **`loader.carregar_config()`** é o ponto único de
leitura de config do app (páginas E scripts): `deep_merge(config.yaml ←
Supabase)`, cache 5 min, **degradação graciosa** (Supabase fora → yaml puro +
aviso). Regra do merge: dicts mesclam chave a chave, MAS coleções que o gestor
possui por inteiro (`colegios`, `colegios_alias`, `grupo_segmento`,
`excecoes_sku`, `planejamento.cobertura_override`) **substituem o bloco** — item
apagado na UI não ressuscita do default do yaml. **Por quê:** filesystem efêmero
do Streamlit Cloud — o save no config.yaml evaporava a cada redeploy, invalidando
a camada "gestor decide". O `config.yaml` segue no git como fonte dos defaults
(Categoria A: IDs de depósito, situações, thresholds); `ruamel.yaml` saiu do
caminho de escrita (e do requirements — nenhum código a importa mais). A tabela
`planejamentos` do plano congelado **não foi criada**: `app.rodada_congelada`
(Pedidos Fase 0) já cumpre o papel de cenário salvo. Porta de acesso:
`etl/config_store.py` (mesmo padrão de gateway testável do
`pedidos/repositorio.py`). DDL: `docs/sql/002_app_parametros.sql`; seed único:
`scripts/seed_parametros.py`.

## 2026-07 · Pedidos de Compra Fase 0: congelar rodada + rascunhos no schema `app`
Primeira fase da ponte Simulador → Bling: o output do `processar_fabrica` deixa de
morrer no CSV e vira documento. **Congelar rodada** = snapshot imutável (resultado
integral por SKU + config completo + data de referência — o motor ancora em
`Timestamp.now()`, recalcular depois diverge, então sem snapshot não há conferência
pedido × cálculo). Do snapshot nascem **pedidos de compra em rascunho** agrupados por
**Colégio × Super Categoria** (1 pedido nosso ↔ 1 futuro pedido de compra no Bling),
com `quantidade_sugerida` imutável vs `quantidade_final` editável (delta = auditoria).
**Onde:** schema **`app`** novo no mesmo Supabase (gravável; DDL em
`docs/sql/001_app_pedidos.sql`) — separado do `public` porque o `public` é o espelho
read-only da pipeline externa, e quando o espelho de `pedidos_compra` do Bling chegar
(roadmap do outro time), `app.pedido_compra` (intenção) × `public.pedidos_compra`
(realidade) ficam autoexplicáveis para o sincronizador futuro. **Consistência sem
transação PostgREST:** unique parcial (1 congelamento vivo por rodada mês×ano) como
trava anti duplo-clique do Streamlit; estado `CONGELANDO`→`ABERTA` como commit lógico;
transições por compare-and-swap; trigger no banco impede editar itens fora de RASCUNHO.
**Novo pacote `pedidos/`** (domínio transacional — `etl/` segue analítico/puro):
`estados.py` (máquina de estados; EMITINDO/EMITIDO/SINCRONIZADO já reservados),
`builder.py` (puro), `repositorio.py` (ÚNICA porta de escrita do Supabase). Página
nova `4_Pedidos.py` (admin). **Observações internas do Bling padronizadas** desde já:
`titulo` (`AKU-PC · COLÉGIO · SUPERCAT · Rmm/aaaa`) persistido + bloco completo sempre
recomposto por `montar_observacoes_bling` no momento do uso (totais refletem edições;
`ref: <uuid>` = chave de reconciliação com o espelho futuro). **Fora de escopo (fases
seguintes):** OAuth2 + emissão via API v3 (app no portal developer do Bling ainda não
registrado — pré-requisito; confirmar nome exato do campo `observacoesInternas`) e
sincronizador contra o espelho.

## 2026-07 · Calendário de rodadas: fonte única (fallback mensal removido)
O **fallback mensal** (`planejamento.rodadas`, meses 1–12 que repetiam todo ano) e o
override `rodadas_meses` foram **removidos**. O calendário explícito de datas
(`rodadas_datas`) passa a ser a **fonte única**. **Por quê:** as duas metodologias
rodavam em paralelo e **divergiam na UI** — a Visão Geral do Simulador simulava por
**meses fixos** (o multiselect "cenário de rodadas" default `[7, 10]`), enquanto a
Sugestão por SKU já lia as **datas explícitas**. Ao abrir "Sugestão por SKU" a tela
piscava as rodadas por data e revertia para as fixas, além de confundir (duas telas
ofereciam configurar meses fixos, metodologia antiga). Mudanças: `_candidatas_rodadas`
/ `_sequencia_rodadas` / `simular_politica_reabastecimento` / `simular_rodadas` perderam
o caminho mensal e o parâmetro de override; multiselects de meses fixos removidos das
páginas Simulador e Configurações; chave `rodadas` retirada do `config.yaml`. Sem
`rodadas_datas` (2+ datas), a Visão Geral só avisa e a Sugestão por SKU cai na cobertura
fixa (`fabrica.cobertura_meses`) — esse fallback de cobertura **permanece** (é outro
mecanismo, não a metodologia antiga). Suíte de testes atualizada. **Perda aceita:** o
"what-if" de antecipar rodada saiu junto; se voltar, deve ser um editor de **datas**
temporário, não meses fixos.

## ~~2026-07 · v1 vai ao ar com persistência via config.yaml (migração Supabase deferida)~~
> Superada pela entrada "Parâmetros migrados do config.yaml para o Supabase" acima.
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
