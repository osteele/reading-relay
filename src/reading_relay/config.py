"""Portable configuration; machine paths live outside the repository."""

import json
import os
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .models import RelayError


def _xdg_directory(variable: str, fallback: Path) -> Path:
    value = os.environ.get(variable, "")
    path = Path(value) if value else None
    return path if path is not None and path.is_absolute() else fallback


def default_config_path() -> Path:
    return _xdg_directory("XDG_CONFIG_HOME", Path.home() / ".config") / "reading-relay/config.toml"


def default_data_dir() -> Path:
    return _xdg_directory("XDG_DATA_HOME", Path.home() / ".local/share") / "reading-relay"


def default_state_dir() -> Path:
    return _xdg_directory("XDG_STATE_HOME", Path.home() / ".local/state") / "reading-relay"


def default_cache_dir() -> Path:
    return _xdg_directory("XDG_CACHE_HOME", Path.home() / ".cache") / "reading-relay"


def _command_for(harness: str, model: str = "") -> list[str]:
    executable = shutil.which(harness) or harness
    if harness == "codex":
        command = [
            executable,
            "exec",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "-c",
            'model_reasoning_effort="medium"',
            "-c",
            "features.shell_tool=false",
            "-",
        ]
        if model:
            command[2:2] = ["--model", model]
        return command
    if harness == "omp":
        command = [
            executable,
            "--no-session",
            "--no-skills",
            "--no-rules",
            "--no-extensions",
            "--no-tools",
            "--print",
        ]
        if model:
            command[1:1] = ["--model", model]
        return command
    raise RelayError(f"No built-in command for harness {harness!r}")


def default_command() -> list[str]:
    return _command_for("codex")


@dataclass
class Config:
    reading_dir: Path = field(default_factory=lambda: Path.home() / "Documents/Reading")
    data_dir: Path = field(default_factory=default_data_dir)
    state_dir: Path = field(default_factory=default_state_dir)
    cache_dir: Path = field(default_factory=default_cache_dir)
    notebook_roots: list[Path] = field(default_factory=list)
    download_roots: list[Path] = field(default_factory=lambda: [Path.home() / "Downloads/Papers"])
    harness: str = "codex"
    model: str = ""
    command: list[str] = field(default_factory=list)
    capacity: int = 3
    max_files: int = 600
    max_candidates: int = 120
    activity_days: int = 30
    defer_days: int = 30
    rank_timeout: int = 180
    run_timeout: int = 540
    download_timeout: int = 35
    max_pdf_bytes: int = 50 * 1024 * 1024
    hour: int = 9
    minute: int = 0

    def effective_command(self) -> list[str]:
        return list(self.command) if self.command else _command_for(self.harness, self.model)

    def validate(self) -> None:
        for key in (
            "capacity",
            "max_files",
            "max_candidates",
            "activity_days",
            "defer_days",
            "rank_timeout",
            "run_timeout",
            "download_timeout",
            "max_pdf_bytes",
        ):
            value = getattr(self, key)
            if type(value) is not int or value < 1:
                raise RelayError(f"{key} must be a positive integer")
        if self.capacity > 10 or self.run_timeout > 900:
            raise RelayError("capacity must be <=10 and run_timeout <=900 seconds")
        if (
            type(self.hour) is not int
            or type(self.minute) is not int
            or not 0 <= self.hour <= 23
            or not 0 <= self.minute <= 59
        ):
            raise RelayError("Invalid daily schedule")
        if self.harness not in {"codex", "omp", "custom"}:
            raise RelayError("harness must be codex, omp, or custom")
        if not isinstance(self.model, str):
            raise RelayError("model must be a string")
        if not isinstance(self.command, list) or not all(
            isinstance(part, str) and part for part in self.command
        ):
            raise RelayError("command must be an argv array of nonempty strings")
        if self.harness == "custom" and not self.command:
            raise RelayError("custom harness requires a command argv array")
        directories = {
            key: getattr(self, key).resolve()
            for key in ("reading_dir", "data_dir", "state_dir", "cache_dir")
        }
        names = list(directories)
        for index, name in enumerate(names):
            for other in names[index + 1 :]:
                left, right = directories[name], directories[other]
                if left == right or left in right.parents or right in left.parents:
                    raise RelayError(f"{name} and {other} must be separate, non-nested directories")
        if any("Mobile Documents" in str(directories[key]) for key in ("data_dir", "state_dir")):
            raise RelayError("Keep data_dir and state_dir outside iCloud Drive")


def load_config(path: Path) -> Config:
    config = Config()
    config_path = path.expanduser().absolute()
    config_dir = config_path.parent

    def resolve_path(value: str) -> Path:
        configured = Path(value).expanduser()
        if not configured.is_absolute():
            configured = config_dir / configured
        return configured.absolute()

    if config_path.exists():
        data = tomllib.loads(config_path.read_text())
        unknown = set(data) - set(config.__dataclass_fields__)
        if unknown:
            raise RelayError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
        for key, value in data.items():
            if key in {"reading_dir", "data_dir", "state_dir", "cache_dir"}:
                if not isinstance(value, str):
                    raise RelayError(f"{key} must be a path string")
                value = resolve_path(value)
            elif key in {"notebook_roots", "download_roots"}:
                if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
                    raise RelayError(f"{key} must be an array of paths")
                value = [resolve_path(p) for p in value]
            setattr(config, key, value)
    config.validate()
    return config


def save_config(config: Config, path: Path) -> None:
    config.validate()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for key in config.__dataclass_fields__:
        value = getattr(config, key)
        if isinstance(value, Path):
            value = str(value)
        elif key in {"notebook_roots", "download_roots"}:
            value = [str(p) for p in value]
        lines.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")
    with path.open("x") as stream:
        stream.write("\n".join(lines) + "\n")
