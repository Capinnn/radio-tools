#!/usr/bin/env python3
"""Check a binary file for MP3 frame sync words. Exits 0 if found, 1 if not."""
import sys

if len(sys.argv) < 2:
    sys.exit(2)
data = open(sys.argv[1], "rb").read()
found = any(
    data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0
    for i in range(len(data) - 1)
)
sys.exit(0 if found else 1)