"""deploy/vm/workers.sh must never start workers while a legacy cycle runs."""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "vm" / "workers.sh"


def run_wait_poll(tmp_path, states):
    """Run wait_poll() with a fake systemctl reporting `states` in order."""
    (tmp_path / "states").write_text("\n".join(states) + "\n")
    fake = tmp_path / "systemctl"
    fake.write_text(f"""#!/bin/bash
echo "$*" >> {tmp_path}/calls
if [[ $1 == show ]]; then
  s=$(head -n1 {tmp_path}/states); sed -i 1d {tmp_path}/states; echo "${{s:-inactive}}"
fi
""")
    fake.chmod(0o755)
    (tmp_path / "sleep").write_text("#!/bin/bash\necho sleep >> " + str(tmp_path / "calls") + "\n")
    (tmp_path / "sleep").chmod(0o755)
    body = subprocess.run(["sed", "-n", "/^wait_poll()/,/^}/p", str(SCRIPT)],
                          capture_output=True, text=True, check=True).stdout
    assert "wait_poll()" in body
    subprocess.run(["bash", "-c", body + "\nwait_poll"], check=True,
                   env={"PATH": f"{tmp_path}:/usr/bin:/bin"})
    return (tmp_path / "calls").read_text().splitlines()


def test_waits_while_cycle_is_activating(tmp_path):
    calls = run_wait_poll(tmp_path, ["activating", "activating", "inactive"])
    assert calls[0] == "stop jobradar-poll.timer"
    assert calls.count("sleep") == 2          # waited through both "activating" checks


def test_returns_immediately_when_idle(tmp_path):
    calls = run_wait_poll(tmp_path, ["inactive"])
    assert "sleep" not in calls


def test_failed_last_cycle_does_not_block(tmp_path):
    assert "sleep" not in run_wait_poll(tmp_path, ["failed"])
