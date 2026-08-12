#!/usr/bin/env bash
# Deploy chow-lite to Cloud Run + Firestore (Google Cloud).
# Prereqs: gcloud CLI, billing-enabled project, GOOGLE_APPLICATION_CREDENTIALS set.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID (e.g. chow-lite-2026)}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-chow-lite}"

echo "==> Enabling APIs"
gcloud services enable run.googleapis.com firestore.googleapis.com     --project "$PROJECT_ID"

echo "==> Creating Firestore database (native mode) if missing"
if ! gcloud firestore databases list --project "$PROJECT_ID" 2>/dev/null | grep -q "(default)"; then
  gcloud firestore databases create --region "$REGION" --project "$PROJECT_ID"
fi

echo "==> Deploying $SERVICE to Cloud Run"
gcloud run deploy "$SERVICE" --source . --region "$REGION" --project "$PROJECT_ID" \
    --allow-unauthenticated --min-instances 0 --max-instances 2 \
    --set-env-vars GEMINI_MODEL=gemini-3.5-flash,FIRESTORE_COLLECTION=chowlite-jobs

echo "==> Applying Firestore rules"
gcloud firestore security-rules update deploy/firestore.rules --project "$PROJECT_ID" 2>/dev/null || true

URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --project "$PROJECT_ID" \
      --format 'value(status.url)')
echo "==> LIVE: $URL"
echo "==> Smoke test: curl $URL/health"
curl -s "$URL/health"
echo
