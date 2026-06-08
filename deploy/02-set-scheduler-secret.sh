#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# 02-set-scheduler-secret.sh
# Garante que SCHEDULER_SECRET esteja configurado no serviço Cloud Run.
# Esse valor é usado em dois lugares:
#   • POST /api/scheduler/run  (header x-scheduler-secret — marcação automática)
#   • POST /api/session-keepalive (header X-Scheduler-Secret — manter sessão viva)
#
# ATENÇÃO: Se você já tem SCHEDULER_SECRET configurado e usa /api/scheduler/run,
# mantenha o valor existente — substitua SECRET= abaixo pelo valor atual.
# Gerar um novo valor vai exigir atualizar o job do Cloud Scheduler que chama
# /api/scheduler/run também.
#
# Uso: bash deploy/02-set-scheduler-secret.sh
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Edite estas variáveis antes de executar ───────────────────────────────────
PROJECT_ID="SEU_PROJECT_ID"      # ex: meu-projeto-123456
SERVICE="proeisweb-novo"
REGION="southamerica-east1"

# Se já tem um SCHEDULER_SECRET configurado, cole o valor existente aqui.
# Se não tem nenhum, deixe em branco para gerar um automaticamente.
SECRET_EXISTENTE=""
# ─────────────────────────────────────────────────────────────────────────────

if [[ -n "$SECRET_EXISTENTE" ]]; then
  SECRET="$SECRET_EXISTENTE"
  echo "==> Usando SCHEDULER_SECRET fornecido (não gerado automaticamente)."
else
  SECRET="$(openssl rand -hex 24)"
  echo "==> SCHEDULER_SECRET gerado automaticamente."
fi

echo ""
echo "    Projeto : $PROJECT_ID"
echo "    Serviço : $SERVICE"
echo "    Região  : $REGION"
echo ""
echo "==> Configurando SCHEDULER_SECRET no serviço Cloud Run..."

gcloud run services update "$SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --update-env-vars="SCHEDULER_SECRET=$SECRET"

echo ""
echo "══════════════════════════════════════════════════════════════"
echo " IMPORTANTE — guarde este valor; será necessário no passo 03:"
echo ""
echo "   SCHEDULER_SECRET=$SECRET"
echo ""
echo " Use-o também para autenticar /api/scheduler/run (se configurado)."
echo "══════════════════════════════════════════════════════════════"
echo ""
echo "    Para confirmar que está ativo:"
echo "    curl https://<URL_DO_SERVICO>/api/health | jq .scheduler_secret"
echo "    # Deve retornar: true"
