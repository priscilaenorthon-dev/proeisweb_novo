#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# setup-tudo.sh — configura min-instances + keepalive em um único comando
# Uso: bash deploy/setup-tudo.sh
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PROJECT_ID="gen-lang-client-0105958041"
SERVICE="proeisweb-novo"
REGION="southamerica-east1"
SERVICE_URL="https://proeisweb-novo-1082055415046.southamerica-east1.run.app"
JOB_NAME="proeis-keepalive"

echo ""
echo "══════════════════════════════════════════════════════════"
echo " PROEIS — Setup de infraestrutura"
echo " Projeto : $PROJECT_ID"
echo " Serviço : $SERVICE ($REGION)"
echo "══════════════════════════════════════════════════════════"
echo ""

# ── Passo 1: min-instances=1 ──────────────────────────────────────────────────
echo "[1/3] Configurando min-instances=1 (elimina cold start)..."
gcloud run services update "$SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --min-instances=1 \
  --quiet
echo "      OK"
echo ""

# ── Passo 2: SCHEDULER_SECRET ────────────────────────────────────────────────
echo "[2/3] Verificando SCHEDULER_SECRET..."

EXISTING_SECRET=$(
  gcloud run services describe "$SERVICE" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format="value(spec.template.spec.containers[0].env[name=SCHEDULER_SECRET].value)" \
    2>/dev/null || true
)

if [[ -n "$EXISTING_SECRET" ]]; then
  SECRET="$EXISTING_SECRET"
  echo "      Reaproveitando secret já configurado."
else
  SECRET="$(openssl rand -hex 24)"
  echo "      Nenhum secret encontrado — gerando novo..."
  gcloud run services update "$SERVICE" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --update-env-vars="SCHEDULER_SECRET=$SECRET" \
    --quiet
  echo "      SCHEDULER_SECRET configurado no Cloud Run."
fi
echo ""

# ── Passo 3: Cloud Scheduler keepalive ───────────────────────────────────────
echo "[3/3] Configurando job keepalive (a cada 10 minutos)..."

KEEPALIVE_URL="${SERVICE_URL}/api/session-keepalive"

if gcloud scheduler jobs describe "$JOB_NAME" \
    --project="$PROJECT_ID" --location="$REGION" &>/dev/null; then
  echo "      Job já existe — atualizando..."
  gcloud scheduler jobs update http "$JOB_NAME" \
    --project="$PROJECT_ID" \
    --location="$REGION" \
    --schedule="*/10 * * * *" \
    --time-zone="America/Sao_Paulo" \
    --uri="$KEEPALIVE_URL" \
    --http-method=POST \
    --headers="X-Scheduler-Secret=$SECRET" \
    --attempt-deadline=120s \
    --quiet
else
  echo "      Criando novo job..."
  gcloud scheduler jobs create http "$JOB_NAME" \
    --project="$PROJECT_ID" \
    --location="$REGION" \
    --schedule="*/10 * * * *" \
    --time-zone="America/Sao_Paulo" \
    --uri="$KEEPALIVE_URL" \
    --http-method=POST \
    --headers="X-Scheduler-Secret=$SECRET" \
    --attempt-deadline=120s \
    --quiet
fi
echo "      OK"
echo ""

# ── Teste imediato ────────────────────────────────────────────────────────────
echo "══════════════════════════════════════════════════════════"
echo " Configuração concluída! Testando keepalive agora..."
echo "══════════════════════════════════════════════════════════"
echo ""

gcloud scheduler jobs run "$JOB_NAME" \
  --project="$PROJECT_ID" --location="$REGION"

sleep 5

echo ""
echo "Resultado do /api/session-status:"
curl -s "${SERVICE_URL}/api/session-status" | python3 -m json.tool 2>/dev/null \
  || curl -s "${SERVICE_URL}/api/session-status"
echo ""
echo ""
echo "Tudo pronto. A partir de agora:"
echo "  • Sem cold start (instância sempre ativa)"
echo "  • Sessão renovada automaticamente a cada 10 min"
