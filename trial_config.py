from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_TIME_VALUE_INR_PER_HOUR = 1300
DEFAULT_ESTIMATED_MINUTES = {
    "limit_hits": 5,
    "forced_switches": 10,
    "context_rebuilds": 7,
    "deferred_tasks": 20,
}


@dataclass(frozen=True)
class Machine:
    name: str
    ssh_target: str | None = None
    tokscale_command: str | None = None
    claude_dir: str = "~/.claude/projects"
    opencode_dir: str = "~/.local/share/opencode/storage/message"


def default_config_path() -> Path:
    return Path.home() / ".config" / "llm-trial" / "config.json"


def default_state_dir() -> Path:
    return Path.home() / ".local" / "state" / "llm-trial"


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def default_config() -> dict[str, Any]:
    return {
        "tokscale_command": "bunx tokscale@latest",
        "daily_capture_phase": "baseline",
        "capture_time_local": "21:00",
        "time_value_inr_per_hour": DEFAULT_TIME_VALUE_INR_PER_HOUR,
        "estimated_minutes": DEFAULT_ESTIMATED_MINUTES,
        "local_paths": {
            "claude": "~/.claude/projects",
            "opencode": "~/.local/share/opencode/storage/message",
        },
        "remotes": [],
    }


def sample_config() -> dict[str, Any]:
    config = default_config()
    config["remotes"] = [
        {
            "name": "pc",
            "ssh_target": "pc",
            "paths": {
                "claude": "~/.claude/projects",
                "opencode": "~/.local/share/opencode/storage/message",
            },
        }
    ]
    return config


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return default_config()

    data = json.loads(path.read_text())
    config = default_config()
    config.update(data)
    config["estimated_minutes"] = {
        **DEFAULT_ESTIMATED_MINUTES,
        **data.get("estimated_minutes", {}),
    }
    config["local_paths"] = {
        **default_config()["local_paths"],
        **data.get("local_paths", {}),
    }
    config["remotes"] = data.get("remotes", [])
    return config


def machine_from_dict(data: dict[str, Any], fallback_paths: dict[str, str]) -> Machine:
    paths = {**fallback_paths, **data.get("paths", {})}
    return Machine(
        name=data["name"],
        ssh_target=data.get("ssh_target"),
        tokscale_command=data.get("tokscale_command"),
        claude_dir=paths["claude"],
        opencode_dir=paths["opencode"],
    )


def parse_remote_specs(specs: list[str], fallback_paths: dict[str, str]) -> list[Machine]:
    machines: list[Machine] = []
    for spec in specs:
        if "=" not in spec:
            raise SystemExit(f"invalid --remote '{spec}', expected name=ssh_target")
        name, ssh_target = spec.split("=", 1)
        machines.append(
            Machine(
                name=name.strip(),
                ssh_target=ssh_target.strip(),
                claude_dir=fallback_paths["claude"],
                opencode_dir=fallback_paths["opencode"],
            )
        )
    return machines


def machines_from_config(config: dict[str, Any], extra_specs: list[str]) -> list[Machine]:
    local_paths = config.get("local_paths", default_config()["local_paths"])
    machines = [
        Machine(
            name="local",
            tokscale_command=config.get("tokscale_command"),
            claude_dir=local_paths["claude"],
            opencode_dir=local_paths["opencode"],
        )
    ]

    for remote in config.get("remotes", []):
        if not remote.get("ssh_target") or not remote.get("name"):
            continue
        machines.append(machine_from_dict(remote, local_paths))

    machines.extend(parse_remote_specs(extra_specs, local_paths))

    deduped: list[Machine] = []
    seen: set[tuple[str, str | None]] = set()
    for machine in machines:
        key = (machine.name, machine.ssh_target)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(machine)
    return deduped
