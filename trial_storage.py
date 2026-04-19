from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def capture_dir(state_dir: Path, phase: str) -> Path:
    return state_dir / phase / "captures"


def write_capture(state_dir: Path, phase: str, payload: dict[str, Any]) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%dT%H%M%S")
    path = capture_dir(state_dir, phase) / f"{stamp}.json"
    save_json(path, payload)
    return path


def capture_paths(state_dir: Path, phases: list[str] | None = None) -> list[Path]:
    phase_dirs = [state_dir / phase for phase in phases] if phases else list(state_dir.glob("*"))
    paths: list[Path] = []
    for phase_dir in phase_dirs:
        capture_root = phase_dir / "captures"
        if not capture_root.exists():
            continue
        paths.extend(sorted(capture_root.glob("*.json")))
    return sorted(paths)


def entries_by_phase(state_dir: Path, phases: list[str] | None = None) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for path in capture_paths(state_dir, phases):
        payload = load_json(path)
        grouped.setdefault(payload["phase"], []).append(payload)
    for entries in grouped.values():
        entries.sort(key=lambda item: item["captured_at"])
    return grouped


def phase_bounds(entries: list[dict[str, Any]]) -> tuple[str, str]:
    start = entries[0]["captured_at"][:10]
    end = entries[-1]["captured_at"][:10]
    return start, end


def phase_meta_path(state_dir: Path, phase: str) -> Path:
    return state_dir / phase / "phase.json"


def load_phase_meta(state_dir: Path, phase: str) -> dict[str, Any] | None:
    path = phase_meta_path(state_dir, phase)
    if not path.exists():
        return None
    return load_json(path)


def save_phase_meta(state_dir: Path, phase: str, payload: dict[str, Any]) -> Path:
    path = phase_meta_path(state_dir, phase)
    save_json(path, payload)
    return path


def state_meta_path(state_dir: Path) -> Path:
    return state_dir / "state.json"


def load_state_meta(state_dir: Path) -> dict[str, Any]:
    path = state_meta_path(state_dir)
    if not path.exists():
        return {}
    return load_json(path)


def save_state_meta(state_dir: Path, payload: dict[str, Any]) -> Path:
    path = state_meta_path(state_dir)
    save_json(path, payload)
    return path


def all_phases(state_dir: Path) -> list[str]:
    if not state_dir.exists():
        return []
    return sorted(path.name for path in state_dir.iterdir() if path.is_dir())


def previous_day(day: str) -> str:
    return (datetime.fromisoformat(day) - timedelta(days=1)).date().isoformat()


def resolve_phase_bounds(state_dir: Path, phase: str, entries: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    meta = load_phase_meta(state_dir, phase)
    if meta and meta.get("start_date"):
        start = meta.get("start_date")
        end = meta.get("end_date") or datetime.now().date().isoformat()
        if start and end and end < start:
            end = start
        return start, end
    if entries:
        return phase_bounds(entries)
    return None, None
