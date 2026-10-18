"""Integration test against the real containerized fleet (docker compose).

    pytest -m docker

Brings the six-device fleet up with the fault overlay, runs a full rollout
through the CLI, then tears the fleet down.
"""

import json
import shutil
import subprocess
import time
import urllib.request

import pytest
from conftest import ROOT

from fleetload import cli

pytestmark = pytest.mark.docker
COMPOSE = ["docker", "compose", "-p", "fleettest", "-f", "docker-compose.yml", "-f", "docker-compose.faults.yml"]


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@pytest.fixture(scope="module")
def docker_fleet():
    if not _docker_available():
        pytest.skip("docker is not available")
    subprocess.run(COMPOSE + ["up", "-d", "--build", "--wait"], cwd=ROOT, check=True, capture_output=True)
    try:
        deadline = time.monotonic() + 60
        for port in range(9001, 9007):
            while True:
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.5)
        yield
    finally:
        subprocess.run(COMPOSE + ["down", "-v"], cwd=ROOT, capture_output=True)


def status(port: int) -> dict:
    return json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=2))


def test_rollout_against_containers(docker_fleet, tmp_path, capsys):
    args = ["-i", str(ROOT / "inventory" / "fleet.yaml"), "--releases", str(tmp_path / "releases"),
            "--baselines", str(ROOT / "baselines"), "--reports", str(tmp_path / "reports")]
    cli.main(args + ["build", str(ROOT / "firmware" / "sensor-head")])
    cli.main(args + ["build", str(ROOT / "firmware" / "gimbal-ctrl")])

    # sh-03 has BOOT_FAIL injected: canary sh-01 passes, batch [sh-02, sh-03] has a failure,
    # so the rollout halts before sh-04, gc-01 and gc-02.
    assert cli.main(args + ["deploy", "sensor-head:2.4.2", "gimbal-ctrl:1.3.0", "--canary", "1", "--batch", "2"]) == 1
    out = capsys.readouterr().out
    assert "ROLLOUT HALTED" in out
    assert status(9001)["running_version"] == "2.4.2"
    assert status(9002)["running_version"] == "2.4.2"
    assert status(9003)["running_version"] == "1.0.0" and status(9003)["last_boot"] == "fallback"
    assert status(9004)["running_version"] == "1.0.0" and status(9004)["boot_count"] == 0

    # exclude the bad unit and finish the rollout; sh-04 corrupts writes, gc-02 has config drift
    rest = ["--only", "sh-04", "gc-01", "gc-02", "--canary", "0", "--batch", "3", "--max-failures", "1"]
    assert cli.main(args + ["deploy", "sensor-head:2.4.2", "gimbal-ctrl:1.3.0", *rest]) == 1
    out = capsys.readouterr().out
    assert "checksum mismatch" in out and "drift: log_level, telemetry_rate_hz" in out
    assert status(9005)["running_version"] == "1.3.0" and status(9006)["running_version"] == "1.3.0"
    assert status(9004)["running_version"] == "1.0.0"
