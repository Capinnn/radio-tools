import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa: E402
from make_test_tracks import write_tone  # noqa: E402


@pytest.fixture
def music_dir(tmp_path):
    """A folder of five short sine WAVs, plus one file that is not audio."""
    folder = tmp_path / "music"
    folder.mkdir()
    for stem, frequency, seconds in (
        ("alpha", 220.0, 1.0),
        ("bravo", 262.0, 1.5),
        ("charlie", 330.0, 2.0),
        ("delta", 392.0, 2.5),
        ("echo", 440.0, 3.0),
    ):
        write_tone(str(folder / f"{stem}.wav"), frequency, seconds)
    (folder / "notes.txt").write_text("not audio")
    return folder


@pytest.fixture
def store(tmp_path, music_dir):
    return core.Store(str(tmp_path), data_dir=str(tmp_path / "data"),
                      music_dir=str(music_dir))


@pytest.fixture
def scanned(store):
    store.scan()
    return store


@pytest.fixture
def client(tmp_path, music_dir, monkeypatch):
    """A Flask test client wired to a throwaway data + music folder."""
    monkeypatch.setenv("STUDIO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("STUDIO_MUSIC_DIR", str(music_dir))
    for module in ("app",):
        sys.modules.pop(module, None)
    import app as flask_app
    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as test_client:
        test_client.store = flask_app.store
        yield test_client
    sys.modules.pop("app", None)
