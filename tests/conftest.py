"""Test fixtures: create synthetic audio files with real tags for testing.

We create small WAV files (pure stdlib + wave module) and tag them with
mutagen where possible. MP3 files need actual audio data, so we generate
minimal valid MP3s using mutagen's ability to create empty files with tags.
"""

import json
import struct
import wave
import io
from pathlib import Path

import pytest

try:
    from mutagen.id3 import ID3, TIT2, TPE1, TALB
    from mutagen.mp3 import MP3
    from mutagen.flac import FLAC
    from mutagen.wave import WAVE
    _HAS_MUTAGEN = True
except Exception:
    _HAS_MUTAGEN = False


def _make_wav(path: Path, duration_sec: float = 2.0, sample_rate: int = 8000):
    """Create a minimal WAV file with silence."""
    n_frames = int(duration_sec * sample_rate)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        # Write silence (zeros)
        frames = b"\x00\x00" * n_frames
        wav.writeframes(frames)


def _make_wav_with_tags(path: Path, artist: str, title: str,
                        duration_sec: float = 2.0):
    """Create a WAV file and set ID3 tags."""
    _make_wav(path, duration_sec)
    if _HAS_MUTAGEN:
        try:
            w = WAVE(str(path))
            if w.tags is None:
                w.add_tags()
            w.tags.add(TPE1(encoding=3, text=artist))
            w.tags.add(TIT2(encoding=3, text=title))
            w.tags.add(TALB(encoding=3, text="Test Album"))
            w.save()
        except Exception:
            pass


def _make_mp3(path: Path, artist: str = None, title: str = None,
              duration_sec: float = 2.0):
    """Create a minimal MP3 file with optional tags.

    We create a tiny silent MP3 by generating a few silent frames.
    """
    # Minimal MP3: ID3 header + a few silent frames
    # An MP3 frame at 32kbps, 32kHz, mono is 104 bytes
    # Frame header: 0xFF 0xE4 0x00 (sync + flags for 32kbps/32kHz/mono)
    frame_header = b"\xff\xe4\x00"
    frame_data = b"\x00" * 101  # rest of frame (104 - 3 header bytes)
    n_frames = int(duration_sec * 38)  # ~38 frames/sec at 32kHz

    with open(path, "wb") as f:
        # Write ID3v2 header if we have tags
        if artist or title:
            from mutagen.id3 import ID3, TIT2, TPE1, TALB
            tags = ID3()
            if title:
                tags.add(TIT2(encoding=3, text=title))
            if artist:
                tags.add(TPE1(encoding=3, text=artist))
            tags.add(TALB(encoding=3, text="Test Album"))
            # Save tags to a temp buffer, then write to file
            buf = io.BytesIO()
            tags.save(buf)
            f.write(buf.getvalue())

        # Write silent frames
        for _ in range(n_frames):
            f.write(frame_header + frame_data)


@pytest.fixture
def music_library(tmp_path):
    """Create a test music library with categorized files.

    Structure:
      library/
        A/
          Artist One - Song Alpha.wav    (tagged: artist + title)
          Artist Two - Song Beta.wav     (tagged)
        B/
          Artist Three - Song Gamma.wav  (tagged)
          Artist One - Song Delta.wav    (tagged, same artist as song1 for gap testing)
        C/
          Artist Four - Song Epsilon.wav (tagged)
        untagged - mystery.wav           (no tags — for trackcheck fix testing)
        dup/
          Artist One - Song Alpha copy.wav  (content duplicate of song1)
    """
    lib = tmp_path / "library"
    lib.mkdir()

    # Category A
    a_dir = lib / "A"
    a_dir.mkdir()
    _make_wav_with_tags(a_dir / "Artist One - Song Alpha.wav",
                        artist="Artist One", title="Song Alpha", duration_sec=3.0)
    _make_wav_with_tags(a_dir / "Artist Two - Song Beta.wav",
                        artist="Artist Two", title="Song Beta", duration_sec=3.0)

    # Category B
    b_dir = lib / "B"
    b_dir.mkdir()
    _make_wav_with_tags(b_dir / "Artist Three - Song Gamma.wav",
                        artist="Artist Three", title="Song Gamma", duration_sec=3.0)
    _make_wav_with_tags(b_dir / "Artist One - Song Delta.wav",
                        artist="Artist One", title="Song Delta", duration_sec=3.0)

    # Category C
    c_dir = lib / "C"
    c_dir.mkdir()
    _make_wav_with_tags(c_dir / "Artist Four - Song Epsilon.wav",
                        artist="Artist Four", title="Song Epsilon", duration_sec=3.0)

    # Untagged file (for trackcheck --fix testing)
    _make_wav(lib / "untagged - mystery.wav", duration_sec=2.0)

    # Duplicate of Song Alpha (same content)
    import shutil
    shutil.copy2(a_dir / "Artist One - Song Alpha.wav",
                 lib / "Artist One - Song Alpha copy.wav")

    return lib


@pytest.fixture
def rotation_config(tmp_path):
    """Create a test rotation config."""
    config = {
        "categories": {
            "A": {"sph": 4, "description": "Heavy rotation"},
            "B": {"sph": 3, "description": "Medium rotation"},
            "C": {"sph": 2, "description": "Light rotation"},
        },
        "rules": {
            "artist_gap": 2,
            "title_gap": 1,
            "category_gap": 1,
        },
        "dayparts": {
            "Morning": {"weights": {"A": 1.5, "B": 1.0, "C": 0.8}},
            "Evening": {"weights": {"A": 0.8, "B": 1.0, "C": 1.2}},
        },
    }
    path = tmp_path / "rotation.json"
    with open(path, "w") as f:
        json.dump(config, f)
    return path


@pytest.fixture
def schedule_config(tmp_path):
    """Create a test schedule config."""
    config = {
        "dayparts": {
            "Morning": {"start": "06:00", "end": "12:00", "rotation": "rotation.json"},
            "Evening": {"start": "12:00", "end": "18:00", "rotation": "rotation.json"},
            "Overnight": {"start": "18:00", "end": "06:00", "rotation": "rotation.json"},
        },
        "grid": {
            "mon": ["Overnight", "Overnight", "Morning", "Morning", "Evening", "Evening", "Overnight", "Overnight"],
            "tue": ["Overnight", "Overnight", "Morning", "Morning", "Evening", "Evening", "Overnight", "Overnight"],
            "wed": ["Overnight", "Overnight", "Morning", "Morning", "Evening", "Evening", "Overnight", "Overnight"],
            "thu": ["Overnight", "Overnight", "Morning", "Morning", "Evening", "Evening", "Overnight", "Overnight"],
            "fri": ["Overnight", "Overnight", "Morning", "Morning", "Evening", "Evening", "Overnight", "Overnight"],
            "sat": ["Overnight", "Overnight", "Morning", "Morning", "Evening", "Evening", "Overnight", "Overnight"],
            "sun": ["Overnight", "Overnight", "Morning", "Morning", "Evening", "Evening", "Overnight", "Overnight"],
        },
    }
    path = tmp_path / "schedule.json"
    with open(path, "w") as f:
        json.dump(config, f)
    return path