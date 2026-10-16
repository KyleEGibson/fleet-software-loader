"""HTTP client for one device's management API."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request


class DeviceUnavailable(Exception):
    """No usable response: connection refused, timeout, or the device is booting."""


class DeviceRejected(Exception):
    """The device answered and refused the request (HTTP 4xx)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class DeviceClient:
    def __init__(self, url: str, timeout: float = 2.0) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, body: bytes | None = None,
                 content_type: str = "application/json", timeout: float | None = None) -> dict:
        request = urllib.request.Request(self.url + path, data=body, method=method,
                                         headers={"Content-Type": content_type})
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            message = json.loads(exc.read() or b"{}").get("error", exc.reason)
            if exc.code == 503:
                raise DeviceUnavailable(message) from None
            raise DeviceRejected(exc.code, message) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
            raise DeviceUnavailable(str(getattr(exc, "reason", exc))) from None

    def status(self) -> dict:
        return self._request("GET", "/status")

    def config(self) -> dict:
        return self._request("GET", "/config")

    def put_config(self, config: dict) -> dict:
        return self._request("PUT", "/config", json.dumps(config).encode())

    def write_slot(self, slot: str, data: bytes) -> dict:
        # transfers take longer than a status query; scale the timeout with size
        return self._request("PUT", f"/slots/{slot}", data, "application/octet-stream",
                             timeout=self.timeout + len(data) / 1_000_000)

    def activate(self, slot: str) -> dict:
        return self._request("POST", "/activate", json.dumps({"slot": slot}).encode())

    def reboot(self) -> dict:
        return self._request("POST", "/reboot", b"")

    def commit(self) -> dict:
        return self._request("POST", "/commit", b"")

    def wait_running(self, timeout: float, poll: float = 0.2) -> dict:
        """Poll until the device answers and reports 'running'; tolerate it being unreachable while booting."""
        deadline = time.monotonic() + timeout
        last_error = "no response"
        while time.monotonic() < deadline:
            try:
                status = self.status()
                if status["state"] == "running":
                    return status
                last_error = f"state {status['state']}"
            except DeviceUnavailable as exc:
                last_error = str(exc)
            time.sleep(poll)
        raise DeviceUnavailable(f"not running after {timeout:g} s ({last_error})")
