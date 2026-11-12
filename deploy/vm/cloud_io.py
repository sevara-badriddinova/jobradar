"""VM metadata credentials; never print tokens, passwords, or secret values."""
import base64
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from recovery import CONTROL, atomic_json, low_load_backup

PROJECT = "jobradar-510801"
BUCKET = PROJECT + "-jobradar-health"
ROOT = Path("/opt/jobradar")
DATA = Path("/var/lib/jobradar")


def progress(message):
    print(datetime.datetime.now(datetime.UTC).isoformat(), message, flush=True)


@contextmanager
def maintenance_lock():
    # Serialize backups with deploy/compaction, but share the worker lock.
    # A legacy poll's exclusive lock makes backup skip, never stop the app.
    with (DATA / "operations.lock").open("a") as operation, (DATA / "maintenance.lock").open("a") as lock:
        fcntl.flock(operation, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        yield


def file_digests(path):
    md5, sha256 = hashlib.md5(), hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            md5.update(chunk)
            sha256.update(chunk)
    return base64.b64encode(md5.digest()).decode(), sha256.hexdigest()


def upload_file(path, name):
    """Stream an atomic GCS object replacement with bounded memory and retries.

    Retrying the same complete archive at the same daily object name is safe.
    GCS publishes the object only after the entire upload succeeds. Check its
    returned checksum, not just the HTTP status, before recording backup health.
    """
    size = path.stat().st_size
    md5, sha256 = file_digests(path)
    url = f"https://storage.googleapis.com/upload/storage/v1/b/{BUCKET}/o?" + urlencode(
        {"uploadType": "media", "name": name})
    for attempt in range(3):
        try:
            with path.open("rb") as stream:
                req = Request(url, data=stream, headers={
                    "Authorization": "Bearer " + token(),
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(size)}, method="POST")
                with urlopen(req, timeout=180) as response:
                    result = json.load(response)
            if int(result["size"]) != size or result.get("md5Hash") != md5:
                raise ValueError("Uploaded backup checksum or size mismatch")
            return {"object": name, "generation": result["generation"],
                    "bytes": size, "md5": md5, "sha256": sha256}
        except HTTPError as exc:
            if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                raise
        except (URLError, TimeoutError, ConnectionError):
            if attempt == 2:
                raise
        progress(f"Upload interrupted; retry {attempt + 1}/2")
        time.sleep(2 ** (attempt + 1))


def publish_backup_health(state):
    body = json.dumps(state).encode()
    url = f"https://storage.googleapis.com/upload/storage/v1/b/{BUCKET}/o?" + urlencode(
        {"uploadType": "media", "name": "backup.json"})
    req = Request(url, data=body, headers={"Authorization": "Bearer " + token(),
                  "Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=30) as response:
        json.load(response)
    # Cleanup stays disabled until this exact release has a verified remote
    # backup. Reboot clears /run; the next successful backup enables it again.
    atomic_json(CONTROL / "backup.json", state)


def token():
    req = Request("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
                  headers={"Metadata-Flavor": "Google"})
    with urlopen(req, timeout=15) as r:
        return json.load(r)["access_token"]


def numbered(version):
    if not version.isascii() or not version.isdecimal() or version.startswith("0"):
        raise ValueError("Expected a positive numeric secret version")
    return version


def anthropic_version():
    try:
        return numbered((ROOT / "anthropic-secret-version").read_text().strip())
    except FileNotFoundError:
        return "1"  # existing installations


def secret(name, access_token, version="1"):
    req = Request(f"https://secretmanager.googleapis.com/v1/projects/{PROJECT}/secrets/{name}/versions/{numbered(version)}:access",
                  headers={"Authorization": "Bearer " + access_token})
    with urlopen(req, timeout=30) as r:
        return base64.b64decode(json.load(r)["payload"]["data"]).decode()


def secure_write(path, body):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="."+path.name+".")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def prepare_rotated_env(version, destination):
    """Stage only the new key, before stopping any service; never print it."""
    version = numbered(version)
    value = secret("jobradar-anthropic-key", token(), version)
    if not value or not value.isascii() or not all(33 <= ord(c) <= 126 for c in value) or any(c in value for c in "'$\\"):
        raise ValueError("Invalid key format")
    lines = (ROOT / "runtime.env").read_text().splitlines(keepends=True)
    positions = [i for i, line in enumerate(lines) if line.startswith("ANTHROPIC_API_KEY=")]
    if len(positions) != 1:
        raise ValueError("Expected one Anthropic entry in the existing runtime settings")
    # Authenticate without submitting a job, prompt, or inference request.
    req = Request("https://api.anthropic.com/v1/models?limit=1",
                  headers={"x-api-key": value, "anthropic-version": "2023-06-01"})
    with urlopen(req, timeout=20) as response:
        if not json.load(response).get("data"):
            raise ValueError("New credential cannot list models")
    lines[positions[0]] = f"ANTHROPIC_API_KEY='{value}'\n"
    secure_write(destination, "".join(lines))


def configure():
    access_token = token()
    password_file = DATA / "database-password"
    if not password_file.exists():
        secure_write(password_file, secrets.token_hex(32))
    password = password_file.read_text().strip()
    secure_write(ROOT / "postgres.env", f"POSTGRES_USER=jobradar\nPOSTGRES_DB=jobradar\nPOSTGRES_PASSWORD={password}\n")
    env = {
        "JOBRADAR_DATABASE_URL": f"postgresql+psycopg://jobradar:{password}@postgres:5432/jobradar",
        "JOBRADAR_MATCH_MODEL": "claude-haiku-4-5-20251001",
        "GOOGLE_CLOUD_PROJECT": PROJECT,
        "JOBRADAR_REGION": "us-east1",
        "JOBRADAR_POLL_JOB": "jobradar-poll",
        "JOBRADAR_HEALTH_BUCKET": BUCKET,
        # $1 per UTC day, paced hourly (docs/26). It belongs here: configure
        # rewrites runtime.env on every stack start, so a line added by hand
        # is lost (Nov 11, 2026: matching ran uncapped for a day).
        "JOBRADAR_AI_DAILY_BUDGET_USD": "1",
    }
    for key, name in {"ANTHROPIC_API_KEY": "jobradar-anthropic-key",
                      "TELEGRAM_BOT_TOKEN": "jobradar-telegram-token",
                      "TELEGRAM_CHAT_ID": "jobradar-telegram-chat-id"}.items():
        value = secret(name, access_token, anthropic_version() if key == "ANTHROPIC_API_KEY" else "1")
        if any(c in value for c in "\r\n'$"):
            raise ValueError("Unexpected character in secret; refusing ambiguous env file")
        env[key] = value
    secure_write(ROOT / "runtime.env", "".join(f"{k}='{v}'\n" for k, v in env.items()))
    subprocess.run(["docker", "login", "-u", "oauth2accesstoken", "--password-stdin",
                    "us-east1-docker.pkg.dev"], input=access_token.encode(),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    print("Runtime settings refreshed from Secret Manager.")


def backup():
    with maintenance_lock():
        started = time.monotonic()
        path = DATA / "backup.dump"
        partial = DATA / "backup.dump.partial"
        with low_load_backup():
            progress("Online backup started; heavy work drained, Telegram remains available")
            with os.fdopen(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as stream:
                # timeout runs INSIDE the container: killing the Docker client
                # alone would leave pg_dump and its snapshot alive on the server.
                try:
                    subprocess.run(["docker", "compose", "exec", "-T", "postgres",
                                    "timeout", "--kill-after=10s", "5400s", "env",
                                    "PGAPPNAME=jobradar-online-backup",
                                    "PGOPTIONS=-c statement_timeout=5400000", "pg_dump",
                                    "-U", "jobradar", "-d", "jobradar", "-Fc", "-Z1",
                                    "--lock-wait-timeout=30s"], cwd=ROOT, stdout=stream, check=True, timeout=5420)
                except BaseException:
                    # Host cancellation also terminates only this backup's DB
                    # session. The in-container timeout is a second bound if
                    # Docker itself is unresponsive or the host is killed.
                    try:
                        subprocess.run(["docker", "compose", "exec", "-T", "postgres", "psql",
                            "-U", "jobradar", "-d", "jobradar", "-c",
                            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE application_name = 'jobradar-online-backup' AND pid <> pg_backend_pid()"],
                            cwd=ROOT, check=True, timeout=15, stdout=subprocess.DEVNULL)
                    except Exception:
                        progress("Backup cancellation unavailable; in-container deadline remains enforced")
                    raise
                stream.flush()
                os.fsync(stream.fileno())
            progress(f"Dump finished: {partial.stat().st_size} bytes in {time.monotonic()-started:.1f}s")
            with partial.open("rb") as stream:
                subprocess.run(["docker", "compose", "exec", "-T", "postgres",
                                "timeout", "--kill-after=10s", "1800s", "pg_restore",
                                "--file=/dev/null"], cwd=ROOT, stdin=stream, check=True, timeout=1820)
        partial.replace(path)
        progress("Archive stream verified; upload started")
        name = f"vm-backups/day-{datetime.datetime.now(datetime.UTC).weekday()}.dump"
        result = upload_file(path, name)
        result.update(completed_at=datetime.datetime.now(datetime.UTC).isoformat(),
                      duration_seconds=round(time.monotonic()-started, 1), archive_verified=True)
        publish_backup_health(result)
        progress(f"Backup uploaded and checksum verified: {name}, {result['bytes']} bytes, "
                 f"{result['duration_seconds']}s")


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        if sys.argv[1] == "prepare-rotated-env" and len(sys.argv) == 4:
            prepare_rotated_env(sys.argv[2], Path(sys.argv[3]))
        else:
            {"configure": configure, "backup": backup}[sys.argv[1]]()
    except Exception as exc:
        print("VM operation failed:", type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
