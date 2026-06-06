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
├── logs/                   # Logs de operações (fallback local quando Firestore indisponível)
├── requirements.txt        # Dependências Python
└── .env                    # Variáveis de ambiente (não versionado)
```

**Stack:**
- Backend: Python 3.11+ / FastAPI / Uvicorn
- Frontend: HTML + vanilla JavaScript (sem framework) + Tailwind CSS (CDN)
- Banco de dados: Google Cloud Firestore
- Infraestrutura: Google Cloud Run (serverless, stateless)
- Deploy: Push para `main` no GitHub → CI/CD automático para Cloud Run
- Região: `southamerica-east1` (São Paulo)

---

## URLs do Site PROEIS (alvos da automação)

```python
BASE_URL     = "https://www.proeis.rj.gov.br/"
DEFAULT_URL  = "https://www.proeis.rj.gov.br/Default.aspx"          # Tela de login
MENU_URL     = "https://www.proeis.rj.gov.br/FrmMenuVoluntario.aspx" # Menu principal
ASSOCIAR_URL = "https://www.proeis.rj.gov.br/FrmEventoAssociar.aspx" # Formulário de filtros/vagas
```

O site usa **ASP.NET WebForms** com `__VIEWSTATE`, `__EVENTVALIDATION` e postbacks. Toda interação exige extração e reenvio desses campos ocultos.

---

## Fluxo de Automação (passo a passo)

```
1. Restaurar sessão do Firestore (cookies salvos)
   └── Se válida: pular login (~0.3s de verificação)
   └── Se inválida/inexistente: fazer login completo (~1.1s + captcha)

2. Navegar para o formulário de filtros
   └── Reutiliza menu já carregado quando possível (sem requisição extra)

3. Para cada data disponível:
   ├── Preencher filtros: convênio + data + CPA
   ├── Resolver captcha obrigatório (~0.9s via Gemini)
   ├── Buscar vagas na página de resultados
   ├── Filtrar por nome_evento, hora_evento, turno, endereço
   └── Clicar "Eu Vou" → confirmar booking

4. Salvar cookies atualizados no Firestore
5. Gravar log da operação no Firestore
```

**Tempo estimado por vaga (com sessão ativa, data conhecida):** ~2 segundos  
**Tempo por evento (varredura em 11 datas):** ~10–15 segundos  
**Gargalo incontornável:** captcha por filtro (~0.9s, exigido pelo PROEIS em cada busca)

---

## Módulo Principal: `proeis_http.py`

### Classe `ProeisHTTP`

```python
client = ProeisHTTP(
    login="123456",
    password="senha",
    twocaptcha_key="",        # opcional
    gemini_api_key="AIza...", # obrigatório para resolver captcha
    debug=True,
)
```

#### Métodos de sessão/HTTP

| Método | Descrição |
|--------|-----------|
| `reset_session()` | Recria `requests.Session` do zero (limpa cookies) |
| `check_auth() → bool` | GET rápido ao MENU_URL para verificar se sessão está ativa; seta `self.soup` |
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
| `navigate_to_service_page()` | Navega do menu até a tela de filtros de vagas |
| `_try_navigate_to_service_page() → bool` | Implementação interna com múltiplos passos de navegação |
| `reset_navigation_state()` | Volta ao menu para limpar estado antes da próxima operação |

#### Métodos de captcha

| Método | Descrição |
|--------|-----------|
| `extract_captcha_image(soup) → str` | Extrai imagem base64 do captcha da página |
| `solve_captcha(image) → CaptchaSubmission` | Resolve via Gemini + 2Captcha em paralelo |
| `solve_page_captcha(soup) → (soup, texto)` | Resolve captcha na página com retry automático |
| `report_bad_captcha()` | Reporta captcha errado ao serviço (para treino) |

#### Métodos de vaga

| Método | Descrição |
|--------|-----------|
| `fill_filters(convenio, data, cpa, prefer)` | Preenche e submete formulário de filtros (inclui captcha) |
| `available_candidates(soup, prefer) → list[Candidate]` | Extrai lista de vagas disponíveis da página |
| `choose_target_event(soup, nome, hora, turno, endereco) → Candidate` | Seleciona a vaga que melhor corresponde aos critérios |
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
    solver_index: int     # 0=Gemini, 1=2Captcha
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
→ {status, version, scheduler_secret: bool, firestore: bool}
```

#### Sessão persistente

```
GET /api/session-status
→ {logged_in: bool, user_name: str, saved_at: str}

POST /api/session-logout
→ {ok: bool}
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
  data: {"type": "log", "line": "..."}
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
→ {ok: bool, nome: str, servicos: [{data, hora, local, convenio}]}
```

#### Agendamento automático (Cloud Scheduler)

```
POST /api/scheduler/run
header: x-scheduler-secret: <SCHEDULER_SECRET>
body: SchedulerRunRequest (opcional — usa eventos do Firestore por padrão)
→ {ok: bool, total: int, results: [{index, status, confirmed, convenio, cpa, ...}]}
```

**Proteção:** o endpoint valida o header `x-scheduler-secret` contra `SCHEDULER_SECRET` do ambiente. Retorna 401 se inválido.

#### Logs de operações

```
GET /api/logs?kind=agendamento|run|listar
→ {logs: [{name, op_id, kind, status, size_kb, created_at, line_count}]}

GET /api/log-content/{op_id}
→ {op_id, name, kind, status, created_at, content: str}
```

### Modelo `RunRequest` (completo)

```python
class RunRequest(BaseModel):
    # Credenciais (se vazios, usa variáveis de ambiente)
    login: str = ""
    password: str = ""
    gemini_api_key: str = ""
    twocaptcha_key: str = ""
    gemini_model: str = ""

    # Evento alvo (obrigatórios: convenio e cpa)
    convenio: str = ""            # ex: "08 BPM"
    cpa: str = ""                 # ex: "CPA/INTER-I"
    data_evento: str = ""         # dd/mm/yyyy ou yyyy-mm-dd (vazio = varre todas)
    disponivel: str = "nao-reserva"  # "nao-reserva" | "reserva"
    quantidade: int = 1           # Quantas vagas marcar
    nome_evento: str = ""         # Substring exata do nome do evento no PROEIS
    hora_evento: str = ""         # ex: "08:00"
    turno: str = ""               # "diurno" | "noturno" | "madrugada"
    endereco: str = ""            # Substring do endereço
    scan_rounds: int = 1          # Rodadas de varredura
    dry_run: bool = False         # Simula sem confirmar

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

#### `events` (default, configurável via `FIRESTORE_EVENTS_COLLECTION`)
Cada documento = um evento agendado. Campos = todos os campos de `RunRequest` + `created_at`, `updated_at`, `id`.

#### `operation_logs` (default, configurável via `FIRESTORE_LOGS_COLLECTION`)
Cada documento = resultado de uma operação. Campos:
```json
{
  "op_id": "a1b2c3d4",
  "kind": "run | listar | agendamento",
  "status": "ok | erro | confirmado | pendente",
  "name": "20260606_120000_a1b2c3d4_run.log",
  "content": "linha1\nlinha2\n...",
  "created_at": "2026-06-06T12:00:00Z",
  "size_kb": 12.3,
  "line_count": 85,
  "result": {}
}
```

#### `proeis_session` (default, configurável via `FIRESTORE_SESSION_COLLECTION`)
Documento único `"current"`:
```json
{
  "cookies": {"PHPSESSID": "...", "ASP.NET_SessionId": "..."},
  "user_name": "3º SGT PRISCILA NORTHON",
  "login": "123456",
  "saved_at": "2026-06-06T12:00:00Z"
}
```
Sessão é considerada válida enquanto `check_auth()` retornar True. Deletada ao clicar "Sair".

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
TWOCAPTCHA_API_KEY=          # Solver alternativo de captcha
GEMINI_MODEL=gemini-2.5-flash-lite  # Padrão atual
SCHEDULER_SECRET=secret123   # Obrigatório para o endpoint /api/scheduler/run
CORS_ORIGINS=*               # Origens permitidas
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
PROEIS_HTTP_ATTEMPTS=2        # Tentativas por requisição HTTP
PROEIS_CONNECT_TIMEOUT=8      # Timeout de conexão (segundos)
PROEIS_READ_TIMEOUT=25        # Timeout de leitura (segundos)
FILTER_MAX_ATTEMPTS=8         # Tentativas de preenchimento de filtro (com captcha)
TWOCAPTCHA_INVALID_RETRIES=2
TWOCAPTCHA_REFRESH_AFTER_INVALIDS=1
GEMINI_TIMEOUT=30
```

### Retry automático (multi-round)

```env
PROEIS_AUTO_RETRY_ROUNDS=0          # Rodadas extras (0 = sem retry)
PROEIS_AUTO_RETRY_WAIT_SECONDS=300  # Espera entre rodadas (segundos)
PROEIS_RECOVERY_WINDOW_SECONDS=0
PROEIS_BATCH_WINDOW_SECONDS=0
PROEIS_BATCH_REPEAT_PAUSE_SECONDS=30
PROEIS_BATCH_MAX_NO_ACTION_ROUNDS=2
```

---

## Frontend: SPA em vanilla JS

Arquivo único `web/app.js` (~1.540 linhas). Sem framework, sem build step.

### Páginas

| ID de nav | Função de render | Descrição |
|-----------|-----------------|-----------|
| `events` | `renderEventsPage()` | Lista, cria, edita e remove eventos agendados. Calendário interativo para selecionar datas/horários. |
| `servicos` | `renderServicosPage()` | Busca e exibe os voluntariados já marcados no PROEIS. |
| `listar` | `renderListarPage()` | Executa listagem de vagas disponíveis com log em tempo real. Em mobile: abas "Log" / "Vagas listadas". |
| `run` | `renderRunPage()` | Executa automação manual para um evento específico. |
| `schedule` | `renderSchedulePage()` | Define horário para execução automática diária (Cloud Scheduler). |
| `settings` | `renderSettingsPage()` | Salva credenciais no localStorage. Testa login real. Configura timeouts avançados. |
| `help` | `renderHelpPage()` | Guia de uso do sistema. |

### Estado global

```js
const state = {
  page: 'events',        // Página atual
  options: {},           // Convênios e CPAs carregados
  events: [],            // Eventos do servidor
  envDefaults: {},       // Defaults do ambiente
}
```

### Configurações do usuário (localStorage)

```js
{
  login: "",
  password: "",
  gemini_api_key: "",
  twocaptcha_key: "",
  convenio: "",
  cpa: "",
  http_attempts: 0,
  connect_timeout: 0,
  read_timeout: 0,
  filter_max_attempts: 0,
  captcha_invalid_retries: 0,
  captcha_refresh_after_invalids: 0,
  gemini_timeout: 0,
  auto_retry_rounds: 0,
  auto_retry_wait_seconds: 0,
}
```

### Streaming SSE (como funciona)

```js
// Frontend consome o stream linha a linha
for await (const event of api.stream('/api/run', body, signal)) {
  if (event.type === 'log')  { appendLog(event.line); }
  if (event.type === 'vaga') { appendVaga(event); }
  if (event.type === 'done') { showResult(event.status); }
}
```

### Sidebar

- **7 botões de navegação** (`nav-events`, `nav-servicos`, `nav-listar`, `nav-run`, `nav-schedule`, `nav-settings`, `nav-help`)
- **Status da API:** ponto verde/vermelho + texto (`status-dot`, `status-text`)
- **Usuário logado:** nome exibido quando `session-status` retorna `logged_in: true` (`user-name-display`)
- **Botão Sair:** chama `logoutSession()` → `POST /api/session-logout` → limpa exibição

---

## Resolução de Captcha

O PROEIS exige captcha em **cada submissão de filtro** (uma por data pesquisada). Não é possível eliminar.

### Solvers disponíveis

1. **Gemini 2.5 Flash-Lite** (padrão, obrigatório) — visão computacional via API Google AI Studio
2. **2Captcha** (opcional) — serviço pago, usado em paralelo

### Estratégia multi-solver

- Ambos os solvers tentam em paralelo
- O primeiro a responder com confiança suficiente vence
- Se resposta inválida: reporta erro, tenta novamente (até `FILTER_MAX_ATTEMPTS=8`)
- Tempo médio: **~0.9s por captcha** com Gemini

---

## Agendamento Automático (Cloud Scheduler)

Configurado no Google Cloud para disparar diariamente às **07:00 horário de Brasília** (10:00 UTC).

**Fluxo:**
1. Cloud Scheduler faz `POST /api/scheduler/run` com header `x-scheduler-secret`
2. Endpoint busca eventos ativos no Firestore
3. Para cada evento: tenta restaurar sessão → se inválida, faz login → marca vaga
4. Salva resultado no Firestore com `kind=agendamento`

---

## Regras de Negócio Importantes

### 1. Matching de nome de evento (crítico)
- É **substring exata normalizada**: `nome_digitado ⊂ texto_da_vaga_no_site`
- Normalização: minúsculo + sem acento + sem espaço duplo
- Exemplo correto: cadastrar `"RAS SÃO FIDÉLIS"` → normaliza para `"ras sao fidelis"` → encontra vagas com esse texto
- Exemplo incorreto: `"ras"` sozinho pode pegar vagas erradas → sempre usar nome específico

### 2. Tipos de vaga
- `"nao-reserva"` = vagas regulares (padrão)
- `"reserva"` = vagas de reserva (RAS voluntário, escala especial)
- O tipo incorreto resulta em não encontrar a vaga mesmo quando existe

### 3. Sessão persistente
- Cookies salvos no Firestore (`proeis_session/current`)
- `check_auth()` valida a cada operação via GET ao MENU_URL
- Se sessão expirou no servidor PROEIS (~20min de inatividade): refaz login automaticamente
- Única forma de limpar: usuário clicar "Sair" → `POST /api/session-logout`

### 4. Cloud Run é stateless
- Sem memória entre requisições
- Todo estado persistente vai para Firestore
- Logs em arquivo ficam no container (efêmeros) — Firestore é a fonte confiável

### 5. Limite de logs
- `_LOGS_LIMIT = 200` — máximo de logs retornados por `/api/logs`
- Filtro por tipo: `?kind=agendamento`, `?kind=run`, `?kind=listar`

### 6. Limpeza de texto (mojibake)
- O PROEIS retorna texto às vezes com encoding corrompido (UTF-8 lido como CP1252)
- `reparar_mojibake()` corrige automaticamente antes de usar os dados
- `_clean_request_text()` aplica isso em todos os campos de `RunRequest`

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
     <span class="text-base">🔧</span><span>Minha Página</span>
   </button>
   ```
2. Criar função `renderMinhaPaginaPage()` em `web/app.js`
3. Adicionar `case 'minha-pagina': renderMinhaPaginaPage(); break;` na função `renderPage()`

### Novo endpoint na API

1. Adicionar em `api/index.py` antes de `app.mount("/", ...)` (última linha)
2. Usar `_try_restore_session(client)` + `login_with_retries(client, ...)` para autenticação
3. Salvar logs com `_save_operation_log(op_id, kind, status, lines, name, result)`

### Nova coleção Firestore

1. Criar função `_minha_collection()` seguindo o padrão:
   ```python
   def _minha_collection():
       return _firestore_db().collection(os.getenv("FIRESTORE_MINHA_COLLECTION", "minha_colecao"))
   ```

### Novo evento SSE para o frontend

No backend, dentro do `_run()` thread:
```python
print(json.dumps({"type": "meu_tipo", "campo": "valor"}))
```
No frontend, no loop de stream:
```js
if (event.type === 'meu_tipo') { /* tratar */ }
```

---

## Estrutura de um evento SSE

```jsonc
// Log textual (aparece no terminal de log)
{"type": "log", "line": "[SESSION] Sessao restaurada para '3º SGT NORTHON' (login ignorado)."}

// Vaga encontrada (aparece na lista de vagas)
{
  "type": "vaga",
  "label": "RAS SÃO FIDÉLIS - R. DAS FLORES, 100",
  "data_evento": "14/06/2026",
  "acao": "Confirmado"
}

// Conclusão da operação
{
  "type": "done",
  "status": "confirmado",  // ou "pendente" ou "erro"
  "confirmed": 1,
  "op_id": "a1b2c3d4",
  "message": ""
}
```
