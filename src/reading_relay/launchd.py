"""User launchd agent with absolute paths and a bounded CLI entry point."""

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .config import Config
from .models import RelayError
from .publishing import atomic_write

LABEL = "org.reading-relay.daily"
BUNDLE_ID = "org.reading-relay"


def app_path() -> Path:
    return Path.home() / "Applications/Reading Relay.app"


def _install_app() -> None:
    app = app_path()
    info_path = app / "Contents/Info.plist"
    if app.exists() and (
        not info_path.is_file()
        or plistlib.loads(info_path.read_bytes()).get("CFBundleIdentifier") != BUNDLE_ID
    ):
        raise RelayError(f"Refusing to replace unrelated application: {app}")
    executable = app / "Contents/MacOS/Reading Relay"
    executable.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(
        info_path,
        plistlib.dumps(
            {
                "CFBundleIdentifier": BUNDLE_ID,
                "CFBundleName": "Reading Relay",
                "CFBundleDisplayName": "Reading Relay",
                "CFBundleExecutable": "Reading Relay",
                "CFBundlePackageType": "APPL",
                "CFBundleVersion": "1",
                "LSBackgroundOnly": True,
            }
        ),
    )
    atomic_write(
        executable,
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m reading_relay "$@"\n'.encode(),
    )
    executable.chmod(0o755)
    register = (
        "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
        "LaunchServices.framework/Support/lsregister"
    )
    try:
        result = subprocess.run(
            [register, "-f", str(app)], capture_output=True, text=True, timeout=20, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise RelayError("Registering Reading Relay.app timed out after 20 seconds") from exc
    if result.returncode:
        raise RelayError(f"Registering Reading Relay.app failed: {result.stderr.strip()[:1000]}")


def plist_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def definition(config: Config, config_path: Path, hour: int, minute: int) -> dict:
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise RelayError("Invalid schedule: hour 0..23, minute 0..59")
    logs = config.state_dir / "logs"
    return {
        "Label": LABEL,
        "AssociatedBundleIdentifiers": [BUNDLE_ID],
        "ProgramArguments": [
            str(app_path() / "Contents/MacOS/Reading Relay"),
            "--config",
            str(config_path.resolve()),
            "run",
        ],
        "WorkingDirectory": str(config.state_dir),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "ProcessType": "Background",
        # Allow background access to iCloud placeholders when their contents are needed.
        "MaterializeDatalessFiles": True,
        "LowPriorityIO": True,
        "Nice": 10,
        "ExitTimeOut": 10,
        "AbandonProcessGroup": False,
        "StandardOutPath": str(logs / "launchd.stdout.log"),
        "StandardErrorPath": str(logs / "launchd.stderr.log"),
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", os.defpath)},
    }


def _domain() -> str:
    if sys.platform == "darwin":
        return f"gui/{os.getuid()}"
    raise RelayError("LaunchAgent management requires macOS")


def launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    if sys.platform != "darwin":
        raise RelayError("LaunchAgent management requires macOS")
    try:
        result = subprocess.run(
            ["/bin/launchctl", *args], check=False, capture_output=True, text=True, timeout=20
        )
    except subprocess.TimeoutExpired as exc:
        raise RelayError(f"launchctl {' '.join(args)} timed out after 20 seconds") from exc
    if check and result.returncode:
        raise RelayError(f"launchctl {' '.join(args)} failed: {result.stderr.strip()[:1000]}")
    return result


def install(config: Config, config_path: Path, hour: int, minute: int) -> Path:
    domain = _domain()
    if not config_path.exists():
        raise RelayError("Run init first so launchd has a persistent configuration")
    data = definition(config, config_path, hour, minute)
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    (config.state_dir / "logs").mkdir(parents=True, exist_ok=True)
    _install_app()
    if path.exists():
        existing = plistlib.loads(path.read_bytes())
        if existing.get("Label") != LABEL:
            raise RelayError(f"Refusing to replace unrelated LaunchAgent: {path}")
        result = launchctl("print", f"{domain}/{LABEL}", check=False)
        if result.returncode == 0:
            launchctl("bootout", f"{domain}/{LABEL}")
    atomic_write(path, plistlib.dumps(data))
    launchctl("bootstrap", domain, str(path))
    return path


def uninstall() -> None:
    domain = f"{_domain()}/{LABEL}"
    path = plist_path()
    if not path.exists():
        raise RelayError("No Reading Relay LaunchAgent is installed")
    if plistlib.loads(path.read_bytes()).get("Label") != LABEL:
        raise RelayError("Refusing to remove unrelated LaunchAgent")
    app = app_path()
    if app.exists():
        info_path = app / "Contents/Info.plist"
        if (
            not info_path.is_file()
            or plistlib.loads(info_path.read_bytes()).get("CFBundleIdentifier") != BUNDLE_ID
        ):
            raise RelayError(f"Refusing to remove unrelated application: {app}")
    if launchctl("print", domain, check=False).returncode == 0:
        launchctl("bootout", domain)
    path.unlink()
    if app.exists():
        shutil.rmtree(app)


def status() -> str:
    domain = _domain()
    result = launchctl("print", f"{domain}/{LABEL}", check=False)
    if result.returncode == 0:
        return result.stdout
    message = result.stderr.strip()
    message_folded = message.casefold()
    if message and not (
        "could not find service" in message_folded
        or "could not find specified service" in message_folded
    ):
        raise RelayError(f"launchctl print failed: {message[:1000]}")
    return "Reading Relay LaunchAgent is not loaded."
