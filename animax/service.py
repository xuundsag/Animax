#!/usr/bin/env python3
"""SSH-safe execution. systemd for reboot/crash recovery, detached mode as fallback."""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "animax.service"
PIDFILE = ROOT / ".detached.json"


def quoted(value):
    value = str(value)
    if "\n" in value or "\r" in value:
        raise ValueError("Newlines are not allowed in service paths.")
    return (
        '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
    )


def unit_text(root, user):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*[$]?", user):
        raise ValueError("Invalid Linux username.")
    return f"""[Unit]
Description=ANIMAX episode uploader
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User={user}
WorkingDirectory={str(root).replace("%", "%%")}
ExecStart={quoted(root / ".venv/bin/python")} {quoted(root / "bot.py")}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONUTF8=1
Environment={quoted("PLAYWRIGHT_BROWSERS_PATH=" + str(root / ".browsers"))}
Restart=on-failure
RestartPreventExitStatus=78
RestartSec=30
KillSignal=SIGTERM
TimeoutStopSec=40
KillMode=mixed
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""


def setup():
    subprocess.run([sys.executable, str(ROOT / "bot.py"), "--setup"], check=True)


def detached_pid():
    if not PIDFILE.exists():
        return None
    record = json.loads(PIDFILE.read_text())
    pid = int(record["pid"])
    try:
        os.kill(pid, 0)
        command = Path(f"/proc/{pid}/cmdline")
        if (
            command.exists()
            and str(ROOT / "bot.py").encode() not in command.read_bytes()
        ):
            return None
        return pid
    except (ProcessLookupError, PermissionError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "install",
            "start",
            "stop",
            "restart",
            "status",
            "logs",
            "detach",
            "stop-detached",
            "status-detached",
            "print-unit",
        ],
    )
    args = parser.parse_args()
    action = args.action
    if action in {"detach", "stop-detached", "status-detached"}:
        if os.name != "posix":
            raise SystemExit("Detached helper is intended for Linux/macOS SSH hosts.")
        pid = detached_pid()
        if action == "status-detached":
            print(f"Running: PID {pid}" if pid else "Not running.")
            return
        if action == "stop-detached":
            if pid:
                os.kill(pid, signal.SIGTERM)
                print("Graceful stop requested. Active job will be saved.")
            else:
                print("Not running.")
            return
        if pid:
            raise SystemExit(f"Already running as PID {pid}.")
        setup()
        logs = ROOT / "logs"
        logs.mkdir(exist_ok=True)
        with (logs / "daemon.log").open("ab") as log:
            child = subprocess.Popen(
                [str(ROOT / ".venv/bin/python"), str(ROOT / "bot.py")],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        PIDFILE.write_text(json.dumps({"pid": child.pid}))
        PIDFILE.chmod(0o600)
        print(
            f"Detached PID {child.pid}. You can disconnect SSH. Logs: {logs / 'daemon.log'}"
        )
        print(
            "This mode does not restart after a reboot/crash. Use systemd when available."
        )
        return
    if os.name != "posix":
        raise SystemExit("systemd setup requires Linux.")
    import pwd

    owner = pwd.getpwuid(ROOT.stat().st_uid).pw_name
    if action == "print-unit":
        print(unit_text(ROOT, owner))
        return
    if not shutil.which("systemctl") or not Path("/run/systemd/system").exists():
        raise SystemExit(
            "systemd is not running on this host. Use: python3 manage.py service detach"
        )
    if action == "install":
        if os.geteuid() != 0:
            raise SystemExit(
                "Install with sudo: sudo python3 manage.py service install"
            )
        prefix = [] if owner == "root" else ["runuser", "-u", owner, "--"]
        subprocess.run(
            prefix + [sys.executable, str(ROOT / "bot.py"), "--setup"], check=True
        )
        destination = Path("/etc/systemd/system") / NAME
        if destination.exists():
            shutil.copy2(destination, destination.with_suffix(".service.bak"))
        destination.write_text(unit_text(ROOT, owner))
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", NAME], check=True)
        subprocess.run(["systemctl", "restart", NAME], check=True)
        print(
            f"Installed {NAME}, running as project owner {owner}. SSH disconnects will not stop it."
        )
        print(
            "Check: systemctl status animax --no-pager | Logs: journalctl -u animax -f"
        )
    elif action == "logs":
        subprocess.run(["journalctl", "-u", NAME, "-f", "--no-pager"])
    else:
        subprocess.run(
            ["systemctl", action, NAME]
            + (["--no-pager"] if action == "status" else []),
            check=True,
        )


if __name__ == "__main__":
    main()
