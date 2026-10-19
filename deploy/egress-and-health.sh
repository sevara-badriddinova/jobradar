#!/usr/bin/env bash
# Deploy the query fixes and independent Telegram watchdog. No DB migration.
# Default is a reviewable plan; --apply performs the deployment.
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT_ID="${PROJECT_ID:-jobradar-510801}"
REGION="${REGION:-us-east1}"
POLL_JOB="${POLL_JOB:-jobradar-poll}"
HEALTH_JOB="${HEALTH_JOB:-jobradar-health}"
HEALTH_BUCKET="${HEALTH_BUCKET:-${PROJECT_ID}-jobradar-health}"
RUNTIME="jobradar-runtime@${PROJECT_ID}.iam.gserviceaccount.com"
INVOKER="jobradar-scheduler@${PROJECT_ID}.iam.gserviceaccount.com"
BUILDER="jobradar-builder@${PROJECT_ID}.iam.gserviceaccount.com"
IMAGE="${IMAGE:-${REGION}-docker.pkg.dev/${PROJECT_ID}/jobradar/worker:egress-20261014}"

cat <<PLAN
Project: ${PROJECT_ID}; region: ${REGION}
Build: ${IMAGE}, using the existing builder and source bucket.
Update ${POLL_JOB}: image and health heartbeat environment only.
Keep its schedule, batch sizes, arguments, secrets, and runtime settings.
Create private bucket ${HEALTH_BUCKET} for two small health-state JSON files.
Grant the existing runtime identity object access on that bucket and read-only
execution-list access on ${POLL_JOB}. No new credentials or database access.
Create ${HEALTH_JOB}: same image, python -m jobradar.health, Telegram secrets only.
Schedule the health check every five minutes; one task, no automatic retries.
Send a Telegram outage message for the current failure, once, then on recovery.
No Neon plan change, data deletion, or schema migration.
PLAN
if [[ "${1:-}" != --apply ]]; then
  echo 'Review this plan, then run with --apply to deploy.'
  exit 0
fi
command -v gcloud >/dev/null
GC=(gcloud --project="$PROJECT_ID" --quiet)
# Refuse to deploy onto a different runtime identity without review.
actual_runtime=$("${GC[@]}" run jobs describe "$POLL_JOB" --region="$REGION" \
  --format='value(spec.template.spec.template.spec.serviceAccountName)')
[[ "$actual_runtime" == "$RUNTIME" ]] || { echo 'Unexpected runtime identity; stopping.' >&2; exit 1; }

"${GC[@]}" builds submit . --config=deploy/cloudbuild.yaml --substitutions="_IMAGE=${IMAGE}" \
  --service-account="projects/${PROJECT_ID}/serviceAccounts/${BUILDER}" \
  --gcs-source-staging-dir="gs://${PROJECT_ID}-jobradar-builds/source"

if ! "${GC[@]}" storage buckets describe "gs://${HEALTH_BUCKET}" >/dev/null 2>&1; then
  "${GC[@]}" storage buckets create "gs://${HEALTH_BUCKET}" --location="$REGION" \
    --uniform-bucket-level-access --public-access-prevention
fi
"${GC[@]}" storage buckets add-iam-policy-binding "gs://${HEALTH_BUCKET}" \
  --member="serviceAccount:${RUNTIME}" --role=roles/storage.objectUser

if ! "${GC[@]}" iam roles describe jobradarHealthReader >/dev/null 2>&1; then
  "${GC[@]}" iam roles create jobradarHealthReader --title='JobRadar execution status reader' \
    --permissions=run.executions.list --stage=GA
fi
"${GC[@]}" run jobs add-iam-policy-binding "$POLL_JOB" --region="$REGION" \
  --member="serviceAccount:${RUNTIME}" --role="projects/${PROJECT_ID}/roles/jobradarHealthReader"

HEALTH_ENV="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},JOBRADAR_REGION=${REGION},JOBRADAR_POLL_JOB=${POLL_JOB},JOBRADAR_HEALTH_BUCKET=${HEALTH_BUCKET}"
"${GC[@]}" run jobs update "$POLL_JOB" --region="$REGION" --image="$IMAGE" --update-env-vars="$HEALTH_ENV"
"${GC[@]}" run jobs deploy "$HEALTH_JOB" --region="$REGION" --image="$IMAGE" \
  --service-account="$RUNTIME" --command=python --args=-m,jobradar.health \
  --tasks=1 --parallelism=1 --max-retries=0 --task-timeout=120s --cpu=1 --memory=512Mi \
  --set-env-vars="$HEALTH_ENV" \
  --set-secrets='TELEGRAM_BOT_TOKEN=jobradar-telegram-token:1,TELEGRAM_CHAT_ID=jobradar-telegram-chat-id:1'
"${GC[@]}" run jobs add-iam-policy-binding "$HEALTH_JOB" --region="$REGION" \
  --member="serviceAccount:${INVOKER}" --role=roles/run.invoker
ACTION=create
if "${GC[@]}" scheduler jobs describe "$HEALTH_JOB" --location="$REGION" >/dev/null 2>&1; then
  ACTION=update
fi
"${GC[@]}" scheduler jobs "$ACTION" http "$HEALTH_JOB" --location="$REGION" \
  --schedule='*/5 * * * *' --time-zone=Etc/UTC \
  --uri="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${HEALTH_JOB}:run" \
  --http-method=POST --headers=Content-Type=application/json --message-body='{}' \
  --oauth-service-account-email="$INVOKER" --oauth-token-scope=https://www.googleapis.com/auth/cloud-platform \
  --max-retry-attempts=0 --attempt-deadline=180s
if [[ $("${GC[@]}" scheduler jobs describe "$HEALTH_JOB" --location="$REGION" --format='value(state)') == PAUSED ]]; then
  "${GC[@]}" scheduler jobs resume "$HEALTH_JOB" --location="$REGION"
fi
# This verifies API permissions, external state, and actual Telegram delivery,
# even while the poller remains blocked by Neon's monthly quota.
"${GC[@]}" run jobs execute "$HEALTH_JOB" --region="$REGION" --wait
echo 'Deployment finished. Verify the health execution logs and Telegram message.'
