"""Install this checkout's Telegram listener as a macOS login service.

Run with .venv/bin/python deploy/install-telegram-listener.py --with-poller.
Credentials stay in .env; launchd restarts the worker if it exits.
"""

import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time


def install(root: Path, name: str, command: str, *, interval: int | None = None):
    label = f"com.jobradar.{name}"
    logs = root / ".local"
    logs.mkdir(exist_ok=True)
    target = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
    target.parent.mkdir(parents=True, exist_ok=True)
    config = {
        "Label": label,
        "WorkingDirectory": str(root),
        "ProgramArguments": ["/bin/zsh", "-c", "set -ae\nsource .env\n" + command],
        "RunAtLoad": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(logs / f"{name}.log"),
        "StandardErrorPath": str(logs / f"{name}.err.log"),
    }
    if interval is None:
        config["KeepAlive"] = True
    else:
        config["StartInterval"] = interval
    domain = f"gui/{os.getuid()}"
    with target.open("wb") as stream:
        plistlib.dump(config, stream)
    removed = subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
    if removed.returncode == 0:
        # bootout can return before the old worker/service has finished exiting.
        # Bootstrapping immediately can otherwise fail with launchd error 5.
        deadline = time.monotonic() + 15
        while subprocess.run(["launchctl", "print", f"{domain}/{label}"],
                             capture_output=True).returncode == 0:
            if time.monotonic() >= deadline:
                raise SystemExit(f"{label} is still stopping; retry installation shortly.")
            time.sleep(0.25)
    # A previous cloud cutover may have persistently disabled this service.
    # Reinstalling its plist alone does not clear launchd's disabled override.
    subprocess.run(["launchctl", "enable", f"{domain}/{label}"], check=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)
    print(f"Installed {label}; logs: {logs}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-poller", action="store_true",
                        help="also scan, score up to 25 jobs, and notify every 5 minutes")
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise SystemExit("This installer requires macOS launchd.")
    root = Path(__file__).resolve().parents[1]
    if not (root / ".env").is_file() or not (root / ".venv/bin/python").is_file():
        raise SystemExit("Set up .env and .venv before installing the listener.")
    install(root, "telegram-listener", "exec .venv/bin/python -u -m jobradar telegram listen")
    if args.with_poller:
        install(root, "poll", '''match_args=()
if [[ -n "${ANTHROPIC_API_KEY:-}${ANTHROPIC_AUTH_TOKEN:-}" ]]; then
    match_args=(--match --match-limit 25)
else
    echo 'AI scoring paused: add ANTHROPIC_API_KEY to .env. Collecting and sending existing matches.' >&2
fi
exec .venv/bin/python -u -m jobradar cycle --discover --collect-limit 100 --notify --digest "${match_args[@]}"
''', interval=300)


if __name__ == "__main__":
    main()
