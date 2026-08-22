# Continuous rotation output for Liquidsoap 2.2.

radio_root = environment.get(
  default="/home/calebpineda/radio-tools/liquidsoap",
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

# prefix="replaygain:" reads an existing track tag or computes one while the
# request is prepared. replaygain() applies that per-track gain to the PCM.
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

# start.sh creates the trigger before Liquidsoap starts. The existence guard
# also lets `liquidsoap --check` work in a fresh checkout with no runtime data.
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

program = fallback(
  id="program",
  track_sensitive=false,
  [rotation, standby]
)

output.icecast(
  %mp3(bitrate=128, samplerate=44100, stereo=true),
  id="icecast_output",
  host=icecast_host,
  port=icecast_port,
  password=icecast_password,
  mount="/radio.mp3",
  name="Personal station",
  description="Continuous rotation",
  genre="Mixed",
  public=false,
  encoding="UTF-8",
  program
)

