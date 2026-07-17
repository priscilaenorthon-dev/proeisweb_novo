# PROEIS Bot — Documentação Completa do Sistema

## O que é

Automação de agendamento de voluntariados no sistema **PROEIS** (Policiamento Reforçado em Eventos, Infraestrutura e Segurança) da Polícia Militar do Rio de Janeiro. O sistema acessa o site `https://www.proeis.rj.gov.br/` em nome do usuário, resolve captchas automaticamente e reserva vagas de serviço voluntário.

---

## Arquitetura Geral

```
proeisweb_novo/
├── proeis_http.py          # Motor de automação (cliente HTTP + lógica PROEIS)
├── api/
│   └── index.py            # Servidor FastAPI — todos os endpoints REST + SSE
├── web/
│   ├── index.html          # Página única da SPA
│   ├── app.js              # Toda a lógica do frontend (vanilla JS)
│   ├── proeis-fixes.js     # Patches e ajustes de UI
│   └── styles.css          # CSS customizado (complementa Tailwind)
├── config/
│   └── proeis_options.json # Lista de convênios e CPAs disponíveis
├── deploy/
│   ├── README.md                        # Ordem de execução e pré-requisitos
│   └── 01-set-min-instances.sh          # Elimina cold start (min-instances=1)
├── logs/                   # Logs de operações (fallback local quando Firestore indisponível)
├── requirements.txt        # Dependências Python
└── .env                    # Variáveis de ambiente (não versionado)
```

**Stack:**
- Backend: Python 3.11+ / FastAPI / Uvicorn
- Frontend: HTML + vanilla JavaScript (sem framework) + Tailwind CSS (compilado em `web/tailwind.css` via `tailwind.config.js`)
- Banco de dados: Google Cloud Firestore
- Infraestrutura: Google Cloud Run (serverless, stateless)
- Deploy: Push para `main` no GitHub → CI/CD automático para Cloud Run
- Região: `southamerica-east1` (São Paulo)

---

## URLs do Site PROEIS (alvos da automação)

```python
BASE_URL     = "https://www.proeis.rj.gov.br/"
DEFAULT_URL  = "https://www.proeis.rj.gov.br/Default.aspx"                      # Tela de login
MENU_URL     = "https://www.proeis.rj.gov.br/FrmMenuVoluntario.aspx"            # Menu principal
ASSOCIAR_URL = "https://www.proeis.rj.gov.br/FrmEventoAssociar.aspx"            # Tela de filtros/vagas
INSCRICOES_URL = "https://www.proeis.rj.gov.br/FrmVoluntarioInscricoesConsultar.aspx"
```

O site usa **ASP.NET WebForms** com `__VIEWSTATE`, `__EVENTVALIDATION` e postbacks. Toda interação exige extração e reenvio desses campos ocultos.

---

## Sessão Persistente (funcionamento)

O sistema mantém o usuário **sempre logado** enquanto estiver usando. O único jeito de sair é clicar o botão **Sair** no sidebar.

### Como funciona

```
Ao abrir o painel:
  → GET /api/session-status lê Firestore
  → Se há sessão salva: exibe nome do policial no sidebar
  → Se não há: sidebar mostra só o status da API

Ao executar qualquer operação (marcar, listar, servicos-marcados):
  1. Carrega cookies do Firestore para o client HTTP
  2. GET para FrmEventoAssociar.aspx (tela de filtros)
     ├── Se carregou diretamente (sessão ativa + acesso direto OK):
     │     soup = tela de filtros → navegação pulada (~0.4s economizados)
     ├── Se redirecionou para o menu (sessão ativa, acesso direto não liberado):
     │     soup = menu → navegação normal via _try_navigate_to_service_page()
     └── Se redirecionou para login (sessão expirada):
           → faz login completo → salva nova sessão no Firestore
  3. Executa a operação
  4. Cookies atualizados ficam prontos para a próxima operação
```

### Detecção de sessão expirada

`check_auth()` usa **CSS selector** (`soup.select_one("#txtSenha")`) para identificar se a resposta é a tela de login. Não usa busca de texto livre no HTML para evitar falsos positivos (scripts ou campos ocultos que mencionem esses IDs).

### Regras de expiração

- **No nosso sistema:** sessão não expira por tempo — dura até o usuário clicar **Sair**
- **No PROEIS:** sessão do servidor expira após ~20 minutos de inatividade (padrão ASP.NET)
- **Se PROEIS expirou:** `check_auth()` detecta o redirecionamento para login → refaz login automaticamente → transparente para o usuário
- **Se usuário clica Sair:** `POST /api/session-logout` apaga `proeis_session/current` do Firestore → próxima operação faz login completo

### Firestore — coleção `proeis_session`

Documento único `"current"` com os campos:
```json
{
  "cookies":   { "PHPSESSID": "...", "ASP.NET_SessionId": "..." },
  "user_name": "3º SGT PRISCILA NORTHON",
  "login":     "123456",
  "saved_at":  "2026-06-06T12:00:00Z"
}
```

---

## Fluxo de Automação (passo a passo)

```
1. Restaurar sessão do Firestore (cookies salvos)
   ├── Se sessão válida no PROEIS:  pular login completamente
   │     → se carregou direto na tela de filtros: navegação também pulada
   │     → se carregou no menu: navega normalmente (1 postback)
   └── Se sessão inválida/inexistente: login completo (~1.1s + captcha de login)
         → após login: salva nova sessão no Firestore

2. Navegar para a tela de filtros (FrmEventoAssociar.aspx)
   └── _try_navigate_to_service_page() reutiliza soup do menu quando possível
       (sem GET extra se check_auth() já carregou o menu)

3. Para cada data disponível:
   ├── Verificar se convênio já está selecionado (pular POST se igual)
   ├── Preencher filtros: data + CPA
   ├── Resolver captcha obrigatório (~0.9s via Gemini)
   ├── POST do formulário → página de resultados
   ├── Filtrar vagas por nome_evento, hora_evento, turno, endereço
   └── Clicar "Eu Vou" → confirmar booking

4. Salvar cookies atualizados no Firestore
5. Gravar log da operação no Firestore
```

**Tempo estimado por vaga (sessão ativa, data conhecida, acesso direto à tela de filtros):** ~1.8–2.2s
**Tempo por evento (varredura em 11 datas, sessão ativa):** ~11–15 segundos
**Gargalo incontornável:** captcha por filtro (~0.9s, exigido pelo PROEIS em cada busca de data)

---

## Módulo Principal: `proeis_http.py`

### Classe `ProeisHTTP`

```python
client = ProeisHTTP(
    login="123456",
    password="senha",
    gemini_api_key="AIza...", # obrigatório para resolver captcha
    debug=True,
)
```

#### Métodos de sessão/HTTP

| Método | Descrição |
|--------|-----------|
| `reset_session()` | Recria `requests.Session` do zero (limpa cookies) |
| `check_auth() → bool` | GET para `ASSOCIAR_URL`; detecta sessão válida por CSS selector; seta `self.soup` |
| `request(method, url, **kwargs) → BeautifulSoup` | Requisição HTTP com retry, timeout e logging |
| `post_form(payload, url) → BeautifulSoup` | Submete formulário ASP.NET com VIEWSTATE |
| `postback(target, argument) → BeautifulSoup` | Executa `__doPostBack` do ASP.NET |
| `form_payload(soup) → dict` | Extrai todos os campos ocultos do formulário atual |
| `require_soup() → BeautifulSoup` | Retorna último soup ou levanta `AutomationError` |

#### Métodos de autenticação

| Método | Descrição |
|--------|-----------|
| `login_flow()` | Fluxo completo: carrega tela, seleciona tipo ID, resolve captcha, submete credenciais |
| `password_for_form(soup) → str` | Extrai hash de senha do campo oculto do formulário |

#### Métodos de navegação

| Método | Descrição |
|--------|-----------|
| `navigate_to_service_page()` | Navega para a tela de filtros; é no-op se já estiver lá |
| `_try_navigate_to_service_page() → bool` | Reutiliza soup do menu se `last_url == MENU_URL`; caso contrário faz GET |
| `reset_navigation_state()` | Volta ao menu para limpar estado antes da próxima operação |

#### Métodos de captcha

| Método | Descrição |
|--------|-----------|
| `extract_captcha_image(soup) → str` | Extrai imagem base64 do captcha da página |
| `solve_captcha(image) → CaptchaSubmission` | Resolve via Gemini |
| `solve_page_captcha(soup) → (soup, texto)` | Resolve captcha na página com retry automático |
| `report_bad_captcha()` | Reporta captcha errado ao serviço (para treino) |

#### Métodos de vaga

| Método | Descrição |
|--------|-----------|
| `fill_filters(convenio, data, cpa, prefer)` | Preenche e submete formulário de filtros; pula POST de convênio se já selecionado |
| `available_candidates(soup, prefer) → list[Candidate]` | Extrai lista de vagas disponíveis da página |
| `choose_target_event(prefer, dry_run, ...) → bool` | Chama `available_candidates` uma vez, filtra por preferência e critérios internamente |
| `mark_scanning_dates(...) → int` | **Método principal**: varre datas e marca vagas; retorna qtd confirmada |
| `list_all_available_dates(convenio, cpa) → int` | Lista todas as vagas disponíveis em todas as datas |
| `dates_for_convenio(convenio) → list[tuple]` | Retorna datas disponíveis para o convênio |

#### Lógica de matching de eventos (regras importantes)

- **Nome do evento:** usa `nome_norm in label_norm` — **correspondência exata por substring**. O que o usuário digita deve estar contido no texto exato da vaga no PROEIS.
- **Normalização:** remove acentos, lowcase, espaços extras. Ex: `"RAS SÃO FIDÉLIS"` → `"ras sao fidelis"`
- **Hora:** busca substring normalizada no label
- **Turno:** `"diurno"` / `"noturno"` / `"madrugada"` por palavras-chave
- **Endereço:** expande abreviações (`"r."` → `"rua"`, `"av."` → `"avenida"`) antes de comparar
- **Tipo de vaga:** `"reserva"` detecta palavras-chave; `"nao-reserva"` é tudo que não é reserva
- **Sem fuzzy matching** — correspondência deve ser exata para evitar reservas em eventos errados
- **Fallback:** se `nao-reserva` não encontrar, tenta `qualquer` com o mesmo filtro de nome (usando o mesmo parse já carregado, sem nova requisição)

### Funções utilitárias globais

```python
norm(value) → str                    # Remove acentos, lowercase, strip
norm_match(value) → str              # norm() + expansão de abreviaturas de endereço
normalize_date_for_site(value) → str # Converte dd/mm/yyyy ou yyyy-mm-dd para dd/mm/yyyy
load_env_file(path)                  # Carrega .env do diretório atual ou do script
login_with_retries(client, reason, attempts=3)  # Login com retry e backoff
emit_vaga(label, data_evento, acao)  # Imprime JSON estruturado da vaga (lido pelo frontend SSE)
reparar_mojibake(value) → str        # Corrige texto com encoding corrompido (UTF-8 mal lido como CP1252)
```

### Estruturas de dados

```python
@dataclass
class Candidate:
    label: str        # Texto completo da linha de vaga (nome + endereço)
    action: str       # Tipo de ação: "submit", "postback", "href"
    payload: dict     # Dados para submissão do formulário
    score: int        # Score de correspondência com os critérios (0–10)

@dataclass
class CaptchaSubmission:
    text: str             # Resposta do captcha
    captcha_id: str|None  # ID no serviço externo (para reportar erro)
    solver_index: int     # 0=Gemini
    confidence: float     # Confiança (0.0–1.0)
```

### Exceções

```python
AutomationError          # Erro genérico de automação (esperado, tratável)
CaptchaInvalidAnswerError  # Resposta inválida do captcha
```

---

## API REST: `api/index.py`

Servidor FastAPI. Montagem de arquivos estáticos da pasta `web/` na raiz `/`.

### Endpoints

#### Saúde e diagnóstico

```
GET /api/health
→ {status, version, firestore: bool}
```

#### Sessão persistente

```
GET /api/session-status
→ {logged_in: bool, user_name: str, saved_at: str}
  Lê o Firestore sem fazer requisição ao PROEIS. Retorna logged_in=true
  enquanto houver documento salvo (independente de a sessão PROEIS estar ativa).

POST /api/session-logout
→ {ok: bool}
  Apaga proeis_session/current do Firestore. Próxima operação fará login completo.
```

#### Credenciais e configuração

```
GET /api/env-defaults
→ {has_login, has_password, has_gemini_api_key, http_attempts,
   connect_timeout, read_timeout, filter_max_attempts, ...}

GET /api/options
→ {convenios: [{value, label}], cpas: [{value, label}]}

POST /api/test-login
body: {login, password, gemini_api_key}
→ {ok: bool, login: str, nome: str, message: str}
NOTA: Este endpoint SEMPRE faz login completo (ignora sessão salva).
É um teste real de credenciais.
```

#### Eventos agendados (Firestore)

```
GET  /api/events              → {events: [{id, convenio, cpa, ...}]}
POST /api/events              body: RunRequest → {ok, event}
PUT  /api/events/{event_id}   body: RunRequest → {ok, event}
DELETE /api/events/{event_id}              → {ok}
```

#### Execução (Streaming SSE)

```
POST /api/run
body: RunRequest
→ Server-Sent Events (text/event-stream)
  data: {"type": "log",  "line": "..."}
  data: {"type": "vaga", "label": "...", "data_evento": "...", "acao": "..."}
  data: {"type": "done", "status": "confirmado|pendente|erro", "confirmed": N}

POST /api/list-vagas
body: ListVagasRequest
→ Server-Sent Events (idêntico ao /api/run)
  data: {"type": "done", "status": "ok|erro", "total": N}
```

#### Serviços já marcados

```
POST /api/servicos-marcados
body: {login, password, gemini_api_key}
→ {ok: bool, nome: str, servicos: [{data_hora, evento, tipo_vaga, convenio,
                                     ponto_encontro, endereco, complemento}]}
```

#### Logs de operações

```
GET /api/logs?kind=run|listar
→ {logs: [{name, op_id, kind, status, size_kb, created_at, line_count}]}

GET /api/log-content/{op_id}
→ {op_id, name, kind, status, created_at, content: str}
```

### Funções auxiliares de sessão em `api/index.py`

Todas as funções abaixo lidam com a persistência de sessão no Firestore. Nenhuma faz requisição ao PROEIS — quem faz é `client.check_auth()`.

```python
_session_collection()
  # Retorna a coleção Firestore proeis_session

_save_session(client, user_name)
  # Salva dict(client.session.cookies) + user_name + login + saved_at

_load_session(client) → {valid: bool, user_name: str}
  # Lê Firestore; se login diferente → invalid; carrega cookies no client

_try_restore_session(client) → bool
  # _load_session() + client.check_auth()
  # True = sessão restaurada e válida, login desnecessário
  # False = precisa fazer login

_fetch_user_name_and_save(client)
  # GET MENU_URL → extrai nome do policial → _save_session()
  # Chamada APÓS login_with_retries() quando sessão era inválida
```

**Padrão obrigatório em todos os endpoints** (exceto `/api/test-login`):
```python
client = ProeisHTTP(login=..., password=..., ...)
if not _try_restore_session(client):
    login_with_retries(client, "motivo")
    _fetch_user_name_and_save(client)
# ... prossegue com a operação
```

### Modelo `RunRequest` (completo)

```python
class RunRequest(BaseModel):
    # Credenciais (se vazios, usa variáveis de ambiente)
    login: str = ""
    password: str = ""
    gemini_api_key: str = ""
    gemini_model: str = ""

    # Evento alvo (obrigatórios: convenio e cpa)
    convenio: str = ""            # ex: "08 BPM"
    cpa: str = ""                 # ex: "CPA/INTER-I"
    data_evento: str = ""         # dd/mm/yyyy ou yyyy-mm-dd (vazio = varre todas)
    disponivel: str = "nao-reserva"  # "nao-reserva" | "reserva"
    quantidade: int = 1
    nome_evento: str = ""         # Substring exata do nome no PROEIS
    hora_evento: str = ""         # ex: "08:00"
    turno: str = ""               # "diurno" | "noturno" | "madrugada"
    endereco: str = ""            # Substring do endereço
    scan_rounds: int = 1
    dry_run: bool = False

    # Configurações avançadas (0 = usar padrão do ambiente)
    http_attempts: int = 0
    connect_timeout: int = 0
    read_timeout: int = 0
    filter_max_attempts: int = 0
    captcha_invalid_retries: int = 0
    captcha_refresh_after_invalids: int = 0
    gemini_timeout: int = 0
    auto_retry_rounds: int = 0
    auto_retry_wait_seconds: int = 0
```

---

## Persistência: Google Cloud Firestore

**Database ID:** `proeis` (configurável via `FIRESTORE_DATABASE`)

### Coleções

#### `events` (padrão, via `FIRESTORE_EVENTS_COLLECTION`)
Cada documento = um evento agendado. Campos = todos os campos de `RunRequest` + `created_at`, `updated_at`, `id`.

#### `operation_logs` (padrão, via `FIRESTORE_LOGS_COLLECTION`)
Cada documento = resultado de uma operação.
```json
{
  "op_id":      "a1b2c3d4",
  "kind":       "run | listar | agendamento",
  "status":     "ok | erro | confirmado | pendente",
  "name":       "20260606_120000_a1b2c3d4_run.log",
  "content":    "linha1\nlinha2\n...",
  "created_at": "2026-06-06T12:00:00Z",
  "size_kb":    12.3,
  "line_count": 85,
  "result":     {}
}
```

#### `proeis_session` (padrão, via `FIRESTORE_SESSION_COLLECTION`)
Documento único `"current"` — sessão persistente do usuário:
```json
{
  "cookies":   { "PHPSESSID": "...", "ASP.NET_SessionId": "..." },
  "user_name": "3º SGT PRISCILA NORTHON",
  "login":     "123456",
  "saved_at":  "2026-06-06T12:00:00Z"
}
```
- Criado/atualizado após cada login bem-sucedido
- Apagado ao clicar Sair (`POST /api/session-logout`)
- Sem TTL automático — o `check_auth()` é o único árbitro de validade

---

## Variáveis de Ambiente

### Obrigatórias

```env
PROEIS_LOGIN=123456
PROEIS_PASSWORD=suasenha
GEMINI_API_KEY=AIzaSy...
```

### Opcionais de serviço

```env
GEMINI_MODEL=gemini-3.1-flash-lite  # Padrão (confiável + melhor acerto que 2.5-flash)
CORS_ORIGINS=*
```

### Firestore

```env
FIRESTORE_DATABASE=proeis
FIRESTORE_EVENTS_COLLECTION=events
FIRESTORE_LOGS_COLLECTION=operation_logs
FIRESTORE_SESSION_COLLECTION=proeis_session
```

### Timeouts e tentativas

```env
PROEIS_HTTP_ATTEMPTS=2
PROEIS_CONNECT_TIMEOUT=8
PROEIS_READ_TIMEOUT=25
FILTER_MAX_ATTEMPTS=8        # Tentativas de filtro com captcha por data
CAPTCHA_INVALID_RETRIES=2
CAPTCHA_REFRESH_AFTER_INVALIDS=1
GEMINI_TIMEOUT=30
```

### Retry automático

```env
PROEIS_AUTO_RETRY_ROUNDS=0
PROEIS_AUTO_RETRY_WAIT_SECONDS=300
PROEIS_RECOVERY_WINDOW_SECONDS=0
PROEIS_BATCH_WINDOW_SECONDS=0
PROEIS_BATCH_REPEAT_PAUSE_SECONDS=30
PROEIS_BATCH_MAX_NO_ACTION_ROUNDS=2
```

---

## Frontend: SPA em vanilla JS

Arquivo único `web/app.js`. Sem framework, sem build step.

### Páginas

| ID de nav | Função de render | Descrição |
|-----------|-----------------|-----------|
| `events` | `renderEventsPage()` | CRUD de eventos. Calendário interativo para datas/horários. |
| `servicos` | `renderServicosPage()` | Serviços marcados no PROEIS. **Cacheia resultado em localStorage** (8h). Na segunda visita mostra instantaneamente sem nova requisição. Botão Atualizar força recarregamento. |
| `listar` | `renderListarPage()` | Lista vagas disponíveis com log em tempo real. Em mobile: abas "Log" / "Vagas listadas". |
| `run` | `renderRunPage()` | Execução manual de automação. |
| `settings` | `renderSettingsPage()` | Credenciais em localStorage + configurações avançadas. |
| `help` | `renderHelpPage()` | Guia de uso. |

### Cache de Serviços Marcados

```js
const _SRV_CACHE_KEY = 'proeis_servicos_v1';
// TTL: 8 horas
// Salvo após cada loadServicos() bem-sucedido
// Exibe "atualizado há Xmin" no subtítulo
// Botão Atualizar limpa e recarrega
```

### Sidebar

- **7 botões de navegação**
- **Status da API:** ponto verde/vermelho (`status-dot`, `status-text`)
- **Nome do policial:** exibido quando `GET /api/session-status` retorna `logged_in: true` (`user-name-display`)
- **Botão Sair:** chama `logoutSession()` → `POST /api/session-logout` → esconde `#user-info`

### Estado global

```js
const state = {
  page: 'events',
  options: {},      // Convênios e CPAs
  events: [],       // Eventos do servidor
  envDefaults: {},  // Defaults do ambiente
}
```

### Configurações do usuário (localStorage)

```js
{
  login, password, gemini_api_key,
  convenio, cpa,
  http_attempts, connect_timeout, read_timeout,
  filter_max_attempts, captcha_invalid_retries,
  captcha_refresh_after_invalids, gemini_timeout,
  auto_retry_rounds, auto_retry_wait_seconds,
}
```

### Streaming SSE

```js
for await (const event of api.stream('/api/run', body, signal)) {
  if (event.type === 'log')  { appendLog(event.line); }
  if (event.type === 'vaga') { appendVaga(event); }
  if (event.type === 'done') { showResult(event.status); }
}
```

---

## Resolução de Captcha

O PROEIS exige captcha em **cada submissão de filtro** (uma por data pesquisada). Não é possível eliminar.

### Solvers disponíveis

1. **Gemini 2.5 Flash** (padrão, obrigatório) — visão computacional via Google AI Studio


### Estratégia multi-solver

- Ambos os solvers tentam em paralelo; o mais rápido vence
- Se resposta inválida: reporta erro e tenta novamente (até `FILTER_MAX_ATTEMPTS=8`)
- Tempo médio: **~0.9s por captcha** com Gemini

---

## Execução em Lote (Batch Rápido)

A marcação é feita exclusivamente pelo painel web (`POST /api/run-batch-fast`).
O agendamento automático via Cloud Scheduler foi removido do sistema.

### Batch único com reconexão

Apenas **um** batch rápido roda por vez em cada instância:

- Se o painel chamar `/api/run-batch-fast` enquanto um batch já roda, o servidor
  **reataca** o painel ao batch em andamento em vez de iniciar outro — batches
  simultâneos na mesma conta do PROEIS derrubam a sessão um do outro.
- Se a conexão do painel cair (ex.: timeout de requisição do Cloud Run), o batch
  **continua rodando no servidor** e o painel reconecta sozinho enviando
  `resume_op_id` + `resume_from` (índice da última linha de log recebida),
  retomando o stream do ponto onde parou.
- O keepalive de sessão é feito pelo próprio painel (`/api/session-keepalive-web`)
  enquanto ele estiver aberto.

---

## Regras de Negócio Importantes

### 1. Matching de nome de evento (crítico)
- Substring exata normalizada: `nome_digitado ⊂ texto_da_vaga_no_site`
- Normalização: minúsculo + sem acento + sem espaço duplo
- Correto: `"RAS SÃO FIDÉLIS"` → encontra vagas contendo `"ras sao fidelis"`
- Incorreto: `"ras"` pode pegar eventos errados → sempre usar nome específico

### 2. Tipos de vaga
- `"nao-reserva"` = vagas regulares (padrão)
- `"reserva"` = vagas de reserva (RAS voluntário, escala especial)
- Se configurado errado, a vaga não é encontrada mesmo quando existe

### 3. Sessão persistente (comportamento esperado)
- **Sidebar mostra nome:** indica que há sessão salva no Firestore
- **Operação não faz login:** `check_auth()` confirmou que o PROEIS aceita os cookies
- **Operação FAZ login:** `check_auth()` detectou que PROEIS expirou a sessão (~20min inatividade) → re-login automático e transparente → sessão salva novamente
- **Sair:** apaga Firestore, próxima operação faz login completo

### 4. Cloud Run é stateless
- Sem memória entre requisições
- Todo estado persistente vai para Firestore
- Logs em arquivo ficam no container (efêmeros) — Firestore é a fonte confiável

### 5. Limite de logs
- `_LOGS_LIMIT = 200` — máximo de logs retornados por `/api/logs`
- Filtro: `?kind=run`, `?kind=listar`

### 6. Limpeza de texto (mojibake)
- O PROEIS retorna texto às vezes com encoding corrompido
- `reparar_mojibake()` corrige automaticamente
- `_clean_request_text()` aplica em todos os campos de `RunRequest`

---

## Dependências Python

```
fastapi==0.115.5
uvicorn==0.32.1
requests==2.32.5
beautifulsoup4==4.14.2
truststore==0.10.4
python-multipart==0.0.20
google-cloud-firestore==2.21.0
```

---

## Como adicionar uma nova funcionalidade

### Nova página no frontend

1. Adicionar botão no `<nav>` do `web/index.html`:
   ```html
   <button onclick="navigate('minha-pagina')" id="nav-minha-pagina" class="nav-item w-full">
     <span>🔧</span><span>Minha Página</span>
   </button>
   ```
2. Criar função `renderMinhaPaginaPage()` em `web/app.js`
3. Adicionar `case 'minha-pagina': renderMinhaPaginaPage(); break;` na função `renderPage()`

### Novo endpoint na API

1. Adicionar em `api/index.py` antes de `app.mount("/", ...)` (última linha)
2. **Obrigatório:** usar o padrão de sessão:
   ```python
   client = ProeisHTTP(login=login_val, password=pwd_val, ...)
   if not _try_restore_session(client):
       login_with_retries(client, "motivo")
       _fetch_user_name_and_save(client)
   ```
3. Salvar logs com `_save_operation_log(op_id, kind, status, lines, name, result)`

### Nova coleção Firestore

```python
def _minha_collection():
    return _firestore_db().collection(os.getenv("FIRESTORE_MINHA_COLLECTION", "minha_colecao"))
```

### Novo evento SSE para o frontend

Backend (dentro do thread `_run()`):
```python
print(json.dumps({"type": "meu_tipo", "campo": "valor"}))
```
Frontend (no loop de stream):
```js
if (event.type === 'meu_tipo') { /* tratar */ }
```

---

## Estrutura de um evento SSE

```jsonc
// Log textual
{"type": "log",  "line": "[SESSION] Sessao restaurada para '3º SGT NORTHON' (login ignorado)."}

// Vaga encontrada
{"type": "vaga", "label": "RAS SÃO FIDÉLIS - R. DAS FLORES, 100",
 "data_evento": "14/06/2026", "acao": "Confirmado"}

// Conclusão
{"type": "done", "status": "confirmado", "confirmed": 1, "op_id": "a1b2c3d4", "message": ""}
```
