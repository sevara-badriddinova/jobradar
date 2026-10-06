"""Store a secret without putting its value in shell history or process arguments.

Usage: python3 deploy/store-secret.py PROJECT_ID SECRET_NAME
Requires an authenticated gcloud CLI and the Secret Manager API enabled.
"""

import getpass
import subprocess
import sys


def main():
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python3 deploy/store-secret.py PROJECT_ID SECRET_NAME")
    project, name = sys.argv[1:]
    base = ["gcloud", "--project", project, "secrets"]
    exists = subprocess.run([*base, "describe", name], capture_output=True).returncode == 0
    value = getpass.getpass(f"Value for {name} (hidden): ").strip()
    if not value:
        raise SystemExit("Empty value; nothing stored")
    command = ([*base, "versions", "add", name, "--data-file=-"] if exists else
               [*base, "create", name, "--replication-policy=automatic", "--data-file=-"])
    subprocess.run(command, input=value.encode(), check=True)


if __name__ == "__main__":
    main()
