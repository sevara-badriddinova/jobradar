#!/usr/bin/env bash
# Run from the project root after creating the database secret (see the guide).
set -euo pipefail
cd "$(dirname "$0")/.."
: "${PROJECT_ID:?Set PROJECT_ID to your Google Cloud project ID}"
REGION="${REGION:-us-east1}"
SCHEDULE="${SCHEDULE:-*/30 * * * *}"
DB_SECRET="${DB_SECRET:-jobradar-database-url}"
DB_SECRET_VERSION="${DB_SECRET_VERSION:-1}"
RUNTIME="jobradar-runtime@${PROJECT_ID}.iam.gserviceaccount.com"
INVOKER="jobradar-scheduler@${PROJECT_ID}.iam.gserviceaccount.com"
BUILDER="jobradar-builder@${PROJECT_ID}.iam.gserviceaccount.com"
BUCKET="${PROJECT_ID}-jobradar-builds"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/jobradar/worker:$(date -u +%Y%m%d%H%M%S)"

command -v gcloud >/dev/null || { echo 'Install gcloud or use Google Cloud Shell.' >&2; exit 1; }
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com \
  artifactregistry.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com \
  iam.googleapis.com logging.googleapis.com storage.googleapis.com --project="$PROJECT_ID"
gcloud secrets versions describe "$DB_SECRET_VERSION" --secret="$DB_SECRET" \
  --project="$PROJECT_ID" >/dev/null

for account in jobradar-runtime jobradar-scheduler jobradar-builder; do
  if ! gcloud iam service-accounts describe "${account}@${PROJECT_ID}.iam.gserviceaccount.com" \
      --project="$PROJECT_ID" >/dev/null 2>&1; then
    gcloud iam service-accounts create "$account" --project="$PROJECT_ID"
  fi
done
if ! gcloud artifacts repositories describe jobradar --location="$REGION" \
    --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud artifacts repositories create jobradar --repository-format=docker \
    --location="$REGION" --project="$PROJECT_ID"
fi
if ! gcloud storage buckets describe "gs://${BUCKET}" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET}" --location="$REGION" \
    --uniform-bucket-level-access --project="$PROJECT_ID"
fi
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${BUILDER}" --role=roles/storage.objectViewer --project="$PROJECT_ID"
gcloud artifacts repositories add-iam-policy-binding jobradar --location="$REGION" \
  --member="serviceAccount:${BUILDER}" --role=roles/artifactregistry.writer --project="$PROJECT_ID"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${BUILDER}" --role=roles/logging.logWriter --condition=None >/dev/null
gcloud secrets add-iam-policy-binding "$DB_SECRET" --member="serviceAccount:${RUNTIME}" \
  --role=roles/secretmanager.secretAccessor --project="$PROJECT_ID"

gcloud builds submit . --config=deploy/cloudbuild.yaml --substitutions="_IMAGE=${IMAGE}" \
  --service-account="projects/${PROJECT_ID}/serviceAccounts/${BUILDER}" \
  --gcs-source-staging-dir="gs://${BUCKET}/source" --project="$PROJECT_ID"

# An existing trigger stays paused if setup fails; resume it after fixing the error.
if gcloud scheduler jobs describe jobradar-poll --location="$REGION" \
    --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud scheduler jobs pause jobradar-poll --location="$REGION" --project="$PROJECT_ID"
fi
# Do not migrate or replace the registry while a previous execution is active.
for job in jobradar-poll jobradar-admin; do
  if gcloud run jobs describe "$job" --region="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
    active=$(gcloud run jobs executions list --job="$job" --region="$REGION" \
      --project="$PROJECT_ID" --filter='NOT status.completionTime:*' --format='value(metadata.name)')
    if [[ -n "$active" ]]; then
      echo "Wait for active executions of ${job} to finish, then rerun setup. Scheduler is paused." >&2
      exit 1
    fi
  fi
done
COMMON=(--image="$IMAGE" --region="$REGION" --project="$PROJECT_ID"
  --service-account="$RUNTIME" --tasks=1 --parallelism=1 --max-retries=0
  --task-timeout=20m --cpu=1 --memory=512Mi
  --set-secrets="JOBRADAR_DATABASE_URL=${DB_SECRET}:${DB_SECRET_VERSION}")
gcloud run jobs deploy jobradar-admin "${COMMON[@]}" --args=init-db
gcloud run jobs execute jobradar-admin --wait --region="$REGION" --project="$PROJECT_ID"
gcloud run jobs update jobradar-admin --args=companies,sync,companies.toml \
  --region="$REGION" --project="$PROJECT_ID"
gcloud run jobs execute jobradar-admin --wait --region="$REGION" --project="$PROJECT_ID"

gcloud run jobs deploy jobradar-poll "${COMMON[@]}" --args=cycle
gcloud run jobs execute jobradar-poll --wait --region="$REGION" --project="$PROJECT_ID"
gcloud run jobs add-iam-policy-binding jobradar-poll --member="serviceAccount:${INVOKER}" \
  --role=roles/run.invoker --region="$REGION" --project="$PROJECT_ID"

ACTION=create
if gcloud scheduler jobs describe jobradar-poll --location="$REGION" \
    --project="$PROJECT_ID" >/dev/null 2>&1; then
  ACTION=update
fi
gcloud scheduler jobs "$ACTION" http jobradar-poll --project="$PROJECT_ID" \
  --location="$REGION" --schedule="$SCHEDULE" --time-zone=Etc/UTC \
  --uri="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/jobradar-poll:run" \
  --http-method=POST --headers=Content-Type=application/json --message-body='{}' \
  --oauth-service-account-email="$INVOKER" --oauth-token-scope=https://www.googleapis.com/auth/cloud-platform \
  --max-retry-attempts=0 --attempt-deadline=180s
if [[ "$ACTION" == update ]]; then
  gcloud scheduler jobs resume jobradar-poll --location="$REGION" --project="$PROJECT_ID"
fi
echo "Ready: jobradar-poll runs on ${SCHEDULE} (UTC)."
