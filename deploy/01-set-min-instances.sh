#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# 01-set-min-instances.sh
# Elimina o cold start do Cloud Run mantendo sempre 1 instância ativa.
#
# Uso: bash deploy/01-set-min-instances.sh
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Edite estas variáveis antes de executar ───────────────────────────────────
PROJECT_ID="SEU_PROJECT_ID"      # ex: meu-projeto-123456
SERVICE="proeisweb-novo"
REGION="southamerica-east1"
# ─────────────────────────────────────────────────────────────────────────────

echo "==> Configurando min-instances=1 para o serviço '$SERVICE'..."
echo "    Projeto : $PROJECT_ID"
echo "    Região  : $REGION"
echo ""

gcloud run services update "$SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --min-instances=1

echo ""
echo "==> Feito. A partir da próxima revisão, o Cloud Run manterá"
echo "    1 instância sempre ativa — sem cold start."
echo ""
echo "    Opcional: adicione --no-cpu-throttling para CPU sempre alocada"
echo "    (resposta ainda mais rápida, custo um pouco maior):"
echo ""
echo "    gcloud run services update $SERVICE \\"
echo "      --project=$PROJECT_ID --region=$REGION \\"
echo "      --min-instances=1 --no-cpu-throttling"
echo ""
echo "    Para reverter (volta ao comportamento padrão serverless):"
echo "    gcloud run services update $SERVICE \\"
echo "      --project=$PROJECT_ID --region=$REGION --min-instances=0"
