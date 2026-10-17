"""Runs real device agents in-process on random ports with short boot times."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "device"))

import agent  # noqa: E402

from fleetload import image as images  # noqa: E402
from fleetload.audit import AuditLog  # noqa: E402
from fleetload.client import DeviceClient  # noqa: E402
from fleetload.inventory import DeviceSpec, load_baselines  # noqa: E402

BOOT_SECONDS = 0.2


class Fleet:
    def __init__(self) -> None:
        self.servers = []
        self.devices: dict[str, agent.Device] = {}
        self.specs: list[DeviceSpec] = []

    def add(self, name: str, role: str = "sensor-head", hw_rev: str = "A", faults: str = "") -> DeviceSpec:
        device = agent.Device(name, role, hw_rev, "1.0.0", {f for f in faults.split(",") if f}, BOOT_SECONDS)
        server = agent.make_server(device, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers.append(server)
        self.devices[name] = device
        spec = DeviceSpec(name, f"http://127.0.0.1:{server.server_address[1]}", role, hw_rev)
        self.specs.append(spec)
        return spec

    def close(self) -> None:
        for server in self.servers:
            server.shutdown()
            server.server_close()


@pytest.fixture
def fleet():
    f = Fleet()
    yield f
    f.close()


@pytest.fixture(scope="session")
def sensor_image():
    return images.build_from_source(ROOT / "firmware" / "sensor-head")


@pytest.fixture(scope="session")
def gimbal_image():
    return images.build_from_source(ROOT / "firmware" / "gimbal-ctrl")


@pytest.fixture(scope="session")
def baselines():
    return load_baselines(ROOT / "baselines")


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl", "test-run", "tester")


def fast_client(spec: DeviceSpec) -> DeviceClient:
    return DeviceClient(spec.url, timeout=0.5)
