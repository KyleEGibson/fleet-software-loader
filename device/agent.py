"""Fake embedded device with A/B slots, a bootloader and a config store.

One of these runs in each container. Image parsing is written separately from
fleetload on purpose, against docs/image_format.md. Stdlib only.

FAULTS env var (comma separated):
  CORRUPT_WRITE  flip a byte in every image written to a slot
  BOOT_FAIL      newly loaded images fail to boot
  CONFIG_DRIFT   start with config that doesn't match the baseline
  SLOW           delay every response past the host's timeout
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SEPARATOR = b"\n---\n"
FORMAT_VERSION = 1
SLOW_DELAY_S = 3.0

DEFAULT_CONFIG = {
    "sensor-head": {"telemetry_rate_hz": 50, "detector_gain": 4, "cooler_setpoint_c": -40.0, "log_level": "info"},
    "gimbal-ctrl": {"telemetry_rate_hz": 100, "max_slew_dps": 60.0, "stow_on_fault": True, "log_level": "info"},
}


class Rejected(Exception):
    """Request refused; maps to an HTTP 4xx status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def parse_image(raw: bytes) -> tuple[dict, bytes]:
    head, sep, payload = raw.partition(SEPARATOR)
    if not sep:
        raise ValueError("missing header separator")
    header = json.loads(head.decode("utf-8"))
    if header.get("format") != FORMAT_VERSION:
        raise ValueError(f"unsupported image format {header.get('format')}")
    if hashlib.sha256(payload).hexdigest() != header.get("payload_sha256"):
        raise ValueError("payload checksum mismatch")
    if len(payload) != header.get("payload_size"):
        raise ValueError("payload size mismatch")
    return header, payload


def factory_image(component: str, version: str) -> bytes:
    payload = f"{component} factory firmware {version}\n".encode()
    header = {
        "format": FORMAT_VERSION, "component": component, "version": version, "hw_compat": ["A", "B"],
        "payload_sha256": hashlib.sha256(payload).hexdigest(), "payload_size": len(payload),
    }
    return json.dumps(header, sort_keys=True).encode() + SEPARATOR + payload


class Device:
    def __init__(self, device_id: str, role: str, hw_rev: str, factory_version: str = "1.0.0",
                 faults: set[str] | None = None, boot_seconds: float = 1.5) -> None:
        if role not in DEFAULT_CONFIG:
            raise ValueError(f"unknown role {role}")
        self.device_id = device_id
        self.role = role
        self.hw_rev = hw_rev
        self.faults = faults or set()
        self.boot_seconds = boot_seconds
        self.lock = threading.Lock()
        self.slots: dict[str, bytes | None] = {"a": factory_image(role, factory_version), "b": None}
        self.active = "a"
        self.previous: str | None = None
        self.pending: str | None = None
        self.trial = False
        self.state = "running"
        self.boot_count = 0
        self.last_boot = "ok"
        self.last_boot_error = ""
        self.config = dict(DEFAULT_CONFIG[role])
        if "CONFIG_DRIFT" in self.faults:
            self.config["log_level"] = "debug"
            self.config["telemetry_rate_hz"] = self.config["telemetry_rate_hz"] // 2

    def _slot_info(self, slot: str) -> dict | None:
        raw = self.slots[slot]
        if raw is None:
            return None
        info = {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
        try:
            header, _ = parse_image(raw)
            info.update(version=header["version"], component=header["component"], valid=True)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            info.update(version=None, component=None, valid=False, error=str(exc))
        return info

    def status(self) -> dict:
        with self.lock:
            active = self._slot_info(self.active)
            return {
                "device_id": self.device_id,
                "role": self.role,
                "hw_rev": self.hw_rev,
                "state": self.state,
                "running_version": active["version"] if active else None,
                "active_slot": self.active,
                "trial": self.trial,
                "slots": {slot: self._slot_info(slot) for slot in self.slots},
                "boot_count": self.boot_count,
                "last_boot": self.last_boot,
                "last_boot_error": self.last_boot_error,
            }

    def _require_running(self) -> None:
        if self.state != "running":
            raise Rejected(409, f"device is {self.state}")

    def write_slot(self, slot: str, data: bytes) -> dict:
        with self.lock:
            self._require_running()
            if slot not in self.slots:
                raise Rejected(404, f"no slot {slot}")
            if slot == self.active:
                raise Rejected(409, f"slot {slot} is running; write the inactive slot")
            if "CORRUPT_WRITE" in self.faults and len(data) > 10:
                data = data[:-5] + bytes([data[-5] ^ 0xFF]) + data[-4:]
            self.slots[slot] = data
            self.pending = None
            return {"slot": slot, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}

    def activate(self, slot: str) -> dict:
        with self.lock:
            self._require_running()
            if self.slots.get(slot) is None:
                raise Rejected(409, f"slot {slot} is empty")
            self.pending = slot
            return {"pending": slot}

    def commit(self) -> dict:
        with self.lock:
            self._require_running()
            self.trial = False
            self.previous = None
            return {"committed": self.active}

    def get_config(self) -> dict:
        with self.lock:
            return dict(self.config)

    def put_config(self, config: dict) -> dict:
        with self.lock:
            self._require_running()
            unknown = set(config) - set(DEFAULT_CONFIG[self.role])
            if unknown:
                raise Rejected(400, f"unknown config keys: {', '.join(sorted(unknown))}")
            self.config = dict(config)
            return dict(self.config)

    def reboot(self) -> dict:
        with self.lock:
            self._require_running()
            self.state = "booting"
        threading.Timer(self.boot_seconds, self._boot).start()
        return {"state": "booting"}

    def _verify_bootable(self, slot: str) -> None:
        header, _ = parse_image(self.slots[slot] or b"")
        if header["component"] != self.role:
            raise ValueError(f"image is for {header['component']}, device is {self.role}")
        if self.hw_rev not in header["hw_compat"]:
            raise ValueError(f"image does not support hardware rev {self.hw_rev}")

    def _boot(self) -> None:
        with self.lock:
            self.boot_count += 1
            self.last_boot, self.last_boot_error = "ok", ""
            if self.trial and self.previous:
                # trial image never committed: revert, like an expired boot counter
                self.active, self.previous, self.trial = self.previous, None, False
                self.last_boot, self.last_boot_error = "reverted", "trial image was not committed"
            elif self.pending and self.pending != self.active:
                try:
                    self._verify_bootable(self.pending)
                    if "BOOT_FAIL" in self.faults:
                        raise ValueError("kernel panic during init (injected fault)")
                    self.previous, self.active, self.trial = self.active, self.pending, True
                except (ValueError, KeyError, json.JSONDecodeError) as exc:
                    self.last_boot, self.last_boot_error = "fallback", str(exc)
            self.pending = None
            self.state = "running"


class Handler(BaseHTTPRequestHandler):
    device: Device  # set on the server class

    def log_message(self, fmt, *args) -> None:  # keep test output quiet
        if os.environ.get("AGENT_LOG"):
            super().log_message(fmt, *args)

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _dispatch(self, method: str) -> None:
        device = self.server.device  # type: ignore[attr-defined]
        if "SLOW" in device.faults:
            time.sleep(SLOW_DELAY_S)
        if device.state == "booting":
            self._body()
            self._send(503, {"error": "device is booting"})
            return
        path = self.path.rstrip("/")
        try:
            if method == "GET" and path == "/status":
                result = device.status()
            elif method == "GET" and path == "/config":
                result = device.get_config()
            elif method == "PUT" and path == "/config":
                result = device.put_config(json.loads(self._body() or b"{}"))
            elif method == "PUT" and path.startswith("/slots/"):
                result = device.write_slot(path.rsplit("/", 1)[1], self._body())
            elif method == "POST" and path == "/activate":
                result = device.activate(json.loads(self._body() or b"{}").get("slot", ""))
            elif method == "POST" and path == "/reboot":
                result = device.reboot()
            elif method == "POST" and path == "/commit":
                result = device.commit()
            else:
                raise Rejected(404, f"no route {method} {path}")
        except Rejected as exc:
            self._send(exc.status, {"error": str(exc)})
        except json.JSONDecodeError:
            self._send(400, {"error": "body is not valid JSON"})
        else:
            self._send(200, result)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_POST(self) -> None:
        self._dispatch("POST")


def make_server(device: Device, host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.device = device  # type: ignore[attr-defined]
    return server


def main() -> None:
    env = os.environ.get
    faults = {f.strip().upper() for f in env("FAULTS", "").split(",") if f.strip()}
    device = Device(env("DEVICE_ID", "dev-01"), env("ROLE", "sensor-head"), env("HW_REV", "A"),
                    env("FACTORY_VERSION", "1.0.0"), faults, float(env("BOOT_SECONDS", "1.5")))
    port = int(env("PORT", "8080"))
    print(f"{device.device_id} ({device.role} rev {device.hw_rev}) on :{port} faults={sorted(faults)}", flush=True)
    make_server(device, port=port).serve_forever()


if __name__ == "__main__":
    main()
