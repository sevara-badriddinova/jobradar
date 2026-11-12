#!/usr/bin/env python3
"""Create an explicit, credential-free release package; no upload/deployment."""
import hashlib
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def package(destination):
    destination.mkdir(parents=True, exist_ok=False)
    for name in ("src", "tests", "deploy"):
        shutil.copytree(ROOT / name, destination / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.env"))
    for name in ("pyproject.toml", "uv.lock", "README.md", "profile.toml", "companies.toml"):
        shutil.copy2(ROOT / name, destination / name)
    host_files = ("compose.yaml", "compose.workers.yaml", "cloud_io.py", "compact-jobs.sh",
                  "install-workers.sh", "workers.sh", "recovery.py", "apply-reliability.sh",
                  "reliability.Dockerfile", "reliability-test.Dockerfile", "test-reliability.sh")
    for name in host_files:
        shutil.copy2(ROOT / "deploy/vm" / name, destination / name)
    shutil.copy2(ROOT / "src/jobradar/runtime.py", destination / "runtime_control.py")
    (destination / "jobradar").mkdir()
    for name in ("cli.py", "db.py", "worker.py", "runtime.py", "health.py", "prune.py"):
        shutil.copy2(ROOT / "src/jobradar" / name, destination / "jobradar" / name)
    (destination / "baseline").mkdir()
    for name in ("compose.yaml", "compose.workers.yaml", "cloud_io.py", "compact-jobs.sh"):
        old = subprocess.check_output(["git", "show", f"HEAD:deploy/vm/{name}"], cwd=ROOT)
        (destination / "baseline" / name).write_bytes(old)
    manifest = "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(destination)}\n"
                       for p in sorted(destination.rglob("*")) if p.is_file())
    (destination / "SHA256SUMS").write_text(manifest)
    archive = destination.with_suffix(".tar.gz")
    with tarfile.open(archive, "w:gz") as out:
        out.add(destination, arcname=destination.name)
    print(f"Prepared {archive}; Linux/PG16 validation and deployment have NOT run.")


if __name__ == "__main__":
    package(Path(sys.argv[1]).resolve())
