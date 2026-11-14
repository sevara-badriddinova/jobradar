"""Compare Haiku 5.5 with the Haiku 4.5 scores already stored. Read-only.

Runs inside the CURRENT production image, so it uses only code that image
already has, and calls the API itself to add the effort setting. It writes
nothing to the database and does not count toward the daily AI budget.

    gcloud compute ssh jobradar-vm --project=jobradar-510801 --zone=us-east1-b \
      --tunnel-through-iap --command 'cd /opt/jobradar && sudo docker compose \
      -f compose.yaml -f compose.workers.yaml run --rm --no-deps -T \
      --entrypoint python worker - --limit 100' \
      < deploy/vm/calibrate-haiku55.py > calibration.csv

The CSV goes to stdout; progress and the summary go to stderr.
Cost: about $0.003 per job per effort, under $1 for 100 jobs.
"""

import argparse
import csv
import sys

import anthropic
from sqlalchemy import select

from jobradar import llm, matcher
from jobradar.candidate import active_profile
from jobradar.db import job_matches, job_reviews, jobs, make_engine
from jobradar.settings import load_settings

NEW_MODEL = "claude-haiku-5-5"
EFFORTS = ("low", "medium")
THRESHOLDS = (70, 75, 80, 85, 90)
LIKED = {"APPLIED", "QUEUED", "SPONSORSHIP"}

parser = argparse.ArgumentParser()
parser.add_argument("--limit", type=int, default=100)
parser.add_argument("--max-usd", type=float, default=1.5, help="stop when this much is spent")
args = parser.parse_args()

client = anthropic.Anthropic()
spent = 0.0


def make_ask(effort):
    """Same signature as llm.ask, plus effort; tallies the cost."""
    def ask(output, *, system, prompt, model, max_tokens=4096, **_):
        global spent
        response = client.messages.parse(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": prompt}], output_format=output,
            output_config={"effort": effort})
        u = response.usage
        spent += llm.cost_usd(model, u.input_tokens or 0, u.output_tokens or 0)
        if response.parsed_output is None:
            raise llm.LLMError(f"no structured output (stop_reason={response.stop_reason})")
        return response.parsed_output
    return ask


def log(*parts):
    print(*parts, file=sys.stderr, flush=True)


engine = make_engine()
preferences = load_settings("profile.toml").preferences
with engine.connect() as conn:
    profile = active_profile(conn)
    rows = conn.execute(
        select(jobs.c.id, jobs.c.source, jobs.c.company, jobs.c.title, jobs.c.location,
               jobs.c.description, jobs.c.raw, job_matches.c.score.label("old_score"),
               job_matches.c.created_at, job_reviews.c.choice)
        .join(job_matches, job_matches.c.job_id == jobs.c.id)
        .outerjoin(job_reviews, job_reviews.c.job_id == jobs.c.id)
        .where(job_matches.c.model.like("claude-haiku-4-5%"))
        .order_by(job_reviews.c.choice.is_(None), job_matches.c.score.desc(), job_matches.c.created_at.desc())
    ).all()

seen, picked = set(), []
for r in rows:  # newest Haiku 4.5 score per job; reviewed jobs first, then the highest scores
    if r.id not in seen:
        seen.add(r.id)
        picked.append(r)
picked = picked[:args.limit]
log(f"{len(picked)} jobs with a Haiku 4.5 score ({sum(r.choice is not None for r in picked)} reviewed by you)")

out = csv.writer(sys.stdout)
out.writerow(["job_id", "company", "title", "your_choice", "haiku45", *[f"haiku55_{e}" for e in EFFORTS]])
results = []
for i, r in enumerate(picked, 1):
    if spent >= args.max_usd:
        log(f"stopping: ${spent:.2f} spent (--max-usd {args.max_usd})")
        break
    new = {}
    for effort in EFFORTS:
        matcher.llm.ask = make_ask(effort)
        try:
            a = matcher.assess_with_review(r, profile.profile, preferences, model=NEW_MODEL, max_tokens=4096)
            new[effort] = matcher.score(a)
        except Exception as exc:  # one bad job must not stop the comparison
            log(f"job {r.id} effort={effort}: {type(exc).__name__}: {exc}")
            new[effort] = None
    results.append((r, new))
    out.writerow([r.id, r.company, r.title, r.choice or "", float(r.old_score), *[new[e] for e in EFFORTS]])
    if i % 10 == 0:
        log(f"{i}/{len(picked)} done, ${spent:.2f} spent")

log(f"\nSpent ${spent:.2f} on {len(results)} jobs.")
log("How many jobs reach each threshold (liked = Applied, Queued or Sponsorship; skipped = Not interested):")
log(f"{'score >=':>9} | {'haiku 4.5':>18} | " + " | ".join(f"{'5.5 ' + e:>18}" for e in EFFORTS))
for t in THRESHOLDS:
    def cell(scores):
        hit = [r for r, s in scores if s is not None and s >= t]
        liked = sum(r.choice in LIKED for r in hit)
        skipped = sum(r.choice == "IGNORED" for r in hit)
        return f"{len(hit):>3} ({liked} liked, {skipped} skip)"
    old = cell([(r, float(r.old_score)) for r, _ in results])
    log(f"{t:>9} | {old:>18} | " + " | ".join(f"{cell([(r, n[e]) for r, n in results]):>18}" for e in EFFORTS))
