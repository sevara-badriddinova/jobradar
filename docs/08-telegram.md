# Slice 8 (plan step 9): Telegram notifications

**Goal:** good matches reach your phone with **at-most-once automatic delivery**, with buttons to
say what you did about them.

```
jobs (MATCHED) ──enqueue──► notifications (pending) ──claim──► sending ──send──► sent ──► jobs NOTIFIED
                                                                  │
                                         unknown outcome ─────────┘ stays "sending": reported, never auto-resent

Telegram button press ──getUpdates──► feedback (APPLIED / IGNORED) + jobs APPLIED  (+ offset saved)
```

## Setting it up (one time, ~2 minutes)

1. In Telegram, message **@BotFather**, send `/newbot` and pick a name. It
   gives you a token.
2. `export TELEGRAM_BOT_TOKEN=<token>`
3. Send any message to your new bot, then run `uv run jobradar telegram setup`.
   It prints your chat id.
4. `export TELEGRAM_CHAT_ID=<chat id>`, then
   `uv run jobradar telegram setup --test` sends a test message.
5. Run `uv run jobradar telegram setup-menu` to add job lists to Telegram's Menu button.

Tip: put both `export` lines in a `.env` file (already in `.gitignore`) and run
`set -a; source .env; set +a` in each new terminal. The token is a password
for your bot, so it never goes in `profile.toml` or git.

## Using it

```bash
uv run jobradar notify            # apply button presses, queue new matches, send alerts (score ≥ 80)
uv run jobradar notify --digest   # ...and also send one digest message for 70–79
uv run jobradar telegram listen   # keep running in another terminal for live button handling
```

`notify` sends alerts and then exits; it does not keep the bot listening.
If Applied or Not interested keeps loading, start `telegram listen` and leave
it running. It processes pending clicks and saves new ones as they arrive.
If credentials are in `.env`, load them first with `set -a; source .env; set +a`.

On macOS, install a background listener once so closing the terminal does not
stop the buttons:

```bash
.venv/bin/python deploy/install-telegram-listener.py
```

The login service reads this checkout's `.env`, starts at login, and restarts
after an exit. Logs are in `.local/telegram-listener*.log`. It runs while your
Mac is awake and you are logged in; an always-on host is needed for 24/7 handling.
To stop and uninstall it, run `launchctl bootout gui/$(id -u)/com.jobradar.telegram-listener`
and remove `~/Library/LaunchAgents/com.jobradar.telegram-listener.plist`.

Database configuration accepts `JOBRADAR_DATABASE_URL` first, then
`DATABASE_URL_UNPOOLED`, then `DATABASE_URL`. The Neon direct URL is preferred
for CLI workers and migrations. Temporary database disconnects are retried
without losing the saved update offset.

To also check job boards every 5 minutes and send eligible alerts, install the
polling service:

```bash
.venv/bin/python deploy/install-telegram-listener.py --with-poller
```

It runs `collect → filter → match → notify --digest`, scoring at most 25 jobs
per run when `ANTHROPIC_API_KEY` (or `ANTHROPIC_AUTH_TOKEN`) is present. Without
the credential it still collects and filters jobs and sends previously scored
matches, but new jobs cannot receive scores or alerts. `.env` is reloaded each
run, so adding the credential does not require reinstalling. Polling logs are
in `.local/poll*.log`. Stop it with
`launchctl bootout gui/$(id -u)/com.jobradar.poll` and remove
`~/Library/LaunchAgents/com.jobradar.poll.plist` to uninstall it.

The button listener runs independently of collection and AI scoring. It uses
short long-polls and bounded acknowledgment requests to recover promptly after
network interruptions. Rapid clicks are saved and acknowledged before editing
cards or delivering queue summaries.

The current profile uses `delivery = "queue"` under `[notifications]`. All matches
at or above `digest_min_score` appear in one persistent Telegram message. New
matches update this message without sending another alert. `delivery = "alerts"`
retains separate immediate/digest notifications, with the same interactive queue.

Open Telegram's **Menu** button or send `/menu` to choose a list. The queue's
**🏠 Main menu** button opens the same choices within the existing message:

- **🆕 New** (`/new`): all unreviewed, open matches, highest score first.
- **❤️ Liked** (`/liked`): jobs you saved for later (existing Add to queue choices are preserved).
- **✅ Applied** (`/applied`): recorded applications, including rejected applications.
- **⏭️ Skipped** (`/skipped`): Not interested and Sponsorship required choices.

**Show queue**, **View all jobs**, and `/queue` still open New directly.

Each page shows five jobs. Tap a job's company/title button to open its details,
reasons, and concerns in the same message. **Like**, **Not interested**, **Mark
applied**, and **Sponsorship required** save your choice immediately. **Open
application** opens the posting; it does not claim you submitted an application.
Use **⬅️ Back to jobs** to return to the list, or **🏠 Main menu** to change lists.
Job lists and details show relative posting ages. Where Greenhouse supplies only
an update timestamp, the label is **Updated**; without a source date, it is
**First seen**, based on when JobRadar discovered the job. Job IDs and list
numbering are hidden; internal identifiers still connect buttons to jobs.
The current profile sets `[filters].max_posting_age_days = 3`: New and Liked
require an actual publication date in the last 72 hours. Missing and future dates
are excluded, and an update or discovery timestamp never substitutes for publication.
The cutoff is checked before AI scoring and delivery, and existing jobs age out
on subsequent scans or queue refreshes. Applied and Skipped remain review history.
Run `jobradar init-db` when upgrading to correct legacy Greenhouse update timestamps,
then `jobradar filter --recheck` to apply the rule to existing jobs.
Pagination, refreshing, and reviewing update the same message. The current
`delivery = "alerts"` setting sends new messages for qualifying matches; optional
`delivery = "queue"` edits the queue without a fresh alert. Each new posting must
explicitly offer visa sponsorship or STEM OPT support.

A skipped job can be restored by liking it. An application fact is preserved:
applied, rejected, or closed jobs cannot be returned to the active shortlist.
Match percentages are stored fit scores, not hiring probabilities. Browsing and
reviewing make no AI, email, or Notion calls.

The message ID and current view survive listener restarts in `app_state`. On
upgrade, the latest existing queue summary is reused. Earlier chat history is
left intact. An edit timeout is retried against the same message; the bot does
not send a replacement just because an edit timed out. If Telegram confirms the
message was deleted, one replacement is created. An uncertain first send is
not repeated automatically; `/queue` explicitly requests recovery.

After upgrading, run `jobradar telegram setup-menu`, restart the listener, and use `jobradar telegram refresh-buttons`
to replace old digest button grids with job-name selectors. Sponsorship remains
a global preference in `profile.toml`; unknown sponsorship is not treated as a
confirmed refusal.

## Decisions and why

**Separate-alert mode: the outbox pattern.** We never "send, then write down that we sent". A
`notifications` row is written first, and `UNIQUE(job_id, channel)` makes
queueing idempotent: re-running `notify` can never queue a job twice. Each row
then moves through `pending → sending → sent`, with the claim (`pending →
sending`) committed *before* the message is sent.

**At-most-once delivery, on purpose.** Telegram has no idempotency key. If the
network drops *after* Telegram accepted a message but *before* we hear back,
we can't know whether it arrived. There are two honest choices:
- **Re-send (at-least-once):** you never miss an alert, but sometimes get two.
- **Don't re-send (at-most-once):** never a duplicate, but sometimes a missed alert.

The plan asks for "retries cannot send the same job multiple times", so this
uses at-most-once:
- **The client retries only when the request certainly wasn't processed:**
  the connection was refused, or Telegram answered 429 "too many requests"
  (it waits Telegram's `retry_after`).
- **Uncertain outcomes stay `sending`:** timeouts after sending and 5xx errors
  are reported by `notify`, never silently resent. The job is still in
  `jobradar top`, and `notify --resend-stuck` re-queues them if you decide to.
- **Definite failures go back to `pending`:** a 400 such as "chat not found" is
  retried on the next run, up to 5 attempts, then marked `failed`.

**A small circuit breaker.** Three definite failures in a row stop the run. A
wrong chat id or a blocked bot then fails fast instead of failing every row.

**Button presses are applied exactly once.** Telegram re-delivers updates until
`getUpdates` is called with a higher `offset`. The offset is saved in
`app_state` *in the same transaction* as the feedback it produced, so a crash
can't apply a press twice or lose one. Answering the button and editing the
message happen afterwards and are cosmetic: if they fail (Telegram rejects
answers to old button presses), the decision is still saved.

A PostgreSQL transaction advisory lock allows only one update consumer at a time, so
`notify` and `telegram listen` cannot race the saved offset. `job_reviews`
stores one current choice per job, while `applications` preserves Applied
facts for future role linking. The queue view is saved with the triggering update in `app_state`. Legacy
`review_responses` rows are retained to locate the existing queue message.
Uncertain first delivery is not automatically retried; `/queue` requests recovery.

**Only your chat can press buttons.** Anyone can find a bot by its name, so a
button press from any other chat id is ignored. Button data is validated
(`applied:<number>`, and the job must exist).

**Feedback is stored for later.** `feedback(job_id, kind, source)` with
`APPLIED`, `IGNORED` (and later `INTERVIEW`, `REJECTED`, `OFFER`). That's the
labelled data a personal ranking model needs, in the plan's later step.

**Untrusted text is escaped.** Titles, company names and AI reasons are
HTML-escaped (messages use Telegram's HTML mode). Only `http(s)` links become
buttons. Long fields are clipped, and digests are split to fit Telegram's
4,096-character limit.

**The token never appears in errors or logs.** It's part of the API URL, so
error messages are built from Telegram's description, never the URL, and
`httpx` request logging stays off.

**Closed before sending → cancelled.** If a job disappears from the company's
board before its alert goes out, the alert is cancelled rather than sending
you to a dead link.

## How it runs today, and next

`notify` is a one-shot command. The installed macOS poller runs rotating batches
of up to 100 boards, filters, matches, and sends alerts plus the digest every
five minutes while the Mac is available. The listener handles buttons separately.
See [matching and coverage policy](13-matching-and-coverage.md) for registry
coverage, sponsorship evidence, and operational limits.
