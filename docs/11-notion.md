# Notion application tracking and sync cost

`uv run jobradar applications sync` chooses Notion when configured. Structured
tracker rows need no AI interpretation, so importing around 500 rows makes zero
LLM calls. The first import reads all pages (up to 100 rows per query); subsequent
imports request edited rows. Schema/database metadata adds a small number of
API requests. This is a cost preference, not a claim that your tracker contains
every update in Gmail.

## Setup

Create a Notion internal connection with read-content access and grant it access
to the tracker. Store its token locally in `.env`; do not commit it. The CLI uses
the Notion API directly and does not require a desktop Notion plugin.

```bash
NOTION_TOKEN='your-token'
NOTION_DATA_SOURCE_ID='your-data-source-uuid'
# Alternatively, for a database containing exactly one data source:
# NOTION_DATABASE_ID='database-uuid-or-copied-database-url'
```

Load the file in the shell before running the CLI:

```bash
set -a
source .env
set +a
```

When given a database ID, the importer retrieves its data-source ID. A database
with multiple data sources requires `NOTION_DATA_SOURCE_ID` explicitly; the
importer does not guess. If both IDs are provided, the data-source ID wins.
The client pins `Notion-Version: 2025-09-03` and uses the
[data-source query endpoint](https://developers.notion.com/reference/query-a-data-source).

## Map your columns

The defaults are `Company`, `Role`, and `Status`. Set the actual names in
`profile.toml`; the schema is validated before importing records:

```toml
[notion]
company_property = "Company"
role_property = "Position"
status_property = "Status"
date_property = "Applied on"  # optional; omit if there is no application date

[notion.status_map]
"Applied" = "APPLIED"
"Phone screen" = "INTERVIEW"
"Interview" = "INTERVIEW"
"Rejected" = "REJECTED"
"Offer" = "OFFER"
"To apply" = "SKIP"
"Saved" = "SKIP"
```

The default status mapping includes Applied, Rejected, Interview, Offer,
Not applied, To apply, and Saved. A custom map replaces the default map, so
include every nonblank status used in your tracker. Names are matched ignoring
case and surrounding whitespace. Unknown statuses stop the import with an
actionable error; blank statuses and explicit SKIP statuses are counted as
skipped. A saved job is never assumed to be an application.

Company, role, and status support title, rich text, select, status, or a
string-valued formula. The optional application-date property must be a Notion
date. Relation and rollup properties are not guessed or recursively crawled;
expose a text/formula column for those values. A missing company is an error;
a blank role is retained as an unmatched application.

## Choose the source

```bash
uv run jobradar applications sync                     # auto: prefer Notion, otherwise Gmail
uv run jobradar applications sync --source notion     # zero LLM calls
uv run jobradar applications sync --source notion --full # re-read every tracker row
uv run jobradar applications sync --source gmail --limit 200
uv run jobradar applications sync --source all         # Notion, then up to 200 new emails
uv run jobradar applications list --unmatched
```

Auto mode skips Gmail when Notion is configured, even if Gmail is also configured.
Use `--source all` if you need confirmations, rejections, interviews, or offers
that have not reached the tracker. All mode requires both sources to be configured.
An invalid or failing Notion source stops the command; it never silently starts
paid email classification. With no sources configured, auto mode only links
application records already stored locally.

Gmail reads the newest unprocessed messages matching the application-related
search within `--days` (default 180), not simply the first 200 arbitrary inbox
messages. IDs are fetched in bounded batches; already-processed messages do not
consume the body-download/classification budget. Repeated runs backfill older
messages within that date window. To cover older history, increase `--days`.

## Idempotency and failure behavior

- A Notion page ID and Gmail message ID identify records within their source.
  Reimporting identical data does not insert duplicates or update unchanged rows.
  The same role can have separate Notion/Gmail evidence; its job status converges.
- One Notion import holds a per-source transaction lock. Its rows and checkpoint
  commit together after all pages parse successfully. A failed page/row rolls
  everything back, so retrying cannot skip unfinished work.
- The checkpoint uses the greatest observed edit timestamp with a one-minute
  inclusive overlap. Timestamp ties are safe because unchanged upserts are
  no-ops. Mapping changes trigger a full backfill; `--full` provides manual
  reconciliation for delayed API visibility or troubleshooting.
- Changed statuses update the source record. Existing REJECTED job states are
  preserved over later APPLIED events. Repeated linking does not duplicate
  feedback. Deleting/archiving a tracker row or moving it to SKIP does not erase
  past application history or automatically make a job eligible again.
- Gmail commits each successful classification with its application record.
  Concurrent runs lock and recheck each message before calling the model.
  Classification failures remain retryable; three consecutive failures stop
  the run. Completed earlier messages remain committed.
- A process crash between the AI response and database commit can still cause
  another paid call on retry. The database effects are idempotent; exactly-once
  external billing cannot be guaranteed.

All Notion operations are reads; the importer never edits the tracker. No new
database migration or dependency is required: applications and checkpoints use
the existing PostgreSQL tables, and HTTP uses the existing `httpx` dependency.

## Verification

The 2026-10-05 suite passes 597 tests, including real PostgreSQL coverage for
repeated and concurrent Gmail/Notion syncs, a 500-row import, failed-page rollback,
checkpoint recovery, and repeated combined-source linking. HTTP/IMAP and AI are
faked in these tests. Live tracker access and its actual column/status mapping
must still be verified after local configuration.
