"""Install the Cagentic gateway as a background login service.

macOS uses a launchd LaunchAgent; Linux uses a systemd user unit. Either way
the gateway runs `cagentic --serve` whenever the machine is on — no CLI
session needed — and restarts automatically if it crashes.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from . import ui

LABEL = "com.cagentic.gateway"

_LAUNCHD_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>-m</string>
        <string>cagentic</string>
        <string>--serve</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>WorkingDirectory</key>
    <string>{home}</string>
    <key>StandardOutPath</key>
    <string>{log}</string>
    <key>StandardErrorPath</key>
    <string>{log}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
"""

_SYSTEMD_UNIT = """[Unit]
Description=Cagentic gateway
After=network.target

[Service]
ExecStart={python} -m cagentic --serve
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
"""


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / "cagentic-gateway.service"


def install() -> int:
    if sys.platform == "darwin":
        return _install_launchd()
    if sys.platform.startswith("linux"):
        return _install_systemd()
    ui.error("background service install is supported on macOS and Linux only.")
    ui.info("On Windows, use Task Scheduler to run `cagentic --serve` at logon.")
    return 1


def uninstall() -> int:
    if sys.platform == "darwin":
        return _uninstall_launchd()
    if sys.platform.startswith("linux"):
        return _uninstall_systemd()
    ui.error("no background service support on this platform.")
    return 1


# ---------------------------------------------------------------- macOS --

def _install_launchd() -> int:
    log = Path.home() / "Library" / "Logs" / "cagentic-gateway.log"
    path = _launchd_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_LAUNCHD_PLIST.format(
        label=LABEL,
        python=sys.executable,
        home=Path.home(),
        log=log,
    ))

    # Reload cleanly if a previous version is already running.
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
    res = subprocess.run(["launchctl", "load", "-w", str(path)],
                         capture_output=True, text=True)
    if res.returncode != 0:
        ui.error(f"launchctl load failed: {res.stderr.strip() or res.stdout.strip()}")
        return 1

    ui.info("gateway service installed — running now, and at every login.")
    ui.info(f"logs: {log}")
    ui.info("remove with: cagentic --uninstall-service")
    return 0


def _uninstall_launchd() -> int:
    path = _launchd_plist_path()
    if not path.exists():
        ui.info("gateway service is not installed.")
        return 0
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
    path.unlink()
    ui.info("gateway service removed.")
    return 0


# ---------------------------------------------------------------- Linux --

def _install_systemd() -> int:
    path = _systemd_unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_SYSTEMD_UNIT.format(python=sys.executable))

    for cmd in (["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", "cagentic-gateway.service"]):
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            ui.error(f"{' '.join(cmd)} failed: {res.stderr.strip()}")
            return 1

    ui.info("gateway service installed — running now, and at every login.")
    ui.info("to keep it running while logged out: loginctl enable-linger $USER")
    ui.info("remove with: cagentic --uninstall-service")
    return 0


def _uninstall_systemd() -> int:
    path = _systemd_unit_path()
    if not path.exists():
        ui.info("gateway service is not installed.")
        return 0
    subprocess.run(["systemctl", "--user", "disable", "--now", "cagentic-gateway.service"],
                   capture_output=True)
    path.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    ui.info("gateway service removed.")
    return 0
