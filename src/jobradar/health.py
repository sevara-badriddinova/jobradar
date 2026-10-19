"""Independent Cloud Run watchdog. No database, migrations, or Telegram polling.

Run as a separate scheduled job: python -m jobradar.health. Cloud Storage holds
the incident state so failures of Neon cannot disable or reset alert deduplication.
"""

import html
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx

from jobradar.telegram import Telegram, TelegramError

log = logging.getLogger(__name__)
STATE_OBJECT = "incident.json"


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def diagnosis(executions, now, *, stale_minutes=30, last_progress=None):
    """Three failed completed runs, or no recent successful completed scan."""
    ordered = sorted(executions, key=lambda e: e["createTime"], reverse=True)
    completed = [e for e in ordered if e.get("completionTime")]
    failed = lambda e: int(e.get("succeededCount", 0)) < int(e.get("taskCount", 1))
    if (len(completed) >= 3 and all(failed(e) for e in completed[:3])
            and (not last_progress or timestamp(last_progress) < timestamp(completed[0]["createTime"]))):
        return "The last three completed scans failed. Job notifications may be interrupted."
    # A successful Cloud Run execution can merely mean an overlap was skipped.
    # Only the worker's end-of-pipeline heartbeat proves the app made progress.
    if not last_progress or now - timestamp(last_progress) > timedelta(minutes=stale_minutes):
        return f"No successful scan has completed in the last {stale_minutes} minutes. The worker or scheduler may be stalled."
    return None


def backup_diagnosis(backup, now, *, max_age_hours=36):
    try:
        completed = timestamp(backup["completed_at"])
        valid = backup.get("archive_verified") and int(backup.get("bytes", 0)) > 0
        age = now - completed
        if valid and timedelta(0) <= age <= timedelta(hours=max_age_hours):
            return None
    except (KeyError, TypeError, ValueError):
        pass
    return f"No verified database backup completed in the last {max_age_hours} hours. Check the VM backup service."


class CloudHealth:
    def __init__(self, client, project, region, job, bucket, state_object=STATE_OBJECT):
        self.client, self.bucket = client, bucket
        self.state_object = state_object
        self.parent = f"projects/{project}/locations/{region}/jobs/{job}"
        response = client.get("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
                              headers={"Metadata-Flavor": "Google"}, timeout=5)
        response.raise_for_status()
        self.headers = {"Authorization": "Bearer " + response.json()["access_token"]}

    def executions(self):
        response = self.client.get(f"https://run.googleapis.com/v2/{self.parent}/executions",
                                   params={"pageSize": 20}, headers=self.headers)
        response.raise_for_status()
        return response.json().get("executions", [])

    def read(self, *, state_object=None):
        name = state_object or self.state_object
        url = f"https://storage.googleapis.com/storage/v1/b/{self.bucket}/o/{quote(name, safe='')}"
        meta = self.client.get(url, headers=self.headers)
        if meta.status_code == 404:
            return {}, "0"
        meta.raise_for_status()
        generation = meta.json()["generation"]
        response = self.client.get(url, params={"alt": "media", "generation": generation}, headers=self.headers)
        response.raise_for_status()
        return response.json(), generation

    def write(self, state, generation):
        response = self.client.post(f"https://storage.googleapis.com/upload/storage/v1/b/{self.bucket}/o",
            params={"uploadType": "media", "name": self.state_object, "ifGenerationMatch": generation},
            headers={**self.headers, "Content-Type": "application/json"}, content=json.dumps(state))
        if response.status_code == 412:
            return None  # another monitor claimed this incident
        response.raise_for_status()
        return response.json()["generation"]

    def progress(self):
        return self.read(state_object="progress.json")[0].get("completed_at")


def record_progress():
    """Optional cloud heartbeat, called only after every requested stage finishes."""
    if not os.environ.get("JOBRADAR_HEALTH_BUCKET"):
        return
    with httpx.Client(timeout=15) as client:
        cloud = CloudHealth(client, os.environ["GOOGLE_CLOUD_PROJECT"],
            os.environ.get("JOBRADAR_REGION", "us-east1"),
            os.environ.get("JOBRADAR_POLL_JOB", "jobradar-poll"), os.environ["JOBRADAR_HEALTH_BUCKET"],
            state_object="progress.json")
        _, generation = cloud.read()
        cloud.write({"completed_at": datetime.now(UTC).isoformat()}, generation)


def check(cloud, tg, chat_id, *, now=None, check_backups=False):
    now = now or datetime.now(UTC)
    previous, generation = cloud.read()
    try:
        reason = diagnosis(cloud.executions(), now, last_progress=cloud.progress())
        if check_backups:
            backup_reason = backup_diagnosis(cloud.read(state_object="backup.json")[0], now)
            reason = " ".join(r for r in (reason, backup_reason) if r) or None
    except httpx.HTTPError:
        # A broken observation API must not be reported as healthy.
        reason = "The health monitor cannot read scan status from Google Cloud. App health is unknown."
    state = {"status": "down" if reason else "healthy", "reason": reason,
             "checked_at": now.isoformat()}
    if previous.get("status") == state["status"] and previous.get("reason") == reason:
        log.info("health unchanged: %s", state["status"])
        return 0
    # Commit the claim before Telegram I/O. Conditional writes prevent duplicate
    # messages from overlapping monitor executions, without consulting Neon.
    claimed = cloud.write(state, generation)
    if claimed is None:
        return 0
    if not previous and not reason:
        return 0  # establishing a healthy baseline is quiet
    body = ("🚨 <b>JobRadar needs attention</b>\n" + html.escape(reason) +
            "\nThis check runs independently of the job database. I’ll notify you when scans recover."
            if reason else "✅ <b>JobRadar recovered</b>\nThe scan pipeline and configured backup checks are healthy again.")
    try:
        tg.send_message(chat_id, body, max_attempts=1, http_timeout=10)
    except TelegramError as exc:
        if not exc.maybe_delivered:
            cloud.write(previous, claimed)  # definite failure: retry next check
        # Unknown delivery stays claimed: never spam on a lost HTTP response.
        log.error("health alert delivery failed (%s); delivery uncertain=%s", type(exc).__name__, exc.maybe_delivered)
        return 1
    log.info("health transition delivered: %s", state["status"])
    return 0


def main():
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        with httpx.Client(timeout=15) as client:
            cloud = CloudHealth(client, os.environ["GOOGLE_CLOUD_PROJECT"],
                os.environ.get("JOBRADAR_REGION", "us-east1"),
                os.environ.get("JOBRADAR_POLL_JOB", "jobradar-poll"), os.environ["JOBRADAR_HEALTH_BUCKET"])
            return check(cloud, Telegram(os.environ["TELEGRAM_BOT_TOKEN"], client), os.environ["TELEGRAM_CHAT_ID"],
                         check_backups=os.environ.get("JOBRADAR_CHECK_BACKUPS") == "1")
    except Exception as exc:
        # Never log tokens, connection strings, HTTP headers, or raw responses.
        log.error("health monitor failed: %s", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
