/* RADIO STUDIO — console client.
 *
 * Audio graph:
 *
 *   deckA.gain ─┐
 *               ├→ musicBus → duckGain → masterGain ─┬→ destination
 *   deckB.gain ─┘                ↑                   └→ splitter → analyserL/R
 *                            micGain ────────────────┘ (joins at masterGain)
 *
 * Two <audio> decks let one track fade down while the next fades up, which is
 * what makes a real crossfade possible. Deck gain is the fade/crossfade stage,
 * duckGain is talk-over, masterGain is the master fader. The mic is patched in
 * after the duck so talking over the music never ducks the mic itself.
 */

'use strict';

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  config: {},
  library: [],
  visible: [],
  queue: [],
  playlists: [],
  categories: [],
  dayparts: [],
  schedule: [],
  current: null,
  played: [],          // track ids, most recent first — drives the Prev button
  filters: { q: '', genre: '', artist: '', category: '', bpmMin: '', bpmMax: '', sort: 'artist' },
  editingId: null,
  filling: false,
  scrubbing: false,
};

/* ------------------------------------------------------------------ utils */

function fmtTime(seconds) {
  if (!isFinite(seconds) || seconds < 0) seconds = 0;
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
    : `${m}:${String(s).padStart(2, '0')}`;
}

function fmtBitrate(bps) {
  return bps ? `${Math.round(bps / 1000)} kbps` : '';
}

function fmtRate(hz) {
  return hz ? `${(hz / 1000).toFixed(1)} kHz` : '';
}

function toast(message, kind = '') {
  const node = document.createElement('div');
  node.className = `toast ${kind}`.trim();
  node.textContent = message;
  $('#toasts').appendChild(node);
  setTimeout(() => {
    node.classList.add('leaving');
    setTimeout(() => node.remove(), 200);
  }, 3400);
}

async function api(path, options = {}) {
  const opts = { ...options };
  if (opts.body && !(opts.body instanceof FormData)) {
    opts.headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
    opts.body = JSON.stringify(opts.body);
  }
  const response = await fetch(path, opts);
  const isJson = (response.headers.get('content-type') || '').includes('json');
  const payload = isJson ? await response.json() : null;
  if (!response.ok) {
    throw new Error((payload && payload.error) || `${response.status} ${response.statusText}`);
  }
  return payload;
}

function trackById(id) {
  return state.library.find((t) => t.id === id) || null;
}

function categoryById(id) {
  return state.categories.find((c) => c.id === id) || null;
}

function setRangeFill(input) {
  const min = Number(input.min || 0);
  const max = Number(input.max || 100);
  const pct = max > min ? ((Number(input.value) - min) / (max - min)) * 100 : 0;
  input.style.setProperty('--pct', `${pct}%`);
}

/* ------------------------------------------------------------------ audio */

const audio = {
  ctx: null,
  decks: [],
  active: 0,
  musicBus: null,
  duckGain: null,
  masterGain: null,
  micGain: null,
  micStream: null,
  analyserL: null,
  analyserR: null,
  crossfadeTimer: 0,
  crossfading: false,
  talkHeld: false,
  talkLatched: false,
  peaks: [0, 0],
};

function makeDeck(id) {
  return {
    id,
    el: document.getElementById(`deck${id}`),
    source: null,
    gain: null,
    track: null,
    ended: false,
    fadeToken: 0,
  };
}

function liveDeck() { return audio.decks[audio.active]; }
function idleDeck() { return audio.decks[1 - audio.active]; }

function ensureAudio() {
  if (audio.ctx) {
    if (audio.ctx.state === 'suspended') audio.ctx.resume();
    return audio.ctx;
  }

  const Ctx = window.AudioContext || window.webkitAudioContext;
  const ctx = new Ctx();
  audio.ctx = ctx;

  audio.musicBus = ctx.createGain();
  audio.duckGain = ctx.createGain();
  audio.masterGain = ctx.createGain();
  audio.micGain = ctx.createGain();

  audio.duckGain.gain.value = 1;
  audio.masterGain.gain.value = Number(state.config.volume ?? 0.85);
  audio.micGain.gain.value = 0;

  audio.musicBus.connect(audio.duckGain);
  audio.duckGain.connect(audio.masterGain);
  audio.micGain.connect(audio.masterGain);
  audio.masterGain.connect(ctx.destination);

  const splitter = ctx.createChannelSplitter(2);
  audio.masterGain.connect(splitter);
  audio.analyserL = ctx.createAnalyser();
  audio.analyserR = ctx.createAnalyser();
  for (const analyser of [audio.analyserL, audio.analyserR]) {
    analyser.fftSize = 1024;
    analyser.smoothingTimeConstant = 0.5;
  }
  splitter.connect(audio.analyserL, 0);
  splitter.connect(audio.analyserR, 1);

  for (const deck of audio.decks) {
    deck.source = ctx.createMediaElementSource(deck.el);
    deck.gain = ctx.createGain();
    deck.gain.gain.value = 0;
    deck.source.connect(deck.gain);
    deck.gain.connect(audio.musicBus);
  }

  if (ctx.state === 'suspended') ctx.resume();
  startMeters();
  return ctx;
}

/**
 * Anchor the parameter at `from`, then ramp. Anchoring first matters:
 * cancelScheduledValues() drops events at times >= now, so a setValueAtTime()
 * made by the caller beforehand would be wiped and the ramp would start from
 * wherever the parameter happened to be.
 */
function rampFrom(param, from, to, seconds) {
  const now = audio.ctx.currentTime;
  param.cancelScheduledValues(now);
  param.setValueAtTime(from, now);
  param.linearRampToValueAtTime(to, now + Math.max(0.02, seconds));
}

function stopCrossfadeTimer() {
  if (audio.crossfadeTimer) {
    clearInterval(audio.crossfadeTimer);
    audio.crossfadeTimer = 0;
  }
  audio.crossfading = false;
}

async function safePlay(deck) {
  try {
    await deck.el.play();
  } catch (err) {
    if (err && err.name === 'NotAllowedError') {
      toast('Click anywhere on the page once to allow audio playback.', 'warn');
    } else if (err && err.name !== 'AbortError') {
      toast(`Playback failed: ${err.message}`, 'error');
    }
  }
}

function loadDeck(deck, track) {
  deck.track = track;
  deck.ended = false;
  deck.el.src = `/api/audio/${track.id}`;
  deck.el.load();
}

/** Start a track on the live deck, optionally ramping up from silence. */
async function playTrack(track, { fadeIn = false } = {}) {
  ensureAudio();
  stopCrossfadeTimer();

  const other = idleDeck();
  const cued = other.track && other.track.id === track.id && other.el.readyState >= 2;
  if (cued) {
    // Already preloaded on the idle deck: swap rather than reload.
    liveDeck().el.pause();
    liveDeck().gain.gain.cancelScheduledValues(audio.ctx.currentTime);
    liveDeck().gain.gain.value = 0;
    other.el.currentTime = 0;
    audio.active = 1 - audio.active;
  } else {
    other.el.pause();
    other.gain.gain.value = 0;
    loadDeck(liveDeck(), track);
  }

  const deck = liveDeck();
  deck.fadeToken += 1;
  deck.ended = false;
  const param = deck.gain.gain;
  param.cancelScheduledValues(audio.ctx.currentTime);
  param.setValueAtTime(fadeIn ? 0 : 1, audio.ctx.currentTime);
  await safePlay(deck);
  if (fadeIn) rampFrom(param, 0, 1, Number(state.config.fadeSeconds) || 2);

  state.current = deck.track;
  state.played.unshift(deck.track.id);
  state.played = state.played.slice(0, 50);
  recordPlay(deck.track);
  renderNowPlaying();
  renderLibrary();
  cueUpcoming();
  return deck;
}

function recordPlay(track) {
  track.playCount = (track.playCount || 0) + 1;
  track.lastPlayed = Date.now() / 1000;
  api('/api/plays', { method: 'POST', body: { trackId: track.id } }).catch(() => {});
}

/** Preload the head of the queue onto the idle deck so a crossfade is gapless. */
function cueUpcoming() {
  const next = peekNext();
  if (!next || !audio.ctx || audio.crossfading) return;
  const other = idleDeck();
  if (!other.track || other.track.id !== next.id) {
    loadDeck(other, next);
    other.gain.gain.value = 0;
  }
}

function peekNext() {
  for (const item of state.queue) {
    const track = trackById(item.trackId);
    if (track) return track;
  }
  return null;
}

/** A crossfade longer than the track can absorb would start on frame one. */
function effectiveCrossfade(duration) {
  const configured = Number(state.config.crossfade) || 0;
  if (!isFinite(duration) || duration <= 0) return configured;
  return Math.min(configured, duration * 0.45);
}

/**
 * Equal-power crossfade, stepped every 50ms. cos/sin keeps summed power
 * constant; two linear ramps would dip in the middle and sound like a hole.
 */
function runCrossfade(outgoing, incoming, seconds) {
  const outParam = outgoing.gain.gain;
  const inParam = incoming.gain.gain;
  const startLevel = outParam.value;
  const t0 = audio.ctx.currentTime;

  outParam.cancelScheduledValues(t0);
  outParam.setValueAtTime(startLevel, t0);
  inParam.cancelScheduledValues(t0);
  inParam.setValueAtTime(0, t0);

  const started = performance.now();
  stopCrossfadeTimer();
  audio.crossfading = true;

  return new Promise((resolve) => {
    audio.crossfadeTimer = setInterval(() => {
      const p = Math.min(1, (performance.now() - started) / (seconds * 1000));
      const now = audio.ctx.currentTime;
      outParam.linearRampToValueAtTime(Math.cos(p * (Math.PI / 2)) * startLevel, now + 0.055);
      inParam.linearRampToValueAtTime(Math.sin(p * (Math.PI / 2)), now + 0.055);
      if (p >= 1) {
        clearInterval(audio.crossfadeTimer);
        audio.crossfadeTimer = 0;
        setTimeout(() => {
          const t = audio.ctx.currentTime;
          outParam.cancelScheduledValues(t);
          outParam.setValueAtTime(0, t);
          inParam.cancelScheduledValues(t);
          inParam.setValueAtTime(1, t);
          audio.crossfading = false;
          resolve();
        }, 70);
      }
    }, 50);
  });
}

/** Move to the next queued track. `seconds` of 0 is a hard cut. */
async function advance(seconds) {
  if (audio.crossfading) return;
  const item = state.queue.shift();
  const next = item ? trackById(item.trackId) : null;
  if (item) { saveQueue(); renderQueue(); }
  if (!next) {
    if (item) return advance(seconds);   // stale id, try the one behind it
    stopPlayback();
    fillQueue();
    return;
  }

  const fade = Math.max(0, seconds === undefined
    ? effectiveCrossfade(state.current ? state.current.duration : 0)
    : seconds);

  ensureAudio();
  if (fade < 0.05) {
    await playTrack(next);
    fillQueue();
    return;
  }

  const outgoing = liveDeck();
  const incoming = idleDeck();
  if (!incoming.track || incoming.track.id !== next.id) loadDeck(incoming, next);
  incoming.el.currentTime = 0;
  incoming.fadeToken += 1;
  outgoing.fadeToken += 1;
  incoming.gain.gain.cancelScheduledValues(audio.ctx.currentTime);
  incoming.gain.gain.setValueAtTime(0, audio.ctx.currentTime);
  await safePlay(incoming);

  audio.active = 1 - audio.active;
  state.current = next;
  state.played.unshift(next.id);
  state.played = state.played.slice(0, 50);
  recordPlay(next);
  renderNowPlaying();
  renderLibrary();

  await runCrossfade(outgoing, incoming, fade);
  outgoing.el.pause();
  cueUpcoming();
  fillQueue();
}

function stopPlayback() {
  stopCrossfadeTimer();
  for (const deck of audio.decks) {
    deck.el.pause();
    if (deck.gain) deck.gain.gain.value = 0;
  }
  state.current = null;
  renderNowPlaying();
  renderLibrary();
}

function isPlaying() {
  const deck = liveDeck();
  return Boolean(deck.track) && !deck.el.paused && !deck.el.ended;
}

async function togglePlay() {
  ensureAudio();
  const deck = liveDeck();
  if (!deck.track) {
    const next = peekNext();
    if (!next) {
      if (state.config.autoDj) { await fillQueue(); if (peekNext()) return advance(0); }
      toast('Queue is empty — add a track from the library.', 'warn');
      return;
    }
    state.queue.shift();
    saveQueue();
    renderQueue();
    await playTrack(next);
    fillQueue();
    return;
  }
  if (deck.el.paused) {
    // A fade-out leaves the deck silent; resuming has to restore its level.
    if (deck.gain.gain.value < 0.02) rampFrom(deck.gain.gain, 0, 1, 0.15);
    await safePlay(deck);
  } else {
    deck.el.pause();
  }
  renderNowPlaying();
}

function fadeOut() {
  ensureAudio();
  const deck = liveDeck();
  if (!deck.track || deck.el.paused) return;
  stopCrossfadeTimer();
  const seconds = Number(state.config.fadeSeconds) || 2;
  const param = deck.gain.gain;
  rampFrom(param, param.value, 0, seconds);
  const token = (deck.fadeToken += 1);
  flashButton($('#fadeOutBtn'), seconds * 1000);
  setTimeout(() => {
    if (deck.fadeToken !== token) return;   // superseded by a newer action
    param.cancelScheduledValues(audio.ctx.currentTime);
    param.setValueAtTime(0, audio.ctx.currentTime);
    deck.el.pause();
    renderNowPlaying();
  }, seconds * 1000 + 60);
}

async function fadeIn() {
  ensureAudio();
  const deck = liveDeck();
  if (!deck.track) { await togglePlay(); return; }
  stopCrossfadeTimer();
  const seconds = Number(state.config.fadeSeconds) || 2;
  const param = deck.gain.gain;
  deck.fadeToken += 1;
  param.cancelScheduledValues(audio.ctx.currentTime);
  param.setValueAtTime(0, audio.ctx.currentTime);
  if (deck.el.paused) await safePlay(deck);
  rampFrom(param, 0, 1, seconds);
  flashButton($('#fadeInBtn'), seconds * 1000);
  renderNowPlaying();
}

function flashButton(button, ms) {
  button.classList.add('active');
  setTimeout(() => button.classList.remove('active'), ms);
}

function applyDuck() {
  ensureAudio();
  const ducked = audio.talkHeld || audio.talkLatched;
  const level = ducked ? Math.max(0, Math.min(1, Number(state.config.duckLevel) || 0.2)) : 1;
  rampFrom(audio.duckGain.gain, audio.duckGain.gain.value, level, 0.18);
  $('#talkBtn').classList.toggle('active', ducked);
  renderNowPlaying();
}

function setTalk(held, latched) {
  if (held !== undefined) audio.talkHeld = held;
  if (latched !== undefined) audio.talkLatched = latched;
  applyDuck();
}

async function toggleMic() {
  if (audio.micStream) {
    audio.micStream.getTracks().forEach((t) => t.stop());
    audio.micStream = null;
    audio.micGain.gain.value = 0;
    $('#micLabel').textContent = 'Enable mic';
    $('#micBtn').classList.remove('on');
    $('#micGain').disabled = true;
    return;
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    toast('This browser has no microphone API available.', 'error');
    return;
  }
  ensureAudio();
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
    });
    audio.micStream = stream;
    const source = audio.ctx.createMediaStreamSource(stream);
    source.connect(audio.micGain);
    audio.micGain.gain.value = Number($('#micGain').value) / 100;
    $('#micGain').disabled = false;
    $('#micLabel').textContent = 'Mic live';
    $('#micBtn').classList.add('on');
    $('#micNote').textContent = 'Mic is mixing into the output. Use headphones to avoid feedback.';
    $('#micNote').classList.add('warn');
  } catch (err) {
    toast(`Microphone unavailable: ${err.message}`, 'error');
    $('#micNote').textContent = 'Microphone access was denied, so mic mixing stays off.';
    $('#micNote').classList.add('warn');
  }
}

/* ------------------------------------------------------------------ meters */

function startMeters() {
  const bufferL = new Float32Array(audio.analyserL.fftSize);
  const bufferR = new Float32Array(audio.analyserR.fftSize);
  const fills = [$('#vuL'), $('#vuR')];
  const peaks = [$('#peakL'), $('#peakR')];

  const draw = () => {
    let loudest = 0;
    [bufferL, bufferR].forEach((buffer, index) => {
      const analyser = index === 0 ? audio.analyserL : audio.analyserR;
      analyser.getFloatTimeDomainData(buffer);
      let sum = 0;
      let peak = 0;
      for (let i = 0; i < buffer.length; i += 1) {
        const sample = buffer[i];
        sum += sample * sample;
        const magnitude = Math.abs(sample);
        if (magnitude > peak) peak = magnitude;
      }
      const rms = Math.sqrt(sum / buffer.length);
      const level = dbToPercent(rms);
      // .meter-fill is a mask sitting on top of a fixed gradient, so it is
      // sized to the *unlit* part of the bar.
      fills[index].style.width = `${100 - level}%`;

      audio.peaks[index] = Math.max(audio.peaks[index] * 0.97, dbToPercent(peak));
      peaks[index].style.left = `${audio.peaks[index]}%`;
      peaks[index].style.opacity = audio.peaks[index] > 1 ? '0.85' : '0';
      if (peak > loudest) loudest = peak;
    });

    const db = loudest > 0 ? 20 * Math.log10(loudest) : -Infinity;
    $('#meterPeak').textContent = db > -60 ? `${db.toFixed(1)} dB` : '-inf dB';
    requestAnimationFrame(draw);
  };
  requestAnimationFrame(draw);
}

/** Map a linear amplitude to 0-100% across a -40dB..0dB scale. */
function dbToPercent(amplitude) {
  if (amplitude <= 0) return 0;
  const db = 20 * Math.log10(amplitude);
  return Math.max(0, Math.min(100, ((db + 40) / 40) * 100));
}

/* ------------------------------------------------------------------ render */

function renderNowPlaying() {
  const track = state.current;
  const playing = isPlaying();
  const ducked = audio.talkHeld || audio.talkLatched;

  const statusEl = $('#nowStatus');
  statusEl.classList.toggle('live', playing && !ducked);
  statusEl.classList.toggle('talk', playing && ducked);
  statusEl.classList.toggle('cued', Boolean(track) && !playing);
  statusEl.textContent = !track ? 'Standby'
    : ducked && playing ? 'Talk over'
    : playing ? 'On air' : 'Paused';

  $('#nowTitle').textContent = track ? track.title : 'Nothing loaded';
  $('#nowArtist').textContent = track ? track.artist : 'Queue a track to begin';

  if (track) {
    const category = categoryById(track.category);
    const bits = [
      track.album,
      track.genre,
      track.bpm ? `${track.bpm} BPM` : '',
      track.format,
      fmtRate(track.sampleRate),
      fmtBitrate(track.bitrate),
      category ? category.name : '',
    ].filter(Boolean);
    $('#nowMeta').textContent = bits.join('  ·  ');
  } else {
    $('#nowMeta').textContent = '';
  }

  const art = $('#nowArt');
  const wanted = track && track.hasArt ? `/api/art/${track.id}` : '';
  if (art.dataset.src !== wanted) {
    art.dataset.src = wanted;
    art.innerHTML = wanted
      ? `<img src="${wanted}" alt="">`
      : '<svg class="ic"><use href="#i-note"></use></svg>';
  }

  $('#playBtn').innerHTML = `<svg class="ic"><use href="#i-${playing ? 'pause' : 'play'}"></use></svg>`;
  $('#playBtn').title = playing ? 'Pause (Space)' : 'Play (Space)';

  const air = $('#airPill');
  air.dataset.on = String(playing);
  $('#airLabel').textContent = playing ? 'ON AIR' : 'OFF AIR';

  const idle = !track;
  $('#fadeOutBtn').disabled = idle;
  $('#fadeInBtn').disabled = idle;
  $('#talkBtn').disabled = idle;
  $('#prevBtn').disabled = state.played.length < 1 && !track;
  $('#nextBtn').disabled = !peekNext();
}

function renderClock() {
  const now = new Date();
  $('#clock').textContent = now.toLocaleTimeString([], { hour12: false });
  $('#clockDate').textContent = now.toLocaleDateString([], {
    weekday: 'short', day: 'numeric', month: 'short',
  });
  const part = state.dayparts.find((d) => now.getHours() >= d.start && now.getHours() < d.end);
  $('#daypartLabel').textContent = part ? part.name : '';
}

function renderProgress() {
  const deck = liveDeck();
  const duration = deck.el.duration;
  const position = deck.el.currentTime;

  if (!state.scrubbing) {
    const pct = isFinite(duration) && duration > 0 ? (position / duration) * 1000 : 0;
    $('#seekBar').value = String(Math.round(pct));
    setRangeFill($('#seekBar'));
  }
  $('#elapsed').textContent = fmtTime(position);
  $('#remaining').textContent = isFinite(duration) && duration > 0
    ? `-${fmtTime(duration - position)}` : '-0:00';

  const next = peekNext();
  if (next) {
    $('#upNextTitle').textContent = `${next.artist} — ${next.title}`;
    const remaining = isFinite(duration) && duration > 0 ? duration - position : 0;
    const fade = effectiveCrossfade(duration);
    $('#upNextCountdown').textContent = deck.track && remaining > 0
      ? `starts in ${fmtTime(Math.max(0, remaining - fade))}${fade > 0.05 ? ` · ${fade.toFixed(1)}s crossfade` : ''}`
      : `${fmtTime(next.duration)} · ready`;
  } else {
    $('#upNextTitle').textContent = state.config.autoDj ? 'Auto-DJ standing by' : 'Nothing queued';
    $('#upNextCountdown').textContent = state.config.autoDj
      ? 'the rotation will pick the next track'
      : '—';
  }
}

function renderLibrary() {
  const rows = $('#libRows');
  const queued = new Set(state.queue.map((i) => i.trackId));
  const list = state.visible;

  $('#libCount').textContent = list.length === state.library.length
    ? `${state.library.length} track${state.library.length === 1 ? '' : 's'}`
    : `${list.length} of ${state.library.length}`;

  const empty = state.library.length === 0;
  $('#libEmpty').hidden = !empty;
  rows.hidden = empty;
  $('.lib-head').hidden = empty;

  if (empty) { rows.innerHTML = ''; return; }

  if (!list.length) {
    rows.innerHTML = '<div class="pl-empty">Nothing matches those filters.</div>';
    return;
  }

  const fragment = document.createDocumentFragment();
  for (const track of list) {
    const category = categoryById(track.category);
    const row = document.createElement('div');
    row.className = 'lib-row';
    row.dataset.id = track.id;
    if (state.current && state.current.id === track.id) row.classList.add('playing');
    if (queued.has(track.id)) row.classList.add('queued');
    row.innerHTML = `
      <div class="row-text">
        <div class="row-title"></div>
        <div class="row-artist"></div>
      </div>
      <div class="row-cell">${category
        ? `<span class="cat-chip" style="color:${category.color}"></span>`
        : '<span class="cat-chip none">—</span>'}</div>
      <div class="row-cell genre-cell"></div>
      <div class="row-cell num bpm-cell">${track.bpm || '—'}</div>
      <div class="row-cell num">${fmtTime(track.duration)}</div>
      <button class="row-edit" title="Edit track"><svg class="ic"><use href="#i-pencil"></use></svg></button>
    `;
    row.querySelector('.row-title').textContent = track.title;
    row.querySelector('.row-artist').textContent = track.artist;
    row.querySelector('.genre-cell').textContent = track.genre || '—';
    if (category) row.querySelector('.cat-chip').textContent = category.name;
    row.title = `${track.artist} — ${track.title}\n${track.filename}`;
    fragment.appendChild(row);
  }
  rows.replaceChildren(fragment);
}

function renderQueue() {
  const rows = $('#queueRows');
  const items = state.queue;
  const tracks = items.map((i) => trackById(i.trackId)).filter(Boolean);
  const total = tracks.reduce((sum, t) => sum + (t.duration || 0), 0);

  $('#queueTally').textContent = items.length
    ? `${items.length} · ${fmtTime(total)}`
    : 'empty';
  $('#clearQueue').disabled = !items.length;
  $('#queueEmpty').hidden = items.length > 0;
  rows.hidden = items.length === 0;

  const fragment = document.createDocumentFragment();
  items.forEach((item, index) => {
    const track = trackById(item.trackId);
    const row = document.createElement('div');
    row.className = 'queue-row';
    row.draggable = true;
    row.dataset.uid = item.uid;
    row.dataset.index = String(index);
    row.innerHTML = `
      <span class="q-grip"><svg class="ic"><use href="#i-grip"></use></svg></span>
      <span class="q-num">${index + 1}</span>
      <span class="q-text"><span class="q-title"></span><span class="q-artist"></span></span>
      <span class="q-time">${track ? fmtTime(track.duration) : ''}</span>
      <button class="q-drop" title="Remove from queue"><svg class="ic"><use href="#i-x"></use></svg></button>
    `;
    row.querySelector('.q-title').textContent = track ? track.title : 'Missing track';
    const artistEl = row.querySelector('.q-artist');
    // Source-specific chip: the rotation engine is the primary path (cyan),
    // the built-in picker fallback gets amber to flag it on the queue rows.
    const badgeHtml = item.source === 'fallback'
      ? '<span class="q-badge q-badge-fallback">fallback</span>'
      : item.source === 'playlistgen'
        ? '<span class="q-badge q-badge-rotation">rotation</span>'
        : '';
    if (track) {
      artistEl.textContent = item.auto ? `${track.artist} · auto` : track.artist;
    } else {
      artistEl.textContent = 'the file is no longer in the library';
    }
    if (badgeHtml) {
      // Build via a <template> so the HTML is parsed once and the chip sits
      // inline after the artist text without re-rendering the row.
      const template = document.createElement('template');
      template.innerHTML = badgeHtml;
      artistEl.appendChild(template.content.firstChild);
    }
    if (item.auto) artistEl.classList.add('q-auto');
    fragment.appendChild(row);
  });
  rows.replaceChildren(fragment);
  renderProgress();
}

function renderPlaylists() {
  const rows = $('#playlistRows');
  if (!state.playlists.length) {
    rows.innerHTML = '<div class="pl-empty">No playlists yet. Build a queue, then press Save queue.</div>';
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const playlist of state.playlists) {
    const row = document.createElement('div');
    row.className = 'pl-row';
    row.dataset.id = playlist.id;
    row.innerHTML = `
      <button class="pl-name" title="Load into the queue"></button>
      <span class="pl-count">${playlist.trackIds.length}</span>
      <button class="ghost small pl-append" title="Add to the end of the queue">Add</button>
      <button class="q-drop pl-delete" title="Delete playlist" style="opacity:1"><svg class="ic"><use href="#i-x"></use></svg></button>
    `;
    row.querySelector('.pl-name').textContent = playlist.name;
    fragment.appendChild(row);
  }
  rows.replaceChildren(fragment);
}

function renderFilterOptions() {
  const fill = (select, values, keep) => {
    const current = keep;
    const options = ['<option value="">' + select.dataset.all + '</option>']
      .concat(values.map((v) => `<option value="${v.replace(/"/g, '&quot;')}"></option>`));
    select.innerHTML = options.join('');
    Array.from(select.options).forEach((option, index) => {
      if (index > 0) option.textContent = values[index - 1];
    });
    select.value = values.includes(current) ? current : '';
  };

  const genres = [...new Set(state.library.map((t) => t.genre).filter(Boolean))].sort();
  const artists = [...new Set(state.library.map((t) => t.artist).filter(Boolean))].sort();
  $('#filterGenre').dataset.all = 'All genres';
  $('#filterArtist').dataset.all = 'All artists';
  fill($('#filterGenre'), genres, state.filters.genre);
  fill($('#filterArtist'), artists, state.filters.artist);

  const categorySelect = $('#filterCategory');
  categorySelect.innerHTML = '<option value="">All categories</option>'
    + state.categories.map((c) => `<option value="${c.id}"></option>`).join('');
  Array.from(categorySelect.options).forEach((option, index) => {
    if (index > 0) option.textContent = state.categories[index - 1].name;
  });
  categorySelect.value = state.categories.some((c) => c.id === state.filters.category)
    ? state.filters.category : '';
}

/* ------------------------------------------------------------- library ops */

let searchTimer = 0;
function scheduleSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearch, 130);
}

async function runSearch() {
  const params = new URLSearchParams();
  const f = state.filters;
  if (f.q) params.set('q', f.q);
  if (f.genre) params.set('genre', f.genre);
  if (f.artist) params.set('artist', f.artist);
  if (f.category) params.set('category', f.category);
  if (f.bpmMin) params.set('bpmMin', f.bpmMin);
  if (f.bpmMax) params.set('bpmMax', f.bpmMax);
  params.set('sort', f.sort);
  try {
    const result = await api(`/api/tracks?${params.toString()}`);
    state.visible = result.tracks;
    renderLibrary();
  } catch (err) {
    toast(`Search failed: ${err.message}`, 'error');
  }
}

async function refreshLibrary() {
  state.library = await api('/api/library');
  renderFilterOptions();
  await runSearch();
  renderQueue();
}

async function scanLibrary() {
  const button = $('#scanBtn');
  button.disabled = true;
  try {
    const result = await api('/api/library/scan', { method: 'POST' });
    await refreshLibrary();
    const parts = [];
    if (result.added) parts.push(`${result.added} added`);
    if (result.updated) parts.push(`${result.updated} updated`);
    if (result.removed) parts.push(`${result.removed} removed`);
    toast(parts.length ? `Scan: ${parts.join(', ')}.` : 'Scan finished — nothing changed.');
  } catch (err) {
    toast(`Scan failed: ${err.message}`, 'error');
  } finally {
    button.disabled = false;
  }
}

/* --------------------------------------------------------------- queue ops */

function saveQueue() {
  api('/api/queue', { method: 'PUT', body: { items: state.queue } }).catch((err) => {
    toast(`Could not save the queue: ${err.message}`, 'error');
  });
}

function enqueue(trackIds, { auto = false, position = null, source = null } = {}) {
  const items = trackIds.map((id) => ({
    uid: Math.random().toString(36).slice(2, 12),
    trackId: id,
    auto,
    source,
  }));
  if (position === null) state.queue.push(...items);
  else state.queue.splice(position, 0, ...items);
  saveQueue();
  renderQueue();
  renderLibrary();
  cueUpcoming();
}

/**
 * Top the queue up from the rotation engine when Auto-DJ is on.
 *
 * Calls /api/rotation/generate which drives the broadcast playlistgen engine
 * (spins-per-hour, daypart weights, artist/category gaps).  When playlistgen is
 * unavailable the backend falls back to the studio's built-in picker and sets
 * `fallback: true`; the caller surfaces that as a toast so the operator knows.
 */
async function fillQueue(force = false) {
  if (!state.config.autoDj && !force) return;
  if (state.filling) return;
  const minimum = Number(state.config.autoDjMinQueue) || 3;
  if (force) {
    // "Fill now" always generates a fresh 30-min block regardless of the
    // current queue length.
  } else if (state.queue.length >= minimum) {
    return;
  }

  state.filling = true;
  try {
    const exclude = state.queue.map((i) => i.trackId);
    if (state.current) exclude.push(state.current.id);
    const result = await api('/api/rotation/generate', {
      method: 'POST',
      body: { excludeIds: exclude, slot: '30min' },
    });
    const ids = result.trackIds || [];
    if (!ids.length) {
      if (force) toast('The rotation found nothing to play — check that tracks have categories.', 'warn');
      return;
    }
    enqueue(ids, { auto: true, source: result.engine || 'playlistgen' });
    if (result.fallback) {
      toast(`playlistgen unavailable — used built-in rotation. ${result.warning || ''}`, 'warn');
    } else if (force) {
      toast(`Rotation generated ${ids.length} tracks (${result.daypart || '—'}).`);
    }
  } catch (err) {
    toast(`Auto-DJ failed: ${err.message}`, 'error');
  } finally {
    state.filling = false;
  }
}

/* ------------------------------------------------------------- rotation UI */

function renderRotation() {
  const now = new Date();
  const currentPart = state.dayparts.find((d) => now.getHours() >= d.start && now.getHours() < d.end);
  $('#rotationDaypart').textContent = currentPart ? currentPart.name : '—';

  const table = $('#rotTable');
  const head = document.createElement('div');
  head.className = 'rot-head';
  head.innerHTML = '<span>Category</span><span>Spins / hour</span><span>Artist gap (min)</span>'
    + state.dayparts.map((d) => {
      const active = currentPart && currentPart.id === d.id ? ' class="now-part"' : '';
      return `<span${active}>${d.name.replace(' Drive', '<br>Drive')}</span>`;
    }).join('')
    + '<span></span>';

  const body = document.createElement('div');
  for (const category of state.categories) {
    const row = document.createElement('div');
    row.className = 'rot-row';
    row.dataset.id = category.id;
    row.innerHTML = `
      <span class="rot-name">
        <input type="color" class="rot-swatch" value="${category.color}" title="Colour">
        <input type="text" class="rot-title" value="">
      </span>
      <input type="number" class="rot-spins" min="0" max="60" value="${category.spinsPerHour}">
      <input type="number" class="rot-gap" min="0" max="600" step="5" value="${category.minArtistGap}">
      ${state.dayparts.map((d) => `<input type="number" class="rot-weight" data-part="${d.id}"
          min="0" max="3" step="0.1" value="${Number(category.weights[d.id] ?? 1).toFixed(1)}">`).join('')}
      <button class="del" title="Delete category"><svg class="ic"><use href="#i-x"></use></svg></button>
    `;
    row.querySelector('.rot-title').value = category.name;
    body.appendChild(row);
  }

  table.replaceChildren(head, body);
}

function collectRotation() {
  return $$('#rotTable .rot-row').map((row) => {
    const weights = {};
    row.querySelectorAll('.rot-weight').forEach((input) => {
      weights[input.dataset.part] = Math.max(0, Number(input.value) || 0);
    });
    return {
      id: row.dataset.id,
      name: row.querySelector('.rot-title').value.trim() || row.dataset.id,
      color: row.querySelector('.rot-swatch').value,
      spinsPerHour: Number(row.querySelector('.rot-spins').value) || 0,
      minArtistGap: Number(row.querySelector('.rot-gap').value) || 0,
      weights,
    };
  });
}

/* ------------------------------------------------------------- schedule UI */

function renderScheduleTargets() {
  const action = $('#schedAction').value;
  const target = $('#schedTarget');
  const options = action === 'playlist'
    ? state.playlists.map((p) => ({ id: p.id, name: p.name }))
    : state.categories.map((c) => ({ id: c.id, name: c.name }));
  target.innerHTML = options.map((o) => `<option value="${o.id}"></option>`).join('')
    || '<option value="">nothing available</option>';
  Array.from(target.options).forEach((option, index) => {
    if (options[index]) option.textContent = options[index].name;
  });
}

function renderSchedule() {
  const rows = $('#schedRows');
  if (!state.schedule.length) {
    rows.innerHTML = '<div class="sched-empty">No scheduled events. Add one above.</div>';
    return;
  }
  const sorted = [...state.schedule].sort((a, b) => a.time.localeCompare(b.time));
  const fragment = document.createDocumentFragment();
  for (const entry of sorted) {
    const target = entry.action === 'playlist'
      ? state.playlists.find((p) => p.id === entry.targetId)
      : state.categories.find((c) => c.id === entry.targetId);
    const row = document.createElement('div');
    row.className = `sched-row${entry.enabled ? '' : ' off'}`;
    row.dataset.id = entry.id;
    row.innerHTML = `
      <span class="sched-time">${entry.time}</span>
      <span class="sched-what"><b></b><small>${entry.mode === 'append' ? 'add to queue' : 'replace queue'}</small></span>
      <span class="sched-repeat">${entry.repeat}</span>
      <label class="switch"><input type="checkbox" class="sched-on" ${entry.enabled ? 'checked' : ''}><span class="track"></span></label>
      <button class="del" title="Delete"><svg class="ic"><use href="#i-x"></use></svg></button>
    `;
    row.querySelector('b').textContent = target
      ? `${entry.action === 'playlist' ? 'Playlist' : 'Category'}: ${target.name}`
      : 'Target is missing';
    fragment.appendChild(row);
  }
  rows.replaceChildren(fragment);
}

function saveSchedule() {
  return api('/api/schedule', { method: 'PUT', body: { entries: state.schedule } })
    .then((entries) => { state.schedule = entries; renderSchedule(); })
    .catch((err) => toast(`Could not save the schedule: ${err.message}`, 'error'));
}

/** Fire any due schedule entries. Called once a second from the clock tick. */
function checkSchedule() {
  const now = new Date();
  const stamp = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
  const startOfMinute = new Date(now);
  startOfMinute.setSeconds(0, 0);
  const minuteEpoch = startOfMinute.getTime() / 1000;

  let dirty = false;
  for (const entry of state.schedule) {
    if (!entry.enabled || entry.time !== stamp) continue;
    if ((entry.lastFired || 0) >= minuteEpoch) continue;   // already fired this minute
    entry.lastFired = minuteEpoch;
    if (entry.repeat === 'once') entry.enabled = false;
    dirty = true;
    fireScheduleEntry(entry);
  }
  if (dirty) saveSchedule();
}

async function fireScheduleEntry(entry) {
  try {
    if (entry.action === 'playlist') {
      const playlist = await api(`/api/playlists/${entry.targetId}`);
      if (!playlist.trackIds.length) {
        toast(`Scheduled playlist "${playlist.name}" has no playable tracks.`, 'warn');
        return;
      }
      applyTrackIds(playlist.trackIds, entry.mode);
      toast(`${entry.time} — loaded playlist "${playlist.name}".`);
    } else {
      const result = await api('/api/rotation/next', {
        method: 'POST',
        body: { count: 8, categoryId: entry.targetId },
      });
      if (!result.picks.length) {
        toast('Scheduled category has no tracks assigned.', 'warn');
        return;
      }
      applyTrackIds(result.picks.map((p) => p.track.id), entry.mode);
      const category = categoryById(entry.targetId);
      toast(`${entry.time} — started ${category ? category.name : 'category'} rotation.`);
    }
  } catch (err) {
    toast(`Scheduled event failed: ${err.message}`, 'error');
  }
}

function applyTrackIds(ids, mode) {
  if (mode === 'replace') {
    state.queue = [];
    enqueue(ids);
    if (!isPlaying()) advance(0);
  } else {
    enqueue(ids);
  }
}

/* ---------------------------------------------------------------- editing */

function openEditor(trackId) {
  const track = trackById(trackId);
  if (!track) return;
  state.editingId = trackId;
  $('#editTitle').value = track.title || '';
  $('#editArtist').value = track.artist || '';
  $('#editAlbum').value = track.album || '';
  $('#editGenre').value = track.genre || '';
  $('#editYear').value = track.year || '';
  $('#editBpm').value = track.bpm || '';

  const select = $('#editCategory');
  select.innerHTML = '<option value="">No category</option>'
    + state.categories.map((c) => `<option value="${c.id}"></option>`).join('');
  Array.from(select.options).forEach((option, index) => {
    if (index > 0) option.textContent = state.categories[index - 1].name;
  });
  select.value = track.category || '';

  $('#editFile').textContent = [
    track.path,
    `${track.format} · ${fmtTime(track.duration)} · ${fmtRate(track.sampleRate)} · ${fmtBitrate(track.bitrate)}`
      .replace(/ · (?= ·|$)/g, ''),
    `${(track.size / 1048576).toFixed(1)} MB · played ${track.playCount || 0}×`,
  ].join('\n');
  $('#editDialog').showModal();
}

async function saveEditor() {
  const id = state.editingId;
  if (!id) return;
  try {
    const result = await api(`/api/tracks/${id}`, {
      method: 'PATCH',
      body: {
        title: $('#editTitle').value.trim(),
        artist: $('#editArtist').value.trim(),
        album: $('#editAlbum').value.trim(),
        genre: $('#editGenre').value.trim(),
        year: $('#editYear').value.trim(),
        bpm: $('#editBpm').value.trim(),
        category: $('#editCategory').value,
      },
    });
    const index = state.library.findIndex((t) => t.id === id);
    if (index >= 0) state.library[index] = result.track;
    if (state.current && state.current.id === id) state.current = result.track;
    for (const deck of audio.decks) if (deck.track && deck.track.id === id) deck.track = result.track;
    renderFilterOptions();
    await runSearch();
    renderQueue();
    renderNowPlaying();
    $('#editDialog').close();
    toast(result.tagsWritten
      ? 'Saved — tags written into the file.'
      : 'Saved in the library. This file format could not store the tags.',
      result.tagsWritten ? '' : 'warn');
  } catch (err) {
    toast(`Could not save: ${err.message}`, 'error');
  }
}

/* ------------------------------------------------------------------ config */

async function pushConfig(updates) {
  Object.assign(state.config, updates);
  try {
    state.config = await api('/api/config', { method: 'PUT', body: updates });
  } catch (err) {
    toast(`Could not save settings: ${err.message}`, 'error');
  }
}

function applyConfigToUi() {
  const config = state.config;
  $('#volume').value = String(Math.round((config.volume ?? 0.85) * 100));
  $('#volValue').textContent = `${Math.round((config.volume ?? 0.85) * 100)}%`;
  $('#crossfade').value = String(config.crossfade ?? 4);
  $('#xfValue').textContent = `${Number(config.crossfade ?? 4).toFixed(1)}s`;
  $('#fadeSeconds').value = String(config.fadeSeconds ?? 2);
  $('#fadeValue').textContent = `${Number(config.fadeSeconds ?? 2).toFixed(1)}s`;
  $('#duckLevel').value = String(Math.round((config.duckLevel ?? 0.2) * 100));
  $('#duckValue').textContent = `${Math.round((config.duckLevel ?? 0.2) * 100)}%`;
  $('#autoDjMinQueue').value = String(config.autoDjMinQueue ?? 3);
  $('#minQueueValue').textContent = String(config.autoDjMinQueue ?? 3);
  $('#autoDjToggle').checked = Boolean(config.autoDj);
  $('#autoDjHint').textContent = config.autoDj
    ? `keeps ${config.autoDjMinQueue} tracks queued`
    : 'off';
  $('#musicDirInput').value = config.musicDir || '';
  ['#volume', '#crossfade', '#fadeSeconds', '#duckLevel', '#autoDjMinQueue', '#micGain', '#seekBar']
    .forEach((sel) => setRangeFill($(sel)));
  if (audio.masterGain) audio.masterGain.gain.value = Number(config.volume ?? 0.85);
}

/* ------------------------------------------------------------------- wiring */

function wireTransport() {
  $('#playBtn').addEventListener('click', togglePlay);
  $('#nextBtn').addEventListener('click', () => advance());
  $('#prevBtn').addEventListener('click', () => {
    const deck = liveDeck();
    if (deck.track && deck.el.currentTime > 3) { deck.el.currentTime = 0; return; }
    const previousId = state.played[1];
    const previous = previousId ? trackById(previousId) : null;
    if (!previous) {
      if (deck.track) deck.el.currentTime = 0;
      return;
    }
    // Put the current track back at the head of the queue before stepping back.
    if (deck.track) enqueue([deck.track.id], { position: 0 });
    state.played.shift();
    state.played.shift();
    playTrack(previous);
  });

  $('#fadeInBtn').addEventListener('click', fadeIn);
  $('#fadeOutBtn').addEventListener('click', fadeOut);
  $('#talkBtn').addEventListener('click', () => setTalk(undefined, !audio.talkLatched));

  const seek = $('#seekBar');
  const applySeek = () => {
    const deck = liveDeck();
    if (deck.el.duration) deck.el.currentTime = (Number(seek.value) / 1000) * deck.el.duration;
  };
  seek.addEventListener('pointerdown', () => { state.scrubbing = true; });
  seek.addEventListener('input', () => { setRangeFill(seek); });
  seek.addEventListener('change', () => { applySeek(); state.scrubbing = false; });

  $('#volume').addEventListener('input', (event) => {
    const value = Number(event.target.value) / 100;
    $('#volValue').textContent = `${event.target.value}%`;
    setRangeFill(event.target);
    ensureAudio();
    audio.masterGain.gain.value = value;
    state.config.volume = value;
  });
  $('#volume').addEventListener('change', () => pushConfig({ volume: state.config.volume }));

  $('#crossfade').addEventListener('input', (event) => {
    state.config.crossfade = Number(event.target.value);
    $('#xfValue').textContent = `${state.config.crossfade.toFixed(1)}s`;
    setRangeFill(event.target);
    renderProgress();
  });
  $('#crossfade').addEventListener('change', () => pushConfig({ crossfade: state.config.crossfade }));

  $('#micBtn').addEventListener('click', toggleMic);
  $('#micGain').addEventListener('input', (event) => {
    setRangeFill(event.target);
    if (audio.micGain) audio.micGain.gain.value = Number(event.target.value) / 100;
  });

  for (const deck of audio.decks) {
    deck.el.addEventListener('timeupdate', () => {
      if (deck !== liveDeck()) return;
      renderProgress();
      const duration = deck.el.duration;
      if (!isFinite(duration) || duration <= 0 || audio.crossfading || deck.ended) return;
      const fade = effectiveCrossfade(duration);
      if (fade > 0.05 && duration - deck.el.currentTime <= fade && peekNext()) {
        deck.ended = true;
        advance(fade);
      }
    });
    deck.el.addEventListener('ended', () => {
      if (deck !== liveDeck() || deck.ended) return;
      deck.ended = true;
      advance(0);
    });
    deck.el.addEventListener('play', renderNowPlaying);
    deck.el.addEventListener('pause', renderNowPlaying);
    deck.el.addEventListener('error', () => {
      if (deck === liveDeck() && deck.track) {
        toast(`Could not play "${deck.track.title}" — skipping.`, 'error');
        advance(0);
      }
    });
  }
}

function wireLibrary() {
  const rows = $('#libRows');
  let clickTimer = 0;

  rows.addEventListener('click', (event) => {
    const editButton = event.target.closest('.row-edit');
    const row = event.target.closest('.lib-row');
    if (!row) return;
    if (editButton) { openEditor(row.dataset.id); return; }
    clearTimeout(clickTimer);
    clickTimer = setTimeout(() => {
      enqueue([row.dataset.id]);
      const track = trackById(row.dataset.id);
      if (track) $('#libSelection').textContent = `Queued "${track.title}".`;
    }, 200);
  });

  rows.addEventListener('dblclick', (event) => {
    const row = event.target.closest('.lib-row');
    if (!row || event.target.closest('.row-edit')) return;
    clearTimeout(clickTimer);
    const track = trackById(row.dataset.id);
    if (track) playTrack(track);
  });

  $('#searchInput').addEventListener('input', (event) => {
    state.filters.q = event.target.value;
    scheduleSearch();
  });
  const bind = (selector, key) => {
    $(selector).addEventListener('change', (event) => {
      state.filters[key] = event.target.value;
      runSearch();
    });
  };
  bind('#filterGenre', 'genre');
  bind('#filterArtist', 'artist');
  bind('#filterCategory', 'category');
  bind('#sortBy', 'sort');
  $('#bpmMin').addEventListener('input', (e) => { state.filters.bpmMin = e.target.value; scheduleSearch(); });
  $('#bpmMax').addEventListener('input', (e) => { state.filters.bpmMax = e.target.value; scheduleSearch(); });

  $('#clearFilters').addEventListener('click', () => {
    state.filters = { q: '', genre: '', artist: '', category: '', bpmMin: '', bpmMax: '', sort: 'artist' };
    $('#searchInput').value = '';
    $('#bpmMin').value = '';
    $('#bpmMax').value = '';
    $('#sortBy').value = 'artist';
    renderFilterOptions();
    runSearch();
  });

  $('#scanBtn').addEventListener('click', scanLibrary);
  $('#emptyOpenSettings').addEventListener('click', () => $('#settingsDialog').showModal());
}

function wireQueue() {
  const rows = $('#queueRows');

  rows.addEventListener('click', (event) => {
    const row = event.target.closest('.queue-row');
    if (!row) return;
    const index = Number(row.dataset.index);
    if (event.target.closest('.q-drop')) {
      state.queue.splice(index, 1);
      saveQueue();
      renderQueue();
      renderLibrary();
      cueUpcoming();
      return;
    }
    const [item] = state.queue.splice(index, 1);
    saveQueue();
    renderQueue();
    const track = trackById(item.trackId);
    if (track) playTrack(track).then(() => fillQueue());
  });

  let dragIndex = null;
  rows.addEventListener('dragstart', (event) => {
    const row = event.target.closest('.queue-row');
    if (!row) return;
    dragIndex = Number(row.dataset.index);
    row.classList.add('dragging');
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', String(dragIndex));
  });
  rows.addEventListener('dragover', (event) => {
    if (dragIndex === null) return;
    event.preventDefault();
    const row = event.target.closest('.queue-row');
    $$('.queue-row').forEach((r) => r.classList.remove('drag-over'));
    if (row) row.classList.add('drag-over');
  });
  rows.addEventListener('drop', (event) => {
    if (dragIndex === null) return;
    event.preventDefault();
    const row = event.target.closest('.queue-row');
    const target = row ? Number(row.dataset.index) : state.queue.length - 1;
    const [moved] = state.queue.splice(dragIndex, 1);
    state.queue.splice(target, 0, moved);
    dragIndex = null;
    saveQueue();
    renderQueue();
    cueUpcoming();
  });
  rows.addEventListener('dragend', () => {
    dragIndex = null;
    $$('.queue-row').forEach((r) => r.classList.remove('drag-over', 'dragging'));
  });

  $('#clearQueue').addEventListener('click', () => {
    state.queue = [];
    saveQueue();
    renderQueue();
    renderLibrary();
  });

  $('#autoDjToggle').addEventListener('change', async (event) => {
    await pushConfig({ autoDj: event.target.checked });
    applyConfigToUi();
    if (event.target.checked) fillQueue();
    renderProgress();
  });

  $('#fillNow').addEventListener('click', () => fillQueue(true));

  $('#savePlaylist').addEventListener('click', () => {
    if (!state.queue.length) { toast('The queue is empty — nothing to save.', 'warn'); return; }
    $('#playlistName').value = '';
    $('#playlistCount').textContent = `${state.queue.length} track${state.queue.length === 1 ? '' : 's'}`;
    $('#playlistDialog').showModal();
    setTimeout(() => $('#playlistName').focus(), 30);
  });

  $('#confirmPlaylist').addEventListener('click', async (event) => {
    event.preventDefault();
    const name = $('#playlistName').value.trim();
    if (!name) { toast('Give the playlist a name.', 'warn'); return; }
    try {
      await api('/api/playlists', {
        method: 'POST',
        body: { name, trackIds: state.queue.map((i) => i.trackId) },
      });
      state.playlists = await api('/api/playlists');
      renderPlaylists();
      renderScheduleTargets();
      $('#playlistDialog').close();
      toast(`Saved playlist "${name}".`);
    } catch (err) {
      toast(`Could not save the playlist: ${err.message}`, 'error');
    }
  });

  $('#playlistRows').addEventListener('click', async (event) => {
    const row = event.target.closest('.pl-row');
    if (!row) return;
    const id = row.dataset.id;
    const playlist = state.playlists.find((p) => p.id === id);
    if (event.target.closest('.pl-delete')) {
      if (!confirm(`Delete the playlist "${playlist.name}"?`)) return;
      await api(`/api/playlists/${id}`, { method: 'DELETE' });
      state.playlists = await api('/api/playlists');
      renderPlaylists();
      renderScheduleTargets();
      return;
    }
    try {
      const loaded = await api(`/api/playlists/${id}`);
      if (!loaded.trackIds.length) { toast('That playlist has no playable tracks left.', 'warn'); return; }
      if (event.target.closest('.pl-append')) {
        enqueue(loaded.trackIds);
        toast(`Added ${loaded.trackIds.length} tracks from "${loaded.name}".`);
      } else {
        state.queue = [];
        enqueue(loaded.trackIds);
        toast(`Loaded "${loaded.name}" — ${loaded.trackIds.length} tracks.`
          + (loaded.missing.length ? ` ${loaded.missing.length} missing.` : ''));
      }
    } catch (err) {
      toast(`Could not load the playlist: ${err.message}`, 'error');
    }
  });
}

function wireDialogs() {
  $('#openRotation').addEventListener('click', () => { renderRotation(); $('#rotationDialog').showModal(); });
  $('#openSchedule').addEventListener('click', () => {
    renderScheduleTargets();
    renderSchedule();
    $('#scheduleDialog').showModal();
  });
  $('#openSettings').addEventListener('click', () => {
    $('#musicDirNow').textContent = `now: ${state.musicDir || 'studio/music'}`;
    $('#settingsDialog').showModal();
  });
  $('#openHelp').addEventListener('click', () => $('#helpDialog').showModal());

  $('#addCategory').addEventListener('click', (event) => {
    event.preventDefault();
    const id = `cat${Date.now().toString(36).slice(-5)}`;
    state.categories.push({
      id,
      name: 'New category',
      color: '#22d3ee',
      spinsPerHour: 2,
      minArtistGap: 60,
      weights: Object.fromEntries(state.dayparts.map((d) => [d.id, 1])),
    });
    renderRotation();
  });

  $('#rotTable').addEventListener('click', (event) => {
    const button = event.target.closest('.del');
    if (!button) return;
    const row = button.closest('.rot-row');
    state.categories = collectRotation().filter((c) => c.id !== row.dataset.id);
    renderRotation();
  });

  $('#saveRotation').addEventListener('click', async (event) => {
    event.preventDefault();
    try {
      const result = await api('/api/rotation', {
        method: 'PUT',
        body: { categories: collectRotation() },
      });
      state.categories = result.categories;
      renderRotation();
      renderFilterOptions();
      renderScheduleTargets();
      await refreshLibrary();
      renderNowPlaying();
      $('#rotationSaved').textContent = 'Saved.';
      setTimeout(() => { $('#rotationSaved').textContent = ''; }, 2500);
    } catch (err) {
      toast(`Could not save the rotation: ${err.message}`, 'error');
    }
  });

  $('#schedAction').addEventListener('change', renderScheduleTargets);
  $('#addSchedule').addEventListener('click', async (event) => {
    event.preventDefault();
    const targetId = $('#schedTarget').value;
    if (!targetId) {
      toast($('#schedAction').value === 'playlist'
        ? 'Save a playlist first, then schedule it.'
        : 'Add a rotation category first.', 'warn');
      return;
    }
    state.schedule.push({
      time: $('#schedTime').value,
      action: $('#schedAction').value,
      targetId,
      mode: $('#schedMode').value,
      repeat: $('#schedRepeat').value,
      enabled: true,
      lastFired: 0,
    });
    await saveSchedule();
  });

  $('#schedRows').addEventListener('click', async (event) => {
    const row = event.target.closest('.sched-row');
    if (!row || !event.target.closest('.del')) return;
    state.schedule = state.schedule.filter((e) => e.id !== row.dataset.id);
    await saveSchedule();
  });
  $('#schedRows').addEventListener('change', async (event) => {
    if (!event.target.classList.contains('sched-on')) return;
    const row = event.target.closest('.sched-row');
    const entry = state.schedule.find((e) => e.id === row.dataset.id);
    if (entry) entry.enabled = event.target.checked;
    await saveSchedule();
  });

  $('#saveMusicDir').addEventListener('click', async (event) => {
    event.preventDefault();
    const button = event.target;
    button.disabled = true;
    try {
      const result = await api('/api/library/scan', {
        method: 'POST',
        body: { musicDir: $('#musicDirInput').value.trim() },
      });
      state.musicDir = result.musicDir;
      state.config.musicDir = $('#musicDirInput').value.trim();
      $('#musicDirNow').textContent = `now: ${result.musicDir}`;
      await refreshLibrary();
      toast(`Scanned ${result.musicDir} — ${result.total} tracks.`);
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      button.disabled = false;
    }
  });

  const slider = (selector, key, format, transform = (v) => v) => {
    $(selector).addEventListener('input', (event) => {
      setRangeFill(event.target);
      const value = transform(Number(event.target.value));
      state.config[key] = value;
      $(format.target).textContent = format.text(value, event.target.value);
    });
    $(selector).addEventListener('change', () => pushConfig({ [key]: state.config[key] }));
  };
  slider('#fadeSeconds', 'fadeSeconds', { target: '#fadeValue', text: (v) => `${v.toFixed(1)}s` });
  slider('#duckLevel', 'duckLevel', { target: '#duckValue', text: (v, raw) => `${raw}%` }, (v) => v / 100);
  slider('#autoDjMinQueue', 'autoDjMinQueue', { target: '#minQueueValue', text: (v) => String(v) });

  $('#saveTrack').addEventListener('click', (event) => { event.preventDefault(); saveEditor(); });
  $('#deleteTrack').addEventListener('click', async (event) => {
    event.preventDefault();
    const track = trackById(state.editingId);
    if (!track) return;
    if (!confirm(`Remove "${track.title}" from the library?\n\nThe file stays on disk, but a rescan will pick it up again unless you move it out of the music folder.`)) return;
    try {
      await api(`/api/tracks/${track.id}`, { method: 'DELETE' });
      state.queue = state.queue.filter((i) => i.trackId !== track.id);
      $('#editDialog').close();
      await refreshLibrary();
      toast('Removed from the library.');
    } catch (err) {
      toast(`Could not remove: ${err.message}`, 'error');
    }
  });
}

function wireDropZone() {
  const veil = $('#dropVeil');
  let depth = 0;

  window.addEventListener('dragenter', (event) => {
    if (!event.dataTransfer || !Array.from(event.dataTransfer.types).includes('Files')) return;
    depth += 1;
    veil.hidden = false;
  });
  window.addEventListener('dragover', (event) => {
    if (!veil.hidden) event.preventDefault();
  });
  window.addEventListener('dragleave', () => {
    depth = Math.max(0, depth - 1);
    if (depth === 0) veil.hidden = true;
  });
  window.addEventListener('drop', async (event) => {
    if (veil.hidden) return;
    event.preventDefault();
    depth = 0;
    veil.hidden = true;
    const files = Array.from(event.dataTransfer.files || []);
    if (files.length) await uploadFiles(files);
  });

  $('#filePicker').addEventListener('change', (event) => {
    const files = Array.from(event.target.files || []);
    if (files.length) uploadFiles(files);
    event.target.value = '';
  });
}

async function uploadFiles(files) {
  const form = new FormData();
  files.forEach((file) => form.append('files', file));
  toast(`Importing ${files.length} file${files.length === 1 ? '' : 's'}…`);
  try {
    const result = await api('/api/library/import', { method: 'POST', body: form });
    await refreshLibrary();
    const skipped = result.skipped.length ? `, ${result.skipped.length} skipped` : '';
    toast(`Imported ${result.saved.length} file${result.saved.length === 1 ? '' : 's'}${skipped}.`);
  } catch (err) {
    toast(`Import failed: ${err.message}`, 'error');
  }
}

function wireKeyboard() {
  const isTyping = (target) => target && (
    target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.tagName === 'SELECT'
    || target.isContentEditable);

  window.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      const open = document.querySelector('dialog[open]');
      if (open) return;                     // dialogs close themselves
      if (document.activeElement === $('#searchInput')) {
        $('#searchInput').value = '';
        state.filters.q = '';
        runSearch();
        $('#searchInput').blur();
      }
      return;
    }
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (document.querySelector('dialog[open]')) return;

    if (event.key === '/' && !isTyping(event.target)) {
      event.preventDefault();
      $('#searchInput').focus();
      $('#searchInput').select();
      return;
    }
    if (event.key === '?') {
      event.preventDefault();
      $('#helpDialog').showModal();
      return;
    }
    if (isTyping(event.target)) return;

    switch (event.key) {
      case ' ':
        event.preventDefault();
        togglePlay();
        break;
      case 'f': case 'F':
        event.preventDefault();
        if (event.shiftKey) fadeIn(); else fadeOut();
        break;
      case 't': case 'T':
        if (event.repeat) break;
        event.preventDefault();
        setTalk(true, undefined);
        break;
      case 'a': case 'A':
        event.preventDefault();
        $('#autoDjToggle').checked = !$('#autoDjToggle').checked;
        $('#autoDjToggle').dispatchEvent(new Event('change'));
        break;
      case 'ArrowLeft':
        event.preventDefault();
        if (event.shiftKey) $('#prevBtn').click();
        else if (liveDeck().track) liveDeck().el.currentTime = Math.max(0, liveDeck().el.currentTime - 5);
        break;
      case 'ArrowRight':
        event.preventDefault();
        if (event.shiftKey) advance();
        else if (liveDeck().track) liveDeck().el.currentTime += 5;
        break;
      case 'ArrowUp': case 'ArrowDown': {
        event.preventDefault();
        const volume = $('#volume');
        volume.value = String(Math.max(0, Math.min(100,
          Number(volume.value) + (event.key === 'ArrowUp' ? 5 : -5))));
        volume.dispatchEvent(new Event('input'));
        volume.dispatchEvent(new Event('change'));
        break;
      }
      default:
        break;
    }
  });

  window.addEventListener('keyup', (event) => {
    if ((event.key === 't' || event.key === 'T') && audio.talkHeld) setTalk(false, undefined);
  });
  // A dropped keyup (window blurred mid-hold) would leave the music ducked.
  window.addEventListener('blur', () => { if (audio.talkHeld) setTalk(false, undefined); });
}

/* --------------------------------------------------------------------- boot */

async function boot() {
  audio.decks = [makeDeck('A'), makeDeck('B')];

  let bootstrap;
  try {
    bootstrap = await api('/api/state');
  } catch (err) {
    toast(`Could not reach the server: ${err.message}`, 'error');
    return;
  }

  state.config = bootstrap.config;
  state.library = bootstrap.library;
  state.visible = bootstrap.library;
  state.queue = bootstrap.queue;
  state.playlists = bootstrap.playlists;
  state.categories = bootstrap.categories;
  state.dayparts = bootstrap.dayparts;
  state.schedule = bootstrap.schedule;
  state.musicDir = bootstrap.musicDir;

  applyConfigToUi();
  renderFilterOptions();
  renderLibrary();
  renderQueue();
  renderPlaylists();
  renderNowPlaying();
  renderClock();
  renderScheduleTargets();

  wireTransport();
  wireLibrary();
  wireQueue();
  wireDialogs();
  wireDropZone();
  wireKeyboard();

  setInterval(() => { renderClock(); checkSchedule(); }, 1000);
  setInterval(renderProgress, 500);

  // The AudioContext can only start from a gesture; arm it on the first one.
  const arm = () => { ensureAudio(); window.removeEventListener('pointerdown', arm); };
  window.addEventListener('pointerdown', arm);

  if (state.config.autoDj) fillQueue();
  if (!state.library.length) {
    $('#libSelection').textContent = 'Library is empty — set a music folder in Settings, then Scan.';
  }
}

boot();
