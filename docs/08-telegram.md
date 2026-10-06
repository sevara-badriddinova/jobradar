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

Thresholds are in `profile.toml` under `[notifications]`. Each alert keeps these
buttons, including after a choice is saved:
- **Apply ↗** opens the posting.
- **✅ Applied** marks the job `APPLIED`, so it's never suggested again.
- **👎 Not interested** removes the role from your shortlist and suppresses further matching/alerts for it.
- **📋 Add to queue** saves the role for later without marking it applied. Repeated clicks add nothing.
- **🛂 Sponsorship required** skips this role because it cannot meet your sponsorship needs.
  This records a role-specific reason, not a company-wide sponsorship judgment.
- **Show queue** lists your selected open, unapplied roles, highest match percentage first.

The queue appears automatically after you decide on every role in the current
notification list. Pending immediate alerts and in-flight sends count toward that list; failed
and cancelled alerts, closed jobs, and applied jobs do not. `/queue` or `/done`
shows the current queue at any time without discarding unreviewed roles.
An unsent daily digest does not delay the current review list.
Digest entries also have controls identified by the job IDs shown beside them.
Match percentages reuse stored fit scores; they are not hiring probabilities.
No email reads, Notion calls, or AI calls happen while reviewing.

Selecting Add to queue can reverse an ignore/sponsorship choice. An existing
application fact is preserved: applied, closed, or filtered roles cannot be
added back to the active queue. Apply ↗ only opens the posting; use Applied to
record that you submitted it.

After upgrading, run `uv run jobradar init-db`, restart the listener, and use
`uv run jobradar telegram refresh-buttons` to restore controls on existing messages.
Sponsorship remains a global preference in `profile.toml`; it is already enabled
for this profile. Unknown sponsorship is not treated as a confirmed refusal.

## Decisions and why

**The outbox pattern.** We never "send, then write down that we sent". A
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

A PostgreSQL advisory lock allows only one update consumer at a time, so
`notify` and `telegram listen` cannot race the saved offset. `job_reviews`
stores one current choice per job, while `applications` preserves Applied
facts for future role linking. A separate `review_responses` outbox saves the
final queue with the triggering update. Uncertain summary delivery is not
automatically retried; `/queue` retrieves it explicitly.

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

`notify` is a one-shot command: button presses are picked up the next time it
runs, or instantly with `telegram listen` running. The scheduler (plan step 5)
will run `collect → filter → match → notify` every few minutes and the digest
once a day, which is what makes this fully automatic.
