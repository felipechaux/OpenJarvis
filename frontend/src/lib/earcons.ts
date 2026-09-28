// ── J.A.R.V.I.S. interface chimes ───────────────────────────────────
// Short synthesized earcons (no audio files) in the spirit of Alexa's
// listen/processing tones, with a glassy "holographic HUD" finish:
//
//   wake       rising three-note glass arpeggio over a soft air sweep
//   processing quick descending two-note blip — "got it, working on it"
//   done       soft bell dyad with shimmer — "your answer is ready"
//
// Everything runs through one shared graph:
//   voices ─ bus ─┬─ dry ────────────┬─ master ─ speakers
//                 └─ short room verb ┘
// Kept deliberately quiet and brief (<0.5s) so the chimes never compete
// with the voice or leak into the wake-word / VAD microphones.

export type Earcon = 'wake' | 'processing' | 'done';

interface ChimeGraph {
  ctx: AudioContext;
  bus: GainNode;
}

let graph: ChimeGraph | null = null;

function makeRoom(ctx: AudioContext, seconds = 0.8, decay = 4): AudioBuffer {
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

function getGraph(): ChimeGraph {
  if (graph && graph.ctx.state !== 'closed') return graph;
  const ctx = new AudioContext();

  const master = ctx.createGain();
  master.gain.value = 0.22;
  master.connect(ctx.destination);

  const bus = ctx.createGain();
  bus.connect(master);

  const verb = ctx.createConvolver();
  verb.buffer = makeRoom(ctx);
  const verbMix = ctx.createGain();
  verbMix.gain.value = 0.28;
  bus.connect(verb);
  verb.connect(verbMix);
  verbMix.connect(master);

  graph = { ctx, bus };
  return graph;
}

/// One glassy partial: sine fundamental plus a quiet octave for sparkle,
/// fast attack and exponential decay.
function glass(
  g: ChimeGraph,
  freq: number,
  at: number,
  dur: number,
  level: number,
  pan = 0,
) {
  const { ctx, bus } = g;
  const env = ctx.createGain();
  env.gain.setValueAtTime(0.0001, at);
  env.gain.exponentialRampToValueAtTime(level, at + 0.008);
  env.gain.exponentialRampToValueAtTime(0.0001, at + dur);

  const panner = ctx.createStereoPanner();
  panner.pan.value = pan;
  env.connect(panner);
  panner.connect(bus);

  const partials: [OscillatorType, number, number][] = [
    ['sine', 1, 1],
    ['triangle', 2, 0.18],
    ['sine', 3.01, 0.06], // slightly inharmonic → bell-like sheen
  ];
  for (const [type, mult, amp] of partials) {
    const osc = ctx.createOscillator();
    osc.type = type;
    osc.frequency.value = freq * mult;
    const a = ctx.createGain();
    a.gain.value = amp;
    osc.connect(a);
    a.connect(env);
    osc.start(at);
    osc.stop(at + dur + 0.05);
  }
}

/// Filtered-noise "air" sweep — the holographic power-up swish.
function airSweep(g: ChimeGraph, at: number, dur: number, from: number, to: number, level: number) {
  const { ctx, bus } = g;
  const len = Math.floor(ctx.sampleRate * dur);
  const buf = ctx.createBuffer(1, len, ctx.sampleRate);
  const data = buf.getChannelData(0);
  for (let i = 0; i < len; i++) data[i] = Math.random() * 2 - 1;

  const src = ctx.createBufferSource();
  src.buffer = buf;
  const bp = ctx.createBiquadFilter();
  bp.type = 'bandpass';
  bp.Q.value = 6;
  bp.frequency.setValueAtTime(from, at);
  bp.frequency.exponentialRampToValueAtTime(to, at + dur);

  const env = ctx.createGain();
  env.gain.setValueAtTime(0.0001, at);
  env.gain.exponentialRampToValueAtTime(level, at + dur * 0.6);
  env.gain.exponentialRampToValueAtTime(0.0001, at + dur);

  src.connect(bp);
  bp.connect(env);
  env.connect(bus);
  src.start(at);
  src.stop(at + dur);
}

export function playEarcon(kind: Earcon): void {
  let g: ChimeGraph;
  try {
    g = getGraph();
  } catch {
    return; // Web Audio unavailable
  }
  if (g.ctx.state === 'suspended') g.ctx.resume().catch(() => {});
  const t = g.ctx.currentTime + 0.02;

  switch (kind) {
    case 'wake':
      // E5 → B5 → E6, fanning slightly left-to-right.
      airSweep(g, t, 0.22, 900, 5200, 0.35);
      glass(g, 659.25, t + 0.03, 0.32, 0.5, -0.25);
      glass(g, 987.77, t + 0.1, 0.32, 0.45, 0);
      glass(g, 1318.51, t + 0.17, 0.42, 0.4, 0.25);
      break;
    case 'processing':
      // D6 → G5: the "request received" drop.
      glass(g, 1174.66, t, 0.16, 0.38, 0.15);
      glass(g, 783.99, t + 0.085, 0.22, 0.34, -0.15);
      break;
    case 'done':
      // A5 + E6 dyad, the fifth arriving a hair late for a soft bloom.
      glass(g, 880, t, 0.5, 0.36, -0.1);
      glass(g, 1318.51, t + 0.035, 0.55, 0.26, 0.1);
      airSweep(g, t, 0.18, 6000, 3000, 0.12);
      break;
  }
}
