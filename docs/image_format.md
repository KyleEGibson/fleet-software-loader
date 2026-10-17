# Firmware image format (FWI v1)

Shared by the loader (`src/fleetload/image.py`) and the device bootloader (`device/agent.py`). The two parsers are written separately from this spec.

## Layout
```
<header: one line of UTF-8 JSON, keys sorted>
\n---\n
<payload bytes>
```

| Header key | Type | Meaning |
|---|---|---|
| `format` | int | Always `1` |
| `component` | string | Which device role runs it: `sensor-head`, `gimbal-ctrl` |
| `version` | string | Semantic version, e.g. `2.4.2` |
| `hw_compat` | list | Hardware revisions it supports, e.g. `["A", "B"]` |
| `payload_size` | int | Payload length in bytes |
| `payload_sha256` | hex | SHA-256 of the payload |

The payload is a tar archive of the component's source directory (`firmware/<component>/`, without `firmware.yaml`).

## Integrity checks
| Where | Check | Catches |
|---|---|---|
| Loader, before sending | Whole-file SHA-256 equals the release manifest; header matches the requested component and version | Tampered or swapped release files |
| Device, after writing | Device returns the SHA-256 of the bytes it stored; loader compares | Corruption in transfer or flash write |
| Bootloader, at boot | Payload SHA-256 and size; component equals device role; device hardware rev in `hw_compat` | Corrupt slot, wrong image, unsupported hardware |
| Loader, after boot | Device reports the target version from the new slot and no fallback | Boot failures the device recovered from on its own |

## Reproducibility
Builds are byte-for-byte reproducible: tar entries are sorted, with mtime 0, uid/gid 0 and no owner names, and the header is serialized with sorted keys. The same source gives the same SHA-256 on any machine (line endings are fixed by `.gitattributes`), so a manifest checksum points to exactly one source state.

## Device API (management port 8080)
| Method | Path | Body | Result |
|---|---|---|---|
| GET | `/status` | | state, running version, active slot, both slots (version, SHA-256, valid), boot count, last boot result |
| PUT | `/slots/{a\|b}` | image bytes | `{sha256, size}` of what was stored; 409 if the slot is running |
| POST | `/activate` | `{"slot": "b"}` | marks the slot for the next boot |
| POST | `/reboot` | | device answers 503 until it is running again |
| POST | `/commit` | | makes a trial image permanent |
| GET/PUT | `/config` | JSON | configuration; unknown keys rejected with 400 |

Boot rules: a newly activated slot boots on trial. If the bootloader rejects it, the device stays on the current slot (`last_boot: fallback`). A trial image that is not committed before the next reboot is reverted (`last_boot: reverted`).
