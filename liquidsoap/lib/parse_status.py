#!/usr/bin/env python3
"""Extract key fields from an Icecast status-json.xsl response.

Usage: parse_status.py STATUS_FILE

Prints one field per line in KEY=VALUE format:
  listeners=N
  server_type=audio/mpeg
  title=Aurelia - Glass Hour
  audio_info=channels=2;samplerate=44100;bitrate=128
"""
import json
import sys

if len(sys.argv) < 2:
    sys.exit(2)

try:
    doc = json.load(open(sys.argv[1]))
    src = doc.get("icestats", {}).get("source", {})
except Exception:
    print("listeners=?")
    print("server_type=?")
    print("title=?")
    print("audio_info=?")
    sys.exit(0)

print(f"listeners={src.get('listeners', '?')}")
print(f"server_type={src.get('server_type', '?')}")
print(f"title={src.get('title', '?')}")
print(f"audio_info={src.get('audio_info', '?')}")