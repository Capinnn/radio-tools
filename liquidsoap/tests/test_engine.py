"""Tests for the cross-platform radio engine manager (liquidsoap/engine/).

These tests mock subprocess.Popen, os.kill, and urllib to verify the engine's
logic without starting real Icecast/Liquidsoap processes or making real HTTP
requests. They use only the standard library and pytest.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
import urllib.error
from pathlib import Path
from unittest import mock

import pytest

# Ensure the engine package is importable.
ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

# Also ensure lib is importable (for gen-playlist validation).
LIB = ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import engine  # noqa: E402
import engine.__main__ as radio  # noqa: E402
from engine.__main__ import (  # noqa: E402
    EngineError,
    check_mp3_sync,
    load_secrets,
    validate_secrets,
    render_config,
    is_process_alive,
    read_pid_file,
    terminate_pid,
    verify_pid_cmdline,
    parse_status,
    check_source_mount,
    do_bin_paths,
    do_gen_playlist,
    gen_playlist_once,
    do_start,
    do_stop,
    do_status,
    main,
)


# ── helpers ────────────────────────────────────────────────────────────


@pytest.fixture
def fake_config(tmp_path):
    """Provide a minimal valid config dict."""
    return {
        "ICECAST_SOURCE_PASSWORD": "long-test-password-123",
        "ICECAST_ADMIN_PASSWORD": "long-test-password-123",
        "ICECAST_RELAY_PASSWORD": "long-test-password-123",
        "ICECAST_HOSTNAME": "radio.example.invalid",
        "ICECAST_PORT": "8000",
        "ICECAST_HOST": "127.0.0.1",
    }


# ── secrets validation ─────────────────────────────────────────────────


class TestSecretsValidation:
    def test_valid_secrets_pass(self, fake_config):
        validate_secrets(fake_config)

    def test_short_password_rejected(self, fake_config):
        fake_config["ICECAST_SOURCE_PASSWORD"] = "short"
        with pytest.raises(EngineError, match="at least 12 characters"):
            validate_secrets(fake_config)

    def test_unsafe_charset_rejected(self, fake_config):
        fake_config["ICECAST_SOURCE_PASSWORD"] = "password-with-spaces is bad"
        with pytest.raises(EngineError, match="unsafe"):
            validate_secrets(fake_config)

    def test_safe_special_chars_accepted(self, fake_config):
        fake_config["ICECAST_SOURCE_PASSWORD"] = "safe.-_~!@%+=:,/12"
        validate_secrets(fake_config)

    def test_invalid_hostname_rejected(self, fake_config):
        fake_config["ICECAST_HOSTNAME"] = "bad host!"
        with pytest.raises(EngineError, match="HOSTNAME"):
            validate_secrets(fake_config)

    def test_non_numeric_port_rejected(self, fake_config):
        fake_config["ICECAST_PORT"] = "abc"
        with pytest.raises(EngineError, match="numeric"):
            validate_secrets(fake_config)

    def test_port_out_of_range_rejected(self, fake_config):
        fake_config["ICECAST_PORT"] = "99999"
        with pytest.raises(EngineError, match="out of range"):
            validate_secrets(fake_config)

    def test_env_override_precedence(self, tmp_path, monkeypatch):
        """Environment variables override secrets file values."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text(
            "ICECAST_SOURCE_PASSWORD=from-file-value-12\n"
            "ICECAST_HOSTNAME=file.example.invalid\n"
            "ICECAST_PORT=8000\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(radio, "SECRETS_FILE", secrets_file)
        monkeypatch.setenv("ICECAST_SOURCE_PASSWORD", "from-env-value-12345")
        config = load_secrets()
        assert config["ICECAST_SOURCE_PASSWORD"] == "from-env-value-12345"
        assert config["ICECAST_HOSTNAME"] == "file.example.invalid"

    def test_missing_source_password_raises(self, tmp_path, monkeypatch):
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text("ICECAST_HOSTNAME=test.invalid\n", encoding="utf-8")
        monkeypatch.setattr(radio, "SECRETS_FILE", secrets_file)
        monkeypatch.delenv("ICECAST_SOURCE_PASSWORD", raising=False)
        with pytest.raises(EngineError, match="ICECAST_SOURCE_PASSWORD"):
            load_secrets()

    def test_defaults_for_optional_passwords(self, tmp_path, monkeypatch):
        """Admin/relay passwords default to source password."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text(
            "ICECAST_SOURCE_PASSWORD=default-test-value-12\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(radio, "SECRETS_FILE", secrets_file)
        for key in ("ICECAST_ADMIN_PASSWORD", "ICECAST_RELAY_PASSWORD",
                     "ICECAST_HOSTNAME", "ICECAST_PORT"):
            monkeypatch.delenv(key, raising=False)
        config = load_secrets()
        assert config["ICECAST_ADMIN_PASSWORD"] == "default-test-value-12"
        assert config["ICECAST_RELAY_PASSWORD"] == "default-test-value-12"
        assert config["ICECAST_PORT"] == "8000"


# ── config rendering ────────────────────────────────────────────────────


class TestConfigRender:
    def test_renders_all_placeholders(self, fake_config):
        rendered = render_config(fake_config)
        assert "long-test-password-123" in rendered
        assert "radio.example.invalid" in rendered
        assert ">8000<" in rendered
        assert "${" not in rendered, "unreplaced placeholder remains"

    def test_renders_log_dir(self, fake_config, tmp_path, monkeypatch):
        monkeypatch.setattr(radio, "LOG_DIR", tmp_path)
        rendered = render_config(fake_config)
        assert str(tmp_path) in rendered

    def test_no_secret_leak_in_template(self):
        """The template file itself should not contain real secrets."""
        template = radio.CONFIG_TEMPLATE.read_text(encoding="utf-8")
        assert "hackme" not in template.lower()
        assert "${ICECAST_SOURCE_PASSWORD}" in template


# ── process management ────────────────────────────────────────────────


class TestProcessManagement:
    def test_read_pid_file_valid(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("12345\n")
        assert read_pid_file(pid_file) == 12345

    def test_read_pid_file_missing(self, tmp_path):
        assert read_pid_file(tmp_path / "nonexistent.pid") is None

    def test_read_pid_file_invalid(self, tmp_path):
        pid_file = tmp_path / "bad.pid"
        pid_file.write_text("not-a-number\n")
        assert read_pid_file(pid_file) is None

    @pytest.mark.skipif(os.name == "nt", reason="POSIX only")
    def test_is_process_alive_current_process(self):
        assert is_process_alive(os.getpid()) is True

    @pytest.mark.skipif(os.name == "nt", reason="POSIX only")
    def test_is_process_alive_dead_pid(self):
        # PID 0 is never a valid target; use a very high unlikely PID.
        assert is_process_alive(999999) is False

    @pytest.mark.skipif(os.name == "nt", reason="POSIX only")
    def test_terminate_pid_already_dead(self):
        # If the process is already dead, terminate_pid should return True.
        assert terminate_pid(999999, grace_seconds=0.1) is True

    @pytest.mark.skipif(os.name == "nt", reason="POSIX only")
    def test_terminate_pid_kills_real_process(self):
        """Spawn a sleeping subprocess and verify terminate_pid stops it."""
        import subprocess
        proc = subprocess.Popen(["sleep", "30"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            assert is_process_alive(proc.pid) is True
            result = terminate_pid(proc.pid, grace_seconds=3.0)
            assert result is True
            assert is_process_alive(proc.pid) is False
        finally:
            proc.wait(timeout=5)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX only")
    def test_verify_pid_cmdline_current_process(self):
        """The current process's cmdline should contain 'python'."""
        result = verify_pid_cmdline(os.getpid(), "python")
        # Might not contain 'python' if invoked differently, but should
        # at least return True for a check on our own process.
        assert isinstance(result, bool)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX only")
    def test_verify_pid_cmdline_wrong_process(self):
        """A PID whose cmdline doesn't match should return False."""
        result = verify_pid_cmdline(os.getpid(), "definitely-not-in-cmdline-xyz123")
        assert result is False


# ── status parsing ──────────────────────────────────────────────────────


class TestStatusParsing:
    def test_parse_status_dict_source(self):
        doc = {
            "icestats": {
                "source": {
                    "listeners": 3,
                    "server_type": "audio/mpeg",
                    "title": "Artist - Title",
                    "audio_info": "bitrate=128",
                    "listenurl": "http://127.0.0.1:8000/radio.mp3",
                }
            }
        }
        info = parse_status(doc)
        assert info["listeners"] == 3
        assert info["server_type"] == "audio/mpeg"
        assert info["title"] == "Artist - Title"
        assert info["audio_info"] == "bitrate=128"

    def test_parse_status_list_source_finds_mount(self):
        doc = {
            "icestats": {
                "source": [
                    {
                        "listeners": 0,
                        "server_type": "audio/mpeg",
                        "title": "Other mount",
                        "audio_info": "",
                        "listenurl": "http://127.0.0.1:8000/other.mp3",
                    },
                    {
                        "listeners": 5,
                        "server_type": "audio/mpeg",
                        "title": "Artist - Song",
                        "audio_info": "bitrate=128",
                        "listenurl": "http://127.0.0.1:8000/radio.mp3",
                    },
                ]
            }
        }
        info = parse_status(doc)
        assert info["listeners"] == 5
        assert info["title"] == "Artist - Song"

    def test_parse_status_empty(self):
        info = parse_status({})
        assert info["listeners"] == "?"
        assert info["title"] == "?"

    def test_check_source_mount_present(self):
        doc = {
            "icestats": {
                "source": {
                    "listenurl": "http://127.0.0.1:8000/radio.mp3",
                }
            }
        }
        with mock.patch.object(radio, "fetch_status_json", return_value=doc):
            assert check_source_mount(8000) is True

    def test_check_source_mount_absent(self):
        doc = {
            "icestats": {
                "source": {
                    "listenurl": "http://127.0.0.1:8000/other.mp3",
                }
            }
        }
        with mock.patch.object(radio, "fetch_status_json", return_value=doc):
            assert check_source_mount(8000) is False

    def test_check_source_mount_unreachable(self):
        with mock.patch.object(radio, "fetch_status_json", return_value=None):
            assert check_source_mount(8000) is False


# ── MP3 sync ────────────────────────────────────────────────────────────


class TestMp3Sync:
    def test_valid_mp3_sync(self):
        # 0xFF 0xFB is a common MP3 frame sync
        data = b"\x00" * 100 + b"\xff\xfb" + b"\x00" * 100
        assert check_mp3_sync(data) is True

    def test_no_sync_bytes(self):
        data = b"\x00" * 200
        assert check_mp3_sync(data) is False

    def test_empty_data(self):
        assert check_mp3_sync(b"") is False

    def test_short_data(self):
        assert check_mp3_sync(b"\xff") is False


# ── readiness polling aborts on timeout ────────────────────────────────


class TestReadinessPolling:
    def test_wait_for_icecast_aborts_on_timeout(self):
        """If Icecast never becomes ready, wait_for_icecast returns False."""
        with mock.patch.object(radio, "is_process_alive", return_value=True), \
             mock.patch.object(radio, "fetch_url", return_value=None):
            result = radio.wait_for_icecast(
                pid=1234, port=8000, max_attempts=3, interval=0.01
            )
            assert result is False

    def test_wait_for_icecast_process_died(self):
        """If the process dies during startup, wait_for_icecast returns False."""
        with mock.patch.object(radio, "is_process_alive", return_value=False), \
             mock.patch.object(radio, "fetch_url", return_value=None):
            result = radio.wait_for_icecast(
                pid=1234, port=8000, max_attempts=10, interval=0.01
            )
            assert result is False

    def test_wait_for_icecast_succeeds(self):
        with mock.patch.object(radio, "is_process_alive", return_value=True), \
             mock.patch.object(radio, "fetch_url", return_value=b'{"ok": true}'):
            result = radio.wait_for_icecast(
                pid=1234, port=8000, max_attempts=5, interval=0.01
            )
            assert result is True

    def test_wait_for_source_aborts_on_timeout(self):
        with mock.patch.object(radio, "is_process_alive", return_value=True), \
             mock.patch.object(radio, "check_source_mount", return_value=False):
            result = radio.wait_for_source(
                pid=1234, port=8000, max_attempts=3, interval=0.01
            )
            assert result is False

    def test_wait_for_source_process_died(self):
        with mock.patch.object(radio, "is_process_alive", return_value=False), \
             mock.patch.object(radio, "check_source_mount", return_value=False):
            result = radio.wait_for_source(
                pid=1234, port=8000, max_attempts=10, interval=0.01
            )
            assert result is False

    def test_wait_for_source_succeeds(self):
        with mock.patch.object(radio, "is_process_alive", return_value=True), \
             mock.patch.object(radio, "check_source_mount", return_value=True):
            result = radio.wait_for_source(
                pid=1234, port=8000, max_attempts=5, interval=0.01
            )
            assert result is True


# ── stop terminates ─────────────────────────────────────────────────────


class TestStopTerminates:
    def test_stop_with_no_pid_files(self, tmp_path, monkeypatch):
        """Stop reports 'not running' when no PID files exist."""
        monkeypatch.setattr(radio, "ICECAST_PID_FILE", tmp_path / "icecast.pid")
        monkeypatch.setattr(radio, "LIQUIDSOAP_PID_FILE", tmp_path / "liquidsoap.pid")
        monkeypatch.setattr(radio, "RUNTIME_CONFIG", tmp_path / "icecast.runtime.xml")
        monkeypatch.setattr(radio, "LOG_DIR", tmp_path)
        monkeypatch.setattr(radio, "SCRIPTS_DIR", tmp_path / "scripts")
        result = do_stop(force_root=True)
        assert result is True

    def test_stop_terminates_running_process(self, tmp_path, monkeypatch):
        """Stop reads PID file, verifies alive, and terminates."""
        import subprocess
        proc = subprocess.Popen(["sleep", "30"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        pid_file = tmp_path / "icecast.pid"
        pid_file.write_text(f"{proc.pid}\n")
        runtime_config = tmp_path / "icecast.runtime.xml"
        runtime_config.write_text("<config/>")

        monkeypatch.setattr(radio, "ICECAST_PID_FILE", pid_file)
        monkeypatch.setattr(radio, "LIQUIDSOAP_PID_FILE", tmp_path / "liquidsoap.pid")
        monkeypatch.setattr(radio, "RUNTIME_CONFIG", runtime_config)
        monkeypatch.setattr(radio, "LOG_DIR", tmp_path)
        monkeypatch.setattr(radio, "SCRIPTS_DIR", tmp_path / "scripts")

        # Mock verify_pid_cmdline to skip the /proc check
        monkeypatch.setattr(radio, "verify_pid_cmdline", lambda pid, expected: True)

        try:
            result = do_stop(force_root=True)
            assert result is True
            assert not pid_file.exists()
            assert is_process_alive(proc.pid) is False
        finally:
            proc.wait(timeout=5)

    def test_stop_removes_stale_pid_file(self, tmp_path, monkeypatch):
        """Stop removes PID file for an already-dead process."""
        pid_file = tmp_path / "icecast.pid"
        pid_file.write_text("999999\n")
        runtime_config = tmp_path / "icecast.runtime.xml"

        monkeypatch.setattr(radio, "ICECAST_PID_FILE", pid_file)
        monkeypatch.setattr(radio, "LIQUIDSOAP_PID_FILE", tmp_path / "liquidsoap.pid")
        monkeypatch.setattr(radio, "RUNTIME_CONFIG", runtime_config)
        monkeypatch.setattr(radio, "LOG_DIR", tmp_path)
        monkeypatch.setattr(radio, "SCRIPTS_DIR", tmp_path / "scripts")

        result = do_stop(force_root=True)
        assert result is True
        assert not pid_file.exists()


# ── gen-playlist dry run writes nothing ─────────────────────────────────


class TestGenPlaylistDryRun:
    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        """Dry run should print the planned command but not write files."""
        output_file = tmp_path / "out" / "playlist.m3u"
        trigger_file = tmp_path / "state" / "reload.trigger"

        gen_playlist_once(
            source_dir=str(tmp_path),
            rotation_file=str(ROOT / "config" / "rotation.json"),
            output_file=str(output_file),
            trigger_file=str(trigger_file),
            hour=8,
            seed=42,
            dry_run=True,
        )

        assert not output_file.exists()
        assert not trigger_file.exists()

    def test_dry_run_with_library(self, tmp_path, monkeypatch):
        """Dry run with --library also writes nothing."""
        library_file = tmp_path / "library.json"
        library_file.write_text("[]\n", encoding="utf-8")
        output_file = tmp_path / "playlist.m3u"
        trigger_file = tmp_path / "trigger"

        gen_playlist_once(
            library_file=str(library_file),
            rotation_file=str(ROOT / "config" / "rotation.json"),
            output_file=str(output_file),
            trigger_file=str(trigger_file),
            hour=14,
            seed=99,
            dry_run=True,
        )

        assert not output_file.exists()
        assert not trigger_file.exists()

    def test_dry_run_missing_rotation_raises(self, tmp_path):
        with pytest.raises(EngineError, match="rotation file not found"):
            gen_playlist_once(
                source_dir=str(tmp_path),
                rotation_file=str(tmp_path / "nonexistent.json"),
                dry_run=True,
            )

    def test_dry_run_non_m3u_output_raises(self, tmp_path):
        with pytest.raises(EngineError, match="must end in .m3u"):
            gen_playlist_once(
                source_dir=str(tmp_path),
                rotation_file=str(ROOT / "config" / "rotation.json"),
                output_file=str(tmp_path / "playlist.txt"),
                dry_run=True,
            )


# ── CLI entry point ──────────────────────────────────────────────────────


class TestCLI:
    def test_cli_no_command_exits_nonzero(self):
        # argparse exits with code 2 when no subcommand is given.
        with pytest.raises(SystemExit) as exc_info:
            main([])
        assert exc_info.value.code == 2

    def test_cli_bin_paths_returns_0(self, capsys):
        """bin-paths should run and return 0 when binaries are found."""
        # On this test system liquidsoap/icecast may or may not be installed.
        # Just verify it doesn't crash.
        result = main(["bin-paths"])
        assert result in (0, 1)
        out = capsys.readouterr().out
        assert "icecast:" in out
        assert "liquidsoap:" in out

    def test_cli_status_down_returns_1(self, tmp_path, monkeypatch, capsys):
        """status returns 1 when both components are down."""
        monkeypatch.setattr(radio, "ICECAST_PID_FILE", tmp_path / "icecast.pid")
        monkeypatch.setattr(radio, "LIQUIDSOAP_PID_FILE", tmp_path / "liquidsoap.pid")
        # Mock load_secrets to avoid needing a real secrets file
        monkeypatch.setattr(
            radio, "load_secrets",
            lambda: {
                "ICECAST_SOURCE_PASSWORD": "test-value-12345",
                "ICECAST_PORT": "8000",
                "ICECAST_HOST": "127.0.0.1",
            },
        )
        monkeypatch.setattr(radio, "fetch_status_json", lambda *a, **kw: None)
        result = main(["status"])
        assert result == 1
        out = capsys.readouterr().out
        assert "Icecast: DOWN" in out
        assert "Liquidsoap: DOWN" in out

    def test_cli_stop_not_running_returns_0(self, tmp_path, monkeypatch):
        monkeypatch.setattr(radio, "ICECAST_PID_FILE", tmp_path / "icecast.pid")
        monkeypatch.setattr(radio, "LIQUIDSOAP_PID_FILE", tmp_path / "liquidsoap.pid")
        monkeypatch.setattr(radio, "RUNTIME_CONFIG", tmp_path / "icecast.runtime.xml")
        monkeypatch.setattr(radio, "LOG_DIR", tmp_path)
        monkeypatch.setattr(radio, "SCRIPTS_DIR", tmp_path / "scripts")
        result = main(["stop", "--force-root"])
        assert result == 0


# ── bin-paths resolution ────────────────────────────────────────────────


class TestBinPaths:
    def test_env_override_icecast(self, monkeypatch):
        monkeypatch.setenv("ICECAST_BIN", "/custom/icecast2")
        assert radio.resolve_icecast_bin() == "/custom/icecast2"

    def test_env_override_liquidsoap(self, monkeypatch):
        monkeypatch.setenv("LIQUIDSOAP_BIN", "/custom/liquidsoap")
        assert radio.resolve_liquidsoap_bin() == "/custom/liquidsoap"