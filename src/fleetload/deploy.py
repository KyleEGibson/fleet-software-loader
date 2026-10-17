"""Per-device load steps (preflight, transfer, reboot, verify, commit, config)
and the canary-then-batches rollout across the fleet."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from .audit import AuditLog
from .client import DeviceClient, DeviceRejected, DeviceUnavailable
from .image import Image
from .inventory import DeviceSpec, config_drift

UPDATED = "UPDATED"
CURRENT = "ALREADY CURRENT"
INCOMPATIBLE = "INCOMPATIBLE"
FAILED = "FAILED"
NOT_ATTEMPTED = "NOT ATTEMPTED"
ROLLED_BACK = "ROLLED BACK"


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""
    seconds: float = 0.0


@dataclass
class DeviceResult:
    device: str
    role: str
    hw_rev: str
    target: str | None
    before: str | None = None
    after: str | None = None
    outcome: str = NOT_ATTEMPTED
    steps: list[Step] = field(default_factory=list)
    drift: list[dict] = field(default_factory=list)

    @property
    def failed_step(self) -> Step | None:
        return next((s for s in self.steps if not s.ok), None)

    @property
    def seconds(self) -> float:
        return sum(s.seconds for s in self.steps)

    @property
    def ok(self) -> bool:
        return self.outcome in (UPDATED, CURRENT, ROLLED_BACK)


@dataclass
class RolloutResult:
    results: list[DeviceResult]
    halted: bool
    halt_reason: str = ""

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.results:
            counts[r.outcome] = counts.get(r.outcome, 0) + 1
        return counts

    @property
    def ok(self) -> bool:
        return not self.halted and all(r.ok or r.outcome == INCOMPATIBLE for r in self.results)


class _StepFailed(Exception):
    pass


class _Runner:

    def __init__(self, spec: DeviceSpec, result: DeviceResult, audit: AuditLog) -> None:
        self.spec, self.result, self.audit = spec, result, audit

    def step(self, name: str, fn: Callable[[], str]) -> None:
        start = time.monotonic()
        try:
            detail, ok = fn(), True
        except _StepFailed as exc:
            detail, ok = str(exc), False
        except DeviceUnavailable as exc:
            detail, ok = f"device unavailable: {exc}", False
        except DeviceRejected as exc:
            detail, ok = f"device rejected request: {exc}", False
        seconds = time.monotonic() - start
        self.result.steps.append(Step(name, ok, detail, seconds))
        self.audit.record(self.spec.name, name, ok, detail=detail, seconds=round(seconds, 3))
        if not ok:
            raise _StepFailed(detail)


def _other(slot: str) -> str:
    return "b" if slot == "a" else "a"


def load_device(spec: DeviceSpec, image: Image, baseline: dict | None, audit: AuditLog,
                client: DeviceClient | None = None, force: bool = False,
                transfer_retries: int = 1, boot_timeout: float = 20.0) -> DeviceResult:
    client = client or DeviceClient(spec.url)
    result = DeviceResult(spec.name, spec.role, spec.hw_rev, image.version)
    run = _Runner(spec, result, audit)
    state: dict = {}

    def preflight() -> str:
        status = client.status()
        state["status"] = status
        result.before = result.after = status["running_version"]
        if status["state"] != "running":
            raise _StepFailed(f"device is {status['state']}")
        if status["role"] != image.component:
            raise _StepFailed(f"device role {status['role']} does not match image component {image.component}")
        if status["hw_rev"] not in image.hw_compat:
            state["incompatible"] = True
            raise _StepFailed(f"hardware rev {status['hw_rev']} not supported by {image.version} "
                              f"(supports {', '.join(image.hw_compat)})")
        if status["running_version"] == image.version and not force:
            state["current"] = True
            return f"already running {image.version}"
        state["target_slot"] = _other(status["active_slot"])
        return f"running {status['running_version']} from slot {status['active_slot']}; loading slot {state['target_slot']}"

    def transfer() -> str:
        slot = state["target_slot"]
        for attempt in range(transfer_retries + 1):
            reply = client.write_slot(slot, image.raw)
            if reply["sha256"] == image.sha256:
                return f"{len(image.raw):,} bytes to slot {slot}, SHA-256 verified" + (
                    f" on attempt {attempt + 1}" if attempt else "")
            audit.record(spec.name, "transfer-checksum", False, expected=image.sha256,
                         actual=reply["sha256"], attempt=attempt + 1)
        raise _StepFailed(f"slot {slot} checksum mismatch after {transfer_retries + 1} attempts; "
                          "running image untouched")

    def reboot() -> str:
        client.activate(state["target_slot"])
        client.reboot()
        state["status"] = client.wait_running(boot_timeout)
        return f"back up after reboot (boot #{state['status']['boot_count']})"

    def verify_boot() -> str:
        status = state["status"]
        result.after = status["running_version"]
        if status["last_boot"] == "fallback":
            raise _StepFailed(f"bootloader rejected the new image ({status['last_boot_error']}); "
                              f"device fell back to {status['running_version']}")
        if status["running_version"] != image.version or status["active_slot"] != state["target_slot"]:
            raise _StepFailed(f"expected {image.version} in slot {state['target_slot']}, found "
                              f"{status['running_version']} in slot {status['active_slot']}")
        return f"running {image.version} from slot {status['active_slot']} (trial boot)"

    def commit() -> str:
        client.commit()
        return "trial image committed"

    def check_config() -> str:
        if baseline is None:
            return "no baseline for this role"
        result.drift = config_drift(client.config(), baseline)
        if result.drift:
            keys = ", ".join(d["key"] for d in result.drift)
            return f"drift from baseline: {keys}"
        return "matches baseline"

    try:
        run.step("preflight", preflight)
        if state.get("current"):
            result.outcome = CURRENT
        else:
            run.step("transfer", transfer)
            run.step("reboot", reboot)
            run.step("verify-boot", verify_boot)
            run.step("commit", commit)
            result.outcome = UPDATED
        run.step("config", check_config)
    except _StepFailed:
        result.outcome = INCOMPATIBLE if state.get("incompatible") else FAILED
    return result


def rollback_device(spec: DeviceSpec, audit: AuditLog, client: DeviceClient | None = None,
                    boot_timeout: float = 20.0) -> DeviceResult:
    """Boot the other slot's image and commit it."""
    client = client or DeviceClient(spec.url)
    result = DeviceResult(spec.name, spec.role, spec.hw_rev, target=None)
    run = _Runner(spec, result, audit)
    state: dict = {}

    def preflight() -> str:
        status = client.status()
        result.before = result.after = status["running_version"]
        other = _other(status["active_slot"])
        info = status["slots"].get(other)
        if not info or not info.get("valid"):
            raise _StepFailed(f"slot {other} holds no valid image to roll back to")
        state.update(slot=other, version=info["version"])
        result.target = info["version"]
        return f"rolling back {status['running_version']} -> {info['version']} (slot {other})"

    def reboot() -> str:
        client.activate(state["slot"])
        client.reboot()
        state["status"] = client.wait_running(boot_timeout)
        return "back up after reboot"

    def verify() -> str:
        status = state["status"]
        result.after = status["running_version"]
        if status["running_version"] != state["version"]:
            raise _StepFailed(f"expected {state['version']}, running {status['running_version']}")
        client.commit()
        return f"running {state['version']}, committed"

    try:
        run.step("preflight", preflight)
        run.step("reboot", reboot)
        run.step("verify-boot", verify)
        result.outcome = ROLLED_BACK
    except _StepFailed:
        result.outcome = FAILED
    return result


def rollout(devices: list[DeviceSpec], images: dict[str, Image], baselines: dict[str, dict],
            audit: AuditLog, canary: int = 1, batch_size: int = 2, max_failures: int = 0,
            client_factory: Callable[[DeviceSpec], DeviceClient] = lambda s: DeviceClient(s.url), **load_options) -> RolloutResult:
    results: dict[str, DeviceResult] = {}
    targets = [d for d in devices if d.role in images]
    for d in devices:
        if d.role not in images:
            results[d.name] = DeviceResult(d.name, d.role, d.hw_rev, None, outcome=NOT_ATTEMPTED,
                                           steps=[Step("plan", True, "no image for this role in the release")])

    def work(spec: DeviceSpec) -> DeviceResult:
        return load_device(spec, images[spec.role], baselines.get(spec.role), audit,
                           client=client_factory(spec), **load_options)

    def failures() -> int:
        return sum(r.outcome == FAILED for r in results.values())

    halted, reason = False, ""
    waves = [[d] for d in targets[:canary]]
    rest = targets[canary:]
    waves += [rest[i:i + batch_size] for i in range(0, len(rest), batch_size)]
    for number, wave in enumerate(waves):
        is_canary = number < canary
        with ThreadPoolExecutor(max_workers=len(wave)) as pool:
            for result in pool.map(work, wave):
                results[result.device] = result
        if failures() > max_failures or (is_canary and failures()):
            halted = True
            failed = [r.device for r in results.values() if r.outcome == FAILED]
            reason = (f"canary {wave[0].name} failed" if is_canary
                      else f"{len(failed)} failure(s) exceeded the limit of {max_failures}")
            audit.record("*", "halt", False, reason=reason, failed=failed)
            break

    for spec in targets:
        results.setdefault(spec.name, DeviceResult(spec.name, spec.role, spec.hw_rev, images[spec.role].version))
    ordered = [results[d.name] for d in devices]
    return RolloutResult(ordered, halted, reason)
