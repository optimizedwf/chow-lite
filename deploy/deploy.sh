#!/usr/bin/env bash
# Deploy nine to Cloud Run + Firestore (Google Cloud).
# Prereqs: gcloud CLI + `gcloud auth login`, billing-enabled project.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID (e.g. nine-2026)}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-nine}"

echo "==> Enabling APIs"
gcloud services enable run.googleapis.com firestore.googleapis.com     --project "$PROJECT_ID"

echo "==> Creating Firestore database (native mode) if missing"
if ! gcloud firestore databases list --project "$PROJECT_ID" 2>/dev/null | grep -q "(default)"; then
  gcloud firestore databases create --region "$REGION" --project "$PROJECT_ID"
fi

# Optional live-model + API-key wiring. If GEMINI_API_KEY secret exists in
# Secret Manager, Cloud Run gets it (so the deployed API runs LIVE Gemini
# end to end). Without the key the API still routes (keyword substrate) but
# every output-producing workflow fails loud — nine never fabricates answers.
#   Create the secret first:
#     echo -n "$GEMINI_API_KEY" | gcloud secrets create GEMINI_API_KEY \
#         --data-file=- --project "$PROJECT_ID"
SECRETS=""
if gcloud secrets describe GEMINI_API_KEY --project "$PROJECT_ID" >/dev/null 2>&1; then
  SECRETS="--set-secrets GEMINI_API_KEY=GEMINI_API_KEY:latest"
  echo "==> GEMINI_API_KEY found in Secret Manager; live model routing enabled"
else
  echo "==> no GEMINI_API_KEY secret -> routing via keyword substrate; output workflows will fail loud (model-or-fail doctrine)"
fi

# torture-16 F5: the API MUST NOT ship publicly unauthenticated. Every
# request can burn the operator's paid Gemini quota and read job records,
# so a public (--allow-unauthenticated) deployment REQUIRES the NINE_API_KEY
# secret. Provide it by setting NINE_API_KEY locally (created + wired
# automatically) or pre-creating the secret:
#   echo -n "$NINE_API_KEY" | gcloud secrets create NINE_API_KEY \
#       --data-file=- --project "$PROJECT_ID"
AUTH_FLAG="--allow-unauthenticated"
if gcloud secrets describe NINE_API_KEY --project "$PROJECT_ID" >/dev/null 2>&1; then
  SECRETS="$SECRETS --set-secrets NINE_API_KEY=NINE_API_KEY:latest"
  echo "==> NINE_API_KEY secret found; X-API-Key required on every endpoint"
elif [ -n "${NINE_API_KEY:-}" ]; then
  if ! gcloud secrets describe NINE_API_KEY --project "$PROJECT_ID" >/dev/null 2>&1; then
    echo -n "$NINE_API_KEY" | gcloud secrets create NINE_API_KEY \
        --data-file=- --project "$PROJECT_ID"
  else
    echo -n "$NINE_API_KEY" | gcloud secrets versions add NINE_API_KEY \
        --data-file=- --project "$PROJECT_ID" >/dev/null
  fi
  SECRETS="$SECRETS --set-secrets NINE_API_KEY=NINE_API_KEY:latest"
  echo "==> NINE_API_KEY created from environment; X-API-Key required on every endpoint"
else
  echo "!! WARNING: no NINE_API_KEY configured — refusing to deploy a PUBLIC unauthenticated API."
  echo "!! Set NINE_API_KEY (local env) or create the NINE_API_KEY secret, then re-run."
  echo "!! (Private deploy: gcloud run deploy --no-allow-unauthenticated + IAP/SA auth.)"
  AUTH_FLAG="--no-allow-unauthenticated"
fi

echo "==> Deploying $SERVICE to Cloud Run"
gcloud run deploy "$SERVICE" --source . --region "$REGION" --project "$PROJECT_ID" \
    $AUTH_FLAG --min-instances 0 --max-instances 2 \
    --set-env-vars GEMINI_MODEL=gemini-3.6-flash,FIRESTORE_COLLECTION=nine-jobs,NINE_MEMORY=firestore \
    $SECRETS

echo "==> Applying Firestore rules"
gcloud firestore security-rules update deploy/firestore.rules --project "$PROJECT_ID" 2>/dev/null || true

URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --project "$PROJECT_ID" \
      --format 'value(status.url)')
echo "==> LIVE: $URL"
echo "==> Smoke test: curl $URL/health"
curl -s "$URL/health"
echo
