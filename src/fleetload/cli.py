"""Command line interface.

    fleetload build firmware/sensor-head --version 2.4.2
    fleetload status
    fleetload deploy sensor-head:2.4.2 gimbal-ctrl:1.3.0 --canary 1 --batch 2
    fleetload rollback sh-03
    fleetload config audit
    fleetload config apply gc-02 [--dry-run]
"""

from __future__ import annotations

import argparse
import getpass
import sys
import uuid
from datetime import datetime

from . import image as images
from .audit import AuditLog
from .client import DeviceClient, DeviceRejected, DeviceUnavailable
from .deploy import FAILED, rollback_device, rollout
from .inventory import config_drift, load_baselines, load_inventory
from .report import write_record


def _run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]


def _table(rows: list[list[str]], header: list[str]) -> None:
    widths = [max(len(str(x)) for x in col) for col in zip(header, *rows)]
    for row in [header, ["-" * w for w in widths], *rows]:
        print("  ".join(str(x).ljust(w) for x, w in zip(row, widths)))


def cmd_build(args) -> int:
    image = images.build_from_source(args.source, args.version)
    path = images.publish(image, args.releases)
    print(f"{image.component} {image.version} -> {path}\n  sha256 {image.sha256}")
    return 0


def cmd_status(args) -> int:
    rows = []
    for spec in load_inventory(args.inventory):
        try:
            s = DeviceClient(spec.url).status()
            other = s["slots"]["b" if s["active_slot"] == "a" else "a"]
            rows.append([spec.name, s["role"], s["hw_rev"], s["state"], s["running_version"], s["active_slot"],
                         other["version"] if other else "empty", s["last_boot"]])
        except DeviceUnavailable as exc:
            rows.append([spec.name, spec.role, spec.hw_rev, "UNREACHABLE", "", "", "", str(exc)[:40]])
    _table(rows, ["device", "role", "hw", "state", "running", "slot", "other slot", "last boot"])
    return 0


def _audit(args, run_id: str) -> AuditLog:
    return AuditLog(f"{args.reports}/audit-{run_id}.jsonl", run_id, args.operator)


def cmd_deploy(args) -> int:
    loads = {}
    for item in args.images:
        component, _, version = item.partition(":")
        loads[component] = images.load_release(args.releases, component, version)
    devices = load_inventory(args.inventory)
    if args.only:
        devices = [d for d in devices if d.name in set(args.only)]
    run_id = _run_id()
    audit = _audit(args, run_id)
    result = rollout(devices, loads, load_baselines(args.baselines), audit, canary=args.canary,
                     batch_size=args.batch, max_failures=args.max_failures, force=args.force)
    _table([[r.device, r.before or "", r.after or "", r.outcome,
             (f"{r.failed_step.name}: {r.failed_step.detail}" if r.failed_step else
              ("drift: " + ", ".join(d["key"] for d in r.drift) if r.drift else ""))[:70]]
            for r in result.results], ["device", "before", "after", "outcome", "detail"])
    if result.halted:
        print(f"\nROLLOUT HALTED: {result.halt_reason}")
    for path in write_record(result, loads, args.reports, run_id, args.operator, args.inventory):
        print(f"record: {path}")
    print(f"audit:  {audit.path}")
    return 0 if result.ok else 1


def cmd_rollback(args) -> int:
    specs = {d.name: d for d in load_inventory(args.inventory)}
    if args.device not in specs:
        print(f"unknown device {args.device}", file=sys.stderr)
        return 2
    run_id = _run_id()
    result = rollback_device(specs[args.device], _audit(args, run_id))
    for step in result.steps:
        print(f"{'ok  ' if step.ok else 'FAIL'} {step.name}: {step.detail}")
    return 1 if result.outcome == FAILED else 0


def cmd_config(args) -> int:
    baselines = load_baselines(args.baselines)
    specs = load_inventory(args.inventory)
    if args.action == "audit":
        drifted = 0
        for spec in specs:
            try:
                drift = config_drift(DeviceClient(spec.url).config(), baselines[spec.role])
            except DeviceUnavailable as exc:
                print(f"{spec.name}: UNREACHABLE ({exc})")
                drifted += 1
                continue
            drifted += bool(drift)
            print(f"{spec.name}: " + ("matches baseline" if not drift else "DRIFT " + "; ".join(
                f"{d['key']} expected {d['expected']} found {d['actual']}" for d in drift)))
        return 1 if drifted else 0
    spec = next((s for s in specs if s.name == args.device), None)
    if spec is None:
        print(f"unknown device {args.device}", file=sys.stderr)
        return 2
    client = DeviceClient(spec.url)
    drift = config_drift(client.config(), baselines[spec.role])
    for d in drift:
        print(f"{d['key']}: {d['actual']} -> {d['expected']}")
    if not drift:
        print("already matches baseline")
    elif args.dry_run:
        print("(dry run: nothing changed)")
    else:
        try:
            client.put_config(baselines[spec.role])
        except DeviceRejected as exc:
            print(f"rejected: {exc}", file=sys.stderr)
            return 1
        AuditLog(f"{args.reports}/audit-{_run_id()}.jsonl", "config", args.operator).record(
            spec.name, "config-apply", True, changes=drift)
        print("baseline applied")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fleetload", description=__doc__.split("\n\n")[0])
    parser.add_argument("-i", "--inventory", default="inventory/fleet.yaml")
    parser.add_argument("--releases", default="releases")
    parser.add_argument("--baselines", default="baselines")
    parser.add_argument("--reports", default="reports")
    parser.add_argument("--operator", default=getpass.getuser())
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("build", help="build a firmware image and add it to the release manifest")
    p.add_argument("source")
    p.add_argument("--version")
    p.set_defaults(fn=cmd_build)

    sub.add_parser("status", help="show every device's running version and slots").set_defaults(fn=cmd_status)

    p = sub.add_parser("deploy", help="load images onto the fleet")
    p.add_argument("images", nargs="+", metavar="COMPONENT:VERSION")
    p.add_argument("--only", nargs="+", metavar="DEVICE", help="limit to these devices")
    p.add_argument("--canary", type=int, default=1)
    p.add_argument("--batch", type=int, default=2)
    p.add_argument("--max-failures", type=int, default=0)
    p.add_argument("--force", action="store_true", help="reload even if already at the target version")
    p.set_defaults(fn=cmd_deploy)

    p = sub.add_parser("rollback", help="boot the device's other slot")
    p.add_argument("device")
    p.set_defaults(fn=cmd_rollback)

    p = sub.add_parser("config", help="audit or apply configuration baselines")
    p.add_argument("action", choices=["audit", "apply"])
    p.add_argument("device", nargs="?")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_config)

    args = parser.parse_args(argv)
    if args.command == "config" and args.action == "apply" and not args.device:
        parser.error("config apply needs a device name")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
