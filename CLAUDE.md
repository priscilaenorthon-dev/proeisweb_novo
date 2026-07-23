# CPROEIS — Automação de marcação de vagas no PROEIS RJ

> Memória do projeto. Este arquivo é lido no início de toda sessão do Claude Code.
> Objetivo do dono: **não perder vaga TITULAR** que abre às **6h de quinta-feira**.

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
- Query com filtro `kind` + `orderBy created_at` precisa de índice composto (falha);
  contorne com orderBy só em created_at, ou filtre e ordene no Python.

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

## Como o usuário volta a este projeto
Este repo + ambiente é o "projeto CPROEIS". Basta abrir uma sessão nova neste
repositório no Claude Code — este CLAUDE.md carrega sozinho e eu já sei tudo.
