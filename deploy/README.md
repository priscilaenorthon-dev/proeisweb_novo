# Scripts de Deploy — Infra Rápida

Esses scripts eliminam o **cold start** e o **login repetido** que tornam a marcação de vagas lenta. Após executar os 3 passos, a marcação passa de 15–30s (caso frio) para ~2–4s consistentes.

## Por que esses scripts existem

| Problema | Causa | Script |
|----------|-------|--------|
| Cold start 5–15s | Cloud Run sem `min-instances` dorme quando ocioso | `01` |
| Login a cada marcação (~1,7s + captcha) | `SCHEDULER_SECRET` ausente → keepalive não funciona | `02` |
| Sessão PROEIS expira em ~20 min | Job keepalive nunca foi criado no Cloud Scheduler | `03` |

## Pré-requisitos

```bash
# Autenticar no gcloud
gcloud auth login

# Selecionar o projeto correto
gcloud config set project SEU_PROJECT_ID

# Confirmar que a API do Cloud Scheduler está ativada
gcloud services enable cloudscheduler.googleapis.com --project=SEU_PROJECT_ID
```

## Ordem de execução

### Passo 1 — Eliminar cold start

```bash
# Edite PROJECT_ID dentro do script, depois execute:
bash deploy/01-set-min-instances.sh
```

Mantém 1 instância do Cloud Run sempre ativa. A primeira requisição após ociosidade deixa de esperar 5–15s.

### Passo 2 — Configurar SCHEDULER_SECRET

```bash
# Se já tem SCHEDULER_SECRET configurado: edite SECRET_EXISTENTE= no script
# Se não tem: deixe em branco e o script gera automaticamente
bash deploy/02-set-scheduler-secret.sh
```

**Guarde o valor exibido** — você vai precisar dele no passo 3.

> Se você já usa `/api/scheduler/run` (marcação automática diária), mantenha o mesmo `SCHEDULER_SECRET`. Gerar um novo exige atualizar o job existente do Cloud Scheduler também.

### Passo 3 — Criar job keepalive (a cada 10 minutos)

```bash
# Edite PROJECT_ID e SCHEDULER_SECRET (do passo 2) dentro do script, depois:
bash deploy/03-setup-keepalive-scheduler.sh
```

Cria um Cloud Scheduler job que chama `POST /api/session-keepalive` a cada 10 minutos. O endpoint renova a sessão no Firestore enquanto ainda está válida — quando expirou, faz login completo e salva. O script é idempotente: se o job já existir, atualiza.

## Verificação

```bash
# 1. Confirmar que SCHEDULER_SECRET está ativo
curl https://proeisweb-novo-1082055415046.southamerica-east1.run.app/api/health \
  | jq .scheduler_secret
# → true

# 2. Forçar execução imediata do keepalive
gcloud scheduler jobs run proeis-keepalive \
  --project=SEU_PROJECT_ID --location=southamerica-east1

# 3. Verificar que sessão está logada sem ninguém ter aberto o painel
curl https://proeisweb-novo-1082055415046.southamerica-east1.run.app/api/session-status \
  | jq .
# → { "logged_in": true, "user_name": "...", "saved_at": "..." }

# 4. Executar uma marcação real e verificar que o log NÃO contém "FASE 1/4: LOGIN"
# (sessão restaurada direto, sem login)
```

## Nota de custo

`min-instances=1` mantém **1 instância sempre ativa**, mesmo sem tráfego. Custo estimado no Cloud Run (região `southamerica-east1`):

| Configuração | Custo mensal estimado |
|---|---|
| `min-instances=0` (padrão) | ~US$ 0 (paga só por uso) |
| `min-instances=1` (CPU throttled) | ~US$ 10–14/mês |
| `min-instances=1` + `--no-cpu-throttling` | ~US$ 15–22/mês |

O Cloud Scheduler adiciona ~US$ 0,10/mês pelos jobs (dentro do free tier de 3 jobs).

## Reverter

```bash
# Voltar ao comportamento serverless (cold start volta)
gcloud run services update proeisweb-novo \
  --project=SEU_PROJECT_ID --region=southamerica-east1 --min-instances=0

# Remover o job keepalive
gcloud scheduler jobs delete proeis-keepalive \
  --project=SEU_PROJECT_ID --location=southamerica-east1
```
