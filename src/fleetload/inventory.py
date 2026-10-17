"""Fleet inventory and per-role config baselines."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class DeviceSpec:
    name: str
    url: str
    role: str
    hw_rev: str


def load_inventory(path: str | Path) -> list[DeviceSpec]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    devices = [DeviceSpec(d["name"], d["url"], d["role"], str(d["hw_rev"])) for d in data.get("devices", [])]
    names = [d.name for d in devices]
    if len(names) != len(set(names)):
        raise ValueError("duplicate device names in inventory")
    return devices


def load_baselines(directory: str | Path) -> dict[str, dict]:
    return {p.stem: yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted(Path(directory).glob("*.yaml"))}


def config_drift(actual: dict, baseline: dict) -> list[dict]:
    drift = []
    for key in sorted(set(actual) | set(baseline)):
        expected, found = baseline.get(key, "<absent>"), actual.get(key, "<absent>")
        if expected != found:
            drift.append({"key": key, "expected": expected, "actual": found})
    return drift
