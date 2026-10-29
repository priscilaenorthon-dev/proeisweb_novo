# CPROEIS — Automação de marcação de vagas no PROEIS RJ

> Memória do projeto. Este arquivo é lido no início de toda sessão do Claude Code.
> Objetivo do dono: **não perder vaga TITULAR** que abre às **6h de quinta-feira**.

## ⭐ CHECAR PRIMEIRO na próxima quinta (30/07) — dúvida do dono
Ler o log `run_scheduled` da quinta (agora completo, 8000 linhas) e responder:
1. **Horário EXATO (Brasília) de cada vaga marcada** — foi antes ou depois das 6h00?
2. **Apareceu alguma vaga TITULAR antes das 6h00** no dropdown/na varredura?
   (Hoje 23/07 não deu pra provar: o log tinha só 2000 linhas e cortou o começo.)
Grep no `content` por: `Clicando em 'Eu Vou'`, `Clicado Eu vou`, `Marcacao confirmada`,
e pelos horários `[HH:MM:SS]` (UTC; Brasília = -3h). Reportar ao dono.

## O que é
App que automatiza a marcação de plantões voluntários (vagas) no site do PROEIS RJ
(`www.proeis.rj.gov.br`, ASP.NET WebForms com __VIEWSTATE e captcha). O usuário
cadastra "eventos" (convênio + CPA + data + hora + tipo) e o sistema loga, resolve
o captcha e marca a vaga.

- **Dono/usuário:** Helpmacae (tihelpmacae@gmail.com). Responder sempre em **português**.
- **Login PROEIS:** ID funcional 51134322 (usuário "AUGUSTO RODRIGUES QUITAR").
- Regra do site: **1 sessão ativa por conta** — um segundo login derruba o primeiro.
- Regra do site: **todas as vagas atualizam 6h de quinta**; cada quinta abre um
  conjunto de datas (a vaga só aparece na data em que o plantão acontece).

## Infra (Google Cloud) — projeto `gen-lang-client-0105958041`
- **Cloud Run:** serviço `proeisweb-novo`, região `southamerica-east1`.
  URL: `https://proeisweb-novo-lgiom3zfja-rj.a.run.app`
  min=max=1 instância (sempre quente), cpuIdle=True, timeout 1800s.
- **Firestore:** database `proeis`. Coleções: `events`, `operation_logs`
  (TTL 14 dias), `captcha_dataset` (TTL 60d), `proeis_session` (doc `current`).
- **Cloud Build:** deploy manual via REST (ver abaixo).
- **Cloud Scheduler** (fuso America/Sao_Paulo, toda quinta):
  - `proeis-warmup-5h47/5h52/5h55` → POST `/api/session-keepalive-web` (aquece a sessão)
  - `proeis-robo-quinta` (5h58) → POST `/api/run-scheduled` (marcação, janela 600s)
  - Avisos (Routine/trigger do Claude, push+email): 6h09 lê o resultado e notifica.

## Custo — REGRA IMPORTANTE
**NÃO usar Gemini em massa.** A IA de captcha é um modelo próprio (ONNX) que resolve
de graça. Gemini só como fallback raro. Rótulos de treino vêm do próprio site
(captchas `accepted=true` no Firestore = rótulo grátis).

## Captcha (OCR próprio)
- CNN PyTorch → ONNX (onnxruntime), preprocess BILINEAR 160x64, CHARS "0123456789ABCDEF".
- Código: `captcha_tests/train/` (train_real.py, export_onnx.py, model.onnx).
- Modelo em produção: ~75% gold (28 imgs humanas), ~92% em holdout real.
- Retreino: `train_real.py` (pré-treino sintético + fine-tune nos reais). Só faz
  deploy do modelo novo se o gold for **>=** o atual. Trigger semanal existe.

## Arquivos principais
- `proeis_http.py` — engine da automação (login, captcha, filtros, marcação em lote
  `run_batch_events`/`run_one_batch_event`, `choose_target_event`).
- `api/index.py` — FastAPI. Endpoints: `/api/run`, `/api/run-batch-fast` (SSE),
  `/api/run-scheduled` (robô 6h, protegido por header `X-Scheduler-Secret`),
  `/api/list-vagas` (SSE), `/api/events`, `/api/session-keepalive-web`, etc.
- `web/` — PWA (app.js, index.html, styles.css, proeis-fixes.js). Sem service worker.

## 🔑 Acesso ao Google Cloud (configurado em 19/08)
Credencial permanente: **conta de serviço `claudinho@gen-lang-client-0105958041.iam.gserviceaccount.com`**,
guardada na **variável de ambiente `CLAUDINHO_SA_KEY`** do ambiente do Claude Code (JSON em uma linha).
Sobrevive à reciclagem do container — não precisa mais pedir reautorização ao dono.

Para obter um token no início da sessão (script versionado, não some com o container):
```bash
TOKEN=$(python3 scripts/gcp_auth.py)
curl -H "Authorization: Bearer $TOKEN" https://firestore.googleapis.com/v1/projects/...
```
Papéis da `claudinho@`: `run.admin`, `cloudscheduler.admin`, `datastore.user`,
`storage.objectAdmin`, `cloudbuild.builds.editor`, `iam.serviceAccountUser` —
cobre Firestore, Cloud Run, Scheduler, Storage e deploys. **Não tem** poder de IAM
nem de faturamento (proposital).
⚠️ Se `CLAUDINHO_SA_KEY` não estiver setada, o dono precisa colá-la nas configurações
do ambiente. **A chave NUNCA vai pro git** — continua valendo a regra.

## Como fazer deploy (fluxo testado)
1. Precisa de token OAuth Google (escopo cloud-platform). O usuário autoriza; o
   refresh_token/SA key ficam no **scratchpad da sessão** (NÃO no git).
2. `git archive HEAD` → upload p/ `gs://gen-lang-client-0105958041_cloudbuild/source/src-<sha>.tar.gz`
3. POST em Cloud Build: steps docker build/push + `gcloud run services update
   proeisweb-novo --image ... --region southamerica-east1`. Espera SUCCESS.
4. Confirma `latestReadyRevision` + `CONDITION_SUCCEEDED` + tráfego 100.
- Branch de trabalho: `claude/cloud-logs-access-12zrvi`.
- **Credenciais nunca vão pro git.** Ficam no scratchpad ou como env var do serviço.

## Como marcar/estudar depende de ler LOGS do Firestore
- `operation_logs` guarda cada operação (kind: `run_scheduled`, `run_batch_fast`,
  `run`, `listar`). Campo `content` = log completo (até 8000 linhas).
- Query com filtro `kind` + `orderBy created_at` **já funciona** — o índice composto
  (`kind` ASC + `created_at` DESC) foi criado em 06/08 e está READY. Antes disso o
  Firestore devolvia `400 FAILED_PRECONDITION "The query requires an index"`, o que
  quebrava o cron de aviso das 6h09 (o dono recebia push dizendo que "o Google
  bloqueou e pediu para ajustar"). Não precisa mais contornar no Python.

## Estado atual das melhorias (roadmap)
Feito:
- Robô agendado 6h (Cloud Scheduler → /api/run-scheduled), titular-only.
- Aquecimento pré-6h (keepalive 5h47/52/55) — entra cedo, mantém sessão viva.
- Fix `KeyError 'data'` (página vazia sob carga vira retry, não crash) via `require_fields`.
- Robô agendado ignora endereço no casamento (env `SCHEDULED_MATCH_ENDERECO=1` reativa).
- Listagem web ao vivo (SSE padding 8KB + flush).
- Log 8000 linhas, TTL 14 dias.
- **Melhoria 1 — cortar datas mortas:** após grace (~6h01), descarta evento sem vaga
  por N rodadas (`BATCH_DEAD_GRACE_SECONDS`, `BATCH_DEAD_DROP_AFTER`).
Em andamento (implementar/testar antes de 30/07):
- **Melhoria 2 — pegar várias vagas na mesma tela** (mesmo convênio+data): marcar
  todas de uma vez sem re-navegar (evita perder vaga enquanto processa outro evento).
- **Melhoria 3 — re-scan na virada das 6h**: descartar telas antigas exatamente às
  6h00 pra pegar o lote novo que cai nesse instante.

## Aprendizados sobre os erros das 6h (da análise dos logs)
- Maior gargalo: **site sobrecarregado às 6h** recusa captcha VÁLIDO em massa
  (a IA acerta, o site rejeita) + derruba conexão (SSL EOF). Defesa = insistência.
- "sem Eu Vou" = data não tem plantão daquele evento (data morta) → melhoria 1.
- "pendente" = vaga existia mas marcação falhou (captcha/site) → melhorias 2 e 3.
- Cada convênio publica datas diferentes; conferir com **Listar Vagas** quais datas
  realmente têm vaga antes de cadastrar.

### Rodada de 06/08 (quinta) — 2 marcadas, 1 titular perdida
- Marcou **MOTO PATRULHA SÃO FIDÉLIS 13/08 e 15/08** (6h00:01 e 6h00:58). IA de
  captcha: **147 resolvidos, 100% OCR-local, 0 Gemini** (conf até 1.00) — a IA não
  falhou nenhuma vez.
- **Perdeu a RAS SÃO FIDÉLIS titular**: o robô achou a vaga e clicou "Eu Vou" **3x**,
  e nas 3 o site respondeu "Confirmacao de sucesso NAO encontrada" (site sobrecarregado
  recusando submissão válida). Sinais de pico: 93 páginas vazias, 36 captchas recusados
  no filtro, 9 quedas de conexão.
- **Próxima melhoria (a fazer):** quando achar a titular mas o site recusar a
  confirmação, **insistir muito mais** nesse evento (usou só 536s de 600s — sobra tempo)
  e **parar de rotular como "sem vaga (data sem plantao)"**, que é diagnóstico errado.
- Lição: às 8h30 o lote já esvaziou (rodada manual deu 0/30). Só vale insistir NA HORA.

## ⚖️ REGRAS OFICIAIS do PROEIS (pesquisadas em 06/08) — LER ANTES DE MEXER NO ROBÔ
**Decreto 48.192/2022** (Diário Oficial RJ, fonte oficial) alterou o Dec. 43.538/2012:
- **§13 — teto semanal: no máximo 2 serviços em dias ÚTEIS + 2 em dias NÃO ÚTEIS, por ciclo semanal.**
- **§4º — no máximo 120 horas efetivas/mês** (= 15 turnos de 8h) e **mínimo 8h de repouso entre serviços**.
- §6º — durante **férias e licença especial PODE** participar (não bloquear por férias).
**Diretriz PM/3 nº 014/11** (BolPM 064): uma vez escalado, **obriga-se a cumprir** (salvo força maior);
**"tentar burlar a regulamentação" = suspensão de 3 meses + falta GRAVÍSSIMA**, reincidência = exclusão;
atestado/afastamento >72h em 30 dias exclui (retorno após 3 meses); restrição médica exclui.

📌 **DECISÃO DO DONO (06/08): não implementar controle de cota no robô e NÃO levantar mais esse
assunto.** O registro acima fica só como referência factual da pesquisa. O controle de quantos
plantões marcar é do dono, não do robô. Não sugerir trava de dias úteis/não úteis novamente.

NÃO encontrado em norma nenhuma (não inventar): horário "6h de quinta" (só "ciclos semanais");
conceito de titular vs reserva; significado de `curso: S/N`.

## Diagnóstico da recusa (por que perdemos vaga) — implementado 06/08
Antes, `confirm_if_needed` logava só `final_text[:1200]` = o dropdown de convênios; sucesso e recusa
saíam **idênticos** no log, então "site sobrecarregado" era dedução, não prova.
Agora `ProeisHTTP.extract_site_feedback()` tira o boilerplate e loga: mensagens de labels/spans
(id/class ~ msg|erro|alerta|valida), `alert()` do ASP.NET, o texto útil, e — o mais importante —
**se o rótulo da vaga alvo ainda aparece na página**:
- alvo **presente** → o site recusou nossa submissão;
- alvo **sumiu** → outro voluntário levou a vaga (corrida), insistir não adianta.
Na próxima quinta, **ler esses `[diagnostico]` no log** antes de decidir qualquer nova melhoria.

### Rodada de 10/09 — 1 marcada, 1 titular perdida (tela vazia)
Marcou só `35 ZE 19/09`. Descartou 4 eventos como "data morta" — e um deles (35 ZE
**15/09, 4 postos titular**) tinha vaga de verdade. Usou 295s de 600s: desistiu com
5 min de janela sobrando. Duas causas, **ambas corrigidas** (commit `405c328`):
1. A carência do corte de datas mortas contava do **início da rodada** (5h58+180s →
   expirava 6h01), mas o site só publicou as datas ~6h02 (depois de 4 erros 500).
   Agora é ancorada nas **6h00** (6h00+180s = 6h03).
2. **Tela vazia** (nenhuma linha "Eu Vou", nem de outros eventos) contava como rodada
   morta. É o site engasgando, não ausência de plantão — quando a data realmente não
   tem vaga, a tela volta com as vagas dos OUTROS eventos. Agora `choose_target_event`
   marca com `PAGINA_VAZIA_MARCA` e `is_empty_page_error()` faz o lote não contar.

### Rodada de 29/10 (quinta) — 4 marcadas, MELHOR resultado até hoje
Marcou 4 titular do lote 02–08/11: **RAS SÃO FIDÉLIS 04/11 22h, 06/11 22h, 07/11 14h,
08/11 06h** (+ a MOTO PATRULHA 01/11 13h da semana anterior = 5 no total, todas TITULAR).
Captcha: **0 recusados, 0 Gemini** — a IA não falhou nenhuma vez. 23.901 linhas de log.
Três problemas reais encontrados, **todos corrigidos** (ver abaixo):
1. **Log cortado:** o cap de 800KB guardava só a CAUDA; o log salvo começava às 6h05 e
   justamente as 4 marcações (6h00–6h05) ficaram de fora. Não deu pra provar horário.
2. **Data errada:** o log disse `08/11 22:00 confirmado`, mas o site gravou **06/11 22:00**.
   O clique caiu numa tela que não era a data pedida (VIEWSTATE/tela velha).
3. **Tempo mal gasto (o pior):** 20 tentativas foram todas em 06/11 e 07/11, que o site
   devolvia **em branco** (170 telas vazias, 9 quedas de conexão), enquanto a data 02/11
   — cortada como "data sem plantão" às ~6h03 com 6 tentativas — publicou a vaga
   **MOTO PATRULHA 02/11 13:00 titular** DEPOIS do corte. Ninguém voltou nela.
   Também escaparam 3 titular visíveis em 07/11 (RAS 06h, MOTO 13h, RAS 22h).

### Correções de 29/10 (as três)
1. **Log guarda começo + fim + linhas-chave** (`_log_excerpt`/`_log_head_tail` em
   `api/index.py`): bloco `=== LINHAS-CHAVE ===` no topo (marcação, `[diagnostico]`,
   `[auditoria]`, resumo por evento — nunca se perdem), depois 60% do começo e 40% do
   fim com marcador `[N linhas omitidas no meio]`. Env: `LOG_HEAD_RATIO` (0.6),
   `LOG_KEY_MAX_LINES` (600).
2. **Travessa de data + auditoria** (`proeis_http.py`): `page_selected_date()` lê a data
   que o site mostra como selecionada e `choose_target_event` **se recusa a clicar** se
   não for a data pedida (erro comum → refaz o filtro, não conta como "sem vaga").
   Depois do sucesso, `audit_marking()` lê a lista oficial de marcados e loga
   `[auditoria] OK` ou `[auditoria] DIVERGENCIA` com a data real. Env: `CHECK_PAGE_DATE`,
   `AUDIT_MARKING`. Helpers novos: `extract_iso_date()`, `same_site_date()`.
3. **Rotação justa + re-admissão de datas** (`run_batch_events`): a fila agora é
   `vaga comprovada → demais → travados em tela vazia` (tela vazia vai pro fim, não
   monopoliza a rodada); e quando sobra janela e só restam eventos travados em tela
   vazia, as datas cortadas **voltam pra fila** (`Re-admitindo ...`). Env:
   `BATCH_DEAD_REVIVE_MAX` (3), `BATCH_DEAD_REVIVE_MIN_REMAINING` (60s).

## ⚠️ Robô agendado IGNORA evento sem data (pegadinha)
`_scheduled_run_requests()` (api/index.py) expande `data_evento` por vírgula; **evento
com data em branco gera ZERO sub-eventos e some da rodada, sem aviso**. No botão
**Executar** do app é diferente: lá em branco funciona (varre e acha a primeira data).
Em 09/09 dois eventos estavam assim e iam ser ignorados em silêncio. **Sempre conferir
se todo evento tem data antes da quinta.**

## Testes (rodar antes de qualquer deploy)
- `python3 tests/test_vaga_viva.py` — vaga comprovada não vira "data morta" e insiste mais.
- `python3 tests/test_diagnostico_recusa.py` — os 3 cenários de recusa ficam distinguíveis no log.
- `python3 tests/test_tela_vazia.py` — tela vazia do site não vira "data morta".
- `python3 tests/test_log_recorte.py` — log de 24k linhas mantém as marcações das 6h.
- `python3 tests/test_data_errada.py` — não clica em tela de data errada; auditoria pega a divergência.
- `python3 tests/test_readmissao_datas.py` — data cortada volta pra fila e pega vaga publicada atrasado.
- Dependências não vêm no container:
  `python3 -m pip install -q --break-system-packages beautifulsoup4 requests fastapi pydantic google-cloud-firestore`
  (os 3 primeiros testes só precisam de `beautifulsoup4 requests`; `test_log_recorte` importa `api.index`).

## Como o usuário volta a este projeto
Este repo + ambiente é o "projeto CPROEIS". Basta abrir uma sessão nova neste
repositório no Claude Code — este CLAUDE.md carrega sozinho e eu já sei tudo.
