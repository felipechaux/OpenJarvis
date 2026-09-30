// ── J.A.R.V.I.S. startup sound ──────────────────────────────────────
// The boot intro's score, synthesized (no audio files, no samples): the
// Stark-HUD startup idiom — sub-bass hits, falling and rising data chirps,
// clocked beep trains, a fast high tick and a sustained scan whine — laid
// out like the classic JARVIS startup cue's first ten seconds (before its
// music).  Every section lands on a BOOT_TIMELINE beat so sound and picture
// (BootIntro.tsx) stay in lockstep:
//
//   ignite  0.55  sub hit + a stepped falling beep cascade, then a breath
//   coils   1.5   hits, two clocked beep trains (2 kHz / 1.2 kHz), rising
//                 chirps and the high tick — one click per reactor coil
//   lock    3.4   the big hit: a low tone and a spray of falling glissandi
//   (calm)  4.5   only the tick and a faint high shimmer under the greeting
//   scan    5.5   hits, a sustained 5.2 kHz scan whine with 1.5 kHz pulses
//   dock    7.1   the reactor flies to the notch; the loudest hit lands on
//                 its arrival, then a dense rising cluster and pulse
//                 ladders fade out by ~10 s
//
// One graph:
//   voices ─ bus ─┬─ dry ────────────┬─ compressor ─ master ─ speakers
//                 └─ short hall verb ┘

/// Cue times in seconds from the start of the intro.  BootIntro reads the
/// same table for its CSS animation delays.
export const BOOT_TIMELINE = {
  ignite: 0.55,
  coilsStart: 1.5,
  coilStep: 0.19,
  lock: 3.4,
  greet: 3.9,
  scan: 5.5,
  dock: 7.1,
  end: 8.4,
} as const;

/// The score runs past the picture (its tail fades by ~10 s).
const SCORE_S = 10.2;

interface Graph {
  ctx: BaseAudioContext;
  bus: GainNode;
}

function hall(ctx: BaseAudioContext, seconds = 2.0, decay = 3.5): AudioBuffer {
  const length = Math.floor(ctx.sampleRate * seconds);
  const ir = ctx.createBuffer(2, length, ctx.sampleRate);
  for (let ch = 0; ch < 2; ch++) {
    const data = ir.getChannelData(ch);
    for (let i = 0; i < length; i++) {
      data[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / length, decay);
    }
  }
  return ir;
}

function build(ctx: BaseAudioContext): Graph {
  const master = ctx.createGain();
  master.gain.value = 0.5;
  const comp = ctx.createDynamicsCompressor();
  comp.threshold.value = -16;
  comp.ratio.value = 3;
  comp.connect(master);
  master.connect(ctx.destination);

  const bus = ctx.createGain();
  bus.connect(comp);
  const verb = ctx.createConvolver();
  verb.buffer = hall(ctx);
  const wet = ctx.createGain();
  wet.gain.value = 0.3;
  bus.connect(verb);
  verb.connect(wet);
  wet.connect(comp);
  return { ctx, bus };
}

function envelope(g: Graph, at: number, attack: number, peak: number, release: number, pan = 0) {
  const env = g.ctx.createGain();
  env.gain.setValueAtTime(0.0001, at);
  env.gain.exponentialRampToValueAtTime(peak, at + attack);
  env.gain.exponentialRampToValueAtTime(0.0001, at + attack + release);
  const p = g.ctx.createStereoPanner();
  p.pan.value = pan;
  env.connect(p);
  p.connect(g.bus);
  return env;
}

function osc(g: Graph, type: OscillatorType, freq: number, at: number, dur: number, into: AudioNode, amp = 1) {
  const o = g.ctx.createOscillator();
  o.type = type;
  o.frequency.setValueAtTime(freq, at);
  const a = g.ctx.createGain();
  a.gain.value = amp;
  o.connect(a);
  a.connect(into);
  o.start(at);
  o.stop(at + dur + 0.05);
  return o;
}

function noise(g: Graph, at: number, dur: number): AudioBufferSourceNode {
  const len = Math.max(1, Math.floor(g.ctx.sampleRate * dur));
  const buf = g.ctx.createBuffer(1, len, g.ctx.sampleRate);
  const data = buf.getChannelData(0);
  for (let i = 0; i < len; i++) data[i] = Math.random() * 2 - 1;
  const src = g.ctx.createBufferSource();
  src.buffer = buf;
  src.start(at);
  src.stop(at + dur);
  return src;
}

/// Short digital beep (sine with a touch of square for bite).
function beep(g: Graph, freq: number, at: number, dur: number, level: number, pan = 0) {
  const env = envelope(g, at, 0.002, level, dur, pan);
  osc(g, 'sine', freq, at, dur, env, 1);
  const lp = g.ctx.createBiquadFilter();
  lp.type = 'lowpass';
  lp.frequency.value = freq * 2;
  lp.connect(env);
  osc(g, 'square', freq, at, dur, lp, 0.18);
}

/// Sub-bass hit: a falling sine body, a click of noise on top and a low
/// noise rumble, into the hall.
function hit(g: Graph, at: number, level: number, len = 0.7) {
  const body = envelope(g, at, 0.003, level, len);
  osc(g, 'sine', 95, at, len, body).frequency.exponentialRampToValueAtTime(32, at + len * 0.8);
  const click = envelope(g, at, 0.001, level * 0.35, 0.05);
  const hp = g.ctx.createBiquadFilter();
  hp.type = 'highpass';
  hp.frequency.value = 3000;
  noise(g, at, 0.05).connect(hp);
  hp.connect(click);
  const rumble = envelope(g, at, 0.01, level * 0.4, len * 1.3);
  const lp = g.ctx.createBiquadFilter();
  lp.type = 'lowpass';
  lp.frequency.value = 160;
  noise(g, at, len * 1.4).connect(lp);
  lp.connect(rumble);
}

/// A tone gliding from → to: rising data chirps and falling glissandi.
function chirp(g: Graph, from: number, to: number, at: number, dur: number, level: number, pan = 0) {
  const env = envelope(g, at, 0.004, level, dur, pan);
  osc(g, 'sine', from, at, dur, env).frequency.exponentialRampToValueAtTime(to, at + dur);
}

/// A clocked beep train: `freq` every `period` s from start to end.
function train(g: Graph, freq: number, start: number, end: number, period: number, dur: number, level: number, pan = 0) {
  for (let t = start; t < end; t += period) beep(g, freq, t, dur, level, pan);
}

/// Sustained tone with a slow vibrato and soft edges (the scan whine).
function tone(g: Graph, freq: number, at: number, dur: number, level: number, pan = 0, vibrato = 0) {
  const env = g.ctx.createGain();
  env.gain.setValueAtTime(0.0001, at);
  env.gain.exponentialRampToValueAtTime(level, at + Math.min(0.15, dur / 4));
  env.gain.setValueAtTime(level, at + dur - Math.min(0.25, dur / 3));
  env.gain.exponentialRampToValueAtTime(0.0001, at + dur);
  const p = g.ctx.createStereoPanner();
  p.pan.value = pan;
  env.connect(p);
  p.connect(g.bus);
  const o = osc(g, 'sine', freq, at, dur, env);
  if (vibrato) {
    const lfo = g.ctx.createOscillator();
    lfo.frequency.value = 5.5;
    const depth = g.ctx.createGain();
    depth.gain.value = vibrato;
    lfo.connect(depth);
    depth.connect(o.frequency);
    lfo.start(at);
    lfo.stop(at + dur + 0.05);
  }
  return o;
}

/// Tiny metallic click for each coil engaging.
function click(g: Graph, at: number, level: number, pan = 0) {
  const bp = g.ctx.createBiquadFilter();
  bp.type = 'bandpass';
  bp.frequency.value = 3400;
  bp.Q.value = 10;
  const env = envelope(g, at, 0.001, level, 0.06, pan);
  noise(g, at, 0.03).connect(bp);
  bp.connect(env);
}

// Deterministic pseudo-random so the score is the same on every launch.
function rng(seed: number) {
  return () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
}

function score(g: Graph, t0: number) {
  const T = BOOT_TIMELINE;
  const at = (s: number) => t0 + s;
  const rand = rng(2008);

  // The fast high tick that clocks the whole cue (~28 Hz, very quiet).
  for (let t = T.coilsStart + 0.1; t < 9.6; t += 0.035) {
    const fade = t > 8.6 ? 9.6 - t : 1;
    beep(g, 6500, at(t), 0.008, 0.02 * fade, Math.sin(t * 3) * 0.5);
  }

  // ── Ignite: hit + stepped falling cascade, then a breath of silence ──
  hit(g, at(T.ignite), 0.8);
  [5124, 4560, 3840, 3050, 2420].forEach((f, i) =>
    beep(g, f, at(T.ignite + 0.06 + i * 0.12), 0.1, 0.1, 0.35 - i * 0.15),
  );
  beep(g, 850, at(T.ignite + 0.5), 0.12, 0.08, -0.2);
  beep(g, 3800, at(T.coilsStart + 0.02), 0.25, 0.08, 0.3);

  // ── Coils: hits, two clocked trains, rising chirps, a click per coil ──
  hit(g, at(T.coilsStart), 0.55, 0.5);
  hit(g, at(T.coilsStart + 0.39), 0.7);
  train(g, 1995, at(T.coilsStart + 0.1), at(T.lock - 0.05), 0.139, 0.045, 0.07, 0.25);
  train(g, 1225, at(T.coilsStart + 0.17), at(T.lock - 0.05), 0.151, 0.04, 0.05, -0.25);
  for (let t = T.coilsStart + 0.45; t < T.lock - 0.15; t += 0.22 + rand() * 0.18) {
    const from = 900 + rand() * 900;
    chirp(g, from, from * (3 + rand() * 2), at(t), 0.2 + rand() * 0.15, 0.05, rand() * 1.4 - 0.7);
  }
  for (let i = 0; i < 10; i++) {
    click(g, at(T.coilsStart + i * T.coilStep), 0.12, Math.sin((i / 10) * Math.PI * 2) * 0.6);
  }

  // ── Lock: the big hit, a low tone and falling glissandi ──
  hit(g, at(T.lock), 1.0, 0.9);
  tone(g, 312, at(T.lock + 0.1), 0.95, 0.035, 0);
  tone(g, 624, at(T.lock + 0.1), 0.95, 0.012, 0);
  for (let i = 0; i < 5; i++) {
    const t = T.lock + 0.55 + i * 0.07;
    chirp(g, 5200 - i * 300, 780 + i * 90, at(t), 0.38, 0.07, (i - 2) * 0.3);
  }
  beep(g, 2930, at(T.lock + 0.6), 0.08, 0.06, 0.2);
  beep(g, 2930, at(T.lock + 0.75), 0.08, 0.05, -0.2);

  // ── Calm under the greeting: a faint high shimmer over the tick ──
  tone(g, 6240, at(T.lock + 1.1), 1.0, 0.03, 0.3);
  tone(g, 6590, at(T.lock + 1.6), 0.9, 0.025, -0.3);

  // ── Scan: hits, a sustained whine with pulses ──
  hit(g, at(T.scan), 0.55, 0.5);
  hit(g, at(T.scan + 0.25), 0.7);
  tone(g, 5230, at(T.scan + 0.05), T.dock + 0.2 - T.scan, 0.03, 0, 6);
  train(g, 1475, at(T.scan + 0.05), at(T.scan + 0.75), 0.15, 0.05, 0.06, -0.3);
  beep(g, 3900, at(T.scan + 0.1), 0.18, 0.05, 0.4);
  beep(g, 1900, at(T.scan + 0.8), 0.25, 0.05, 0.2);
  tone(g, 430, at(T.scan + 0.7), 0.35, 0.025, -0.1);
  hit(g, at(T.dock + 0.03), 0.5, 0.4);

  // ── Dock: the loudest hit on arrival (dock + 0.55, BootIntro ARRIVAL_S),
  //    then a dense rising cluster and pulse ladders fading out by ~10 s ──
  chirp(g, 600, 5000, at(T.dock + 0.1), 0.45, 0.05, 0);
  hit(g, at(T.dock + 0.52), 1.0, 1.0);
  const tail = T.dock + 0.6;
  (
    [
      [2130, 2480, 0.05],
      [2480, 2640, 0.04],
      [2580, 2980, 0.03],
    ] as const
  ).forEach(([from, to, lvl], i) => {
    const o = tone(g, from, at(tail + i * 0.15), 9.9 - tail - i * 0.15, lvl, (i - 1) * 0.4);
    o.frequency.exponentialRampToValueAtTime(to, at(9.8));
  });
  const ladder = (start: number) => {
    for (let n = 0; n < 12; n++) beep(g, 3000 + n * 90, at(start + n * 0.03), 0.025, 0.045, 0.3 - n * 0.05);
  };
  ladder(tail + 0.4);
  ladder(tail + 1.3);
  hit(g, at(tail + 0.8), 0.45, 0.6);
  hit(g, at(tail + 1.8), 0.35, 0.6);
  for (let t = tail + 0.3; t < 9.4; t += 0.3 + rand() * 0.3) {
    const from = 1800 + rand() * 800;
    chirp(g, from, from * 1.4, at(t), 0.4, 0.03 * Math.max(0.2, (9.6 - t) / 2.5), rand() * 1.2 - 0.6);
  }
}

/// Plays the startup sound; returns a stop function (fades out, then closes).
export function playBootSound(): () => void {
  let g: Graph;
  let ctx: AudioContext;
  try {
    ctx = new AudioContext();
    g = build(ctx);
  } catch {
    return () => {};
  }
  if (ctx.state === 'suspended') ctx.resume().catch(() => {});
  score(g, ctx.currentTime + 0.05);
  const closeAt = setTimeout(() => ctx.close().catch(() => {}), (SCORE_S + 2.5) * 1000);
  return () => {
    clearTimeout(closeAt);
    g.bus.gain.setTargetAtTime(0, ctx.currentTime, 0.08);
    setTimeout(() => ctx.close().catch(() => {}), 600);
  };
}

/// The same score rendered offline (faster than real time), e.g. to check
/// its levels or bounce it to a file.
export function renderBootSound(sampleRate = 44100): Promise<AudioBuffer> {
  const ctx = new OfflineAudioContext(2, Math.ceil(sampleRate * (SCORE_S + 2)), sampleRate);
  score(build(ctx), 0.05);
  return ctx.startRendering();
}

/// Plays the user's own startup sound (`~/.openjarvis/boot-sound.mp3`, read
/// by the desktop app's `boot_sound` command) from `offset` seconds in, so
/// it lines up with an intro that started while the file was loading.
/// Resolves to a stop function, or null when there is no such file (or
/// outside the desktop app) and the synthesized score should play instead.
/// Nothing starts once `alive()` is false (the intro unmounted meanwhile).
export async function playCustomBootSound(
  offset: () => number,
  alive: () => boolean,
): Promise<(() => void) | null> {
  let bytes: ArrayBuffer;
  try {
    const { invoke } = await import('@tauri-apps/api/core');
    bytes = await invoke<ArrayBuffer>('boot_sound');
  } catch {
    return null;
  }
  let ctx: AudioContext;
  try {
    ctx = new AudioContext();
    const buffer = await ctx.decodeAudioData(bytes);
    if (ctx.state === 'suspended') await ctx.resume().catch(() => {});
    if (!alive()) {
      ctx.close().catch(() => {});
      return () => {};
    }
    const gain = ctx.createGain();
    gain.connect(ctx.destination);
    const src = ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(gain);
    const from = Math.max(0, offset());
    src.start(0, Math.min(from, buffer.duration));
    const closeAt = setTimeout(
      () => ctx.close().catch(() => {}),
      (buffer.duration - from + 1) * 1000,
    );
    return () => {
      clearTimeout(closeAt);
      gain.gain.setTargetAtTime(0, ctx.currentTime, 0.08);
      setTimeout(() => ctx.close().catch(() => {}), 600);
    };
  } catch {
    return null;
  }
}
