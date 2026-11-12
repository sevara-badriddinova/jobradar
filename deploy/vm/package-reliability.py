#!/usr/bin/env python3
"""Create an explicit, credential-free release package; no upload/deployment.

    python3 deploy/vm/package-reliability.py DESTINATION [PRODUCTION_SOURCE_REF]

The image overlay holds every file under src/jobradar that changed between the
source of the image running on the VM (PRODUCTION_SOURCE, verified against the
image by cloudshell-release.sh) and HEAD, so no changed module can be left out
(Nov 12, 2026: a hand-kept list missed llm.py and matcher.py). baseline/ holds
the VM's live host files from deploy/vm/live-baseline; the apply script refuses
to run when the VM differs from them.
"""
import hashlib
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_SOURCE = "b40bb90"   # Oct 29, 2026: two-stage scoring, running on jobradar-vm
LIVE_BASELINE = ROOT / "deploy/vm/live-baseline"
BASELINE_FILES = ("compose.yaml", "compose.workers.yaml", "cloud_io.py", "compact-jobs.sh")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True)


def overlay_files(ref):
    if git("status", "--porcelain", "--", "src", "deploy").strip():
        raise SystemExit("src/ or deploy/ has uncommitted changes; package a commit")
    changed = git("diff", "--name-only", "--no-renames", ref, "HEAD", "--", "src/jobradar").split()
    gone = [p for p in changed if not (ROOT / p).exists()]
    if gone:
        raise SystemExit(f"files removed since {ref} cannot be shipped as an overlay: {gone}")
    if any("/migrations/" in p for p in changed):
        raise SystemExit(f"schema migrations changed since {ref}; an overlay cannot ship them")
    return changed


def package(destination, ref=PRODUCTION_SOURCE):
    overlay = overlay_files(ref)
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
    for rel in overlay:
        target = destination / "jobradar" / Path(rel).relative_to("src/jobradar")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, target)
    (destination / "OVERLAY").write_text(f"source {ref} -> {git('rev-parse', 'HEAD').strip()}\n"
                                         + "".join(f"{p}\n" for p in overlay))
    (destination / "baseline").mkdir()
    for name in BASELINE_FILES:
        shutil.copy2(LIVE_BASELINE / name, destination / "baseline" / name)
    manifest = "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(destination)}\n"
                       for p in sorted(destination.rglob("*")) if p.is_file())
    (destination / "SHA256SUMS").write_text(manifest)
    archive = destination.with_suffix(".tar.gz")
    with tarfile.open(archive, "w:gz") as out:
        out.add(destination, arcname=destination.name)
    print(f"Prepared {archive} with {len(overlay)} overlay file(s); "
          "Linux/PG16 validation and deployment have NOT run.")


if __name__ == "__main__":
    package(Path(sys.argv[1]).resolve(), *sys.argv[2:3])
