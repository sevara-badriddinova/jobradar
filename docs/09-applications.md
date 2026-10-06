# Slice 9 (plan step 10): never suggest a role you've already applied to

```
Gmail (IMAP, read-only) ──► cached AI classification ─┐
Notion tracker (preferred, no AI) ─────────────────────┼──► applications ──► link_applications() ──► jobs APPLIED / REJECTED
jobradar applied <id> ─────────────────────────────────┘                     (same company + same role,
                                                                              including later reposts)
```

## Setting up inbox access (one time, ~3 minutes)

1. Turn on 2-Step Verification for your Google account (required for app passwords).
2. Open https://myaccount.google.com/apppasswords, create one called "jobradar",
   and copy the 16-character password.
3. Add to your `.env` (already in `.gitignore`):
   ```bash
   GMAIL_ADDRESS=you@gmail.com
   GMAIL_APP_PASSWORD='abcdefghijklmnop'
   ```
4. Load the environment with `set -a; source .env; set +a` and run
   `uv run jobradar applications sync --source gmail`. It looks back 180 days
   (`--days` to change), processing at most 200 unprocessed matching emails per
   run, newest first (`--limit` to change). Re-run to continue the backfill.

## Using it

```bash
uv run jobradar applications sync              # Notion when configured, otherwise Gmail
uv run jobradar applications sync --source all # Notion first, then bounded Gmail updates
uv run jobradar applications list              # everything found, and which job it matched
uv run jobradar applications list --unmatched  # applications with no exact job match, plus suggestions
uv run jobradar applied 123 456                # mark jobs by hand (ids are shown by `jobradar top`)
```

`match` and `notify` re-check applications first, so a role you've applied to
is never scored by the AI or sent to Telegram, even if it was found after you
applied.

## Decisions and why

**IMAP with an app password, not Google OAuth.** OAuth with a read-only scope
is least-privilege, but for a personal, unverified Google app those logins
expire every 7 days, and a background job would silently stop. An app password
doesn't expire. The trade-off is that the password could read (and send)
mail, so:
- it lives only in `.env`
- the mailbox is opened **read-only** (`EXAMINE`)
- bodies are fetched with `BODY.PEEK`, so nothing is ever marked as read

You can revoke it at any time on the same Google page.

**Gmail does the first filtering, the AI the reading.** One Gmail search
(`X-GM-RAW`, the same syntax as the Gmail search box) picks out emails that
look application-related. Your real inbox showed why rules alone can't do the
rest: confirmations come from Ashby, Greenhouse, Workday, SmartRecruiters,
Oracle and companies' own systems, some name the role and some don't, and
rejections and a German email are mixed in. So the cheap AI model reads each
candidate email once and returns a structured verdict:
- `application_received`, `rejection`, `interview`, `offer` or `other`
- company
- the exact role title

Job alerts, newsletters and security-code emails come back as `other`.

**Committed classifications are reused.** Every Gmail id (`X-GM-MSGID`) is
stored in `processed_emails`, whatever the verdict. The sync first fetches IDs,
so already-processed emails aren't downloaded on normal subsequent runs. A
per-message PostgreSQL lock and a second check prevent overlapping syncs from
classifying the same message concurrently. The application and processed marker
commit together; failed classifications remain retryable. A crash after a paid
AI response but before commit may require another paid call: database idempotency
does not guarantee exactly-once external billing.

**Notion first minimizes AI cost.** Default auto mode chooses Notion when any
Notion configuration is present and requires that configuration to be complete.
Structured imports make no LLM calls. Gmail is used automatically only when
Notion is absent; a failed Notion import does not trigger paid fallback. Use
`--source all` for updates missing from Notion, or `--source gmail` explicitly.
The 200-message cap bounds attempted classifications, not a dollar amount; SDK
retries and message length affect actual cost. See [Notion setup](11-notion.md).

**Re-running a sync is safe.** Applications upsert by `(source, external_id)`;
unchanged records cause no update, feedback remains unique, and linking uses
current database status so a later application cannot overwrite a rejection.
Gmail and Notion records for the same role keep separate provenance but converge
on the same job status. No new application rows are created by repeating either
source's unchanged input.

**One place decides which jobs an application covers.** Gmail, Notion and the
`applied` command only *record* applications. `link_applications()` decides,
with a deliberately strict rule:
- **The same company:** "Notion" also matches "Notion Labs", but "Notion"
  doesn't match "Notional".
- **The same role:** the identifying words of the two titles must be
  identical, ignoring years, requisition ids, months, seasons and
  "US/remote/hybrid". So "Software Engineer, New Grad" matches "…New Grad
  (2027)", but "Early Career" doesn't match "Early Career (AI)", and
  "Engineer I" doesn't match "Engineer II".

It marks **every** matching job, so a role reposted later (new id, new board)
is suppressed too.

**Why not fuzzy matching?** Similarity scores rate "Software Engineer I" vs "II"
at 97%. A wrong match would silently hide a job you haven't applied to, which
is worse than showing one you have. Near-misses are shown as suggestions by
`applications list --unmatched`, so you confirm them with `jobradar applied <id>`.

**Rejections are recorded too.** A rejection email moves the job to
`REJECTED`, and later "applied" emails don't move it back. Every application
and rejection is also stored as feedback, the training data for a future
ranking model.

**Emails without a role.** "Thanks for applying to Together AI" names no role,
so it's stored but marks nothing, because guessing would risk hiding the wrong
job. It shows up in `applications list --unmatched`.

## H1B / visa sponsorship (hard filter)

Set `needs_visa_sponsorship = true` under `[preferences]` in `profile.toml`.
`jobradar filter` then rejects postings that explicitly rule you out:
- **No sponsorship:** "unable to sponsor", "not eligible for visa
  sponsorship", "without current or future sponsorship", "Visa sponsorship: No"
- **Citizenship:** "U.S. citizenship required"
- **Clearance:** "must obtain a security clearance"
- **Export controls:** ITAR "U.S. persons"

Postings that don't mention sponsorship pass, and that's most of them.
Equal-opportunity boilerplate ("without regard to … citizenship status") and
positive statements ("we sponsor H-1B") are covered by tests so they never
trigger it. After turning it on, run `jobradar filter --recheck`: it also
re-checks already-matched jobs that haven't been sent yet.
