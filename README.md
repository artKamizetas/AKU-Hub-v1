# AKU Dashboard — AK Uniformes

Dashboard interativo de estoque, PCP e vendas integrado ao Bling ERP.
Roda localmente via Excel ou na nuvem via Google Sheets + Streamlit Community Cloud.

---

## Sumário

1. [Rodar localmente (Excel)](#rodar-localmente)
2. [Configuração do Sistema](#configuração-do-sistema)
   - [UI de Configurações (Admin)](#ui-de-configurações-admin)
   - [Gerenciar Exceções de SKU](#gerenciar-exceções-de-sku)
   - [Upload de Dados](#upload-de-dados)
3. [Deploy no Streamlit Cloud (Google Sheets)](#deploy-no-streamlit-community-cloud)
   - [Passo 1 — Criar a Service Account](#passo-1--criar-a-service-account-no-google-cloud)
   - [Passo 2 — Compartilhar a planilha](#passo-2--compartilhar-a-planilha-com-a-service-account)
   - [Passo 3 — Configurar os Secrets](#passo-3--configurar-os-secrets-no-streamlit-cloud)
   - [Passo 4 — Conectar o repositório](#passo-4--conectar-o-repositório-ao-streamlit-cloud)
   - [Passo 5 — Acessar e compartilhar o link](#passo-5--acessar-e-compartilhar-o-link-público)
4. [Estrutura do projeto](#estrutura-do-projeto)
5. [Atualização dos dados](#atualização-dos-dados)

---

## Rodar localmente

**Pré-requisito:** Python 3.10+

```bash
# 1. Ativar o ambiente virtual (Windows)
venv\Scripts\activate

# 2. Instalar dependências (só precisa fazer uma vez)
pip install -r requirements.txt

# 3. Colocar o Excel na pasta data/
#    Nome exato: "Integração Bling ERP.xlsx"

# 4. Rodar
streamlit run app.py
```

Em modo local, o Excel é lido automaticamente. Nenhuma configuração de Google Sheets é necessária.

---

## Configuração do Sistema

A partir da versão 2.0, todos os parâmetros operacionais podem ser alterados **diretamente pela UI**, sem necessidade de editar `config.yaml`.

### UI de Configurações (Admin)

Acesse **⚙️ Configurações** (disponível apenas para usuários com `role="admin"`).

**Aba 1 — Parâmetros Gerais**
- Metas comerciais (Natal, Mossoró)
- Parâmetros de Logística (VM padrão, dias de análise, cobertura mínima)
- Parâmetros de Fábrica (período histórico, crescimento, sazonalidade, cobertura, correção)
- Parâmetros de Planejamento (meses de rodadas, lead time, buffer, período de sazonalidade)
- IDs de Status de Pedido (Em Aberto, Em Andamento, Pronto para Retirada)

**Aba 2 — Exceções de SKU**
- **Baixar template CSV** com SKUs atuais e seus overrides
- **Upload de CSV** para aplicar exceções (vm, dias_analise, sazonalidade, correcao_manual)
- Alterações são salvas automaticamente em `config.yaml`

**Aba 3 — Upload de Dados**
- Fazer upload de novo arquivo Excel (`Integração Bling ERP.xlsx`)
- Validação automática de schema antes de aceitar
- Resumo de registros carregados (pedidos, produtos, estoque)

**Aba 4 — Sistema**
- Informações de versão (Python, Streamlit, Pandas)
- Datas de última modificação dos arquivos
- Botão de "Forçar Recarga de Dados" (limpa cache)
- Download de backup do `config.yaml` atual

### Gerenciar Exceções de SKU

Exceções permitem sobrescrever regras globais para produtos específicos (ex: SKU especial com VM diferente ou sazonalidade diferente).

1. Acesse **⚙️ Configurações → Exceções de SKU**
2. Clique em **⬇️ Baixar CSV** para ver template com SKUs atuais
3. Preencha as colunas desejadas:
   - `sku` (obrigatório): código do produto
   - `vm_override`: unidades de exposição (opcional)
   - `dias_analise`: dias para calcular giro (opcional)
   - `sazonalidade`: multiplicador de demanda (opcional)
   - `correcao_manual`: ajuste fixo (opcional)
4. Salve como CSV e clique em **📤 Fazer Upload**
5. Valide o preview e clique em **✅ Aplicar Exceções**

### Upload de Dados

Sempre que precisar atualizar os dados do Bling:

1. Exporte o arquivo Excel do Bling (ou atualize a Google Sheets)
2. Acesse **⚙️ Configurações → Upload de Dados**
3. Selecione o arquivo Excel (`.xlsx` ou `.xls`)
4. Confirme no preview → Clique em **💾 Substituir dados**
5. O dashboard recarregará com os novos dados

> **Nota:** Todas as alterações são feitas em `config.yaml`, que é versionado no git. Para produção, você pode revisar as mudanças antes de fazer `git push`.

---

## Deploy no Streamlit Community Cloud

Deploy **100% gratuito**: Streamlit Community Cloud + Google Sheets API (ambos no free tier).

### Passo 1 — Criar a Service Account no Google Cloud

A Service Account é uma conta de serviço que permite ao dashboard ler a planilha sem interação humana.

1. Acesse [console.cloud.google.com](https://console.cloud.google.com/)
2. Crie um projeto (ou use um existente)
3. Vá em **APIs e Serviços → Biblioteca** e habilite:
   - **Google Sheets API**
   - **Google Drive API**
4. Vá em **APIs e Serviços → Credenciais → Criar Credenciais → Conta de Serviço**
5. Nome sugerido: `bling-dashboard` → **Criar e continuar → Concluir**
6. Clique na conta criada → aba **Chaves → Adicionar Chave → Criar nova chave → JSON**
7. O arquivo JSON será baixado — guarde com segurança

O JSON tem esta estrutura (você vai precisar de todos os campos):
```json
{
  "type": "service_account",
  "project_id": "...",
  "private_key_id": "...",
  "private_key": "-----BEGIN RSA PRIVATE KEY-----\n...",
  "client_email": "bling-dashboard@SEU-PROJETO.iam.gserviceaccount.com",
  ...
}
```

> Anote o valor de **`client_email`** — será usado no próximo passo.

---

### Passo 2 — Compartilhar a planilha com a Service Account

1. Abra a planilha **Integração Bling ERP** no Google Sheets
2. Clique em **Compartilhar** (canto superior direito)
3. Cole o `client_email` da Service Account no campo de e-mail
4. Defina permissão como **Leitor** (somente leitura é suficiente)
5. Clique em **Enviar**
6. Anote o **Sheet ID** — é o trecho da URL entre `/d/` e `/edit`:
   ```
   https://docs.google.com/spreadsheets/d/ESTE_É_O_SHEET_ID/edit
   ```

> **Atenção:** Os nomes das abas devem ser exatamente:
> `Pedidos`, `Itens`, `Produtos`, `EstoqueV3`, `Produtos_detalhes`,
> `Vendedores`, `Lojas`, `Situações`, `Depósitos`

---

### Passo 3 — Configurar os Secrets no Streamlit Cloud

1. Acesse [share.streamlit.io](https://share.streamlit.io) e faça login
2. No app, clique em **⋮ (menu) → Settings → Secrets**
3. Cole o conteúdo abaixo substituindo pelos valores reais do seu JSON:

```toml
sheet_id = "COLE_O_SHEET_ID_AQUI"

[gcp_service_account]
type = "service_account"
project_id = "SEU_PROJECT_ID"
private_key_id = "SEU_PRIVATE_KEY_ID"
private_key = "-----BEGIN RSA PRIVATE KEY-----\nSUA_CHAVE_PRIVADA_AQUI\n-----END RSA PRIVATE KEY-----\n"
client_email = "bling-dashboard@SEU-PROJETO.iam.gserviceaccount.com"
client_id = "SEU_CLIENT_ID"
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "https://www.googleapis.com/robot/v1/metadata/x509/bling-dashboard%40SEU-PROJETO.iam.gserviceaccount.com"
universe_domain = "googleapis.com"
```

4. Clique em **Save**

> **Dica:** A `private_key` tem quebras de linha. Copie-a diretamente do JSON — os `\n` devem ser preservados.

---

### Passo 4 — Conectar o repositório ao Streamlit Cloud

1. Suba o código para um repositório no GitHub (pode ser privado):
   ```bash
   git init
   git add .
   git commit -m "Deploy inicial do Bling Dashboard"
   git remote add origin https://github.com/SEU_USUARIO/bling-dashboard.git
   git push -u origin main
   ```
2. Em [share.streamlit.io](https://share.streamlit.io), clique em **New app**
3. Preencha:
   - **Repository:** `SEU_USUARIO/bling-dashboard`
   - **Branch:** `main`
   - **Main file path:** `app.py`
4. Clique em **Deploy!**

O Streamlit Cloud instala as dependências do `requirements.txt` e sobe o app em alguns minutos.

---

### Passo 5 — Acessar e compartilhar o link público

O Streamlit Cloud gera uma URL pública no formato:
```
https://SEU-USUARIO-bling-dashboard-app-XXXX.streamlit.app
```

Qualquer pessoa com o link pode acessar. Para restringir acesso por e-mail, use **Share → Invite viewers** no painel do Streamlit Cloud.

Para atualizar o app após mudanças no código: basta fazer `git push` — o redeploy é automático.

---

## Estrutura do projeto

```
bling_dashboard/
├── app.py                         # Ponto de entrada do Streamlit
├── auth.py                        # Autenticação e controle de acesso por perfil
├── config.yaml                    # Metas, IDs e configurações operacionais
├── requirements.txt               # Dependências Python
├── run.bat                        # Atalho Windows: ativa venv e roda o app
├── .gitignore                     # Arquivos excluídos do repositório
├── README.md                      # Este arquivo
├── CLAUDE.md                      # Resumo do projeto para agentes
├── docs/                          # Documentação humana (arquitetura, dados, PCP, decisões)
├── .streamlit/
│   ├── config.toml                # Tema / configurações do Streamlit
│   ├── secrets.toml               # Credenciais reais (NÃO commitar — está no .gitignore)
│   └── secrets.toml.example       # Modelo de secrets (commitar este)
├── etl/
│   ├── loader.py                  # Leitura dos dados do Supabase (via PostgREST)
│   ├── daily.py                   # Lógica de metas comerciais
│   ├── logistica.py               # Lógica de reposição de loja
│   ├── demanda.py                 # Motor único de demanda por SKU (base de fábrica + planejamento)
│   ├── fabrica.py                 # Lógica de PCP (sugestão tática por SKU)
│   ├── planejamento.py            # Planejamento anual de rodadas
│   └── vm_dinamico.py             # Cálculo de Visual Merchandising
├── pages/
│   ├── 0_Home.py                  # Visão geral / status
│   ├── 1_Daily.py                 # Dashboard comercial
│   ├── 2_Logistica.py             # Reposição de loja
│   ├── 3_Fabrica.py               # PCP / Simulador de produção
│   └── 5_Configuracoes.py         # ⭐ UI de configuração (admin only)
├── scripts/                       # Utilitários CLI (rodar da raiz: python scripts/<nome>.py)
│   ├── exportar_vm.py             # Exporta data/VM_Calculado.xlsx
│   ├── memoria_calculo.py         # Memória de cálculo do VM Dinâmico (por SKU)
│   └── memoria_calculo_fabrica.py # Memória de cálculo do PCP (por SKU)
├── assets/                        # Estáticos (favicon)
└── data/                          # Saídas locais (ex: VM_Calculado.xlsx — não versionado)
```

---

## Atualização dos dados

| Modo | Como atualizar |
|------|----------------|
| **Local (Excel)** | Substitua `data/Integração Bling ERP.xlsx` e clique em "Recarregar Dados" no app |
| **Streamlit Cloud (Sheets)** | Atualize a planilha Google Sheets. O cache expira a cada **1 hora** automaticamente. Para forçar, clique em "Recarregar Dados" no app |

> O cache de 1 hora evita exceder o limite gratuito da Google Sheets API (300 requisições/minuto por projeto).

---

## Dúvidas Comuns

**"Aba ausente: Situações" ou similar**
→ Verifique se os nomes das abas na planilha Google Sheets estão exatamente iguais aos listados no Passo 2.

**"Erro ao conectar ao Google Sheets"**
→ Verifique se (1) a planilha foi compartilhada com o `client_email`, (2) os Secrets foram salvos corretamente, (3) as APIs do Google foram habilitadas.

**"No module named gspread"**
→ O ambiente virtual não está ativado, ou as dependências não foram instaladas. Rode `pip install -r requirements.txt`.
