"""Writes the software load record (HTML + Markdown)."""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape

from . import __version__
from .deploy import RolloutResult
from .image import Image

_env = Environment(loader=PackageLoader("fleetload", "templates"), autoescape=select_autoescape(["html", "j2"]))


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "uncommitted"


def write_record(rollout: RolloutResult, images: dict[str, Image], out_dir: str | Path,
                 run_id: str, operator: str, inventory: str) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    context = {
        "rollout": rollout, "images": images, "run_id": run_id, "operator": operator,
        "inventory": inventory, "tool_version": __version__, "commit": git_commit(),
        "generated": datetime.now().isoformat(timespec="seconds"), "counts": rollout.counts(),
        "verdict": "PASS" if rollout.ok else "FAIL",
    }
    html = out_dir / f"load-record-{run_id}.html"
    html.write_text(_env.get_template("record.html.j2").render(**context), encoding="utf-8")
    md = out_dir / f"load-record-{run_id}.md"
    md.write_text(_markdown(context), encoding="utf-8")
    return [html, md]


def _markdown(c: dict) -> str:
    lines = [
        f"# Software Load Record {c['run_id']}: {c['verdict']}",
        "",
        f"Operator **{c['operator']}** · inventory `{c['inventory']}` · fleetload {c['tool_version']} "
        f"(`{c['commit']}`) · {c['generated']}",
        "",
        "| Component | Version | Image SHA-256 |",
        "|---|---|---|",
    ]
    lines += [f"| {i.component} | {i.version} | `{i.sha256}` |" for i in c["images"].values()]
    if c["rollout"].halted:
        lines += ["", f"**Rollout halted:** {c['rollout'].halt_reason}"]
    lines += ["", "| Device | Role | HW | Before | After | Outcome | Detail |", "|---|---|---|---|---|---|---|"]
    for r in c["rollout"].results:
        failed = r.failed_step
        detail = f"{failed.name}: {failed.detail}" if failed else (
            "config drift: " + ", ".join(d["key"] for d in r.drift) if r.drift else "")
        lines.append(f"| {r.device} | {r.role} | {r.hw_rev} | {r.before or '-'} | {r.after or '-'} "
                     f"| {r.outcome} | {detail} |")
    return "\n".join(lines) + "\n"
