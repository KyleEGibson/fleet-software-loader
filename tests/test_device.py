"""Behaviour of the simulated device's bootloader and API."""

import time

import pytest

from fleetload.client import DeviceClient, DeviceRejected, DeviceUnavailable


def boot(client: DeviceClient) -> dict:
    client.reboot()
    return client.wait_running(5)


def test_fresh_device_runs_factory_image(fleet):
    status = DeviceClient(fleet.add("d1").url).status()
    assert status["running_version"] == "1.0.0"
    assert status["active_slot"] == "a" and status["slots"]["b"] is None


def test_running_slot_cannot_be_overwritten(fleet, sensor_image):
    client = DeviceClient(fleet.add("d1").url)
    with pytest.raises(DeviceRejected, match="slot a is running"):
        client.write_slot("a", sensor_image.raw)


def test_unreachable_while_booting(fleet):
    client = DeviceClient(fleet.add("d1").url)
    client.reboot()
    with pytest.raises(DeviceUnavailable):
        client.status()
    assert client.wait_running(5)["boot_count"] == 1


def test_bootloader_rejects_wrong_hardware(fleet, sensor_image):
    client = DeviceClient(fleet.add("d1", hw_rev="C").url)
    client.write_slot("b", sensor_image.raw)
    client.activate("b")
    status = boot(client)
    assert status["last_boot"] == "fallback"
    assert "hardware rev C" in status["last_boot_error"]
    assert status["running_version"] == "1.0.0"


def test_bootloader_rejects_wrong_component(fleet, gimbal_image):
    client = DeviceClient(fleet.add("d1", role="sensor-head").url)
    client.write_slot("b", gimbal_image.raw)
    client.activate("b")
    assert "image is for gimbal-ctrl" in boot(client)["last_boot_error"]


def test_uncommitted_trial_image_reverts(fleet, sensor_image):
    """If the host dies after the trial boot, the next reboot restores the old image."""
    client = DeviceClient(fleet.add("d1").url)
    client.write_slot("b", sensor_image.raw)
    client.activate("b")
    assert boot(client)["running_version"] == "2.4.2"
    status = boot(client)  # no commit
    assert status["running_version"] == "1.0.0"
    assert status["last_boot"] == "reverted"


def test_committed_image_survives_reboot(fleet, sensor_image):
    client = DeviceClient(fleet.add("d1").url)
    client.write_slot("b", sensor_image.raw)
    client.activate("b")
    boot(client)
    client.commit()
    assert boot(client)["running_version"] == "2.4.2"


def test_config_rejects_unknown_keys(fleet):
    client = DeviceClient(fleet.add("d1").url)
    with pytest.raises(DeviceRejected, match="unknown config keys: bogus"):
        client.put_config({"bogus": 1})


def test_slow_device_times_out(fleet):
    client = DeviceClient(fleet.add("d1", faults="SLOW").url, timeout=0.3)
    start = time.monotonic()
    with pytest.raises(DeviceUnavailable):
        client.status()
    assert time.monotonic() - start < 2


def test_image_with_bad_payload_never_boots(fleet, sensor_image):
    client = DeviceClient(fleet.add("d1").url)
    raw = bytearray(sensor_image.raw)
    raw[-3] ^= 0xFF
    client.write_slot("b", bytes(raw))
    client.activate("b")
    status = boot(client)
    assert status["last_boot"] == "fallback" and "checksum" in status["last_boot_error"]
    assert status["slots"]["b"]["valid"] is False
