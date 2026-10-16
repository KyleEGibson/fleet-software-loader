"""Firmware image format and reproducible release builds.

An image is a one-line JSON header, a separator and the payload:

    {"component": "sensor-head", "format": 1, "hw_compat": ["A", "B"],
     "payload_sha256": "...", "payload_size": 10240, "version": "2.4.2"}
    ---
    <payload bytes: a tar archive of the component's files>

See docs/image_format.md. Builds are reproducible: the payload tar uses fixed
timestamps, owners and ordering, so the same source always produces the same
SHA-256. That is what lets a release manifest be trusted.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path

import yaml

SEPARATOR = b"\n---\n"
FORMAT_VERSION = 1


class ImageError(ValueError):
    pass


@dataclass(frozen=True)
class Image:
    header: dict
    payload: bytes
    raw: bytes

    @property
    def component(self) -> str:
        return self.header["component"]

    @property
    def version(self) -> str:
        return self.header["version"]

    @property
    def hw_compat(self) -> list[str]:
        return list(self.header["hw_compat"])

    @property
    def sha256(self) -> str:
        """Checksum of the whole image file, as recorded in the release manifest."""
        return hashlib.sha256(self.raw).hexdigest()

    @property
    def filename(self) -> str:
        return f"{self.component}-{self.version}.fwi"


def build(component: str, version: str, payload: bytes, hw_compat: list[str]) -> Image:
    header = {
        "format": FORMAT_VERSION,
        "component": component,
        "version": version,
        "hw_compat": sorted(hw_compat),
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "payload_size": len(payload),
    }
    raw = json.dumps(header, sort_keys=True).encode() + SEPARATOR + payload
    return Image(header, payload, raw)


def parse(raw: bytes) -> Image:
    head, sep, payload = raw.partition(SEPARATOR)
    if not sep:
        raise ImageError("missing header separator")
    try:
        header = json.loads(head.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImageError(f"unreadable header: {exc}") from None
    missing = {"format", "component", "version", "hw_compat", "payload_sha256", "payload_size"} - set(header)
    if missing:
        raise ImageError(f"header missing {', '.join(sorted(missing))}")
    if header["format"] != FORMAT_VERSION:
        raise ImageError(f"unsupported format {header['format']}")
    if len(payload) != header["payload_size"]:
        raise ImageError("payload size mismatch")
    if hashlib.sha256(payload).hexdigest() != header["payload_sha256"]:
        raise ImageError("payload checksum mismatch")
    return Image(header, payload, raw)


def _deterministic_tar(source: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(p for p in source.rglob("*") if p.is_file() and p.name != "firmware.yaml"):
            data = path.read_bytes()
            info = tarfile.TarInfo(path.relative_to(source).as_posix())
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def build_from_source(source: str | Path, version: str | None = None) -> Image:
    """Build an image from firmware/<component>/ (firmware.yaml + payload files)."""
    source = Path(source)
    spec = yaml.safe_load((source / "firmware.yaml").read_text(encoding="utf-8"))
    return build(spec["component"], version or str(spec["version"]), _deterministic_tar(source), spec["hw_compat"])


# ------------------------------------------------------------------ releases


def manifest_path(release_dir: str | Path) -> Path:
    return Path(release_dir) / "manifest.json"


def publish(image: Image, release_dir: str | Path) -> Path:
    """Write the image into the release directory and record it in the manifest."""
    release_dir = Path(release_dir)
    release_dir.mkdir(parents=True, exist_ok=True)
    path = release_dir / image.filename
    path.write_bytes(image.raw)
    manifest = load_manifest(release_dir)
    manifest[f"{image.component}:{image.version}"] = {
        "file": image.filename, "component": image.component, "version": image.version,
        "hw_compat": image.hw_compat, "sha256": image.sha256, "size": len(image.raw),
    }
    manifest_path(release_dir).write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return path


def load_manifest(release_dir: str | Path) -> dict:
    path = manifest_path(release_dir)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def load_release(release_dir: str | Path, component: str, version: str) -> Image:
    """Load an image from a release, refusing it unless it matches the manifest."""
    entry = load_manifest(release_dir).get(f"{component}:{version}")
    if entry is None:
        raise ImageError(f"{component} {version} is not in the release manifest")
    raw = (Path(release_dir) / entry["file"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
        raise ImageError(f"{entry['file']} does not match its manifest checksum")
    image = parse(raw)
    if (image.component, image.version) != (component, version):
        raise ImageError(f"{entry['file']} header says {image.component} {image.version}")
    return image
