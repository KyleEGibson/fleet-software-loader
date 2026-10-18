"""End-to-end through the command line: build, deploy, record, config audit/apply, rollback."""

import yaml
from conftest import ROOT

from fleetload import cli


def write_inventory(tmp_path, fleet):
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.safe_dump({"devices": [
        {"name": s.name, "url": s.url, "role": s.role, "hw_rev": s.hw_rev} for s in fleet.specs]}))
    return str(path)


def common(tmp_path, inventory):
    return ["-i", inventory, "--releases", str(tmp_path / "releases"), "--baselines", str(ROOT / "baselines"),
            "--reports", str(tmp_path / "reports"), "--operator", "tester"]


def test_build_deploy_and_record(tmp_path, fleet, capsys):
    fleet.add("sh-01")
    fleet.add("gc-01", role="gimbal-ctrl", faults="CONFIG_DRIFT")
    args = common(tmp_path, write_inventory(tmp_path, fleet))
    assert cli.main(args + ["build", str(ROOT / "firmware" / "sensor-head")]) == 0
    assert cli.main(args + ["build", str(ROOT / "firmware" / "gimbal-ctrl")]) == 0
    assert cli.main(args + ["deploy", "sensor-head:2.4.2", "gimbal-ctrl:1.3.0"]) == 0
    out = capsys.readouterr().out
    assert "UPDATED" in out and "drift: log_level, telemetry_rate_hz" in out

    record = next((tmp_path / "reports").glob("load-record-*.md")).read_text(encoding="utf-8")
    assert ": PASS" in record and "| sh-01 | sensor-head | A | 1.0.0 | 2.4.2 | UPDATED |" in record
    html = next((tmp_path / "reports").glob("load-record-*.html")).read_text(encoding="utf-8")
    assert "Software Load Record" in html and "telemetry_rate_hz" in html


def test_deploy_exit_code_reflects_failure(tmp_path, fleet):
    fleet.add("sh-01", faults="BOOT_FAIL")
    args = common(tmp_path, write_inventory(tmp_path, fleet))
    cli.main(args + ["build", str(ROOT / "firmware" / "sensor-head")])
    assert cli.main(args + ["deploy", "sensor-head:2.4.2"]) == 1
    record = next((tmp_path / "reports").glob("load-record-*.md")).read_text(encoding="utf-8")
    assert ": FAIL" in record and "Rollout halted" in record


def test_config_audit_and_apply(tmp_path, fleet, capsys):
    fleet.add("gc-01", role="gimbal-ctrl", faults="CONFIG_DRIFT")
    args = common(tmp_path, write_inventory(tmp_path, fleet))
    assert cli.main(args + ["config", "audit"]) == 1
    assert cli.main(args + ["config", "apply", "gc-01", "--dry-run"]) == 0
    assert "dry run" in capsys.readouterr().out
    assert cli.main(args + ["config", "audit"]) == 1          # dry run changed nothing
    assert cli.main(args + ["config", "apply", "gc-01"]) == 0
    assert cli.main(args + ["config", "audit"]) == 0


def test_rollback_command(tmp_path, fleet, capsys):
    fleet.add("sh-01")
    args = common(tmp_path, write_inventory(tmp_path, fleet))
    cli.main(args + ["build", str(ROOT / "firmware" / "sensor-head")])
    cli.main(args + ["deploy", "sensor-head:2.4.2"])
    assert cli.main(args + ["rollback", "sh-01"]) == 0
    assert "running 1.0.0, committed" in capsys.readouterr().out
