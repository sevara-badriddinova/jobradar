# Cloud deployment — October 13, 2026

This records the deployed personal configuration; `12-cloud-run.md` describes
the minimal setup defaults. Running `deploy/setup.sh` restores its database-only
defaults, so reapply the integrations and cycle arguments below afterward.

## Release

- Google Cloud project: `jobradar-510801`, region: `us-east1`.
- Image: `us-east1-docker.pkg.dev/jobradar-510801/jobradar/worker:repair-20261013`.
- Successful build: `fc2e4283-07c0-4805-9ae9-39070cf51332`.
- Database migration: `0008`, tested on an isolated Neon branch before production.
- Production profile: 1.58 years of full-time experience; 1,375 application
  records and 35 historical notification records migrated from the local worker.
- Runtime service account: `jobradar-runtime@jobradar-510801.iam.gserviceaccount.com`.

## Required runtime settings

```text
cycle --discover --collect-limit 50 --match --match-limit 25 --notify --digest
```

The polling job uses one task, one CPU, 1 GiB memory, a 3,600-second timeout,
and no automatic task retries. The existing five-minute scheduler invokes it;
the database advisory lock prevents overlapping cycles. Each cycle rotates
through 50 least-recently attempted boards (increased from 25 on October 14).
Directory discovery refreshes daily. The original verification below used 25.

Secret bindings are `JOBRADAR_DATABASE_URL=jobradar-database-url:3`,
`ANTHROPIC_API_KEY=jobradar-anthropic-key:1`,
`TELEGRAM_BOT_TOKEN=jobradar-telegram-token:1`, and
`TELEGRAM_CHAT_ID=jobradar-telegram-chat-id:1`. Secret values are never included
in the image or this document. Matching uses `claude-haiku-4-5-20251001`.

The image's `profile.toml` requires a known publication date within 72 hours,
explicit sponsorship or STEM OPT support in the specific posting, and the
existing US/software engineering/experience requirements. Missing or future
dates fail the age gate. Export-license sponsorship does not count as immigration
support. Immediate alerts require score 80; this cycle also delivers eligible
digest matches scoring at least 70.

## Verification

Cloud verification execution `jobradar-admin-vj95n` completed with exit 0:

- Anthropic structured-response request succeeded with the configured model.
- Telegram authenticated the configured bot and chat; historical message
  buttons were relinked to migrated job IDs.
- All eight collectors fetched live public listings successfully: Greenhouse
  148, Ashby 31, Lever 84, Workday 184, Workable 1, Recruitee 1, Personio 1,
  and Rippling 4.
- Directory discovery registered 12,237 boards, with no unsupported platforms.
- Filter, match, and notify commands each returned exit 0. No jobs in that
  verification batch met all eligibility rules, so no new job alerts were sent.

Live checks cover one board per platform, not every board. The full registry
rotation still takes time; five-minute invocation does not mean every board
is scanned every five minutes. Long runs skip overlapping invocations.

The first production cycle, `jobradar-poll-tscbq`, completed successfully at
15:33:18 UTC in 2 minutes 57 seconds. All 25 boards succeeded; discovery,
collection, filtering, matching, and notification stages returned exit 0.
The production database then contained 6,729 jobs. The filter examined 1,071
jobs: 950 were older than three days, two lacked publication dates, five lacked
explicit sponsorship, and 114 failed title requirements. No jobs qualified,
and no new Telegram messages were sent or failed. The scheduler was confirmed
`ENABLED` with `*/5 * * * *` in UTC.

Local `com.jobradar.poll` and `com.jobradar.telegram-listener` LaunchAgents are
stopped and disabled so cloud is the only Telegram update consumer. Telegram
buttons are handled during notification cycles, rather than by a continuously
running local listener. The isolated migration-test endpoint is suspended.

## Credential audit — October 13, 2026

The retained Cloud Build source archive contains 57 files. Scanning it against
the deployed Secret Manager values and database password found no matches;
it contains no `.env`, private credentials file, or Git directory.

Image digest `sha256:fa254d108ad13a49a68874b2fec7b397f4ae317efd5a5e68abf422602cd5702d`
matches the release tag and original build. Cloud Build audit
`de4b78d3-9eb4-4980-9891-11194024bea0` scanned all 12 layers and metadata
(38 archive members, 315,947,087 bytes), including lower-layer contents.
No Anthropic API-key, Telegram-token, or Neon-password patterns were found.
The private-key signature in `libgnutls.so.30.34.3` was reviewed: all 10 PEM
blocks exactly matched publicly distributed
[GnuTLS self-test fixtures](https://github.com/gnutls/gnutls/blob/master/lib/crypto-selftests-pk.c).
After that verified classification, the scan reported no credential findings.
The image was audited without injecting application secrets into the build.

After fetching origin branches and tags, scanning 144 nonignored working files
and all 254 stored Git objects found no production credentials. PostgreSQL URL
matches were development defaults or documentation/test examples. The ignored
local `.env` still contains credentials and has owner-only mode `0600`.

Cloud Run's database, Anthropic, Telegram token, and Telegram chat-ID settings
are Secret Manager references, not plaintext configuration values. The runtime
service account has `secretAccessor` on each of these four secrets, no
project-level role bindings, and no user-managed service-account keys.
The database connection requires TLS and channel binding. The admin job binds
only the database secret.

**Historical exception:** the temporary upload archive sent to Cloud Shell
included `private/credentials.json` to initialize the three integration secrets.
It was outside the separate Docker build directory. Both the temporary archive
and extracted credentials file were deleted from local staging and Cloud Shell
after deployment. This audit cannot claim credentials were never packaged or
that all credential copies exist exclusively in Secret Manager. Production
credentials were not intentionally printed in commands or audit results.
