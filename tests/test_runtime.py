import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest
from fpdf import FPDF

from reading_relay import cli, config, launchd
from reading_relay.config import Config, load_config, save_config
from reading_relay.models import Candidate, RelayError
from reading_relay.publishing import _HTTPSRedirectHandler, filename, publish
from reading_relay.store import marker


def test_launchctl_timeout_is_an_actionable_error(monkeypatch):
    monkeypatch.setattr(launchd.sys, "platform", "darwin")
    timeout = subprocess.TimeoutExpired(["/bin/launchctl", "print"], 20)
    with (
        patch.object(launchd.subprocess, "run", side_effect=timeout),
        pytest.raises(RelayError),
    ):
        launchd.launchctl("print")


def test_schedule_status_surfaces_launchctl_failures(monkeypatch):
    monkeypatch.setattr(launchd.sys, "platform", "darwin")
    monkeypatch.setattr(launchd.os, "getuid", lambda: 501, raising=False)
    failure = subprocess.CompletedProcess(["launchctl", "print"], 1, "", "Operation not permitted")
    monkeypatch.setattr(launchd, "launchctl", lambda *args, **kwargs: failure)
    with pytest.raises(RelayError):
        launchd.status()


def test_redirect_handler_rejects_insecure_hops_before_following():
    handler = _HTTPSRedirectHandler()
    request = urllib.request.Request("https://papers.example/source.pdf")
    safe = handler.redirect_request(request, None, 302, "Found", {}, "https://cdn.example/file.pdf")
    assert safe.full_url == "https://cdn.example/file.pdf"
    with pytest.raises(RelayError, match="unsafe PDF redirect"):
        handler.redirect_request(request, None, 302, "Found", {}, "http://papers.example/file.pdf")
    with pytest.raises(RelayError, match="unsafe PDF redirect"):
        handler.redirect_request(
            request, None, 302, "Found", {}, "https://user:pass@cdn.example/file.pdf"
        )


def test_unicode_filename_fits_portable_component_limit(tmp_path):
    candidate = Candidate("doi:10.1234/example", "論" * 100)
    name = filename(candidate)
    assert len(name.encode("utf-8")) <= 255
    assert name.endswith(f" [{marker(candidate.id)}].pdf")

    source = tmp_path / "source.pdf"
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(text="Research paper")
    pdf.output(str(source))
    config = Config(
        reading_dir=tmp_path / "Reading",
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
        cache_dir=tmp_path / "cache",
    )
    target = config.reading_dir / "Up Next" / name
    publish(source, target, config)
    assert target.is_file()


def test_launchd_management_is_rejected_before_platform_io(monkeypatch, tmp_path):
    monkeypatch.setattr(launchd.sys, "platform", "win32")
    monkeypatch.setattr(launchd, "plist_path", lambda: tmp_path / "unexpected.plist")
    with pytest.raises(RelayError, match="requires macOS"):
        launchd.uninstall()
    with pytest.raises(RelayError, match="requires macOS"):
        launchd.status()


def test_xdg_defaults_and_config_round_trip(monkeypatch, tmp_path):
    roots = {
        name: tmp_path / name.removeprefix("XDG_").lower()
        for name in (
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "XDG_CACHE_HOME",
        )
    }
    for name, path in roots.items():
        monkeypatch.setenv(name, str(path))

    current = Config()
    assert config.default_config_path() == roots["XDG_CONFIG_HOME"] / "reading-relay/config.toml"
    assert current.data_dir == roots["XDG_DATA_HOME"] / "reading-relay"
    assert current.state_dir == roots["XDG_STATE_HOME"] / "reading-relay"
    assert current.cache_dir == roots["XDG_CACHE_HOME"] / "reading-relay"
    assert current.notebook_roots == []

    path = roots["XDG_CONFIG_HOME"] / "reading-relay/config.toml"
    save_config(current, path)
    for name in roots:
        monkeypatch.setenv(name, str(tmp_path / "changed" / name))
    loaded = load_config(path)
    assert loaded.data_dir == current.data_dir
    assert loaded.state_dir == current.state_dir
    assert loaded.cache_dir == current.cache_dir


def test_relative_config_paths_are_independent_of_caller_cwd(monkeypatch, tmp_path):
    config_dir = tmp_path / "configuration"
    config_dir.mkdir()
    path = config_dir / "config.toml"
    path.write_text(
        'reading_dir = "Reading"\n'
        'data_dir = "data"\n'
        'state_dir = "state"\n'
        'cache_dir = "cache"\n'
        'notebook_roots = ["notebooks"]\n'
        'download_roots = ["downloads"]\n'
    )
    first_cwd = tmp_path / "first"
    second_cwd = tmp_path / "second"
    first_cwd.mkdir()
    second_cwd.mkdir()

    monkeypatch.chdir(first_cwd)
    first = load_config(path)
    monkeypatch.chdir(second_cwd)
    second = load_config(path)

    assert first.reading_dir == second.reading_dir == config_dir / "Reading"
    assert first.data_dir == second.data_dir == config_dir / "data"
    assert first.state_dir == second.state_dir == config_dir / "state"
    assert first.cache_dir == second.cache_dir == config_dir / "cache"
    assert first.notebook_roots == second.notebook_roots == [config_dir / "notebooks"]
    assert first.download_roots == second.download_roots == [config_dir / "downloads"]


def test_empty_or_relative_xdg_values_use_portable_fallbacks(monkeypatch):
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, "" if name.endswith("CONFIG_HOME") else "relative/path")

    assert config.default_config_path() == Path.home() / ".config/reading-relay/config.toml"
    current = Config()
    assert current.data_dir == Path.home() / ".local/share/reading-relay"
    assert current.state_dir == Path.home() / ".local/state/reading-relay"
    assert current.cache_dir == Path.home() / ".cache/reading-relay"


def test_xdg_directories_cannot_overlap(tmp_path):
    current = Config(
        reading_dir=tmp_path / "reading",
        data_dir=tmp_path / "app",
        state_dir=tmp_path / "app/state",
        cache_dir=tmp_path / "cache",
    )
    with pytest.raises(RelayError, match="separate, non-nested"):
        current.validate()


def test_xdg_data_dir_rejects_icloud_path(tmp_path):
    current = Config(
        reading_dir=tmp_path / "reading",
        data_dir=tmp_path / "Mobile Documents" / "relay-data",
        state_dir=tmp_path / "state",
        cache_dir=tmp_path / "cache",
    )
    with pytest.raises(RelayError, match="outside iCloud Drive"):
        current.validate()


def test_custom_harness_requires_a_configured_argv():
    with pytest.raises(RelayError, match="custom harness requires"):
        Config(harness="custom").validate()


def test_non_init_command_requires_existing_config(monkeypatch, tmp_path):
    missing = tmp_path / "missing.toml"
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / "unexpected" / name))
    monkeypatch.setattr(sys, "argv", ["reading-relay", "--config", str(missing), "list"])
    assert cli.main() == 1
    assert not missing.exists()
    assert not (tmp_path / "unexpected").exists()


def test_windows_cli_deadline_is_actionable(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    with pytest.raises(RelayError):
        cli._run_with_deadline(1, lambda: time.sleep(2))
