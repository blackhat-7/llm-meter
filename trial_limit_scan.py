#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, date
from pathlib import Path
from typing import Any


CLAUDE_PROVIDER_RE = re.compile(r"provider\((?P<provider>[^,]+),(?P<model>[^:]+):\s*(?P<status>\d+)\)")
SESSION_PERCENT_RE = re.compile(r"current session[^0-9]*(?P<percent>\d{1,3})%", re.IGNORECASE)
WEEKLY_PERCENT_RE = re.compile(r"weekly(?:\s+limits?)?[^0-9]*(?P<percent>\d{1,3})%", re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument("--claude-dir", required=True)
    parser.add_argument("--opencode-dir", required=True)
    return parser.parse_args()


def parse_iso_day(value: str) -> date:
    return date.fromisoformat(value)


def parse_timestamp(value: str | int | float | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000)
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def in_range(moment: datetime | None, start: date, end: date) -> bool:
    if moment is None:
        return False
    return start <= moment.date() <= end


def clean_message(text: str) -> str:
    compact = " ".join(text.split())
    return compact[:400]


def classify_limit(text: str) -> tuple[str | None, int | None]:
    lower = text.lower()
    if "subscription usage cap exceeded" in lower or "usage cap exceeded" in lower:
        return "subscription_cap_exceeded", 402 if "402" in lower else None
    if "limit reached" in lower and "reset" in lower:
        return "limit_reached", None
    if "quota exceeded" in lower:
        return "quota_exceeded", None
    if "temporary restriction" in lower:
        return "rate_limit", None
    if "too many requests" in lower:
        return "rate_limit", 429 if "429" in lower else None
    if re.search(r"\b429\b", lower):
        return "rate_limit", 429
    return None, None


def extract_usage_percent(text: str) -> dict[str, int]:
    values: dict[str, int] = {}
    if match := SESSION_PERCENT_RE.search(text):
        values["current_session"] = int(match.group("percent"))
    if match := WEEKLY_PERCENT_RE.search(text):
        values["weekly"] = int(match.group("percent"))
    return values


def scan_claude(path: Path, start: date, end: date) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not path.exists():
        return events

    for file_path in path.glob("**/*.jsonl"):
        try:
            with file_path.open() as handle:
                for raw in handle:
                    try:
                        entry = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    moment = parse_timestamp(entry.get("timestamp"))
                    if not in_range(moment, start, end):
                        continue

                    if entry.get("isApiErrorMessage"):
                        content = entry.get("message", {}).get("content", [])
                        text = " ".join(part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text")
                        kind, inferred_status = classify_limit(text)
                        if not kind:
                            continue
                        provider = None
                        model = None
                        status = entry.get("apiErrorStatus") or inferred_status
                        if match := CLAUDE_PROVIDER_RE.search(text):
                            provider = match.group("provider")
                            model = match.group("model")
                            status = int(match.group("status"))
                        events.append(
                            {
                                "timestamp": moment.isoformat(),
                                "source": "claude",
                                "kind": kind,
                                "provider": provider,
                                "model": model,
                                "status": status,
                                "message": clean_message(text),
                                "file": str(file_path),
                            }
                        )
                        continue

                    if entry.get("type") == "system":
                        text = str(entry.get("content", ""))
                        kind, status = classify_limit(text)
                        usage = extract_usage_percent(text)
                        if not kind and not usage:
                            continue
                        payload: dict[str, Any] = {
                            "timestamp": moment.isoformat(),
                            "source": "claude",
                            "kind": kind or "usage_sample",
                            "provider": None,
                            "model": None,
                            "status": status,
                            "message": clean_message(text),
                            "file": str(file_path),
                        }
                        payload.update(usage)
                        events.append(payload)
                        continue

                    content = entry.get("message", {}).get("content", [])
                    texts = [part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"]
                    if not texts:
                        continue
                    text = " ".join(texts)
                    if "limit reached" not in text.lower():
                        continue
                    usage = extract_usage_percent(text)
                    events.append(
                        {
                            "timestamp": moment.isoformat(),
                            "source": "claude",
                            "kind": "limit_reached",
                            "provider": None,
                            "model": entry.get("message", {}).get("model"),
                            "status": None,
                            "message": clean_message(text),
                            "file": str(file_path),
                            **usage,
                        }
                    )
        except OSError:
            continue
    return events


def scan_opencode(path: Path, start: date, end: date) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not path.exists():
        return events

    for file_path in path.glob("**/*.json"):
        try:
            entry = json.loads(file_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue

        error = entry.get("error")
        if not error:
            continue

        moment = parse_timestamp(entry.get("time", {}).get("created") or entry.get("time", {}).get("completed"))
        if not in_range(moment, start, end):
            continue

        text = error.get("data", {}).get("message") or error.get("message") or error.get("name") or ""
        kind, status = classify_limit(str(text))
        if not kind:
            continue

        events.append(
            {
                "timestamp": moment.isoformat(),
                "source": "opencode",
                "kind": kind,
                "provider": entry.get("providerID"),
                "model": entry.get("modelID"),
                "status": status,
                "message": clean_message(str(text)),
                "file": str(file_path),
            }
        )
    return events


def main() -> int:
    args = parse_args()
    start = parse_iso_day(args.since)
    end = parse_iso_day(args.until)
    events = scan_claude(Path(args.claude_dir).expanduser(), start, end)
    events.extend(scan_opencode(Path(args.opencode_dir).expanduser(), start, end))
    events.sort(key=lambda item: item["timestamp"])
    print(json.dumps(events))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
