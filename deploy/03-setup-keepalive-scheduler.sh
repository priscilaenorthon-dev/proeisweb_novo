#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# 03-setup-keepalive-scheduler.sh
# Cria (ou atualiza) um job no Cloud Scheduler que chama /api/session-keepalive
# a cada 10 minutos, mantendo a sessão do PROEIS sempre viva no Firestore.
#
# Por que 10 minutos? O servidor ASP.NET do PROEIS expira sessões após ~20 min
# de inatividade. Disparar a cada 10 min garante margem de segurança.
#
# Pré-requisito: executar 01-set-min-instances.sh e 02-set-scheduler-secret.sh
# antes deste script.
#
# Uso: bash deploy/03-setup-keepalive-scheduler.sh
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Edite estas variáveis antes de executar ───────────────────────────────────
PROJECT_ID="SEU_PROJECT_ID"      # ex: meu-projeto-123456
REGION="southamerica-east1"
SERVICE_URL="https://proeisweb-novo-1082055415046.southamerica-east1.run.app"

# Cole aqui o SCHEDULER_SECRET gerado/exibido pelo passo 02:
SCHEDULER_SECRET="COLE_O_SECRET_DO_PASSO_02_AQUI"

JOB_NAME="proeis-keepalive"
# ─────────────────────────────────────────────────────────────────────────────

if [[ "$SCHEDULER_SECRET" == "COLE_O_SECRET_DO_PASSO_02_AQUI" ]]; then
  echo "ERRO: edite SCHEDULER_SECRET neste script com o valor gerado no passo 02."
  exit 1
fi

KEEPALIVE_URL="${SERVICE_URL}/api/session-keepalive"

echo "==> Configurando Cloud Scheduler job '$JOB_NAME'..."
echo "    Projeto  : $PROJECT_ID"
echo "    Região   : $REGION"
echo "    URL      : $KEEPALIVE_URL"
echo "    Schedule : a cada 10 minutos (*/10 * * * *)"
echo ""

# Tenta criar; se já existir, atualiza.
if gcloud scheduler jobs describe "$JOB_NAME" \
    --project="$PROJECT_ID" --location="$REGION" &>/dev/null; then

  echo "==> Job já existe — atualizando..."
  gcloud scheduler jobs update http "$JOB_NAME" \
    --project="$PROJECT_ID" \
    --location="$REGION" \
    --schedule="*/10 * * * *" \
    --time-zone="America/Sao_Paulo" \
    --uri="$KEEPALIVE_URL" \
    --http-method=POST \
    --headers="X-Scheduler-Secret=$SCHEDULER_SECRET" \
    --attempt-deadline=120s

else
  echo "==> Criando novo job..."
  gcloud scheduler jobs create http "$JOB_NAME" \
    --project="$PROJECT_ID" \
    --location="$REGION" \
    --schedule="*/10 * * * *" \
    --time-zone="America/Sao_Paulo" \
    --uri="$KEEPALIVE_URL" \
    --http-method=POST \
    --headers="X-Scheduler-Secret=$SCHEDULER_SECRET" \
    --attempt-deadline=120s
fi

echo ""
echo "==> Job '$JOB_NAME' configurado com sucesso."
echo ""
echo "    Para testar manualmente (execução imediata):"
echo "    gcloud scheduler jobs run $JOB_NAME \\"
echo "      --project=$PROJECT_ID --location=$REGION"
echo ""
echo "    Para verificar o resultado:"
echo "    curl ${SERVICE_URL}/api/session-status | jq ."
echo "    # logged_in deve ser true sem que ninguém tenha aberto o painel"
echo ""
echo "    Para remover o job:"
echo "    gcloud scheduler jobs delete $JOB_NAME \\"
echo "      --project=$PROJECT_ID --location=$REGION"
