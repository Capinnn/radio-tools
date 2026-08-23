"""Tests for the engine's platform path handling (Windows gaps).

Covers the pure helpers behind binary resolution, Icecast share-directory
detection, the runtime-config webroot/adminroot rewrite, and the
``--dry-run`` / ``paths`` commands.  Everything runs on any host: the
Windows cases inject a fake environment, a fake directory lister, and a fake
``exists`` probe rather than touching the real filesystem.
"""

from __future__ import annotations

import sys
from pathlib import Path, PureWindowsPath
from unittest import mock

import pytest

# Ensure the engine package is importable (mirrors test_engine.py).
ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

import engine.__main__ as radio  # noqa: E402
from engine.__main__ import (  # noqa: E402
    EngineError,
    effective_icecast_paths,
    find_icecast_share_dirs,
    icecast_install_root,
    icecast_paths_for,
    list_bin_candidates,
    main,
    resolve_bin,
)


# ── fixtures ───────────────────────────────────────────────────────────


WIN_ENV = {
    "PATH": r"C:\Windows\system32;C:\tools\bin",
    "ProgramFiles": r"C:\Program Files",
    "ProgramFiles(x86)": r"C:\Program Files (x86)",
    "LOCALAPPDATA": r"C:\Users\dj\AppData\Local",
}

POSIX_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin"}


def fake_lister(tree: dict[str, list[str]]):
    """Directory lister backed by a plain dict of ``path -> subdir names``."""
    def list_dir(base: Path) -> list[str]:
        return tree.get(str(base), [])
    return list_dir


def fake_exists(present):
    """Existence probe that only answers True for *present* paths."""
    known = {str(p) for p in present}
    return lambda path: str(path) in known


# ── candidate generation ───────────────────────────────────────────────


class TestCandidateList:
    def test_posix_order_matches_legacy_behaviour(self):
        """env → PATH (icecast2 sweep, then icecast) → /usr/bin → /usr/local."""
        env = dict(POSIX_ENV, ICECAST_BIN="/opt/icecast/bin/icecast")
        candidates = list_bin_candidates(env, "posix", "icecast")

        assert candidates[0] == "/opt/icecast/bin/icecast"
        # Full sweep for icecast2 across PATH before any icecast lookup.
        assert candidates[1:4] == [
            "/usr/local/bin/icecast2", "/usr/bin/icecast2", "/bin/icecast2",
        ]
        assert candidates[4:7] == [
            "/usr/local/bin/icecast", "/usr/bin/icecast", "/bin/icecast",
        ]
        # Platform defaults are present (deduped against the PATH entries).
        assert "/usr/bin/icecast2" in candidates
        assert "/usr/bin/icecast" in candidates

    def test_posix_liquidsoap_defaults(self):
        candidates = list_bin_candidates(POSIX_ENV, "posix", "liquidsoap")
        assert "/usr/bin/liquidsoap" in candidates
        assert "/usr/local/bin/liquidsoap" in candidates
        assert all(not c.endswith(".exe") for c in candidates)

    def test_posix_never_globs_program_files(self):
        listed: list[Path] = []
        list_bin_candidates(
            POSIX_ENV, "posix", "icecast",
            list_dir=lambda base: listed.append(base) or [],
        )
        assert listed == []

    def test_windows_env_var_first(self):
        env = dict(WIN_ENV, ICECAST_BIN=r"D:\icecast\icecast.exe")
        candidates = list_bin_candidates(
            env, "nt", "icecast", list_dir=fake_lister({}),
        )
        assert candidates[0] == r"D:\icecast\icecast.exe"

    def test_windows_path_before_program_files(self):
        tree = {r"C:\Program Files": ["Icecast2 2.4.4"]}
        candidates = list_bin_candidates(
            WIN_ENV, "nt", "icecast", list_dir=fake_lister(tree),
        )
        path_hit = candidates.index(r"C:\tools\bin\icecast.exe")
        pf_hit = candidates.index(
            r"C:\Program Files\Icecast2 2.4.4\bin\icecast.exe"
        )
        assert path_hit < pf_hit

    @pytest.mark.parametrize(
        "dirname",
        ["Icecast2 2.4.4", "Icecast 2.4.4", "icecast", "Icecast2 Win32"],
    )
    def test_windows_icecast_directory_name_variants(self, dirname):
        """The install directory name differs between builds; all match."""
        tree = {r"C:\Program Files": [dirname, "Notepad++"]}
        candidates = list_bin_candidates(
            WIN_ENV, "nt", "icecast", list_dir=fake_lister(tree),
        )
        assert str(
            PureWindowsPath(r"C:\Program Files") / dirname / "bin"
            / "icecast.exe"
        ).replace("/", "\\") in [c.replace("/", "\\") for c in candidates]
        assert not any("Notepad++" in c for c in candidates)

    def test_windows_bin_subdir_and_direct_exe(self):
        tree = {r"C:\Program Files": ["Icecast 2.4.4"]}
        candidates = [
            c.replace("/", "\\")
            for c in list_bin_candidates(
                WIN_ENV, "nt", "icecast", list_dir=fake_lister(tree),
            )
        ]
        install = r"C:\Program Files\Icecast 2.4.4"
        assert candidates.index(install + r"\bin\icecast.exe") < \
            candidates.index(install + r"\icecast.exe")

    def test_windows_searches_program_files_x86(self):
        tree = {r"C:\Program Files (x86)": ["Icecast2 2.4.4"]}
        candidates = [
            c.replace("/", "\\")
            for c in list_bin_candidates(
                WIN_ENV, "nt", "icecast", list_dir=fake_lister(tree),
            )
        ]
        assert (
            r"C:\Program Files (x86)\Icecast2 2.4.4\bin\icecast.exe"
            in candidates
        )

    def test_windows_liquidsoap_localappdata_programs(self):
        tree = {
            r"C:\Users\dj\AppData\Local\Programs": ["liquidsoap-2.2.4"],
        }
        candidates = [
            c.replace("/", "\\")
            for c in list_bin_candidates(
                WIN_ENV, "nt", "liquidsoap", list_dir=fake_lister(tree),
            )
        ]
        assert (
            r"C:\Users\dj\AppData\Local\Programs\liquidsoap-2.2.4"
            r"\liquidsoap.exe" in candidates
        )
        assert r"C:\Program Files\Liquidsoap\liquidsoap.exe" in candidates

    def test_windows_fixed_fallbacks_without_any_listing(self):
        candidates = [
            c.replace("/", "\\")
            for c in list_bin_candidates(
                WIN_ENV, "nt", "icecast", list_dir=fake_lister({}),
            )
        ]
        assert r"C:\Program Files\Icecast2\bin\icecast.exe" in candidates

    def test_candidates_are_deduped(self):
        """"Icecast*" and "icecast*" both match; the path appears once."""
        tree = {r"C:\Program Files": ["Icecast2 2.4.4"]}
        candidates = list_bin_candidates(
            WIN_ENV, "nt", "icecast", list_dir=fake_lister(tree),
        )
        lowered = [c.lower() for c in candidates]
        assert len(lowered) == len(set(lowered))

    def test_missing_program_files_env_uses_defaults(self):
        env = {"PATH": ""}
        candidates = list_bin_candidates(
            env, "nt", "icecast", list_dir=fake_lister({}),
        )
        assert candidates  # never empty — fixed fallbacks remain


# ── resolution ─────────────────────────────────────────────────────────


class TestResolveBin:
    def test_picks_first_existing_candidate(self):
        tree = {r"C:\Program Files": ["Icecast2 2.4.4"]}
        target = r"C:\Program Files\Icecast2 2.4.4\bin\icecast.exe"
        found, tried = resolve_bin(
            "icecast", WIN_ENV, "nt",
            exists=fake_exists([target]),
            list_dir=fake_lister(tree),
        )
        assert found is not None
        assert str(found).replace("/", "\\") == target
        assert tried

    def test_returns_none_and_tried_list_on_failure(self):
        found, tried = resolve_bin(
            "liquidsoap", WIN_ENV, "nt",
            exists=lambda path: False,
            list_dir=fake_lister({}),
        )
        assert found is None
        assert len(tried) > 1
        assert any("liquidsoap.exe" in c for c in tried)

    def test_env_override_wins_without_existence_probe(self):
        env = dict(WIN_ENV, LIQUIDSOAP_BIN=r"D:\ls\liquidsoap.exe")
        found, tried = resolve_bin(
            "liquidsoap", env, "nt",
            exists=lambda path: False,
            list_dir=fake_lister({}),
        )
        assert str(found) == r"D:\ls\liquidsoap.exe"
        assert tried == [r"D:\ls\liquidsoap.exe"]

    def test_posix_resolution_prefers_path_over_usr_bin(self):
        found, _tried = resolve_bin(
            "liquidsoap", POSIX_ENV, "posix",
            exists=fake_exists(["/usr/local/bin/liquidsoap",
                                "/usr/bin/liquidsoap"]),
        )
        assert str(found) == "/usr/local/bin/liquidsoap"


# ── icecast share directories ──────────────────────────────────────────


class TestShareDirs:
    def test_official_windows_layout(self):
        root = PureWindowsPath(r"C:\Program Files\Icecast2 2.4.4")
        web = root / "share" / "icecast" / "web"
        admin = root / "share" / "icecast" / "admin"
        found_web, found_admin = find_icecast_share_dirs(
            root, exists=fake_exists([web, admin])
        )
        assert found_web == web
        assert found_admin == admin

    def test_flat_layout_fallback(self):
        root = PureWindowsPath(r"C:\Icecast")
        found_web, found_admin = find_icecast_share_dirs(
            root, exists=fake_exists([root / "web", root / "admin"])
        )
        assert found_web == root / "web"
        assert found_admin == root / "admin"

    def test_missing_dirs_return_none(self):
        found_web, found_admin = find_icecast_share_dirs(
            PureWindowsPath(r"C:\Nothing"), exists=lambda path: False
        )
        assert found_web is None
        assert found_admin is None

    def test_partial_install_resolves_each_independently(self):
        root = PureWindowsPath(r"C:\Program Files\Icecast 2.4.4")
        web = root / "share" / "icecast" / "web"
        found_web, found_admin = find_icecast_share_dirs(
            root, exists=fake_exists([web])
        )
        assert found_web == web
        assert found_admin is None

    def test_install_root_from_bin_exe(self):
        root = icecast_install_root(
            r"C:\Program Files\Icecast2 2.4.4\bin\icecast.exe"
        )
        assert str(root).replace("/", "\\") == \
            r"C:\Program Files\Icecast2 2.4.4"

    def test_install_root_from_direct_exe(self):
        root = icecast_install_root(r"C:\Icecast\icecast.exe")
        assert str(root).replace("/", "\\") == r"C:\Icecast"

    def test_install_root_none_when_unresolved(self):
        assert icecast_install_root(None) is None


# ── rendered config rewrite ────────────────────────────────────────────


TEMPLATE = (radio.CONFIG_TEMPLATE).read_text(encoding="utf-8")


class TestIcecastPathsFor:
    def test_windows_rewrite_swaps_only_the_two_paths(self):
        root = r"C:\Program Files\Icecast2 2.4.4"
        web = PureWindowsPath(root) / "share" / "icecast" / "web"
        admin = PureWindowsPath(root) / "share" / "icecast" / "admin"
        out = icecast_paths_for(
            root, TEMPLATE, None, None,
            platform="nt", exists=fake_exists([web, admin]),
        )

        assert f"<webroot>{web}</webroot>" in out
        assert f"<adminroot>{admin}</adminroot>" in out
        assert "/usr/share/icecast2" not in out

        # Everything else is byte-identical to the template.
        restored = out.replace(
            f"<webroot>{web}</webroot>",
            "<webroot>/usr/share/icecast2/web</webroot>",
        ).replace(
            f"<adminroot>{admin}</adminroot>",
            "<adminroot>/usr/share/icecast2/admin</adminroot>",
        )
        assert restored == TEMPLATE

    def test_posix_leaves_template_untouched(self):
        out = icecast_paths_for(
            "/usr", TEMPLATE, None, None,
            platform="posix", exists=lambda path: True,
        )
        assert out == TEMPLATE

    def test_env_override_wins_over_detection(self):
        root = r"C:\Program Files\Icecast2 2.4.4"
        detected_web = PureWindowsPath(root) / "share" / "icecast" / "web"
        out = icecast_paths_for(
            root, TEMPLATE, r"D:\radio\web", r"D:\radio\admin",
            platform="nt", exists=fake_exists([detected_web]),
        )
        assert r"<webroot>D:\radio\web</webroot>" in out
        assert r"<adminroot>D:\radio\admin</adminroot>" in out
        assert str(detected_web) not in out

    def test_env_override_applies_on_posix_too(self):
        out = icecast_paths_for(
            "/usr", TEMPLATE, "/srv/icecast/web", "/srv/icecast/admin",
            platform="posix", exists=lambda path: False,
        )
        assert "<webroot>/srv/icecast/web</webroot>" in out
        assert "<adminroot>/srv/icecast/admin</adminroot>" in out

    def test_warns_and_keeps_template_when_share_dirs_missing(self):
        warnings: list[str] = []
        out = icecast_paths_for(
            r"C:\Program Files\Icecast2 2.4.4", TEMPLATE, None, None,
            platform="nt", exists=lambda path: False,
            warn=warnings.append,
        )
        assert out == TEMPLATE
        assert warnings == ["Icecast webroot not found; using template paths"]

    def test_no_warning_when_both_found(self):
        root = r"C:\Icecast"
        warnings: list[str] = []
        icecast_paths_for(
            root, TEMPLATE, None, None, platform="nt",
            exists=fake_exists([PureWindowsPath(root) / "web",
                                PureWindowsPath(root) / "admin"]),
            warn=warnings.append,
        )
        assert warnings == []

    def test_unresolved_install_root_keeps_template(self):
        warnings: list[str] = []
        out = icecast_paths_for(
            None, TEMPLATE, None, None, platform="nt",
            exists=lambda path: True, warn=warnings.append,
        )
        assert out == TEMPLATE
        assert warnings

    def test_render_config_uses_icecast_bin(self, monkeypatch, tmp_path):
        """render_config threads the resolved binary into the rewrite."""
        monkeypatch.setenv("ICECAST_WEBROOT", str(tmp_path / "web"))
        monkeypatch.setenv("ICECAST_ADMINROOT", str(tmp_path / "admin"))
        rendered = radio.render_config(
            {
                "ICECAST_SOURCE_PASSWORD": "long-test-password-123",
                "ICECAST_ADMIN_PASSWORD": "long-test-password-123",
                "ICECAST_RELAY_PASSWORD": "long-test-password-123",
                "ICECAST_HOSTNAME": "radio.example.invalid",
                "ICECAST_PORT": "8000",
            },
            r"C:\Program Files\Icecast2 2.4.4\bin\icecast.exe",
        )
        assert f"<webroot>{tmp_path / 'web'}</webroot>" in rendered
        assert "${" not in rendered

    def test_effective_paths_reports_template_values_on_posix(self,
                                                              monkeypatch):
        monkeypatch.delenv("ICECAST_WEBROOT", raising=False)
        monkeypatch.delenv("ICECAST_ADMINROOT", raising=False)
        monkeypatch.setattr(radio.os, "name", "posix")
        web, admin = effective_icecast_paths("/usr/bin/icecast2")
        assert web == "/usr/share/icecast2/web"
        assert admin == "/usr/share/icecast2/admin"

    def test_effective_paths_honours_env_override(self, monkeypatch):
        monkeypatch.setenv("ICECAST_WEBROOT", "/srv/web")
        monkeypatch.setenv("ICECAST_ADMINROOT", "/srv/admin")
        web, admin = effective_icecast_paths("/usr/bin/icecast2")
        assert (web, admin) == ("/srv/web", "/srv/admin")


# ── dry run ────────────────────────────────────────────────────────────


@pytest.fixture
def dry_run_env(tmp_path, monkeypatch):
    """Stub out secrets, scripts, and log dirs for a start dry run."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "station.liq").write_text("# fake\n", encoding="utf-8")
    (scripts / "live.liq").write_text("# fake\n", encoding="utf-8")

    monkeypatch.setattr(radio, "SCRIPTS_DIR", scripts)
    monkeypatch.setattr(radio, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(radio, "RUNTIME_CONFIG",
                        tmp_path / "logs" / "icecast.runtime.xml")
    monkeypatch.setattr(radio, "ICECAST_PID_FILE", tmp_path / "icecast.pid")
    monkeypatch.setattr(radio, "LIQUIDSOAP_PID_FILE",
                        tmp_path / "liquidsoap.pid")
    monkeypatch.setattr(
        radio, "load_secrets",
        lambda: {
            "ICECAST_SOURCE_PASSWORD": "long-test-password-123",
            "ICECAST_ADMIN_PASSWORD": "long-test-password-123",
            "ICECAST_RELAY_PASSWORD": "long-test-password-123",
            "ICECAST_HOSTNAME": "radio.example.invalid",
            "ICECAST_PORT": "8000",
            "ICECAST_HOST": "127.0.0.1",
        },
    )
    monkeypatch.setattr(
        radio, "resolve_bin",
        lambda which, *a, **kw: (
            Path(f"/fake/bin/{which}"), [f"/fake/bin/{which}"]
        ),
    )
    return tmp_path


class TestDryRun:
    def test_start_dry_run_spawns_nothing(self, dry_run_env, monkeypatch,
                                          capsys):
        popen = mock.Mock(side_effect=AssertionError("Popen was called"))
        run = mock.Mock(side_effect=AssertionError("subprocess.run called"))
        spawn = mock.Mock(side_effect=AssertionError("spawn_process called"))
        monkeypatch.setattr(radio.subprocess, "Popen", popen)
        monkeypatch.setattr(radio.subprocess, "run", run)
        monkeypatch.setattr(radio, "spawn_process", spawn)

        assert main(["start", "--dry-run"]) == 0

        popen.assert_not_called()
        run.assert_not_called()
        spawn.assert_not_called()
        out = capsys.readouterr().out
        assert "DRY RUN: would start icecast at /fake/bin/icecast" in out
        assert "liquidsoap /fake/bin/liquidsoap" in out

    def test_start_dry_run_writes_no_runtime_config(self, dry_run_env):
        assert main(["start", "--dry-run"]) == 0
        assert not (dry_run_env / "logs" / "icecast.runtime.xml").exists()

    def test_start_dry_run_reports_share_dirs(self, dry_run_env, capsys):
        main(["start", "--dry-run"])
        out = capsys.readouterr().out
        assert "webroot:" in out
        assert "adminroot:" in out

    def test_start_dry_run_uses_live_script(self, dry_run_env, capsys):
        assert main(["start", "--live", "--dry-run"]) == 0
        assert "live.liq" in capsys.readouterr().out

    def test_start_dry_run_fails_on_missing_binary(self, dry_run_env,
                                                   monkeypatch):
        monkeypatch.setattr(
            radio, "resolve_bin",
            lambda which, *a, **kw: (None, [f"/nowhere/{which}"]),
        )
        with pytest.raises(EngineError, match="not found"):
            radio.do_start_dry_run()

    def test_start_dry_run_validates_secrets(self, dry_run_env, monkeypatch):
        monkeypatch.setattr(
            radio, "load_secrets",
            lambda: {
                "ICECAST_SOURCE_PASSWORD": "short",
                "ICECAST_ADMIN_PASSWORD": "short",
                "ICECAST_RELAY_PASSWORD": "short",
                "ICECAST_HOSTNAME": "radio.example.invalid",
                "ICECAST_PORT": "8000",
            },
        )
        with pytest.raises(EngineError, match="at least 12 characters"):
            radio.do_start_dry_run()

    def test_stop_dry_run_terminates_nothing(self, dry_run_env, monkeypatch,
                                             capsys):
        (dry_run_env / "icecast.pid").write_text("4242\n", encoding="utf-8")
        terminate = mock.Mock(
            side_effect=AssertionError("terminate_pid was called")
        )
        monkeypatch.setattr(radio, "terminate_pid", terminate)
        monkeypatch.setattr(radio, "is_process_alive", lambda pid: True)

        assert main(["stop", "--dry-run"]) == 0

        terminate.assert_not_called()
        assert (dry_run_env / "icecast.pid").exists()
        assert "DRY RUN: would stop Icecast (PID 4242)." in \
            capsys.readouterr().out


# ── paths command ──────────────────────────────────────────────────────


class TestPathsCommand:
    def test_paths_prints_platform_and_dirs(self, capsys):
        code = main(["paths"])
        assert code in (0, 1)
        out = capsys.readouterr().out
        assert "platform:" in out
        assert ("Windows" in out) or ("POSIX" in out)
        for key in ("icecast:", "liquidsoap:", "icecast-webroot:",
                    "icecast-adminroot:"):
            assert key in out

    def test_paths_show_lists_tried_candidates_on_failure(self, monkeypatch,
                                                          capsys):
        monkeypatch.setattr(
            radio, "resolve_bin",
            lambda which, *a, **kw: (None, [f"/nowhere/{which}"]),
        )
        assert main(["paths", "--show"]) == 1
        out = capsys.readouterr().out
        assert "tried for icecast:" in out
        assert "/nowhere/liquidsoap" in out

    def test_bin_paths_verbose_lists_candidates(self, monkeypatch, capsys):
        monkeypatch.setattr(
            radio, "resolve_bin",
            lambda which, *a, **kw: (None, [f"/nowhere/{which}"]),
        )
        assert main(["bin-paths", "--verbose"]) == 1
        assert "/nowhere/icecast" in capsys.readouterr().out
