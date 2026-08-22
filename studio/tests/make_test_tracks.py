#!/usr/bin/env python3
"""Generate short sine-wave WAV files for trying the console out.

    python tests/make_test_tracks.py               # into studio/music/
    python tests/make_test_tracks.py /tmp/tracks   # somewhere else

Each file gets a distinct pitch and duration so it is obvious which one is
playing, and they are long enough to exercise a crossfade.
"""

import math
import os
import struct
import sys
import wave

SAMPLE_RATE = 44100

# (filename stem, frequency Hz, seconds)
TRACKS = [
    ("test-tone-a-220hz", 220.0, 6.0),
    ("test-tone-c-262hz", 261.63, 7.5),
    ("test-tone-e-330hz", 329.63, 9.0),
    ("test-tone-g-392hz", 392.0, 5.0),
    ("test-tone-a-440hz", 440.0, 10.0),
]


def write_tone(path, frequency, seconds, amplitude=0.28):
    """Write one stereo 16-bit sine tone with 50ms ramps at both ends."""
    frames = int(SAMPLE_RATE * seconds)
    ramp = int(SAMPLE_RATE * 0.05)
    samples = bytearray()
    for index in range(frames):
        envelope = 1.0
        if index < ramp:
            envelope = index / ramp
        elif index > frames - ramp:
            envelope = max(0.0, (frames - index) / ramp)
        value = amplitude * envelope * math.sin(2 * math.pi * frequency * index / SAMPLE_RATE)
        sample = int(max(-1.0, min(1.0, value)) * 32767)
        samples += struct.pack("<hh", sample, sample)

    with wave.open(path, "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(bytes(samples))


def main(target=None):
    target = target or os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "music")
    os.makedirs(target, exist_ok=True)
    written = []
    for stem, frequency, seconds in TRACKS:
        path = os.path.join(target, stem + ".wav")
        write_tone(path, frequency, seconds)
        written.append(path)
        print(f"  {os.path.basename(path):<28} {frequency:>7.2f} Hz  {seconds:>4.1f}s")
    print(f"\n{len(written)} files in {target}")
    return written


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
