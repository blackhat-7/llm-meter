from __future__ import annotations

import json
import shlex
import subprocess
from collections import defaultdict
from typing import Any

from trial_config import Machine


def run_subprocess(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"command failed with exit code {result.returncode}"
        raise RuntimeError(detail)
    return result.stdout


def candidate_tokscale_commands(machine: Machine, config: dict[str, Any]) -> list[str]:
    configured = machine.tokscale_command or config.get("tokscale_command")
    candidates = [configured] if configured else []
    candidates.extend(
        [
            "bunx tokscale@latest",
            "npx tokscale@latest",
            "$HOME/.nix-profile/bin/bunx tokscale@latest",
            "/nix/var/nix/profiles/default/bin/bunx tokscale@latest",
        ]
    )

    seen: set[str] = set()
    deduped: list[str] = []
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        deduped.append(candidate)
    return deduped


def run_machine_command(machine: Machine, args: list[str], config: dict[str, Any]) -> str:
    errors: list[str] = []
    for tokscale_command in candidate_tokscale_commands(machine, config):
        command = shlex.split(tokscale_command) + args
        if machine.ssh_target:
            remote_command = " ".join(shlex.quote(part) for part in command)
            wrapped = ["ssh", "-o", "BatchMode=yes", machine.ssh_target, f"sh -lc {shlex.quote(remote_command)}"]
        else:
            wrapped = command
        try:
            return run_subprocess(wrapped)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{tokscale_command}: {exc}")
    raise RuntimeError("; ".join(errors))


def fetch_graph(machine: Machine, since: str, until: str, config: dict[str, Any]) -> dict[str, Any]:
    stdout = run_machine_command(
        machine,
        ["graph", "--since", since, "--until", until, "--no-spinner"],
        config,
    )
    return json.loads(stdout)


def fetch_hourly(machine: Machine, since: str, until: str, config: dict[str, Any]) -> dict[str, Any]:
    stdout = run_machine_command(
        machine,
        ["hourly", "--json", "--since", since, "--until", until, "--no-spinner"],
        config,
    )
    return json.loads(stdout)


def empty_totals() -> dict[str, float | int]:
    return {
        "cost": 0.0,
        "tokens": 0,
        "messages": 0,
        "input": 0,
        "output": 0,
        "cacheRead": 0,
        "cacheWrite": 0,
        "reasoning": 0,
    }


def add_totals(bucket: dict[str, float | int], *, cost: float, messages: int, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int, reasoning: int) -> None:
    bucket["cost"] += cost
    bucket["messages"] += messages
    bucket["tokens"] += input_tokens + output_tokens + cache_read + cache_write + reasoning
    bucket["input"] += input_tokens
    bucket["output"] += output_tokens
    bucket["cacheRead"] += cache_read
    bucket["cacheWrite"] += cache_write
    bucket["reasoning"] += reasoning


def summarize_range(machines: list[Machine], since: str, until: str, config: dict[str, Any]) -> dict[str, Any]:
    daily_totals: dict[str, dict[str, float | int]] = defaultdict(empty_totals)
    provider_totals: dict[str, dict[str, float | int]] = defaultdict(empty_totals)
    client_totals: dict[str, dict[str, float | int]] = defaultdict(empty_totals)
    model_totals: dict[tuple[str, str, str], dict[str, float | int]] = defaultdict(empty_totals)
    machine_totals: dict[str, dict[str, float | int]] = defaultdict(empty_totals)
    machine_errors: list[dict[str, str]] = []

    peak_hour: dict[str, Any] | None = None

    for machine in machines:
        try:
            graph = fetch_graph(machine, since, until, config)
            hourly = fetch_hourly(machine, since, until, config)
        except Exception as exc:  # noqa: BLE001
            machine_errors.append({"machine": machine.name, "error": str(exc)})
            continue

        for contribution in graph.get("contributions", []):
            date = contribution["date"]
            totals = contribution.get("totals", {})
            breakdown = contribution.get("tokenBreakdown", {})
            add_totals(
                daily_totals[date],
                cost=float(totals.get("cost", 0.0)),
                messages=int(totals.get("messages", 0)),
                input_tokens=int(breakdown.get("input", 0)),
                output_tokens=int(breakdown.get("output", 0)),
                cache_read=int(breakdown.get("cacheRead", 0)),
                cache_write=int(breakdown.get("cacheWrite", 0)),
                reasoning=int(breakdown.get("reasoning", 0)),
            )
            add_totals(
                machine_totals[machine.name],
                cost=float(totals.get("cost", 0.0)),
                messages=int(totals.get("messages", 0)),
                input_tokens=int(breakdown.get("input", 0)),
                output_tokens=int(breakdown.get("output", 0)),
                cache_read=int(breakdown.get("cacheRead", 0)),
                cache_write=int(breakdown.get("cacheWrite", 0)),
                reasoning=int(breakdown.get("reasoning", 0)),
            )

            for client in contribution.get("clients", []):
                client_name = client["client"]
                provider_name = client["providerId"]
                model_name = client["modelId"]
                tokens = client.get("tokens", {})
                messages = int(client.get("messages", 0))
                cost = float(client.get("cost", 0.0))
                input_tokens = int(tokens.get("input", 0))
                output_tokens = int(tokens.get("output", 0))
                cache_read = int(tokens.get("cacheRead", 0))
                cache_write = int(tokens.get("cacheWrite", 0))
                reasoning = int(tokens.get("reasoning", 0))

                add_totals(
                    provider_totals[provider_name],
                    cost=cost,
                    messages=messages,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read=cache_read,
                    cache_write=cache_write,
                    reasoning=reasoning,
                )
                add_totals(
                    client_totals[client_name],
                    cost=cost,
                    messages=messages,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read=cache_read,
                    cache_write=cache_write,
                    reasoning=reasoning,
                )
                add_totals(
                    model_totals[(client_name, provider_name, model_name)],
                    cost=cost,
                    messages=messages,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read=cache_read,
                    cache_write=cache_write,
                    reasoning=reasoning,
                )

        for entry in hourly.get("entries", []):
            if peak_hour is None or float(entry.get("cost", 0.0)) > float(peak_hour.get("cost", 0.0)):
                peak_hour = {
                    "machine": machine.name,
                    "hour": entry["hour"],
                    "cost": float(entry.get("cost", 0.0)),
                    "messages": int(entry.get("messageCount", 0)),
                }

    totals = empty_totals()
    for day in daily_totals.values():
        add_totals(
            totals,
            cost=float(day["cost"]),
            messages=int(day["messages"]),
            input_tokens=int(day["input"]),
            output_tokens=int(day["output"]),
            cache_read=int(day["cacheRead"]),
            cache_write=int(day["cacheWrite"]),
            reasoning=int(day["reasoning"]),
        )

    return {
        "range": {"start": since, "end": until},
        "totals": totals,
        "days": [
            {"date": date, **values}
            for date, values in sorted(daily_totals.items())
        ],
        "providers": [
            {"provider": name, **values}
            for name, values in sorted(provider_totals.items(), key=lambda item: float(item[1]["cost"]), reverse=True)
        ],
        "clients": [
            {"client": name, **values}
            for name, values in sorted(client_totals.items(), key=lambda item: float(item[1]["cost"]), reverse=True)
        ],
        "models": [
            {"client": key[0], "provider": key[1], "model": key[2], **values}
            for key, values in sorted(model_totals.items(), key=lambda item: float(item[1]["cost"]), reverse=True)
        ],
        "machines": [
            {"machine": name, **values}
            for name, values in sorted(machine_totals.items(), key=lambda item: float(item[1]["cost"]), reverse=True)
        ],
        "peak_hour": peak_hour,
        "errors": machine_errors,
    }
