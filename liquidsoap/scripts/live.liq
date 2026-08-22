# Live-assist output for Liquidsoap 2.2.

radio_root = environment.get(
  default="/home/<you>/radio-tools/liquidsoap",
  "RADIO_ROOT"
)
playlist_path = environment.get(
  default=radio_root ^ "/data/playlist.m3u",
  "RADIO_PLAYLIST"
)
trigger_path = environment.get(
  default=radio_root ^ "/data/playlist.trigger",
  "RADIO_PLAYLIST_TRIGGER"
)
icecast_host = environment.get(default="127.0.0.1", "ICECAST_HOST")
icecast_port = int_of_string(
  default=8000,
  environment.get(default="8000", "ICECAST_PORT")
)
icecast_password = environment.get(default="", "ICECAST_SOURCE_PASSWORD")

rotation_playlist = playlist(
  id="rotation",
  mode="normal",
  loop=true,
  prefetch=1,
  reload_mode="never",
  prefix="replaygain:",
  playlist_path
)

def reload_rotation()
  log.important(label="rotation", "Playlist trigger changed; reloading.")
  rotation_playlist.reload(empty_queue=true)
end

if file.exists(trigger_path) then
  ignore(file.watch(trigger_path, reload_rotation))
else
  log.important(label="rotation", "Playlist trigger is absent; reload watch disabled.")
end

rotation = replaygain(rotation_playlist)
rotation = crossfade(
  duration=4.,
  fade_in=4.,
  fade_out=4.,
  rotation
)

def standby_metadata(_)
  [("title", "Rotation unavailable")]
end

standby = metadata.map(
  update=false,
  insert_missing=true,
  standby_metadata,
  blank()
)
rotation_bed = fallback(
  id="rotation_bed",
  track_sensitive=false,
  [rotation, standby]
)

# PulseAudio is the default live input. It is fallible so a missing/disconnected
# device hands control back to rotation. clock_safe=false lets the main output
# clock drive it alongside file sources.
live_input = input.pulseaudio(
  id="live_input",
  clock_safe=false,
  fallible=true
)

# JACK alternative: comment the input.pulseaudio block above and uncomment:
# live_input = input.jack(id="live_input", clock_safe=false, fallible=true)

def live_transition(previous, next)
  add(
    normalize=false,
    [fade.out(duration=1., previous), fade.in(duration=1., next)]
  )
end

# This active sink advances the rotation even while live_input has priority.
# Sharing the source is clock-safe: each output receives the same cached frame.
output.dummy(
  id="rotation_keepalive",
  fallible=false,
  rotation_bed
)

program = fallback(
  id="live_priority",
  track_sensitive=false,
  transition_length=1.,
  transitions=[live_transition, live_transition],
  [live_input, rotation_bed]
)

output.icecast(
  %mp3(bitrate=128, samplerate=44100, stereo=true),
  id="icecast_output",
  host=icecast_host,
  port=icecast_port,
  password=icecast_password,
  mount="/radio.mp3",
  name="Personal station",
  description="Live assist with continuous rotation",
  genre="Mixed",
  public=false,
  encoding="UTF-8",
  program
)

