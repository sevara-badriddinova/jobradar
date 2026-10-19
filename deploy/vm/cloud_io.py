"""VM metadata credentials; never print tokens, passwords, or secret values."""
import base64
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from contextlib import contextmanager
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PROJECT = "jobradar-510801"
BUCKET = PROJECT + "-jobradar-health"
ROOT = Path("/opt/jobradar")
DATA = Path("/var/lib/jobradar")


def progress(message):
    print(datetime.datetime.now(datetime.UTC).isoformat(), message, flush=True)


@contextmanager
def maintenance_lock():
    # Polling takes this same lock non-blockingly. Backups wait for a running
    # cycle to finish, then keep disk-heavy polling out until upload completes.
    with (DATA / "maintenance.lock").open("a") as lock:
        progress("Waiting for the current polling cycle before backup")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


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


def token():
    req = Request("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
                  headers={"Metadata-Flavor": "Google"})
    with urlopen(req, timeout=15) as r:
        return json.load(r)["access_token"]


def secret(name, access_token):
    req = Request(f"https://secretmanager.googleapis.com/v1/projects/{PROJECT}/secrets/{name}/versions/1:access",
                  headers={"Authorization": "Bearer " + access_token})
    with urlopen(req, timeout=30) as r:
        return base64.b64decode(json.load(r)["payload"]["data"]).decode()


def secure_write(path, body):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(body)
    tmp.replace(path)


def configure():
    access_token = token()
    password_file = Path("/var/lib/jobradar/database-password")
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
    }
    for key, name in {"ANTHROPIC_API_KEY": "jobradar-anthropic-key",
                      "TELEGRAM_BOT_TOKEN": "jobradar-telegram-token",
                      "TELEGRAM_CHAT_ID": "jobradar-telegram-chat-id"}.items():
        value = secret(name, access_token)
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
        progress("Backup dump started (compression level 1)")
        with os.fdopen(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as stream:
            subprocess.run(["docker", "compose", "exec", "-T", "postgres", "pg_dump",
                            "-U", "jobradar", "-d", "jobradar", "-Fc", "-Z1",
                            "--lock-wait-timeout=30s"], cwd=ROOT, stdout=stream, check=True, timeout=2700)
            stream.flush()
            os.fsync(stream.fileno())
        progress(f"Dump finished: {partial.stat().st_size} bytes in {time.monotonic()-started:.1f}s")
        # Read/decompress every archive entry, not just its table of contents.
        with partial.open("rb") as stream:
            subprocess.run(["docker", "compose", "exec", "-T", "postgres", "pg_restore",
                            "--file=/dev/null"], cwd=ROOT, stdin=stream, check=True, timeout=1800)
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
    try:
        {"configure": configure, "backup": backup}[sys.argv[1]]()
    except Exception as exc:
        print("VM operation failed:", type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
