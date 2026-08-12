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

echo "==> Deploying $SERVICE to Cloud Run"
gcloud run deploy "$SERVICE" --source . --region "$REGION" --project "$PROJECT_ID" \
    --allow-unauthenticated --min-instances 0 --max-instances 2 \
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
