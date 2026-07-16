# Scripts de Deploy — Infra Rápida

## Por que este script existe

| Problema | Causa | Script |
|----------|-------|--------|
| Cold start 5–15s | Cloud Run sem `min-instances` dorme quando ocioso | `01` |

> **Nota:** a marcação automática via Cloud Scheduler (`/api/scheduler/run`) e o
> keepalive via Cloud Scheduler (`/api/session-keepalive`) foram **removidos** do
> sistema. A marcação agora é feita exclusivamente pelo painel web, e a sessão é
> mantida viva pelo keepalive do próprio painel (`/api/session-keepalive-web`),
> que roda enquanto o painel estiver aberto.

## Pré-requisitos

```bash
# Autenticar no gcloud
gcloud auth login

# Selecionar o projeto correto
gcloud config set project SEU_PROJECT_ID
```

## Passo 1 — Eliminar cold start

```bash
# Edite PROJECT_ID dentro do script, depois execute:
bash deploy/01-set-min-instances.sh
```

Mantém 1 instância do Cloud Run sempre ativa. A primeira requisição após ociosidade deixa de esperar 5–15s.

## Timeout de requisição (recomendado)

O batch rápido pode levar mais de 5 minutos, e o padrão do Cloud Run corta a
conexão do painel aos 300s. O painel reconecta sozinho ao batch em andamento,
mas subir o timeout reduz o número de reconexões:

```bash
gcloud run services update proeisweb-novo \
  --project=SEU_PROJECT_ID --region=southamerica-east1 --timeout=1800
```

## Nota de custo

`min-instances=1` mantém **1 instância sempre ativa**, mesmo sem tráfego. Custo estimado no Cloud Run (região `southamerica-east1`):

| Configuração | Custo mensal estimado |
|---|---|
| `min-instances=0` (padrão) | ~US$ 0 (paga só por uso) |
| `min-instances=1` (CPU throttled) | ~US$ 10–14/mês |
| `min-instances=1` + `--no-cpu-throttling` | ~US$ 15–22/mês |

## Reverter

```bash
# Voltar ao comportamento serverless (cold start volta)
gcloud run services update proeisweb-novo \
  --project=SEU_PROJECT_ID --region=southamerica-east1 --min-instances=0
```
