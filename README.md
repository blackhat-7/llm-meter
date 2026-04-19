# llm-meter

Tracks whether a higher LLM plan is worth it.

It does 3 things:

- stores small manual friction captures
- reconstructs historical usage from `tokscale` for local + remote machines
- scans Claude Code and OpenCode history for recorded limit events

## Setup

```bash
cd ~/Documents/projects/llm-meter
uv run llm-meter init-config
```

Edit `~/.config/llm-meter/config.json`.

Minimal example:

```json
{
  "tokscale_command": "bunx tokscale@latest",
  "daily_capture_phase": "baseline",
  "capture_time_local": "21:00",
  "time_value_inr_per_hour": 1300,
  "estimated_minutes": {
    "limit_hits": 5,
    "forced_switches": 10,
    "context_rebuilds": 7,
    "deferred_tasks": 20
  },
  "local_paths": {
    "claude": "~/.claude/projects",
    "opencode": "~/.local/share/opencode/storage/message"
  },
  "remotes": [
    {
      "name": "pc",
      "ssh_target": "pc",
      "paths": {
        "claude": "~/.claude/projects",
        "opencode": "~/.local/share/opencode/storage/message"
      }
    }
  ]
}
```

## Commands

Start baseline and backdate it if needed:

```bash
uv run llm-meter phase-start baseline --date 2026-03-20
```

Switch to max later:

```bash
uv run llm-meter phase-start max
```

Manual friction capture for current phase:

```bash
uv run llm-meter capture
```

Zero-input capture:

```bash
uv run llm-meter capture --non-interactive
```

Phase report:

```bash
uv run llm-meter report
```

Last 30 days without phases:

```bash
uv run llm-meter report --last 30
```

Custom range:

```bash
uv run llm-meter report --since 2026-03-20 --until 2026-04-19
```

Scheduler:

```bash
uv run llm-meter install-schedule --time 21:00
uv run llm-meter uninstall-schedule
```

Scheduled captures follow the current phase.

## Daily workflow

Before the trial starts:

1. run `uv run llm-meter phase-start baseline --date YYYY-MM-DD`
2. use `--date` to backdate the start if you want older history included

Normal day:

1. do nothing for usage tracking
2. optionally run `uv run llm-meter capture` once at the end of the day if you want manual friction notes
3. if you installed the scheduler, it can run `capture --non-interactive` automatically

When you want stats:

1. run `uv run llm-meter report` for phase-based comparison
2. run `uv run llm-meter report --last 30` for a rolling month view

When you upgrade from baseline to max:

1. run `uv run llm-meter phase-start max`
2. from that point, new captures go to `max`
3. run `uv run llm-meter report` later to compare `baseline` vs `max`

## Important note on switching mid-day

`tokscale` reporting is day/hour based here, not minute based.

So if you switch plans in the middle of a day, that day cannot be split perfectly between phases.

Best practice:

- switch phases at the start of a day
- if you switch mid-day, start the new phase that day and accept that the day is attributed to the new phase

## Data

- config: `~/.config/llm-meter/config.json`
- phase metadata: `~/.local/state/llm-meter/<phase>/phase.json`
- state: `~/.local/state/llm-meter/state.json`
- captures: `~/.local/state/llm-meter/<phase>/captures/YYYY-MM-DDTHHMMSS.json`

## Data retention

Do not delete these if you want to retain historical usage data:

- `~/.claude/projects`
- `~/.local/share/opencode/storage/message`
- `~/.config/llm-meter`
- `~/.local/state/llm-meter`


## What is detected

- historical usage by day, hour, provider, client, model
- recorded limit events present in Claude/OpenCode local history

## What is not currently detected

- exact live session/weekly usage percentages from Claude/OpenAI UIs
- limit events that never appear in local history
