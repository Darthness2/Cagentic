"""Install the Cagentic gateway as a background login service.

macOS uses a launchd LaunchAgent; Linux uses a systemd user unit. Either way
the gateway runs `cagentic --serve` whenever the machine is on — no CLI
session needed — and restarts automatically if it crashes.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

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
    try:
        if sys.platform == "darwin":
            return _install_launchd()
        if sys.platform.startswith("linux"):
            return _install_systemd()
        ui.error("background service install is supported on macOS and Linux only.")
        ui.info("On Windows, use Task Scheduler to run `cagentic --serve` at logon.")
        return 1
    except OSError as exc:
        ui.error(f"could not install gateway service: {exc}")
        target = _launchd_plist_path() if sys.platform == "darwin" else _systemd_unit_path()
        ui.warn(f"installation stopped; inspect {target} before retrying")
        return 1


def uninstall() -> int:
    try:
        if sys.platform == "darwin":
            return _uninstall_launchd()
        if sys.platform.startswith("linux"):
            return _uninstall_systemd()
        ui.error("no background service support on this platform.")
        return 1
    except OSError as exc:
        ui.error(f"could not remove gateway service: {exc}")
        target = _launchd_plist_path() if sys.platform == "darwin" else _systemd_unit_path()
        ui.warn(f"removal stopped; inspect {target} and the service manager state")
        return 1


# ---------------------------------------------------------------- macOS --


def _install_launchd() -> int:
    log = Path.home() / "Library" / "Logs" / "cagentic-gateway.log"
    path = _launchd_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _LAUNCHD_PLIST.format(
            label=LABEL,
            python=_xml_escape(sys.executable),
            home=_xml_escape(str(Path.home())),
            log=_xml_escape(str(log)),
        ),
        encoding="utf-8",
    )

    # Reload cleanly if a previous version is already running.
    try:
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
        res = subprocess.run(["launchctl", "load", "-w", str(path)], capture_output=True, text=True)
    except OSError as exc:
        ui.error(f"could not run launchctl: {exc}")
        ui.warn(f"partial state: {path} was written, but launchd was not loaded")
        return 1
    if res.returncode != 0:
        ui.error(f"launchctl load failed: {res.stderr.strip() or res.stdout.strip()}")
        ui.warn(
            f"partial state: {path} was written, but launchd did not load it; "
            "fix the reported error and retry `cagentic --install-service`"
        )
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
    try:
        result = subprocess.run(
            ["launchctl", "unload", str(path)],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        ui.error(f"could not run launchctl: {exc}")
        ui.warn(f"no files were removed; the service definition remains at {path}")
        return 1
    if result.returncode != 0:
        ui.error(f"launchctl unload failed: {result.stderr.strip() or result.stdout.strip()}")
        ui.warn(f"no files were removed; the service definition remains at {path}")
        return 1
    path.unlink()
    ui.info("gateway service removed.")
    return 0


# ---------------------------------------------------------------- Linux --


def _install_systemd() -> int:
    path = _systemd_unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_SYSTEMD_UNIT.format(python=_systemd_quote(sys.executable)), encoding="utf-8")

    for cmd in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "cagentic-gateway.service"],
    ):
        try:
            res = subprocess.run(cmd, capture_output=True, text=True)
        except OSError as exc:
            ui.error(f"could not run {' '.join(cmd)}: {exc}")
            ui.warn(
                f"partial state: {path} was written; inspect the service manager "
                "before retrying `cagentic --install-service`"
            )
            return 1
        if res.returncode != 0:
            ui.error(f"{' '.join(cmd)} failed: {res.stderr.strip()}")
            ui.warn(
                f"partial state: {path} was written; inspect the service manager "
                "before retrying `cagentic --install-service`"
            )
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
    try:
        result = subprocess.run(
            ["systemctl", "--user", "disable", "--now", "cagentic-gateway.service"],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        ui.error(f"could not run systemctl disable: {exc}")
        ui.warn(f"the service definition remains at {path}; manager state may be partial")
        return 1
    if result.returncode != 0:
        ui.error(f"systemctl disable failed: {result.stderr.strip() or result.stdout.strip()}")
        ui.warn(f"the service definition remains at {path}; manager state may be partial")
        return 1
    try:
        path.unlink()
    except OSError as exc:
        ui.error(f"service was disabled, but {path} could not be removed: {exc}")
        ui.warn("partial state: the service is disabled but its definition remains")
        return 1
    try:
        result = subprocess.run(
            ["systemctl", "--user", "daemon-reload"], capture_output=True, text=True
        )
    except OSError as exc:
        ui.error(f"could not run systemctl daemon-reload: {exc}")
        ui.warn(
            f"partial state: the service is disabled and {path} was removed, "
            "but systemd still needs a successful daemon-reload"
        )
        return 1
    if result.returncode != 0:
        ui.error(
            f"systemctl daemon-reload failed: {result.stderr.strip() or result.stdout.strip()}"
        )
        ui.warn(
            f"partial state: the service is disabled and {path} was removed, "
            "but systemd still needs a successful daemon-reload"
        )
        return 1
    ui.info("gateway service removed.")
    return 0


def _systemd_quote(value: str) -> str:
    """Quote an executable path for systemd's ExecStart grammar."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'
