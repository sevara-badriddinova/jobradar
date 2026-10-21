"""Command-line entry point.

    jobradar init-db
    jobradar companies sync companies.toml [--dry-run]
    jobradar companies list [--all]
    jobradar collect                     # every enabled company
    jobradar collect stripe figma        # just these (board or name)
    jobradar collect --priority 1        # only priority 1
    jobradar filter [--recheck]          # apply hard filters from profile.toml
    jobradar profile extract [RESUME] [--force]
    jobradar profile show [--json]
    jobradar match [--limit 25] [--rematch]   # AI-score jobs that passed the filters
    jobradar top [--min-score 70]             # best matches first
    jobradar notify [--digest] [--resend-stuck]   # Telegram alerts (+ handle button presses)
    jobradar telegram setup [--test]          # find your chat id, send a test message
    jobradar telegram listen                  # react to button presses instantly
    jobradar applications sync [--days 180]   # read application emails, mark those jobs APPLIED
    jobradar applications list [--unmatched]
    jobradar applied JOB_ID [JOB_ID ...]      # mark by hand (ids are shown by `top`)
    jobradar new --hours 24
    jobradar runs
"""

import argparse
import json
import logging
import os
import sys
import httpx
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import OperationalError

from jobradar.applications import link_applications, mark_applied_by_hand, unmatched
from jobradar.candidate import ResumeError, active_profile, extract_and_store, refresh_experience
from jobradar.collectors import build_collector
from jobradar.companies import (
    CompanyFileError, list_companies, load_company_file, matches, sync_companies,
)
from jobradar.db import app_state, applications, collector_runs, job_matches, jobs, make_engine, migrate
from jobradar.filters import run_filters
from jobradar.http import make_client
from jobradar.inbox import InboxError, sync_inbox
from jobradar.notion import Notion, NotionError, sync_notion
from jobradar.llm import LLMError
from jobradar.matcher import run_matching
from jobradar.notifications import enqueue, process_updates, refresh_buttons, resend_stuck, send_digest, send_immediate
from jobradar.review import not_skipped
from jobradar.queue_ui import request_queue
from jobradar.telegram import Telegram, TelegramError
from jobradar.pipeline import run_all
from jobradar.settings import SettingsError, load_settings


def cmd_init_db(args) -> int:
    migrate(make_engine())
    print("schema ready")
    return 0


def cmd_companies_sync(args) -> int:
    try:
        specs = load_company_file(args.file)
    except CompanyFileError as exc:
        print(exc, file=sys.stderr)
        return 2
    with make_engine().connect() as conn:
        result = sync_companies(conn, specs)
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()

    prefix = "[dry run] would have " if args.dry_run else ""
    for label, names in (("added", result.added), ("updated", result.updated),
                         ("disabled", result.disabled)):
        if names:
            print(f"{prefix}{label} {len(names)}: {', '.join(names)}")
    print(f"{result.unchanged} unchanged" + ("" if result.changed else " (registry already in sync)"))
    return 0


def cmd_companies_list(args) -> int:
    with make_engine().connect() as conn:
        rows = list_companies(conn, enabled_only=not args.all)
    print(f"{'name':<22}{'collector':<12}{'board':<22}{'priority':>8}  enabled")
    for r in rows:
        print(f"{r.name:<22}{r.collector:<12}{r.board:<22}{r.priority:>8}  {'yes' if r.enabled else 'no'}")
    print(f"{len(rows)} compan{'y' if len(rows) == 1 else 'ies'}")
    return 0


def cmd_companies_discover(args) -> int:
    from jobradar.discovery import SUPPORTED, refresh_directory
    # Cloud Run starts with an ephemeral filesystem on each invocation. A
    # local timestamp alone refreshed and resynced all 12k boards every scan.
    engine = make_engine()
    try:
        with engine.connect() as conn:
            cached = conn.execute(select(app_state.c.value).where(
                app_state.c.key == "directory_sync")).scalar_one_or_none()
        cached = json.loads(cached) if cached else None
        if cached and not args.force and cached.get("collector_platforms") == sorted(SUPPORTED):
            age = (datetime.now(UTC) - datetime.fromisoformat(cached["synced_at"])).total_seconds()
            if 0 <= age < 86400:
                print(f"directory already synced: {cached['registered_boards']} boards (daily refresh)")
                return 0
        with make_client() as client:
            report = refresh_directory(client, args.file, force=args.force)
        with engine.begin() as conn:
            sync_companies(conn, load_company_file(args.file))
            value = json.dumps(dict(synced_at=datetime.now(UTC).isoformat(),
                collector_platforms=sorted(SUPPORTED), registered_boards=report["registered_boards"]))
            conn.execute(insert(app_state).values(key="directory_sync", value=value)
                .on_conflict_do_update(index_elements=[app_state.c.key], set_={"value": value}))
    except (CompanyFileError, OSError, ValueError, httpx.HTTPError) as exc:
        print(f"board discovery failed: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    print(f"registered {report['registered_boards']} boards; unsupported: "
          f"{report['unsupported_directory_boards']}. Sponsorship is checked per posting.")
    return 0


def cmd_collect(args) -> int:
    engine = make_engine()
    with engine.connect() as conn:
        targets = list_companies(conn, priority=args.priority, names=args.companies or None,
                                 **({"limit": args.limit} if args.limit else {}))

    if args.companies:
        missing = [n for n in args.companies if not any(matches(t, n) for t in targets)]
        if missing:
            print(f"not in the registry (or disabled): {', '.join(missing)}\n"
                  "add them to companies.toml and run: jobradar companies sync companies.toml",
                  file=sys.stderr)
            return 2
    if not targets:
        print("no companies to collect; add some to companies.toml and run "
              "`jobradar companies sync companies.toml`", file=sys.stderr)
        return 2

    try:
        window = load_settings(args.profile).ingestion.max_age_days
    except SettingsError as exc:
        print(exc, file=sys.stderr)
        return 2
    with make_client() as client:  # one pooled client shared by all boards
        collectors = [build_collector(t.collector, t.board, company=t.name, client=client)
                      for t in targets]
        reports = run_all(engine, collectors, ingest_max_age_days=window)

    print(f"{'source/board':<32}{'ok':<5}{'fetched':>8}{'new':>6}{'seen':>6}"
          f"{'chg':>5}{'dup':>5}{'closed':>8}{'secs':>7}")
    for rep in reports:
        r, label = rep.result, f"{rep.source}/{rep.board}"
        if rep.ok and r:
            print(f"{label:<32}{'yes':<5}{rep.fetched:>8}{r.new:>6}{r.seen:>6}"
                  f"{r.changed:>5}{r.duplicates:>5}{r.closed:>8}{rep.seconds:>7.1f}")
        else:
            print(f"{label:<32}{'NO':<5}  {rep.error}")
    return 0 if all(r.ok for r in reports) else 1


def cmd_filter(args) -> int:
    try:
        settings = load_settings(args.profile)
    except SettingsError as exc:
        print(exc, file=sys.stderr)
        return 2
    with make_engine().begin() as conn:
        result = run_filters(conn, settings.filters, recheck=args.recheck,
                             needs_sponsorship=settings.preferences.needs_visa_sponsorship is True)
    print(f"{result.passed} passed, {result.rejected.total()} rejected")
    for reason, count in result.rejected.most_common():
        print(f"{count:>6}  {reason}")
    return 0


def cmd_profile_extract(args) -> int:
    try:
        resume = args.resume or load_settings(args.profile).preferences.resume
        with make_engine().begin() as conn:
            row, reused = extract_and_store(conn, resume, force=args.force)
    except (SettingsError, ResumeError, LLMError) as exc:
        print(exc, file=sys.stderr)
        return 2
    p = row.profile
    print(f"{'reused' if reused else 'saved'} profile v{row.id} from {resume}"
          + (" (unchanged resume; use --force to re-extract)" if reused else ""))
    print(f"  level: {p['experience_level']}, graduation: {p['graduation'] or '?'}, "
          f"{p['years_of_experience']:g} years full-time")
    print(f"  languages: {', '.join(p['languages']) or '-'}")
    print(f"  roles: {', '.join(p['target_roles']) or '-'}")
    return 0


def cmd_profile_show(args) -> int:
    try:
        preferences = load_settings(args.profile).preferences
    except SettingsError as exc:
        print(exc, file=sys.stderr)
        return 2
    with make_engine().connect() as conn:
        row = active_profile(conn)
    if row is None:
        print("no profile yet; run: jobradar profile extract path/to/resume.pdf", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"profile": row.profile, "preferences": preferences.model_dump()}, indent=2))
        return 0
    print(f"profile v{row.id} (extracted {row.created_at:%Y-%m-%d} with {row.model})")
    for key, value in row.profile.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            print(f"{key}:")
            for item in value:
                print("  - " + "; ".join(f"{k}: {', '.join(v) if isinstance(v, list) else v}"
                                         for k, v in item.items() if v))
        else:
            print(f"{key}: {', '.join(value) if isinstance(value, list) else value}")
    print("preferences (profile.toml):")
    for key, value in preferences.model_dump(exclude={"resume"}).items():
        print(f"  {key}: {value}")
    return 0


def cmd_match(args) -> int:
    try:
        settings = load_settings(args.profile)
        preferences = settings.preferences
    except SettingsError as exc:
        print(exc, file=sys.stderr)
        return 2
    engine = make_engine()
    with engine.begin() as conn:
        profile = refresh_experience(conn)
    if profile is None:
        print("no profile yet; run: jobradar profile extract path/to/resume.pdf", file=sys.stderr)
        return 2
    with engine.begin() as conn:  # never spend AI calls on roles you've applied to
        link_applications(conn)
    from jobradar import llm
    from jobradar.ai_budget import DailyBudgetMeter, budget_from_env
    llm.set_meter(DailyBudgetMeter(engine, budget_from_env()))  # records spend; caps when configured
    try:
        result = run_matching(engine, profile, preferences, limit=args.limit, rematch=args.rematch,
                              max_posting_age_days=settings.filters.max_posting_age_days,
                              discovery_days=settings.filters.max_discovery_age_days)
    except llm.BudgetExceeded as exc:
        print(f"{exc}; remaining jobs stay queued for tomorrow", file=sys.stderr)
        return 0
    print(f"assessed {result.assessed}, reused {result.cached} cached, failed {result.failed}")
    if result.aborted:
        print("stopped early after repeated failures (see the errors above)", file=sys.stderr)
    if result.scores:
        print("see the best matches with: jobradar top")
    return 1 if result.failed else 0


def cmd_top(args) -> int:
    with make_engine().connect() as conn:
        rows = conn.execute(
            select(jobs.c.id, jobs.c.company, jobs.c.title, jobs.c.location, jobs.c.url, jobs.c.match_score,
                   job_matches.c.assessment)
            .join(job_matches, jobs.c.match_id == job_matches.c.id)
            .where(jobs.c.status == "MATCHED", jobs.c.closed_at.is_(None),
                   jobs.c.match_score >= args.min_score, not_skipped())
            .order_by(jobs.c.match_score.desc(), jobs.c.first_seen_at.desc())
            .limit(args.limit)
        ).all()
    for r in rows:
        a = r.assessment
        print(f"{float(r.match_score):5.1f}  {r.company} | {r.title} | {r.location or '-'}")
        print(f"       [{r.id}] {r.url}")
        for reason in a["reasons"]:
            print(f"       + {reason}")
        for concern in a["concerns"]:
            print(f"       - {concern}")
        if a["missing_skills"]:
            print(f"       missing: {', '.join(a['missing_skills'])}")
    print(f"{len(rows)} match(es) scoring {args.min_score:g}+")
    if rows:
        print("already applied to one? jobradar applied <id>")
    return 0


SETUP_HELP = """To set up Telegram:
  1. In Telegram, message @BotFather, send /newbot and follow the steps.
  2. export TELEGRAM_BOT_TOKEN=<the token it gives you>
  3. Send any message to your new bot, then run: jobradar telegram setup
  4. export TELEGRAM_CHAT_ID=<the chat id it prints>"""


def _telegram_env(need_chat: bool = True) -> tuple[str, str] | None:
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or (need_chat and not chat_id):
        missing = "TELEGRAM_BOT_TOKEN" if not token else "TELEGRAM_CHAT_ID"
        print(f"{missing} is not set.\n\n{SETUP_HELP}", file=sys.stderr)
        return None
    return token, chat_id


def prepare_delivery(engine, chat_id, profile_settings) -> dict:
    """Pre-delivery reconciliation shared by `notify` and the queue-mode worker."""
    settings = profile_settings.notifications
    max_age = profile_settings.filters.max_posting_age_days
    with engine.begin() as conn:
        link_applications(conn)  # roles you've applied to are never announced
        run_filters(conn, profile_settings.filters,
                    needs_sponsorship=profile_settings.preferences.needs_visa_sponsorship is True)
        profile = refresh_experience(conn)
        if profile is not None:
            from jobradar.matcher import invalidate_stale_matches
            invalidate_stale_matches(conn, profile, profile_settings.preferences)
        queued = enqueue(conn, settings, max_posting_age_days=max_age,
                         discovery_days=profile_settings.filters.max_discovery_age_days)
        if settings.delivery == "queue":
            request_queue(conn, chat_id, min_score=settings.digest_min_score, publish=True,
                          max_posting_age_days=max_age)
        else:
            request_queue(conn, chat_id, create=False, min_score=settings.digest_min_score, publish=False,
                          max_posting_age_days=max_age)
    return queued


def deliver_queue_mode(engine, tg, chat_id, profile_settings) -> int:
    """Queue (Mini App) delivery: one durable queue update, never per-job alerts."""
    process_updates(engine, tg, chat_id)
    queued = prepare_delivery(engine, chat_id, profile_settings)
    process_updates(engine, tg, chat_id)
    return queued["immediate"] + queued["digest"]


def cmd_sponsorship_review(args) -> int:
    """The held-for-review queue: roles that neither offer nor refuse sponsorship."""
    from jobradar import queue as work
    from jobradar import sponsorship_review as review
    engine = make_engine()
    with engine.begin() as conn:
        try:
            for review_id in args.keep or []:
                job_id = review.decide(conn, review_id, "kept")
                if job_id is not None:
                    work.enqueue(conn, "match", [job_id])
                print(f"#{review_id} kept" + (f": job {job_id} queued for matching" if job_id else ""))
            for review_id in args.dismiss or []:
                review.decide(conn, review_id, "dismissed")
                print(f"#{review_id} dismissed")
        except LookupError as exc:
            print(exc, file=sys.stderr)
            return 1
        rows = review.pending(conn, args.limit)
        totals = review.counts(conn)
    print(f"sponsorship review: {totals.get('pending', 0)} pending, {totals.get('kept', 0)} kept, "
          f"{totals.get('dismissed', 0)} dismissed")
    for r in rows:
        posted = r.posted_at.strftime("%Y-%m-%d") if r.posted_at else "undated"
        print(f"#{r.id:<6} {posted}  {r.company[:28]:<28} {r.title[:60]:<60} {(r.location or '-')[:30]}\n"
              f"        {r.url}")
    return 0


def cmd_notify(args) -> int:
    try:
        profile_settings = load_settings(args.profile)
        settings = profile_settings.notifications
        max_age = profile_settings.filters.max_posting_age_days
    except SettingsError as exc:
        print(exc, file=sys.stderr)
        return 2
    if (env := _telegram_env()) is None:
        return 2
    token, chat_id = env
    engine = make_engine()
    with make_client() as client:
        tg = Telegram(token, client)
        if args.resend_stuck:
            with engine.begin() as conn:
                print(f"re-queued {resend_stuck(conn)} notification(s) with unknown delivery")
        try:
            pressed = process_updates(engine, tg, chat_id)
        except TelegramError as exc:
            print(f"telegram: {exc}", file=sys.stderr)
            return 2
        queued = prepare_delivery(engine, chat_id, profile_settings)
        if settings.delivery == "queue":
            # The listener shares the consumer lock and will render this update
            # if it owns the lock right now. Pending jobs remain durable.
            process_updates(engine, tg, chat_id)
            print(f"button presses applied: {pressed}")
            print(f"queue update saved: {queued['immediate'] + queued['digest']} new job(s)")
            return 0
        policy = dict(max_posting_age_days=max_age,
                      discovery_days=profile_settings.filters.max_discovery_age_days,
                      require_explicit_sponsorship=profile_settings.filters.require_explicit_sponsorship)
        sent = send_immediate(engine, tg, chat_id, **policy)
        digest = send_digest(engine, tg, chat_id, **policy) if args.digest else None

    print(f"button presses applied: {pressed}")
    print(f"queued: {queued['immediate']} immediate, {queued['digest']} for the digest")
    print(f"alerts: {sent.sent} sent, {sent.failed} failed, {sent.cancelled} cancelled (no longer eligible)")
    if digest:
        print(f"digest: {digest.sent} job(s) sent, {digest.failed} failed, {digest.cancelled} cancelled")
    stuck = sent.stuck + (digest.stuck if digest else 0)
    if stuck:
        print(f"{stuck} notification(s) may or may not have reached Telegram and were NOT resent.\n"
              "Check your chat; to send them again run: jobradar notify --resend-stuck", file=sys.stderr)
    return 1 if sent.failed or (digest and digest.failed) else 0


def cmd_telegram_setup(args) -> int:
    if (env := _telegram_env(need_chat=False)) is None:
        return 2
    token, chat_id = env
    with make_client() as client:
        tg = Telegram(token, client)
        try:
            bot = tg.get_me()
            chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name")
                     or u["message"]["chat"].get("title", "") for u in tg.get_updates(0) if "message" in u}
            print(f"bot: @{bot['username']}")
            if chats:
                print("chats that messaged the bot:")
                for cid, name in chats.items():
                    print(f"  {cid}  {name}")
                print(f"then: export TELEGRAM_CHAT_ID={next(iter(chats))}")
            else:
                print(f"no messages yet: send any message to @{bot['username']} and run this again")
            if args.test:
                if not chat_id:
                    print("set TELEGRAM_CHAT_ID first to send a test message", file=sys.stderr)
                    return 2
                tg.send_message(chat_id, "✅ <b>jobradar</b> is connected. New matches will show up here.")
                print("test message sent")
        except TelegramError as exc:
            print(f"telegram: {exc}", file=sys.stderr)
            return 2
    return 0


def cmd_telegram_listen(args) -> int:
    if (env := _telegram_env()) is None:
        return 2
    token, chat_id = env
    engine = make_engine()
    try:
        settings = load_settings(args.profile)
    except SettingsError as exc:
        print(exc, file=sys.stderr)
        return 2
    policy_ready = False
    print("listening for button presses (Ctrl-C to stop)")
    with make_client() as client:
        tg = Telegram(token, client)
        try:
            while True:
                try:
                    if not policy_ready:
                        with engine.begin() as conn:
                            request_queue(conn, chat_id, max_posting_age_days=settings.filters.max_posting_age_days,
                                          min_score=settings.notifications.digest_min_score)
                        policy_ready = True
                    if pressed := process_updates(engine, tg, chat_id, timeout=5):
                        print(f"applied {pressed} button press(es)")
                    else:
                        time.sleep(1)  # another listener may own the update-consumer lock
                except TelegramError as exc:
                    logging.getLogger(__name__).warning("telegram: %s; retrying in 1s", exc)
                    time.sleep(1)
                except OperationalError:
                    # A sleeping database or dropped connection must not kill
                    # live buttons. Do not log URLs, SQL parameters, or secrets.
                    logging.getLogger(__name__).warning("database unavailable; retrying in 5s")
                    engine.dispose()
                    time.sleep(5)
        except KeyboardInterrupt:
            return 0


def cmd_telegram_menu(args) -> int:
    if (env := _telegram_env()) is None:
        return 2
    token, chat_id = env
    with make_client() as client:
        try:
            Telegram(token, client).setup_menu(chat_id)
        except TelegramError as exc:
            print(f"telegram: {exc}", file=sys.stderr)
            return 2
    print("Telegram menu configured: New, Liked, Applied, Skipped")
    return 0


def cmd_telegram_refresh_buttons(args) -> int:
    if (env := _telegram_env()) is None:
        return 2
    token, chat_id = env
    with make_client() as client:
        try:
            count = refresh_buttons(make_engine(), Telegram(token, client), chat_id)
        except TelegramError as exc:
            print(f"telegram: {exc}", file=sys.stderr)
            return 2
    print(f"restored buttons on {count} message(s)")
    return 0


def cmd_applications_sync(args) -> int:
    try:
        settings = load_settings(args.profile)
    except SettingsError as exc:
        print(exc, file=sys.stderr)
        return 2
    address, password = os.environ.get("GMAIL_ADDRESS", ""), os.environ.get("GMAIL_APP_PASSWORD", "")
    token = os.environ.get("NOTION_TOKEN", "")
    source_id = os.environ.get("NOTION_DATA_SOURCE_ID", "")
    database_id = os.environ.get("NOTION_DATABASE_ID", "")
    source = args.source
    if source == "auto":
        source = "notion" if token or source_id or database_id else ("gmail" if address or password else "none")
    if source in {"notion", "all"} and not (token and (source_id or database_id)):
        print("Notion requires NOTION_TOKEN and NOTION_DATA_SOURCE_ID (or NOTION_DATABASE_ID). "
              "See docs/11-notion.md. No inbox fallback was started.", file=sys.stderr)
        return 2
    if source in {"gmail", "all"} and not (address and password):
        print("Gmail requires both GMAIL_ADDRESS and GMAIL_APP_PASSWORD.", file=sys.stderr)
        return 2
    engine = make_engine()
    if source in {"notion", "all"}:
        try:
            with make_client() as client:
                notion = sync_notion(engine, Notion(token, client), settings.notion,
                                     data_source_id=source_id, database_id=database_id, full=args.full)
        except NotionError as exc:
            print(f"notion: {exc}; inbox not started", file=sys.stderr)
            return 2
        print(f"notion: read {notion.read} row(s), changed {notion.changed}, skipped {notion.skipped}; 0 AI calls")
    failed = 0
    if source in {"gmail", "all"}:
        try:
            inbox = sync_inbox(engine, address, password, days=args.days, limit=args.limit)
        except (InboxError, LLMError) as exc:
            print(exc, file=sys.stderr)
            # Earlier per-message commits / Notion imports still need linking.
            with engine.begin() as conn:
                link_applications(conn)
            return 2
        failed = inbox.failed
        found = ", ".join(f"{n} {kind.lower()}" for kind, n in inbox.found.most_common()) or "nothing new"
        print(f"inbox: read {inbox.read} new email(s): {found}"
              + (f"; {inbox.failed} failed (retried next time)" if inbox.failed else ""))
    else:
        print("inbox: skipped (Notion is preferred; use --source all for inbox updates)" if source == "notion" else
              "inbox: skipped (no sources configured; linking stored applications only)")
    with engine.begin() as conn:
        linked = link_applications(conn)
    print(f"jobs marked as applied/rejected: {linked.jobs_marked}")
    return 1 if failed else 0


def cmd_applications_list(args) -> int:
    with make_engine().connect() as conn:
        if args.unmatched:
            for app, nearest in unmatched(conn, limit=args.limit):
                print(f"{app.kind:<9} {app.company} | {app.role or '(role not named)'}  [{app.source}]")
                for similarity, job in nearest if app.role else []:
                    print(f"          maybe: [{job.id}] {job.title} ({similarity:.0%} similar)")
            print("if one of them is the role you applied to: jobradar applied <id>")
            return 0
        rows = conn.execute(
            select(applications, jobs.c.title.label("job_title"))
            .outerjoin(jobs, applications.c.job_id == jobs.c.id)
            .order_by(applications.c.occurred_at.desc().nulls_last()).limit(args.limit)).all()
    for r in rows:
        when = f"{r.occurred_at:%Y-%m-%d}" if r.occurred_at else "          "
        linked = f"-> [{r.job_id}]" if r.job_id else "(no matching job)"
        print(f"{when} {r.kind:<9} {r.company} | {r.role or '(role not named)'} {linked}  [{r.source}]")
    print(f"{len(rows)} application(s)")
    return 0


def cmd_applied(args) -> int:
    engine = make_engine()
    with engine.begin() as conn:
        for job_id in args.job_ids:
            label = mark_applied_by_hand(conn, job_id)
            print(f"[{job_id}] {label}" if label else f"[{job_id}] no such job", file=sys.stdout if label else sys.stderr)
        linked = link_applications(conn)
    print(f"jobs marked as applied: {linked.jobs_marked}")
    return 0


def cmd_new(args) -> int:
    since = datetime.now(UTC) - timedelta(hours=args.hours)
    with make_engine().connect() as conn:
        rows = conn.execute(
            select(jobs.c.id, jobs.c.company, jobs.c.title, jobs.c.location,
                   jobs.c.first_seen_at, jobs.c.url)
            .where(jobs.c.status == "DISCOVERED", jobs.c.first_seen_at >= since,
                   jobs.c.closed_at.is_(None))
            .order_by(jobs.c.first_seen_at.desc())
            .limit(args.limit)
        ).all()
    for r in rows:
        print(f"[{r.id}] {r.company} | {r.title} | {r.location or '-'}\n      {r.url}")
    print(f"{len(rows)} new job(s) in the last {args.hours}h")
    return 0


def cmd_worker(args) -> int:
    """Run the continuous pipeline until SIGTERM (or --duration seconds)."""
    import signal

    from jobradar.cycle import cycle_lock
    from jobradar.health import record_progress
    from jobradar.worker import ALL_ROLES, Worker, WorkerConfig

    roles = frozenset(r.strip() for r in args.roles.split(",") if r.strip())
    if not roles <= ALL_ROLES:
        print(f"unknown roles: {sorted(roles - ALL_ROLES)}", file=sys.stderr)
        return 2
    try:
        load_settings(args.profile)
    except SettingsError as exc:
        print(exc, file=sys.stderr)
        return 2
    telegram = None
    if "notify" in roles:
        env = _telegram_env()
        if env is None:
            return 2
        telegram = (Telegram(env[0], make_client()), env[1])
    from jobradar.ai_budget import budget_from_env
    budget = args.ai_daily_budget_usd if args.ai_daily_budget_usd is not None else budget_from_env()
    config = WorkerConfig(roles=roles, collect_concurrency=args.collect_concurrency,
                          base_interval=args.base_interval_minutes * 60,
                          byte_budget=args.byte_budget_mb * 1024 * 1024,
                          ai_daily_budget_usd=budget)
    pool = config.collect_concurrency + 4
    engine = make_engine(pool_size=pool, max_overflow=2, pool_timeout=60)

    def discover():
        main(["--profile", args.profile, "companies", "discover"])

    try:
        # Mutual exclusion with the legacy cycle: hold its lock for our lifetime.
        with cycle_lock(engine) as acquired:
            if not acquired:
                print("a legacy cycle or another worker holds the pipeline lock; exiting", file=sys.stderr)
                return 75
            worker = Worker(engine, config, settings_loader=lambda: load_settings(args.profile),
                            telegram=telegram, profile_path=args.profile,
                            discover=discover if args.discover else None,
                            heartbeat=record_progress)
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: worker.stop.set())
            worker.run(duration=args.duration)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    finally:
        engine.dispose()
    import threading
    if any(t.is_alive() and not t.daemon for t in threading.enumerate() if t is not threading.main_thread()):
        # A collection still finishing an HTTP page would hold the interpreter
        # open past docker's grace period. Leases are already released and any
        # open transaction rolls back when the connection drops.
        logging.shutdown()
        sys.stdout.flush()
        os._exit(0)
    return 0


def cmd_metrics(args) -> int:
    from jobradar.metrics import snapshot
    with make_engine().connect() as conn:
        print(json.dumps(snapshot(conn), indent=2, default=str, sort_keys=True))
    return 0


def cmd_requeue(args) -> int:
    """Explicitly re-run a stage for some jobs, or make boards due now."""
    from jobradar import queue
    with make_engine().begin() as conn:
        if args.stage == "boards":
            n = queue.requeue_boards(conn, args.targets or None)
            print(f"{n} board(s) due now")
            return 0
        if args.targets:
            ids = [int(t) for t in args.targets]
        elif args.stage == "filter":
            ids = conn.execute(select(jobs.c.id).where(jobs.c.closed_at.is_(None),
                jobs.c.filtered_at.is_(None))).scalars().all()
        else:
            ids = conn.execute(select(jobs.c.id).where(jobs.c.closed_at.is_(None),
                jobs.c.status.in_(["DISCOVERED", "REPOSTED"]), jobs.c.filtered_at.is_not(None))).scalars().all()
        n = queue.enqueue(conn, args.stage, ids, priority=50)
    print(f"{n} {args.stage} task(s) queued")
    return 0


def cmd_runs(args) -> int:
    with make_engine().connect() as conn:
        rows = conn.execute(
            select(collector_runs).order_by(collector_runs.c.id.desc()).limit(args.limit)
        ).all()
    for r in rows:
        status = "ok " if r.ok else "ERR"
        print(f"{r.started_at:%Y-%m-%d %H:%M:%S} {status} {r.source}/{r.board} "
              f"fetched={r.fetched} new={r.new} closed={r.closed}"
              + (f"  {r.error}" if r.error else ""))
    return 0


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def cmd_cycle(args) -> int:
    from jobradar.cycle import cycle_lock, run_cycle

    if args.digest and not args.notify:
        print("--digest requires --notify", file=sys.stderr)
        return 2
    engine = make_engine()
    try:
        with cycle_lock(engine) as acquired:
            if not acquired:
                print("another cycle is running; skipped")
                return 0
            return run_cycle(args, main)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobradar")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--profile", default="profile.toml", help="your settings file (default: profile.toml)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create or upgrade the database schema").set_defaults(func=cmd_init_db)

    p = sub.add_parser("worker", help="run the continuous multi-worker pipeline (until SIGTERM)")
    p.add_argument("--roles", default="schedule,collect,filter,match,notify")
    p.add_argument("--collect-concurrency", type=positive_int, default=4)
    p.add_argument("--ai-daily-budget-usd", type=float,
                   help="pause matching (jobs stay queued) once today's UTC spend reaches this; "
                        "default: $JOBRADAR_AI_DAILY_BUDGET_USD, else uncapped")
    p.add_argument("--base-interval-minutes", type=positive_int, default=80,
                   help="healthy board revisit interval, multiplied by its priority "
                        "(80 x priority 3 = every 4 h, sized to the 1 GB VM)")
    p.add_argument("--byte-budget-mb", type=positive_int, default=64)
    p.add_argument("--discover", action="store_true", help="refresh the board directory hourly (daily-cached)")
    p.add_argument("--duration", type=float, help="stop after this many seconds (trials)")
    p.set_defaults(func=cmd_worker)

    p = sub.add_parser("metrics", help="queue depth/age, failures, board coverage, alert latency (JSON)")
    p.set_defaults(func=cmd_metrics)

    p = sub.add_parser("requeue", help="explicitly queue jobs for a stage, or make boards due now")
    p.add_argument("stage", choices=["filter", "match", "boards"])
    p.add_argument("targets", nargs="*", help="job ids (filter/match) or board/company names (boards)")
    p.set_defaults(func=cmd_requeue)

    p = sub.add_parser("cycle", help="run one scheduled collection/filter cycle, then exit")
    p.add_argument("--applications", choices=["none", "auto", "notion", "gmail", "all"], default="none")
    p.add_argument("--inbox-limit", type=positive_int, default=200)
    p.add_argument("--match", action="store_true", help="enable paid AI matching")
    p.add_argument("--match-limit", type=positive_int, default=25)
    p.add_argument("--collect-limit", type=positive_int, help="rotate through this many least-recently polled boards")
    p.add_argument("--discover", action="store_true", help="refresh the public board directory at most daily")
    p.add_argument("--notify", action="store_true", help="enable Telegram delivery")
    p.add_argument("--digest", action="store_true", help="include a digest on this cycle")
    p.set_defaults(func=cmd_cycle)

    companies = sub.add_parser("companies", help="manage the company registry")
    csub = companies.add_subparsers(dest="companies_command", required=True)
    p = csub.add_parser("sync", help="make the registry match a companies.toml file")
    p.add_argument("file", nargs="?", default="companies.toml")
    p.add_argument("--dry-run", action="store_true", help="show changes without saving them")
    p.set_defaults(func=cmd_companies_sync)
    p = csub.add_parser("list", help="show registered companies")
    p.add_argument("--all", action="store_true", help="include disabled companies")
    p.set_defaults(func=cmd_companies_list)
    p = csub.add_parser("discover", help="expand the registry from public ATS board addresses")
    p.add_argument("--file", default="companies.toml")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_companies_discover)

    p = sub.add_parser("collect", help="poll registered companies once")
    p.add_argument("companies", nargs="*", help="board or company names (default: all enabled)")
    p.add_argument("--limit", type=positive_int, help="poll the least-recently attempted boards first")
    p.add_argument("--priority", type=int, choices=[1, 2, 3],
                   help="only companies at this priority or more important (1 = highest)")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("filter", help="apply the hard filters to newly discovered jobs")
    p.add_argument("--recheck", action="store_true",
                   help="re-evaluate already-filtered jobs too (after editing [filters])")
    p.set_defaults(func=cmd_filter)

    profile = sub.add_parser("profile", help="your candidate profile (from your resume)")
    psub = profile.add_subparsers(dest="profile_command", required=True)
    p = psub.add_parser("extract", help="read your resume and save a profile with the AI")
    p.add_argument("resume", nargs="?", help="PDF or text file (default: [preferences].resume)")
    p.add_argument("--force", action="store_true", help="re-extract even if the resume is unchanged")
    p.set_defaults(func=cmd_profile_extract)
    p = psub.add_parser("show", help="show the active profile and your preferences")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_profile_show)

    p = sub.add_parser("match", help="AI-score jobs that passed the hard filters")
    p.add_argument("--limit", type=int, default=25, help="at most this many jobs per run (cost control)")
    p.add_argument("--rematch", action="store_true",
                   help="also revisit matched jobs (after changing preferences or profile)")
    p.set_defaults(func=cmd_match)

    p = sub.add_parser("top", help="best matches first")
    p.add_argument("--min-score", type=float, default=70)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_top)

    p = sub.add_parser("sponsorship-review",
                       help="roles held because the posting neither offers nor refuses visa sponsorship")
    p.add_argument("--limit", type=positive_int, default=50)
    p.add_argument("--keep", type=int, nargs="+", metavar="ID",
                   help="keep: AI-score this role and alert on a good match")
    p.add_argument("--dismiss", type=int, nargs="+", metavar="ID", help="dismiss (stays dismissed if reposted)")
    p.set_defaults(func=cmd_sponsorship_review)

    p = sub.add_parser("notify", help="send Telegram alerts for new matches and apply button presses")
    p.add_argument("--digest", action="store_true", help="also send the digest of lower-scoring matches")
    p.add_argument("--resend-stuck", action="store_true",
                   help="re-queue notifications whose delivery is unknown (may duplicate)")
    p.set_defaults(func=cmd_notify)

    telegram = sub.add_parser("telegram", help="Telegram setup and live button handling")
    tsub = telegram.add_subparsers(dest="telegram_command", required=True)
    tsub.add_parser("setup-menu", help="configure Telegram's job navigation menu").set_defaults(func=cmd_telegram_menu)
    p = tsub.add_parser("setup", help="show the bot and the chat ids that messaged it")
    p.add_argument("--test", action="store_true", help="send a test message to TELEGRAM_CHAT_ID")
    p.set_defaults(func=cmd_telegram_setup)
    tsub.add_parser("listen", help="apply button presses as they happen").set_defaults(func=cmd_telegram_listen)
    tsub.add_parser("refresh-buttons", help="restore review buttons on existing alerts").set_defaults(func=cmd_telegram_refresh_buttons)

    apps = sub.add_parser("applications", help="jobs you've applied to (from your inbox)")
    asub = apps.add_subparsers(dest="applications_command", required=True)
    p = asub.add_parser("sync", help="import applications; prefer Notion to avoid inbox AI costs")
    p.add_argument("--source", choices=["auto", "notion", "gmail", "all"], default="auto",
                   help="auto: Notion when configured, otherwise Gmail; all: Notion then Gmail")
    p.add_argument("--days", type=positive_int, default=180, help="how far back to look in your inbox")
    p.add_argument("--limit", type=positive_int, default=200, help="maximum new emails per run (default: 200)")
    p.add_argument("--full", action="store_true", help="re-read all Notion rows instead of only edited rows")
    p.set_defaults(func=cmd_applications_sync)
    p = asub.add_parser("list", help="applications found so far")
    p.add_argument("--unmatched", action="store_true", help="only those not linked to a job, with suggestions")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_applications_list)

    p = sub.add_parser("applied", help="mark jobs as applied by id (ids are shown by `top`)")
    p.add_argument("job_ids", type=int, nargs="+")
    p.set_defaults(func=cmd_applied)

    p = sub.add_parser("new", help="list newly discovered canonical jobs")
    p.add_argument("--hours", type=float, default=24)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("runs", help="recent collector runs")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_runs)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
