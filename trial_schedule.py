from __future__ import annotations

import platform
import shlex
import subprocess
from pathlib import Path


SCHEDULE_LABEL = "dev.illusion.llm-meter"


def scheduler_command(project_dir: Path, state_dir: Path) -> str:
    log_path = state_dir / "scheduler.log"
    return (
        f"cd {shlex.quote(str(project_dir))} && "
        f"uv run llm-meter capture --non-interactive "
        f">> {shlex.quote(str(log_path))} 2>&1"
    )


def install_schedule(project_dir: Path, capture_time: str, state_dir: Path) -> Path:
    current_os = platform.system()
    hour_text, minute_text = capture_time.split(":", 1)

    if current_os == "Darwin":
        plist_path = Path.home() / "Library" / "LaunchAgents" / f"{SCHEDULE_LABEL}.plist"
        plist = f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\"> 
<dict>
  <key>Label</key>
  <string>{SCHEDULE_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-lc</string>
    <string>{scheduler_command(project_dir, state_dir)}</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>{int(hour_text)}</integer>
    <key>Minute</key>
    <integer>{int(minute_text)}</integer>
  </dict>
  <key>RunAtLoad</key>
  <false/>
</dict>
</plist>
"""
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(plist)
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True, text=True)
        result = subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "failed to load launch agent")
        return plist_path

    user_dir = Path.home() / ".config" / "systemd" / "user"
    service_path = user_dir / f"{SCHEDULE_LABEL}.service"
    timer_path = user_dir / f"{SCHEDULE_LABEL}.timer"
    user_dir.mkdir(parents=True, exist_ok=True)

    service_path.write_text(
        "[Unit]\n"
        "Description=LLM meter daily capture\n\n"
        "[Service]\n"
        f"ExecStart=/bin/sh -lc '{scheduler_command(project_dir, state_dir)}'\n"
    )
    timer_path.write_text(
        "[Unit]\n"
        "Description=Run LLM meter capture daily\n\n"
        "[Timer]\n"
        f"OnCalendar=*-*-* {int(hour_text):02d}:{int(minute_text):02d}:00\n"
        "Persistent=true\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )

    for command in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", timer_path.name],
    ):
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "failed to configure systemd timer")
    return timer_path


def uninstall_schedule() -> tuple[int, str]:
    current_os = platform.system()
    if current_os == "Darwin":
        plist_path = Path.home() / "Library" / "LaunchAgents" / f"{SCHEDULE_LABEL}.plist"
        if not plist_path.exists():
            return 1, "scheduler not installed"
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True, text=True)
        plist_path.unlink()
        return 0, f"removed {plist_path}"

    timer_path = Path.home() / ".config" / "systemd" / "user" / f"{SCHEDULE_LABEL}.timer"
    service_path = Path.home() / ".config" / "systemd" / "user" / f"{SCHEDULE_LABEL}.service"
    if not timer_path.exists():
        return 1, "scheduler not installed"
    subprocess.run(["systemctl", "--user", "disable", "--now", timer_path.name], capture_output=True, text=True)
    timer_path.unlink()
    if service_path.exists():
        service_path.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)
    return 0, f"removed {timer_path}"
