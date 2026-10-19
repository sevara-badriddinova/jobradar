# Matching and coverage policy — October 13, 2026

The user requires postings published within the last 72 hours and explicit
sponsorship support in the specific posting. These are eligibility requirements,
not targets to relax when there are no matches. Immediate alerts remain at 80;
the per-cycle digest covers 70–79. Scores are fit scores, not hiring probabilities.

## Experience and AI decisions

Full-time experience is calculated from the recorded employment intervals,
including employment before graduation. Overlapping intervals count once;
month-only end dates are treated conservatively as exclusive. Present employment
is capped at the current month. Unknown dates are not invented. Internships and
other experience remain in the profile but are not added to the full-time total.
The live profile was versioned from 0 to 1.58 years (19 completed months), retaining
the original profile. `match` refreshes this calculation without rereading the
resume or paying for another extraction.

Every AI rejection must supply a verbatim job requirement and a conflicting
candidate fact with its field path. Code checks that the quoted candidate value
actually exists at that path; the model cannot substitute an invented biography.
Negative assessments, graduation-date mismatches, and claims
that a known skill is missing receive a second assessment. Code verifies the
quotes and common contradictions. An unverified rejection becomes a concern,
not an automatic zero score. Fit dimensions are retained, so an unsuitable job
is not automatically promoted. Missing technologies alone reduce fit rather
than veto eligibility. Semantic review remains fallible; the stored assessment
retains concerns and evidence for inspection.

## Fresh decisions and reposts

Cache identity includes the candidate facts, preferences, model, prompt, scoring
policy, job title, company, description, primary and secondary locations. Before
matching or sending, outdated unsent scores are withdrawn. Work is selected
before applying the batch budget, so fresh cached rows cannot starve stale work.
A final content/state check prevents an AI result from overwriting a job changed
or acted on during the request.

Ingestion refreshes changed raw data and resets unacted-on jobs for filtering.
Reposts enter filtering and matching; application linking includes REPOSTED jobs.
A live repost also participates in duplicate detection. Applied/rejected history,
review choices, and already-sent notification identities remain protected.
Cancelled alerts that were never attempted can be requeued after correction;
uncertain or already-sent deliveries are not automatically resent.

## Sponsorship evidence and freshness

`require_explicit_sponsorship = true` requires affirmative visa/H-1B sponsorship
or STEM OPT support wording in the posting. Questions, historical claims,
E-Verify alone, hypothetical availability, and transfer-only support do not qualify.
Explicit refusal takes precedence over positive boilerplate. A positive opening
sentence followed by an inability to sponsor every role is treated as conditional,
not proof of support for the specific role. The evidence is
shown in alerts/digests. This is a conservative text policy: unfamiliar positive
wording may need an additional tested pattern, and posting wording is not a
guarantee of an offer, visa approval, or STEM OPT compliance.

STEM OPT and H-1B are distinct. STEM OPT requires an E-Verify employer and an
employer-supported training plan; an H-1B claim does not establish those facts.
Sources: [E-Verify employer requirement](https://www.e-verify.gov/faq/am-i-required-to-participate-in-e-verify-in-order-to-hire-f-1-students-who-seek-a-stem-opt),
[DHS Form I-983 overview](https://studyinthestates.dhs.gov/form-i-983-overview).

The shared publication check rejects missing dates, future dates, and dates older
than 72 hours. Discovery and update timestamps are never substituted. The policy
is checked during filtering, matching, queue browsing, and immediate delivery.
Applied/skipped history is retained. Conditional sponsorship statements are not
interpreted as legal conclusions by the matcher.

## Broad discovery and remaining coverage

`jobradar companies discover` imports all supported addresses from the
[public directory](https://kalebconfer-sys.github.io/job-board-directory/), validates
the board URL host and token, preserves manual names/priorities/disabled entries,
and writes an audit report to `companies.coverage.json`. Sponsorship is evaluated
on each posting, not inferred from the directory or company history.

The expanded import registers 12,237 boards across Greenhouse, Ashby, Lever,
Workday, Workable, Recruitee, Rippling, and Personio. All 6,012 previously
unsupported directory entries now have collectors. See [collector details](14-public-ats-collectors.md).
Custom career sites and companies missing from this public directory remain outside coverage. Registration is not proof a board
has been polled successfully; `collector_runs` records the real attempts/results.

The macOS poller uses `cycle --discover --collect-limit 100 --match --match-limit 25
--notify --digest`. Directory refreshes are limited to once daily; failure retains
the existing registry. Each scan starts with never-polled/least-recently attempted
boards, including failures, to avoid starvation. At a five-minute cadence and
short cycles, a full 12,237-board rotation takes at least 123 cycles, roughly 10.25 hours. Long requests,
Mac sleep, database outages, and network failures extend that time. This is a
local background service, not an always-on cloud deployment.

## Operation

```bash
# Load the existing local environment first.
jobradar companies discover
jobradar filter --recheck
jobradar match --limit 50
jobradar notify --digest
.venv/bin/python deploy/install-telegram-listener.py --with-poller
```

The integration suite uses only `jobradar_test`, fake AI answers, and fake Telegram.
It includes repost-to-alert delivery, applied-repost suppression, batch progress,
changed-job races, three-day/explicit-support gates, and notification idempotency.
Live scoring separately requires the user's authorization to transmit the stored
professional profile to the configured Anthropic API.
