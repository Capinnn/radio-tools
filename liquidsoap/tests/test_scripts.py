import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "bin" / "gen-playlist.sh"


def run_generator(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(GENERATOR), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def rotation_file(tmp_path: Path) -> Path:
    path = tmp_path / "rotation.json"
    path.write_text(
        json.dumps(
            {
                "categories": {"A": {"sph": 4}},
                "rules": {"artist_gap": 2, "title_gap": 1, "category_gap": 1},
                "dayparts": {},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_gen_playlist_help():
    result = run_generator("--help")
    assert result.returncode == 0, result.stderr
    assert "Usage: gen-playlist.sh" in result.stdout
    assert "--loop" in result.stdout
    assert "--dry-run" in result.stdout


def test_dry_run_source_path_writes_nothing(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    rotation = rotation_file(tmp_path)
    output = tmp_path / "out" / "playlist.m3u"
    trigger = tmp_path / "state" / "reload.trigger"

    result = run_generator(
        "--source",
        str(music),
        "--rotation",
        str(rotation),
        "--output",
        str(output),
        "--trigger",
        str(trigger),
        "--hour",
        "8",
        "--seed",
        "42",
        "--dry-run",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Dry run:" in result.stdout
    assert str(music) in result.stdout
    assert "--hour 8" in result.stdout
    assert not output.exists()
    assert not trigger.exists()


def test_loop_dry_run_accepts_library_path_without_sleeping(tmp_path):
    library = tmp_path / "library.json"
    library.write_text("[]\n", encoding="utf-8")
    rotation = rotation_file(tmp_path)
    output = tmp_path / "playlist.m3u"

    result = run_generator(
        "--library",
        str(library),
        "--rotation",
        str(rotation),
        "--output",
        str(output),
        "--loop",
        "--dry-run",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "--library" in result.stdout
    assert str(library) in result.stdout
    assert "Next generation" not in result.stdout
    assert not output.exists()

