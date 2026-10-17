"""Image format, reproducible builds and release manifest integrity."""

import json

import agent
import pytest

from fleetload import image as images


def test_round_trip(sensor_image):
    parsed = images.parse(sensor_image.raw)
    assert (parsed.component, parsed.version) == ("sensor-head", "2.4.2")
    assert parsed.hw_compat == ["A", "B"]


def test_build_is_reproducible(tmp_path):
    from conftest import ROOT
    first = images.build_from_source(ROOT / "firmware" / "sensor-head")
    second = images.build_from_source(ROOT / "firmware" / "sensor-head")
    assert first.sha256 == second.sha256


def test_version_override_changes_checksum(sensor_image):
    from conftest import ROOT
    other = images.build_from_source(ROOT / "firmware" / "sensor-head", version="2.4.3")
    assert other.version == "2.4.3" and other.sha256 != sensor_image.sha256


@pytest.mark.parametrize("mutate, message", [
    (lambda raw: raw[:-1] + bytes([raw[-1] ^ 1]), "payload checksum mismatch"),
    (lambda raw: raw[:-1], "payload size mismatch"),
    (lambda raw: raw.replace(images.SEPARATOR, b"\n"), "missing header separator"),
    (lambda raw: b"{not json" + raw[raw.index(images.SEPARATOR):], "unreadable header"),
])
def test_damaged_images_rejected(sensor_image, mutate, message):
    with pytest.raises(images.ImageError, match=message):
        images.parse(mutate(sensor_image.raw))


def test_device_and_host_agree_on_the_format(sensor_image):
    """The device's parser was written separately from the host's; each must accept the other's images."""
    header, _ = agent.parse_image(sensor_image.raw)
    assert header["version"] == "2.4.2"
    factory = images.parse(agent.factory_image("gimbal-ctrl", "1.0.0"))
    assert factory.component == "gimbal-ctrl"


def test_release_refuses_tampered_file(tmp_path, sensor_image):
    images.publish(sensor_image, tmp_path)
    assert images.load_release(tmp_path, "sensor-head", "2.4.2").sha256 == sensor_image.sha256
    path = tmp_path / sensor_image.filename
    path.write_bytes(path.read_bytes() + b"\x00")
    with pytest.raises(images.ImageError, match="does not match its manifest"):
        images.load_release(tmp_path, "sensor-head", "2.4.2")


def test_release_refuses_unlisted_version(tmp_path, sensor_image):
    images.publish(sensor_image, tmp_path)
    with pytest.raises(images.ImageError, match="not in the release manifest"):
        images.load_release(tmp_path, "sensor-head", "9.9.9")


def test_release_refuses_renamed_image(tmp_path, sensor_image, gimbal_image):
    """A file swapped for another valid image (manifest checksum updated) is caught by its header."""
    images.publish(sensor_image, tmp_path)
    manifest = json.loads(images.manifest_path(tmp_path).read_text())
    (tmp_path / sensor_image.filename).write_bytes(gimbal_image.raw)
    manifest["sensor-head:2.4.2"]["sha256"] = gimbal_image.sha256
    images.manifest_path(tmp_path).write_text(json.dumps(manifest))
    with pytest.raises(images.ImageError, match="header says gimbal-ctrl"):
        images.load_release(tmp_path, "sensor-head", "2.4.2")
