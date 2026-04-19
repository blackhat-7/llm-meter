#!/usr/bin/env python3

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from trial_config import (
    DEFAULT_ESTIMATED_MINUTES,
    DEFAULT_TIME_VALUE_INR_PER_HOUR,
    default_config_path,
    default_state_dir,
    load_config,
    machines_from_config,
    sample_config,
    save_json as save_config_json,
)
from trial_limits import collect_limit_events
from trial_schedule import install_schedule, uninstall_schedule
from trial_storage import (
    all_phases,
    entries_by_phase,
    load_phase_meta,
    load_state_meta,
    previous_day,
    resolve_phase_bounds,
    save_phase_meta,
    save_state_meta,
    write_capture,
)
from trial_tokscale import summarize_range


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Track LLM usage and real limit friction")
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_config = subparsers.add_parser("init-config", help="Write a sample config file")
    init_config.add_argument("--force", action="store_true", help="Overwrite existing config")

    capture = subparsers.add_parser("capture", help="Capture manual friction notes for a phase")
    capture.add_argument("--phase", default=None, help="Phase name, eg baseline or max")
    capture.add_argument("--non-interactive", action="store_true", help="Skip prompts and store zero manual metrics")
    capture.add_argument("--note", default="", help="Optional note")

    phase_start = subparsers.add_parser("phase-start", help="Start or switch the current phase")
    phase_start.add_argument("phase", help="Phase name, eg baseline or max")
    phase_start.add_argument("--date", default=None, help="Start date YYYY-MM-DD, defaults to today")

    report = subparsers.add_parser("report", help="Show a historical phase summary")
    report.add_argument("--phase", action="append", default=[], help="Only include selected phases")
    report.add_argument("--remote", action="append", default=[], help="Extra remote in name=ssh_target form")
    report.add_argument("--since", default=None, help="Ad-hoc start date YYYY-MM-DD")
    report.add_argument("--until", default=None, help="Ad-hoc end date YYYY-MM-DD")
    report.add_argument("--last", type=int, default=None, help="Ad-hoc last N days")

    install = subparsers.add_parser("install-schedule", help="Install a daily capture scheduler")
    install.add_argument("--time", default=None, help="24-hour local time, eg 21:00")

    subparsers.add_parser("uninstall-schedule", help="Remove the daily capture scheduler")
    return parser.parse_args()


def prompt_with_default(label: str, default: str) -> str:
    prompt = f"{label} [{default}]: " if default else f"{label}: "
    value = input(prompt).strip()
    return value or default


def prompt_manual(existing: dict[str, Any] | None, note: str, non_interactive: bool) -> dict[str, Any]:
    existing = existing or {}
    manual = {
        "limit_hits": int(existing.get("limit_hits", 0)),
        "forced_switches": int(existing.get("forced_switches", 0)),
        "context_rebuilds": int(existing.get("context_rebuilds", 0)),
        "deferred_tasks": int(existing.get("deferred_tasks", 0)),
        "friction_score": int(existing.get("friction_score", 3)),
        "notes": note or existing.get("notes", ""),
    }
    if non_interactive:
        return manual

    print("Only the subjective friction fields are manual now. Usage comes from historical tokscale data.")
    manual["limit_hits"] = int(prompt_with_default("Limit hits", str(manual["limit_hits"])))
    manual["forced_switches"] = int(prompt_with_default("Forced model switches", str(manual["forced_switches"])))
    manual["context_rebuilds"] = int(prompt_with_default("Context rebuilds", str(manual["context_rebuilds"])))
    manual["deferred_tasks"] = int(prompt_with_default("Deferred tasks", str(manual["deferred_tasks"])))
    manual["friction_score"] = int(prompt_with_default("Friction score 1-5", str(manual["friction_score"])))
    manual["notes"] = prompt_with_default("Notes", manual["notes"])
    return manual


def init_config_command(args: argparse.Namespace) -> int:
    if args.config.exists() and not args.force:
        print(f"config already exists: {args.config}")
        return 1
    save_config_json(args.config, sample_config())
    print(f"wrote sample config to {args.config}")
    return 0


def capture_command(args: argparse.Namespace, config: dict[str, Any]) -> int:
    state_meta = load_state_meta(args.state_dir)
    phase = args.phase or state_meta.get("current_phase") or config.get("daily_capture_phase", "baseline")
    existing_entries = entries_by_phase(args.state_dir, [phase]).get(phase, [])
    previous = existing_entries[-1]["manual"] if existing_entries else None
    manual = prompt_manual(previous, args.note, args.non_interactive)
    payload = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "phase": phase,
        "manual": manual,
    }
    path = write_capture(args.state_dir, phase, payload)
    print(f"saved {path}")
    return 0


def phase_start_command(args: argparse.Namespace, config: dict[str, Any]) -> int:
    start_date = args.date or datetime.now().date().isoformat()
    state_meta = load_state_meta(args.state_dir)
    current_phase = state_meta.get("current_phase")

    if current_phase and current_phase != args.phase:
        current_meta = load_phase_meta(args.state_dir, current_phase) or {"phase": current_phase}
        if not current_meta.get("end_date"):
            current_meta["end_date"] = previous_day(start_date)
            save_phase_meta(args.state_dir, current_phase, current_meta)

    phase_meta = load_phase_meta(args.state_dir, args.phase) or {"phase": args.phase}
    if not phase_meta.get("start_date"):
        phase_meta["start_date"] = start_date
    if phase_meta.get("end_date") and phase_meta["end_date"] < start_date:
        phase_meta.pop("end_date", None)
    save_phase_meta(args.state_dir, args.phase, phase_meta)

    save_state_meta(args.state_dir, {"current_phase": args.phase})
    print(f"current phase: {args.phase}")
    print(f"phase start date: {phase_meta['start_date']}")
    if current_phase and current_phase != args.phase:
        print(f"closed {current_phase} at {previous_day(start_date)}")
    return 0


def manual_summary(entries: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    totals = {key: 0 for key in DEFAULT_ESTIMATED_MINUTES}
    friction_scores: list[int] = []
    notes: list[str] = []
    for entry in entries:
        manual = entry["manual"]
        for key in totals:
            totals[key] += int(manual.get(key, 0))
        friction_scores.append(int(manual.get("friction_score", 3)))
        if manual.get("notes"):
            notes.append(str(manual["notes"]))

    weights = config.get("estimated_minutes", DEFAULT_ESTIMATED_MINUTES)
    estimated_minutes = sum(totals[key] * int(weights.get(key, DEFAULT_ESTIMATED_MINUTES[key])) for key in totals)
    rate = float(config.get("time_value_inr_per_hour", DEFAULT_TIME_VALUE_INR_PER_HOUR))
    return {
        "totals": totals,
        "avg_friction_score": (sum(friction_scores) / len(friction_scores)) if friction_scores else None,
        "estimated_minutes": estimated_minutes,
        "estimated_value_inr": estimated_minutes / 60 * rate,
        "notes": notes[-5:],
    }


def fmt_int(value: int | float) -> str:
    return f"{int(value):,}"


def fmt_compact(value: int | float) -> str:
    number = float(value)
    abs_number = abs(number)
    if abs_number >= 1_000_000_000:
        return f"{number / 1_000_000_000:.1f}B"
    if abs_number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if abs_number >= 1_000:
        return f"{number / 1_000:.1f}K"
    return str(int(number))


def format_tokens(row: dict[str, Any]) -> str:
    return (
        f"tokens={fmt_compact(row['tokens'])} "
        f"(in={fmt_compact(row['input'])}, out={fmt_compact(row['output'])}, "
        f"cache_r={fmt_compact(row['cacheRead'])}, cache_w={fmt_compact(row['cacheWrite'])})"
    )


def print_phase_report(phase: str, phase_entries: list[dict[str, Any]], usage: dict[str, Any], limits: dict[str, Any], manual: dict[str, Any]) -> None:
    start = usage["range"]["start"]
    end = usage["range"]["end"]
    totals = usage["totals"]
    print(f"Phase: {phase}")
    print(f"  Range: {start} -> {end} ({len(phase_entries)} manual captures)")
    print(
        "  Historical usage: "
        f"cost=${totals['cost']:.2f} messages={fmt_int(totals['messages'])} "
        f"{format_tokens(totals)}"
    )

    if usage["peak_hour"]:
        peak = usage["peak_hour"]
        print(f"  Peak hour: {peak['hour']} on {peak['machine']} (${peak['cost']:.2f}, {peak['messages']} messages)")

    provider_text = ", ".join(
        f"{row['provider']} ${row['cost']:.2f} ({fmt_compact(row['tokens'])} tok)"
        for row in usage["providers"][:4]
    )
    if provider_text:
        print(f"  Top providers: {provider_text}")

    limit_summary = limits["summary"]
    print(
        "  Limit events: "
        f"{limit_summary['total']} total, usage samples={limit_summary['usage_samples']}"
    )
    if limit_summary["by_kind"]:
        print("  By kind: " + ", ".join(f"{row['kind']}={row['count']}" for row in limit_summary["by_kind"]))
    if limit_summary["by_provider"]:
        print("  By provider: " + ", ".join(f"{row['provider']}={row['count']}" for row in limit_summary["by_provider"][:5]))

    totals_manual = manual["totals"]
    print(
        "  Manual friction: "
        f"limit_hits={totals_manual['limit_hits']} forced_switches={totals_manual['forced_switches']} "
        f"context_rebuilds={totals_manual['context_rebuilds']} deferred_tasks={totals_manual['deferred_tasks']}"
    )
    if manual["avg_friction_score"] is not None:
        print(f"  Avg friction score: {manual['avg_friction_score']:.2f}/5")
    print(f"  Estimated friction cost: {manual['estimated_minutes']} min ~= INR {manual['estimated_value_inr']:.0f}")

    if manual["notes"]:
        print("  Recent notes:")
        for note in manual["notes"]:
            print(f"    - {note}")

    if usage["errors"]:
        for error in usage["errors"]:
            print(f"  Usage warning: {error['machine']}: {error['error']}")
    if limits["errors"]:
        for error in limits["errors"]:
            print(f"  Limit warning: {error['machine']}: {error['error']}")
    print()


def print_ad_hoc_report(label: str, usage: dict[str, Any], limits: dict[str, Any]) -> None:
    totals = usage["totals"]
    print(f"Range: {label}")
    print(
        "  Historical usage: "
        f"cost=${totals['cost']:.2f} messages={fmt_int(totals['messages'])} "
        f"{format_tokens(totals)}"
    )
    if usage["peak_hour"]:
        peak = usage["peak_hour"]
        print(f"  Peak hour: {peak['hour']} on {peak['machine']} (${peak['cost']:.2f}, {peak['messages']} messages)")
    provider_text = ", ".join(
        f"{row['provider']} ${row['cost']:.2f} ({fmt_compact(row['tokens'])} tok)"
        for row in usage["providers"][:4]
    )
    if provider_text:
        print(f"  Top providers: {provider_text}")
    limit_summary = limits["summary"]
    print(f"  Limit events: {limit_summary['total']} total, usage samples={limit_summary['usage_samples']}")
    if limit_summary["by_kind"]:
        print("  By kind: " + ", ".join(f"{row['kind']}={row['count']}" for row in limit_summary["by_kind"]))
    if limit_summary["by_provider"]:
        print("  By provider: " + ", ".join(f"{row['provider']}={row['count']}" for row in limit_summary["by_provider"][:5]))
    print()


def resolve_ad_hoc_range(args: argparse.Namespace) -> tuple[str, str] | None:
    if args.last:
        until = datetime.now().date().isoformat()
        since = (datetime.now() - timedelta(days=args.last - 1)).date().isoformat()
        return since, until
    if args.since or args.until:
        return args.since or datetime.now().date().isoformat(), args.until or datetime.now().date().isoformat()
    return None


def print_comparison(results: dict[str, dict[str, Any]]) -> None:
    if "baseline" not in results or "max" not in results:
        return
    baseline = results["baseline"]
    max_phase = results["max"]

    baseline_days = max(1, len(baseline["usage"]["days"]))
    max_days = max(1, len(max_phase["usage"]["days"]))
    baseline_limits = baseline["limits"]["summary"]["total"] / baseline_days
    max_limits = max_phase["limits"]["summary"]["total"] / max_days
    baseline_minutes = baseline["manual"]["estimated_minutes"] / max(1, len(baseline["entries"]))
    max_minutes = max_phase["manual"]["estimated_minutes"] / max(1, len(max_phase["entries"]))

    print("Comparison: baseline vs max")
    print(f"  Limit events/day: {baseline_limits:.2f} -> {max_limits:.2f} ({baseline_limits - max_limits:+.2f})")
    print(f"  Estimated friction min/capture: {baseline_minutes:.1f} -> {max_minutes:.1f} ({baseline_minutes - max_minutes:+.1f})")

    baseline_score = baseline["manual"]["avg_friction_score"]
    max_score = max_phase["manual"]["avg_friction_score"]
    if baseline_score is not None and max_score is not None:
        print(f"  Avg friction score: {baseline_score:.2f} -> {max_score:.2f} ({max_score - baseline_score:+.2f})")
    print()


def report_command(args: argparse.Namespace, config: dict[str, Any]) -> int:
    machines = machines_from_config(config, args.remote)
    if ad_hoc := resolve_ad_hoc_range(args):
        since, until = ad_hoc
        usage = summarize_range(machines, since, until, config)
        limits = collect_limit_events(machines, since, until)
        print_ad_hoc_report(f"{since} -> {until}", usage, limits)
        return 0

    grouped = entries_by_phase(args.state_dir, args.phase or None)
    requested_phases = sorted(set(args.phase or all_phases(args.state_dir)))
    if not requested_phases:
        print("no phases found")
        return 1

    results: dict[str, dict[str, Any]] = {}
    for phase in requested_phases:
        phase_entries = grouped.get(phase, [])
        since, until = resolve_phase_bounds(args.state_dir, phase, phase_entries)
        if since is None or until is None:
            continue
        usage = summarize_range(machines, since, until, config)
        limits = collect_limit_events(machines, since, until)
        manual = manual_summary(phase_entries, config)
        results[phase] = {"entries": phase_entries, "usage": usage, "limits": limits, "manual": manual}
        print_phase_report(phase, phase_entries, usage, limits, manual)

    if not results:
        print("no phases with usable bounds found")
        return 1
    print_comparison(results)
    return 0


def install_schedule_command(args: argparse.Namespace, config: dict[str, Any], state_dir: Path) -> int:
    capture_time = args.time or config.get("capture_time_local", "21:00")
    path = install_schedule(Path(__file__).resolve().parent, capture_time, state_dir)
    print(f"installed scheduler: {path}")
    print(f"daily capture time: {capture_time}")
    print("scheduled captures follow the current phase")
    return 0


def uninstall_schedule_command() -> int:
    code, message = uninstall_schedule()
    print(message)
    return code


def main() -> int:
    args = parse_args()
    if args.command == "init-config":
        return init_config_command(args)

    config = load_config(args.config)
    if args.command == "capture":
        return capture_command(args, config)
    if args.command == "phase-start":
        return phase_start_command(args, config)
    if args.command == "report":
        return report_command(args, config)
    if args.command == "install-schedule":
        return install_schedule_command(args, config, args.state_dir)
    if args.command == "uninstall-schedule":
        return uninstall_schedule_command()
    raise SystemExit(f"unknown command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
