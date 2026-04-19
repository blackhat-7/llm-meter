from __future__ import annotations

import base64
import json
import shlex
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from trial_config import Machine


SCANNER_FILE = Path(__file__).with_name("trial_limit_scan.py")


def run_subprocess(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"command failed with exit code {result.returncode}"
        raise RuntimeError(detail)
    return result.stdout


def run_scanner(machine: Machine, since: str, until: str) -> list[dict[str, Any]]:
    if machine.ssh_target is None:
        command = [
            sys.executable,
            str(SCANNER_FILE),
            "--since",
            since,
            "--until",
            until,
            "--claude-dir",
            machine.claude_dir,
            "--opencode-dir",
            machine.opencode_dir,
        ]
    else:
        encoded = base64.b64encode(SCANNER_FILE.read_text().encode()).decode()
        bootstrap = (
            "import base64;"
            f"source=base64.b64decode('{encoded}').decode();"
            "exec(compile(source, 'trial_limit_scan.py', 'exec'), {'__name__': '__main__'})"
        )
        remote_command = " ".join(
            [
                "python3",
                "-c",
                shlex.quote(bootstrap),
                "--since",
                shlex.quote(since),
                "--until",
                shlex.quote(until),
                "--claude-dir",
                shlex.quote(machine.claude_dir),
                "--opencode-dir",
                shlex.quote(machine.opencode_dir),
            ]
        )
        command = ["ssh", "-o", "BatchMode=yes", machine.ssh_target, f"sh -lc {shlex.quote(remote_command)}"]

    return json.loads(run_subprocess(command))


def summarize_limit_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    by_kind: dict[str, int] = defaultdict(int)
    by_provider: dict[str, int] = defaultdict(int)
    by_source: dict[str, int] = defaultdict(int)
    usage_samples = 0

    for event in events:
        by_kind[event["kind"]] += 1
        by_source[event["source"]] += 1
        provider = event.get("provider") or "unknown"
        by_provider[provider] += 1
        if event["kind"] == "usage_sample":
            usage_samples += 1

    return {
        "total": len(events),
        "usage_samples": usage_samples,
        "by_kind": [{"kind": kind, "count": count} for kind, count in sorted(by_kind.items(), key=lambda item: (-item[1], item[0]))],
        "by_provider": [
            {"provider": provider, "count": count}
            for provider, count in sorted(by_provider.items(), key=lambda item: (-item[1], item[0]))
        ],
        "by_source": [{"source": source, "count": count} for source, count in sorted(by_source.items(), key=lambda item: (-item[1], item[0]))],
        "latest": events[-5:],
    }


def collect_limit_events(machines: list[Machine], since: str, until: str) -> dict[str, Any]:
    machine_errors: list[dict[str, str]] = []
    events: list[dict[str, Any]] = []
    for machine in machines:
        try:
            rows = run_scanner(machine, since, until)
        except Exception as exc:  # noqa: BLE001
            machine_errors.append({"machine": machine.name, "error": str(exc)})
            continue
        for row in rows:
            row["machine"] = machine.name
            events.append(row)

    events.sort(key=lambda item: item["timestamp"])
    return {
        "events": events,
        "summary": summarize_limit_events(events),
        "errors": machine_errors,
    }
