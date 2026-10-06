# Google Cloud Run Jobs + Cloud Scheduler + Neon

Cloud Scheduler starts a short-lived Cloud Run Job. The container runs
`jobradar cycle`, saves state to Neon PostgreSQL, and exits. There is no web
server and no process that must stay running on your laptop.

The default cycle is **collect → filter**, every 30 minutes. Matching,
application imports, and Telegram are explicit opt-ins below. The default needs
only a database credential. No AI calls or messages are made by this setup.

## 1. Create the accounts and database

Create/select a Google Cloud project with billing enabled. Use its **project ID**,
not its display name or project number. You need permission to enable APIs,
create service accounts/resources, grant their IAM roles, and act as the runtime,
build, and Scheduler service accounts. Project Owner can perform the initial
setup; the deployed accounts receive narrower permissions from the script.

In the [Neon console](https://console.neon.tech), create a project and a database
named `jobradar`. Choose a region close to your Cloud Run region. `us-east1` is
the script's editable default; choose your actual region before deploying.
In **Connect**, disable **Connection pooling**, select your database/role, and
copy the connection string. Keep `sslmode=require` and any
`channel_binding=require` parameter Neon supplies. For example:

```text
postgresql://ROLE:PASSWORD@ep-YOUR-ENDPOINT.REGION.aws.neon.tech/jobradar?sslmode=require&channel_binding=require
```

The hostname must not contain `-pooler`. Our overlap guard uses a session-level
PostgreSQL advisory lock; transaction pooling cannot preserve that session.
JobRadar accepts Neon's `postgresql://` URL and selects psycopg 3 automatically.
Use the generated URL so special characters in credentials are correctly encoded.

This creates a fresh database. Existing local jobs, application history,
notification history, and extracted profiles are **not copied** by `init-db`.
If you want to preserve local history, pause local workers and migrate the entire
database to Neon with `pg_dump`/`pg_restore` before the first collection. A fresh
database treats existing board listings as newly discovered.

## 2. Authenticate and store the database secret

Use [Google Cloud Shell](https://shell.cloud.google.com) and upload this project
folder, or install the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install)
locally and run `gcloud auth login`. Cloud Shell already includes gcloud and Python.
Run the following in a **Bash shell**, from the JobRadar project root:

```bash
export PROJECT_ID='YOUR_PROJECT_ID'
export REGION='us-east1'
gcloud config set project "$PROJECT_ID"
gcloud services enable secretmanager.googleapis.com --project="$PROJECT_ID"
python3 deploy/store-secret.py "$PROJECT_ID" jobradar-database-url
```

Paste the Neon direct connection string at the hidden prompt. It is sent to
Secret Manager through stdin and never written to the repo or command history.
The setup defaults to secret version `1`. If adding a new version to an existing
secret, set `DB_SECRET_VERSION` to the version the command reports.

Do not upload `.env` or a resume. `.gcloudignore` and `.dockerignore` allow only
the application source and explicit build inputs. `profile.toml` and
`companies.toml` **are** included in the private image; review their contents.

## 3. Deploy and verify

Review `companies.toml` and the filters in `profile.toml`, then:

```bash
export SCHEDULE='*/30 * * * *'
bash deploy/setup.sh
```

The script enables the required APIs and creates:

| Resource | Purpose |
|---|---|
| Artifact Registry `jobradar` | Private container images |
| Cloud Storage `PROJECT_ID-jobradar-builds` | Source archives for Cloud Build |
| `jobradar-builder` account | Read build source, write images and build logs |
| `jobradar-runtime` account | Read only the configured database secret |
| `jobradar-scheduler` account | Invoke only the polling job |
| Cloud Run Job `jobradar-admin` | Apply migrations and sync the company registry |
| Cloud Run Job `jobradar-poll` | Run one polling cycle |
| Cloud Scheduler `jobradar-poll` | Start a cycle every 30 minutes, UTC |

It builds a Linux image in Cloud Build, runs migrations and registry sync, and
**waits for a successful first polling run before creating the schedule**.
Configuration: one task, one CPU, 512 MiB, 20-minute timeout, no automatic task
retries. A failed board does not block other boards or filtering, but the cycle
exits nonzero so the failed run is visible. Fix any failed boards shown in the
logs and rerun setup if the first poll fails.

Scheduler calls the Cloud Run Jobs API using **OAuth** with the dedicated
invoker service account. It does not call an HTTP endpoint inside the container.
The job-level database lock skips overlapping cycles. It does not serialize
manual `collect`, `match`, or `notify` commands; avoid running those concurrently
against the production database. Do not run two setup scripts simultaneously.

On a redeployment, setup pauses an existing schedule and refuses to proceed
while old polling/admin executions are active. If a later step fails, the
schedule stays paused. Fix the failure and rerun setup to restore it. Re-running
setup restores the basic `cycle` arguments and database-only secret bindings;
reapply optional integrations below if you previously enabled them.

```bash
gcloud run jobs executions list --job=jobradar-poll --region="$REGION"
gcloud run jobs logs read jobradar-poll --region="$REGION" --limit=100
gcloud scheduler jobs describe jobradar-poll --location="$REGION"
```

Scheduler success means that a run was requested, not that collection succeeded.
Inspect **Cloud Run → Jobs → jobradar-poll → Executions** for completion and logs.
Create a Cloud Monitoring alert for failed executions if you want failure emails.
No failure-notification destination is configured by this script.

## 4. Optional AI matching and Telegram

First extract your resume profile **into the Neon database**, from your laptop,
using the existing `jobradar profile extract /path/to/resume.pdf` command.
Set `JOBRADAR_DATABASE_URL` to the same Neon URL and `ANTHROPIC_API_KEY` locally.
The cloud image does not need your resume after extraction; the profile lives
in PostgreSQL. An existing profile in local Docker PostgreSQL is not in Neon.

Store the optional secrets with the same hidden prompt:

```bash
python3 deploy/store-secret.py "$PROJECT_ID" jobradar-anthropic-key
python3 deploy/store-secret.py "$PROJECT_ID" jobradar-telegram-token
python3 deploy/store-secret.py "$PROJECT_ID" jobradar-telegram-chat-id

for secret in jobradar-anthropic-key jobradar-telegram-token jobradar-telegram-chat-id; do
  gcloud secrets add-iam-policy-binding "$secret" \
    --member="serviceAccount:jobradar-runtime@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role=roles/secretmanager.secretAccessor --project="$PROJECT_ID"
done

gcloud run jobs update jobradar-poll --region="$REGION" --project="$PROJECT_ID" \
  --update-secrets='ANTHROPIC_API_KEY=jobradar-anthropic-key:1,TELEGRAM_BOT_TOKEN=jobradar-telegram-token:1,TELEGRAM_CHAT_ID=jobradar-telegram-chat-id:1' \
  --args=cycle,--match,--match-limit,25,--notify
```

Use the actual secret version numbers if they differ from `1`. This allows up to
25 AI assessments per execution. It is a per-cycle budget, not a daily budget.
Telegram buttons are processed on each notification cycle; they are not instant.
Do not run `telegram listen` inside a scheduled Job: it never exits. Existing
notification/state correctness limitations remain documented in the roadmap.

If you use a Notion tracker, store/bind `NOTION_TOKEN` the same way, set
`NOTION_DATA_SOURCE_ID` with `--update-env-vars`, then use:

```bash
gcloud run jobs update jobradar-poll --region="$REGION" --project="$PROJECT_ID" \
  --args=cycle,--applications,notion,--match,--match-limit,25,--notify
```

See [Notion setup](11-notion.md) for property mapping. Gmail also works with
`--applications,gmail`, its documented environment variables, and
`--inbox-limit,200`. Application-sync failures stop downstream matching and
notifications so stale application data does not trigger alerts.

`--digest` means “send a digest on this execution”; it does not implement a
daily digest schedule. Do not add it to every 30-minute run expecting daily mail.

## Operations

Change cadence:

```bash
gcloud scheduler jobs update http jobradar-poll --location="$REGION" --schedule='0 * * * *'
```

Pause/resume new executions:

```bash
gcloud scheduler jobs pause jobradar-poll --location="$REGION"
gcloud scheduler jobs resume jobradar-poll --location="$REGION"
```

Pause does not cancel an already-running execution. To update source, company
boards, or profile settings, edit the files and rerun `bash deploy/setup.sh`.
Registry sync makes Neon match `companies.toml`, disabling removed companies.
Changing filter settings does not automatically recheck old jobs: run
`jobradar filter --recheck` against Neon during a quiet maintenance window.

Cloud Build, stored images/archives, Scheduler, Cloud Run executions, Neon, and
optional AI usage may incur charges. Set a Google Cloud budget notification and
review Neon usage. Budgets notify; they do not automatically stop spending.
Add image/archive retention policies as builds accumulate.

If IAM was just created and a deployment reports permission denied, allow time
for propagation and rerun. For Scheduler 403s, verify the job's `roles/run.invoker`
binding and that the Google-managed Cloud Scheduler service agent still has
`roles/cloudscheduler.serviceAgent`. Do not grant that service-agent role to the
custom invoker account.

References: [Google scheduling guide](https://docs.cloud.google.com/run/docs/execute/jobs-on-schedule),
[Cloud Run job deployment flags](https://docs.cloud.google.com/sdk/gcloud/reference/run/jobs/deploy),
[custom Cloud Build accounts](https://docs.cloud.google.com/build/docs/securing-builds/configure-user-specified-service-accounts),
[Neon connection pooling](https://neon.com/docs/connect/connection-pooling).
