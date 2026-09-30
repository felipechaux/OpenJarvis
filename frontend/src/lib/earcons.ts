// ── J.A.R.V.I.S. interface chimes ───────────────────────────────────
// Short synthesized earcons (no audio files) in the spirit of Alexa's
// listen/processing tones, with a glassy "holographic HUD" finish:
//
//   wake       rising three-note glass arpeggio over a soft air sweep
//   processing quick descending two-note blip — "got it, working on it"
//   done       soft bell dyad with shimmer — "your answer is ready"
//   shutter    glassy click + tick — "looking at your screen"
//   switch     two quick rising fifths — "changed model / provider"
//   session    three-note bell call — "a coding session needs you"
//   error      soft falling minor second — "that did not work"
//   dismiss    single falling fourth, very soft — "heard nothing, standing by"
//   reveal     crisp tick + rising glass fourth — notch pill opens
//   conceal    softer tick + falling fourth — notch pill closes
//   scan       holographic noise sweep + high tick — browser task begins
//   point      swoosh then octave leap — "look here" (for on-screen pointing)
//
// reveal/conceal/point take their cue from Clicky (farzaa/clicky), which
// ships a ~75 ms bright click (enter.mp3, ~2.1 kHz) and an eShop-style
// G5 → G6 octave bell (eshop.mp3); here they are rebuilt from the same
// glass partials so they sit with the rest of the HUD palette.
//
// Everything runs through one shared graph:
//   voices ─ bus ─┬─ dry ────────────┬─ master ─ speakers
//                 └─ short room verb ┘
// Kept deliberately quiet and brief (<0.5s) so the chimes never compete
// with the voice or leak into the wake-word / VAD microphones.

export type Earcon =
  | 'wake'
  | 'processing'
  | 'done'
  | 'shutter'
  | 'switch'
  | 'session'
  | 'error'
  | 'dismiss'
  | 'reveal'
  | 'conceal'
  | 'scan'
  | 'point';

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
    case 'shutter':
      // Camera-ish: a bright noise click, then a high glass tick.
      airSweep(g, t, 0.06, 7000, 2500, 0.5);
      glass(g, 2093, t + 0.05, 0.12, 0.3, 0.2);
      break;
    case 'switch':
      // G5 → D6, then A5 → E6: two quick fifths climbing.
      glass(g, 783.99, t, 0.18, 0.32, -0.2);
      glass(g, 1174.66, t + 0.06, 0.2, 0.28, -0.2);
      glass(g, 880, t + 0.14, 0.2, 0.32, 0.2);
      glass(g, 1318.51, t + 0.2, 0.3, 0.28, 0.2);
      break;
    case 'session':
      // C6 → E6 → G6 bell call, a touch slower than wake.
      glass(g, 1046.5, t, 0.35, 0.4, -0.2);
      glass(g, 1318.51, t + 0.13, 0.35, 0.36, 0);
      glass(g, 1567.98, t + 0.26, 0.5, 0.34, 0.2);
      break;
    case 'error':
      // E5 → D#5: a gentle "uh-oh", no harsh buzz.
      glass(g, 659.25, t, 0.22, 0.34, 0);
      glass(g, 622.25, t + 0.14, 0.4, 0.3, 0);
      break;
    case 'dismiss':
      // B5 → F#5, barely there: listening ended without a request.
      glass(g, 987.77, t, 0.14, 0.2, 0.1);
      glass(g, 739.99, t + 0.07, 0.24, 0.17, -0.1);
      break;
    case 'reveal':
      // Clicky's "enter" click, glassier: a 35 ms air tick, then B5 → E6.
      airSweep(g, t, 0.035, 8000, 3500, 0.28);
      glass(g, 987.77, t + 0.02, 0.16, 0.22, -0.1);
      glass(g, 1318.51, t + 0.065, 0.26, 0.2, 0.1);
      break;
    case 'conceal':
      // The mirror of reveal, quieter and lower: E6 → B5.
      airSweep(g, t, 0.03, 6000, 2500, 0.2);
      glass(g, 1318.51, t + 0.015, 0.12, 0.16, 0.1);
      glass(g, 987.77, t + 0.06, 0.2, 0.16, -0.1);
      break;
    case 'scan':
      // Rising holographic sweep with two high ticks — "going out to the web".
      airSweep(g, t, 0.16, 2400, 7200, 0.2);
      glass(g, 1567.98, t + 0.1, 0.12, 0.18, -0.2);
      glass(g, 2093, t + 0.16, 0.18, 0.16, 0.2);
      break;
    case 'point':
      // Flight swoosh, then the eShop-style octave leap G5 → G6 on arrival.
      airSweep(g, t, 0.16, 1200, 4800, 0.2);
      glass(g, 783.99, t + 0.1, 0.14, 0.28, 0);
      glass(g, 1567.98, t + 0.16, 0.3, 0.26, 0.2);
      break;
  }
}
