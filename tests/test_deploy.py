"""Load procedure and rollout behaviour, including every injected fault."""

import json

from conftest import fast_client

from fleetload import deploy
from fleetload import image as images
from fleetload.client import DeviceClient


def load(spec, image, baselines, audit, **kw):
    return deploy.load_device(spec, image, baselines.get(spec.role), audit, client=fast_client(spec),
                              boot_timeout=5, **kw)


def test_healthy_device_is_updated_and_committed(fleet, sensor_image, baselines, audit):
    spec = fleet.add("sh-01")
    result = load(spec, sensor_image, baselines, audit)
    assert result.outcome == deploy.UPDATED
    assert (result.before, result.after) == ("1.0.0", "2.4.2")
    assert [s.name for s in result.steps] == ["preflight", "transfer", "reboot", "verify-boot", "commit", "config"]
    status = DeviceClient(spec.url).status()
    assert status["active_slot"] == "b" and status["trial"] is False
    assert len(audit.entries) == 6 and audit.path.read_text().count("\n") == 6


def test_second_load_is_a_no_op(fleet, sensor_image, baselines, audit):
    spec = fleet.add("sh-01")
    load(spec, sensor_image, baselines, audit)
    result = load(spec, sensor_image, baselines, audit)
    assert result.outcome == deploy.CURRENT
    assert DeviceClient(spec.url).status()["boot_count"] == 1  # not rebooted again


def test_force_reloads(fleet, sensor_image, baselines, audit):
    spec = fleet.add("sh-01")
    load(spec, sensor_image, baselines, audit)
    assert load(spec, sensor_image, baselines, audit, force=True).outcome == deploy.UPDATED


def test_corrupted_transfer_is_retried_then_refused(fleet, sensor_image, baselines, audit):
    spec = fleet.add("sh-01", faults="CORRUPT_WRITE")
    result = load(spec, sensor_image, baselines, audit)
    assert result.outcome == deploy.FAILED
    assert result.failed_step.name == "transfer" and "after 2 attempts" in result.failed_step.detail
    assert sum(e["step"] == "transfer-checksum" for e in audit.entries) == 2
    status = DeviceClient(spec.url).status()
    assert status["running_version"] == "1.0.0" and status["boot_count"] == 0  # never rebooted


def test_boot_failure_detected_and_device_left_on_old_image(fleet, sensor_image, baselines, audit):
    spec = fleet.add("sh-01", faults="BOOT_FAIL")
    result = load(spec, sensor_image, baselines, audit)
    assert result.outcome == deploy.FAILED
    assert result.failed_step.name == "verify-boot"
    assert "fell back to 1.0.0" in result.failed_step.detail
    assert result.after == "1.0.0"


def test_incompatible_hardware_is_not_touched(fleet, baselines, audit):
    image = images.build("sensor-head", "2.5.0", b"rev B only", ["B"])
    spec = fleet.add("sh-01", hw_rev="A")
    result = load(spec, image, baselines, audit)
    assert result.outcome == deploy.INCOMPATIBLE
    assert DeviceClient(spec.url).status()["slots"]["b"] is None


def test_unreachable_device_fails_preflight(fleet, sensor_image, baselines, audit):
    spec = fleet.add("sh-01", faults="SLOW")
    result = load(spec, sensor_image, baselines, audit)
    assert result.outcome == deploy.FAILED
    assert result.failed_step.name == "preflight" and "unavailable" in result.failed_step.detail


def test_config_drift_reported(fleet, gimbal_image, baselines, audit):
    spec = fleet.add("gc-01", role="gimbal-ctrl", faults="CONFIG_DRIFT")
    result = load(spec, gimbal_image, baselines, audit)
    assert result.outcome == deploy.UPDATED
    assert {d["key"] for d in result.drift} == {"log_level", "telemetry_rate_hz"}


def test_rollback_restores_previous_image(fleet, sensor_image, baselines, audit):
    spec = fleet.add("sh-01")
    load(spec, sensor_image, baselines, audit)
    result = deploy.rollback_device(spec, audit, client=fast_client(spec), boot_timeout=5)
    assert result.outcome == deploy.ROLLED_BACK
    assert (result.before, result.after) == ("2.4.2", "1.0.0")


def test_rollback_refused_without_valid_image(fleet, audit):
    spec = fleet.add("sh-01")
    result = deploy.rollback_device(spec, audit, client=fast_client(spec), boot_timeout=5)
    assert result.outcome == deploy.FAILED and "no valid image" in result.failed_step.detail


def run(fleet, images_by_role, baselines, audit, **kw):
    return deploy.rollout(fleet.specs, images_by_role, baselines, audit, client_factory=fast_client,
                          boot_timeout=5, **kw)


def test_rollout_updates_mixed_fleet(fleet, sensor_image, gimbal_image, baselines, audit):
    for name in ("sh-01", "sh-02", "sh-03"):
        fleet.add(name)
    fleet.add("gc-01", role="gimbal-ctrl")
    result = run(fleet, {"sensor-head": sensor_image, "gimbal-ctrl": gimbal_image}, baselines, audit)
    assert result.ok and not result.halted
    assert {r.device: r.after for r in result.results} == {
        "sh-01": "2.4.2", "sh-02": "2.4.2", "sh-03": "2.4.2", "gc-01": "1.3.0"}


def test_canary_failure_halts_everything(fleet, sensor_image, baselines, audit):
    fleet.add("sh-01", faults="BOOT_FAIL")
    fleet.add("sh-02")
    fleet.add("sh-03")
    result = run(fleet, {"sensor-head": sensor_image}, baselines, audit, canary=1)
    assert result.halted and "canary sh-01 failed" in result.halt_reason
    assert [r.outcome for r in result.results] == [deploy.FAILED, deploy.NOT_ATTEMPTED, deploy.NOT_ATTEMPTED]
    assert all(fleet.devices[n].status()["running_version"] == "1.0.0" for n in ("sh-02", "sh-03"))


def test_batch_failure_beyond_limit_halts(fleet, sensor_image, baselines, audit):
    fleet.add("sh-01")
    fleet.add("sh-02", faults="CORRUPT_WRITE")
    fleet.add("sh-03")
    fleet.add("sh-04")
    result = run(fleet, {"sensor-head": sensor_image}, baselines, audit, canary=1, batch_size=2, max_failures=0)
    outcomes = {r.device: r.outcome for r in result.results}
    assert result.halted
    assert outcomes == {"sh-01": deploy.UPDATED, "sh-02": deploy.FAILED,
                        "sh-03": deploy.UPDATED, "sh-04": deploy.NOT_ATTEMPTED}


def test_failure_within_limit_continues(fleet, sensor_image, baselines, audit):
    fleet.add("sh-01")
    fleet.add("sh-02", faults="CORRUPT_WRITE")
    fleet.add("sh-03")
    fleet.add("sh-04")
    result = run(fleet, {"sensor-head": sensor_image}, baselines, audit, canary=1, batch_size=2, max_failures=1)
    assert not result.halted
    assert result.counts() == {deploy.UPDATED: 3, deploy.FAILED: 1}
    assert not result.ok  # a failed device still fails the run


def test_devices_without_an_image_are_left_alone(fleet, sensor_image, baselines, audit):
    fleet.add("sh-01")
    fleet.add("gc-01", role="gimbal-ctrl")
    result = run(fleet, {"sensor-head": sensor_image}, baselines, audit)
    gimbal = next(r for r in result.results if r.device == "gc-01")
    assert gimbal.outcome == deploy.NOT_ATTEMPTED
    assert fleet.devices["gc-01"].status()["boot_count"] == 0


def test_audit_log_is_json_lines(fleet, sensor_image, baselines, audit):
    load(fleet.add("sh-01"), sensor_image, baselines, audit)
    entries = [json.loads(line) for line in audit.path.read_text().splitlines()]
    assert {e["run_id"] for e in entries} == {"test-run"}
    assert all({"time", "device", "step", "ok"} <= set(e) for e in entries)
