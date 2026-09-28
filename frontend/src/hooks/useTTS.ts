import { useCallback, useRef, useState, useEffect } from 'react';
import { getBase } from '../lib/api';
import { useAppStore } from '../lib/store';

export interface AudioAnalyzerData {
  frequencyData: Uint8Array | number[] | null;
  averageLevel: number;
  bassLevel: number;
  trebleLevel: number;
}

const ABBREVS = /\b(Mr|Mrs|Ms|Dr|Prof|St|Jr|Sr|vs|etc|No|Fig)\./g;

// Edge-TTS voice IDs.  British male for English (matches Iron Man's JARVIS),
// Castilian male for Spanish (more formal, distinct 'c'/'z' that pairs well
// with the British formality of the English voice).
const VOICE_EN = 'en-GB-RyanNeural';
const VOICE_ES = 'es-ES-AlvaroNeural';

// Heuristic Spanish detector.  Two signals catch nearly every real sentence:
//   1. Spanish-only orthography: ñ, inverted punctuation, accented vowels, ü.
//   2. Short greetings/connectors that often lack diacritics ("hola", "gracias").
// We deliberately keep the wordlist tight to avoid false positives on English
// text that happens to contain "que" or "como" (English imports).
const SPANISH_ORTHOGRAPHY = /[ñ¿¡áéíóúüÑÁÉÍÓÚÜ]/;
const SPANISH_WORDS = /\b(hola|gracias|buenas|buenos|dias|dia|tardes|noches|señor|señora|usted|ustedes|estas|estoy|estamos|aqui|alli|tambien)\b/i;

function detectVoice(text: string): string {
  if (SPANISH_ORTHOGRAPHY.test(text) || SPANISH_WORDS.test(text)) {
    return VOICE_ES;
  }
  return VOICE_EN;
}

/// Pick the voice for a chunk of text honouring the app-level language
/// setting.  A fixed language uses ONE voice for everything — per-sentence
/// auto-detection made bilingual replies flip accents mid-response, which
/// is exactly the "two accents / two languages" complaint.  Auto-detection
/// only remains for the explicit 'auto' setting.
function resolveVoice(text: string): string {
  const lang = useAppStore.getState().settings.language;
  if (lang === 'es') return VOICE_ES;
  if (lang === 'en') return VOICE_EN;
  return detectVoice(text);
}

function cleanForSpeech(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, '')
    .replace(/`[^`]+`/g, '')
    .replace(/\*\*?([^*]+)\*\*?/g, '$1')
    .replace(/#{1,6}\s/g, '')
    // Horizontal rules / heading underlines — "===", "---", "***", "___".
    // These get read literally by TTS as "equals equals equals" etc.
    .replace(/^\s*[=\-*_]{3,}\s*$/gm, '')
    // Bullet markers at the start of a line (•, *, -, +, ·) — keep the text,
    // drop the marker so the speaker doesn't say "asterisk" or pause oddly.
    .replace(/^[\s]*[•·*+\-]\s+/gm, '')
    // Numbered list prefixes like "1. " or "12) ".
    .replace(/^\s*\d+[.)]\s+/gm, '')
    // Markdown links [text](url) → text (drop the URL).
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    // Blockquote markers ">" at the start of a line.
    .replace(/^\s*>\s+/gm, '')
    // Collapse any remaining sequences of decorative chars TTS would vocalise.
    .replace(/[=]{2,}/g, '')
    .replace(/\s{2,}/g, ' ')
    .replace(ABBREVS, '$1') // strip periods from abbreviations so TTS doesn't pause
    .trim();
}

// ── J.A.R.V.I.S. voice polish ────────────────────────────────────────
// A subtle "holographic" finish on top of the neural voice.  Everything is
// deliberately gentle — the goal is a polished, slightly larger-than-life
// butler, never a robot:
//
//   input ─ highpass 80Hz ─ warmth +1.5dB@200 ─ mud −2dB@380 ─ presence +2dB@3k
//         ─ air +2.5dB shelf@9k ─ compressor (2.5:1, soft knee) ─┬─ dry ───────────┐
//                                                               ├─ room reverb ───┤ (≈11%)
//                                                               └─ shimmer comb ──┤ (≈5%)
//                                                                                 └─ limiter ─ output
//
// The room reverb is a short (~0.55s), decorrelated-stereo synthetic IR,
// highpassed so the tail never muddies consonants.  The shimmer is a tiny
// 7ms comb on the upper band only — the faint metallic sheen of JARVIS.
// The graph is built once per AudioContext and reused across sentences so
// reverb tails flow naturally between them.
interface VoiceFxChain {
  ctx: AudioContext;
  input: AudioNode;
  output: AudioNode;
}

function makeRoomImpulse(ctx: AudioContext, seconds = 0.55, decay = 5.5): AudioBuffer {
  const length = Math.floor(ctx.sampleRate * seconds);
  const ir = ctx.createBuffer(2, length, ctx.sampleRate);
  for (let ch = 0; ch < 2; ch++) {
    const data = ir.getChannelData(ch);
    for (let i = 0; i < length; i++) {
      const t = i / length;
      // Independent noise per channel → natural stereo width.
      data[i] = (Math.random() * 2 - 1) * Math.pow(1 - t, decay);
    }
  }
  return ir;
}

function buildVoiceFx(ctx: AudioContext): VoiceFxChain {
  const peq = (type: BiquadFilterType, freq: number, gain = 0, q = 0.9) => {
    const f = ctx.createBiquadFilter();
    f.type = type;
    f.frequency.value = freq;
    f.gain.value = gain;
    f.Q.value = q;
    return f;
  };

  // Tone shaping
  const highpass = peq('highpass', 80, 0, 0.707);
  const warmth = peq('peaking', 200, 1.5, 0.9);
  const mud = peq('peaking', 380, -2, 1.1);
  const presence = peq('peaking', 3000, 2, 1.0);
  const air = peq('highshelf', 9000, 2.5);

  const comp = ctx.createDynamicsCompressor();
  comp.threshold.value = -20;
  comp.knee.value = 12;
  comp.ratio.value = 2.5;
  comp.attack.value = 0.005;
  comp.release.value = 0.18;

  highpass.connect(warmth);
  warmth.connect(mud);
  mud.connect(presence);
  presence.connect(air);
  air.connect(comp);

  const sum = ctx.createGain();
  sum.gain.value = 1;

  // Dry path (with a little make-up gain after compression)
  const dry = ctx.createGain();
  dry.gain.value = 1.08;
  comp.connect(dry);
  dry.connect(sum);

  // Short "holographic room" reverb, low mix
  const reverbHp = peq('highpass', 450, 0, 0.707);
  const reverb = ctx.createConvolver();
  reverb.buffer = makeRoomImpulse(ctx);
  const reverbMix = ctx.createGain();
  reverbMix.gain.value = 0.11;
  comp.connect(reverbHp);
  reverbHp.connect(reverb);
  reverb.connect(reverbMix);
  reverbMix.connect(sum);

  // Upper-band shimmer comb (7ms, light feedback), panned slightly right
  // so it reads as width rather than as an echo.
  const shimmerBand = peq('highpass', 2500, 0, 0.707);
  const shimmerDelay = ctx.createDelay(0.05);
  shimmerDelay.delayTime.value = 0.007;
  const shimmerFb = ctx.createGain();
  shimmerFb.gain.value = 0.25;
  const shimmerMix = ctx.createGain();
  shimmerMix.gain.value = 0.05;
  const shimmerPan = ctx.createStereoPanner();
  shimmerPan.pan.value = 0.35;
  comp.connect(shimmerBand);
  shimmerBand.connect(shimmerDelay);
  shimmerDelay.connect(shimmerFb);
  shimmerFb.connect(shimmerDelay);
  shimmerDelay.connect(shimmerMix);
  shimmerMix.connect(shimmerPan);
  shimmerPan.connect(sum);

  // Safety limiter so the boosts never clip.
  const limiter = ctx.createDynamicsCompressor();
  limiter.threshold.value = -2;
  limiter.knee.value = 0;
  limiter.ratio.value = 20;
  limiter.attack.value = 0.002;
  limiter.release.value = 0.1;
  sum.connect(limiter);

  return { ctx, input: highpass, output: limiter };
}

export function useTTS() {
  const [speaking, setSpeaking] = useState(false);
  const [audioData, setAudioData] = useState<AudioAnalyzerData>({
    frequencyData: null,
    averageLevel: 0,
    bassLevel: 0,
    trebleLevel: 0,
  });
  const sourceRef = useRef<AudioBufferSourceNode | null>(null);
  const ctxRef = useRef<AudioContext | null>(null);
  const analyzerRef = useRef<AnalyserNode | null>(null);
  const animationFrameRef = useRef<number | null>(null);
  const dataArrayRef = useRef<Uint8Array | null>(null);
  // Persistent per-AudioContext output stage: [voice FX] → analyser → speakers.
  // The analyser sits AFTER the FX so the reactor animation reacts to exactly
  // what the user hears.
  const outAnalyserRef = useRef<AnalyserNode | null>(null);
  const fxRef = useRef<VoiceFxChain | null>(null);

  const queueRef = useRef<string[]>([]);
  const drainingRef = useRef(false);
  const stopRequestedRef = useRef(false);

  const analyzeAudio = useCallback(() => {
    const analyzer = analyzerRef.current;
    if (!analyzer || !dataArrayRef.current) return;

    analyzer.getByteFrequencyData(dataArrayRef.current);

    const sum = dataArrayRef.current.reduce((a, b) => a + b, 0);
    const average = sum / dataArrayRef.current.length / 255;

    const bassEnd = Math.floor(dataArrayRef.current.length * 0.3);
    const bassSum = dataArrayRef.current.slice(0, bassEnd).reduce((a, b) => a + b, 0);
    const bass = bassSum / bassEnd / 255;

    const trebleStart = Math.floor(dataArrayRef.current.length * 0.6);
    const trebleSum = dataArrayRef.current.slice(trebleStart).reduce((a, b) => a + b, 0);
    const treble = trebleSum / (dataArrayRef.current.length - trebleStart) / 255;

    setAudioData({
      frequencyData: dataArrayRef.current,
      averageLevel: Math.min(average * 2.5, 1),
      bassLevel: Math.min(bass * 3, 1),
      trebleLevel: Math.min(treble * 2, 1),
    });

    if (speaking) {
      animationFrameRef.current = requestAnimationFrame(analyzeAudio);
    }
  }, [speaking]);

  useEffect(() => {
    if (speaking) {
      analyzeAudio();
    } else {
      if (animationFrameRef.current) cancelAnimationFrame(animationFrameRef.current);
      setAudioData({ frequencyData: null, averageLevel: 0, bassLevel: 0, trebleLevel: 0 });
    }
    return () => {
      if (animationFrameRef.current) cancelAnimationFrame(animationFrameRef.current);
    };
  }, [speaking, analyzeAudio]);

  // Fetch audio bytes only — no playback.  When no voice is forced, pick
  // EN vs ES from the text itself so Spanish replies don't get spoken in
  // English (which sounds awful and is unintelligible to non-English
  // listeners).  Detection runs after markdown stripping so list bullets
  // / code fences don't skew it.
  const fetchAudio = useCallback(async (text: string, voiceId?: string): Promise<ArrayBuffer | null> => {
    const clean = cleanForSpeech(text);
    if (!clean) return null;
    const voice = voiceId ?? resolveVoice(clean);
    try {
      const res = await fetch(`${getBase()}/v1/speech/synthesize`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: clean, voice_id: voice }),
      });
      if (!res.ok) {
        console.warn('[TTS] synthesize failed:', res.status);
        return null;
      }
      return await res.arrayBuffer();
    } catch (e) {
      console.warn('[TTS] fetch error:', e);
      return null;
    }
  }, []);

  // Play a pre-fetched audio buffer and return a Promise that resolves when done
  const playBuffer = useCallback(async (arrayBuf: ArrayBuffer): Promise<void> => {
    if (!ctxRef.current || ctxRef.current.state === 'closed') {
      ctxRef.current = new AudioContext();
    }
    const ctx = ctxRef.current;
    if (ctx.state === 'suspended') await ctx.resume();

    const audioBuf = await ctx.decodeAudioData(arrayBuf);
    const source = ctx.createBufferSource();
    source.buffer = audioBuf;

    // (Re)build the output stage when the context changed.
    if (!outAnalyserRef.current || outAnalyserRef.current.context !== ctx) {
      const out = ctx.createAnalyser();
      out.fftSize = 64;
      out.smoothingTimeConstant = 0.8;
      out.connect(ctx.destination);
      outAnalyserRef.current = out;
      fxRef.current = null;
    }
    const analyzer = outAnalyserRef.current;
    analyzerRef.current = analyzer;
    dataArrayRef.current = new Uint8Array(analyzer.frequencyBinCount);

    // Voice FX is read per sentence so toggling it in Settings applies to
    // the very next sentence without reloading.
    if (useAppStore.getState().settings.voiceFx) {
      if (!fxRef.current || fxRef.current.ctx !== ctx) {
        fxRef.current = buildVoiceFx(ctx);
        fxRef.current.output.connect(analyzer);
      }
      source.connect(fxRef.current.input);
    } else {
      source.connect(analyzer);
    }
    sourceRef.current = source;

    return new Promise<void>((resolve) => {
      source.onended = () => {
        sourceRef.current = null;
        analyzerRef.current = null;
        resolve();
      };
      source.start(0);
    });
  }, []);

  // Drains the queue with a 1-sentence lookahead: fetches sentence N+1 while sentence N plays
  const drainQueue = useCallback(async () => {
    if (drainingRef.current) return;
    drainingRef.current = true;
    setSpeaking(true);

    // Prefetched audio for the next item in queue
    let prefetched: Promise<ArrayBuffer | null> | null = null;

    while (!stopRequestedRef.current && queueRef.current.length > 0) {
      const text = queueRef.current.shift()!;

      // Use already-in-flight prefetch if it matches the current item, else fetch now
      const bufPromise = prefetched ?? fetchAudio(text);
      prefetched = null;

      // Immediately kick off prefetch for the next item
      if (queueRef.current.length > 0) {
        prefetched = fetchAudio(queueRef.current[0]);
      }

      const buf = await bufPromise;

      // After awaiting the fetch, new items may have arrived — start prefetch if idle
      if (!prefetched && queueRef.current.length > 0) {
        prefetched = fetchAudio(queueRef.current[0]);
      }

      if (buf && !stopRequestedRef.current) {
        await playBuffer(buf);
      }

      // After playback, new items may have arrived
      if (!prefetched && queueRef.current.length > 0) {
        prefetched = fetchAudio(queueRef.current[0]);
      }
    }

    drainingRef.current = false;
    if (!stopRequestedRef.current) setSpeaking(false);
  }, [fetchAudio, playBuffer]);

  // Enqueue a sentence — audio fetch starts immediately, plays in order
  const enqueue = useCallback((text: string) => {
    stopRequestedRef.current = false;
    queueRef.current.push(text);
    drainQueue();
  }, [drainQueue]);

  // Immediate speak — interrupts queue.  Like fetchAudio, voice auto-detects
  // when not overridden.
  const speak = useCallback(async (text: string, voiceId?: string) => {
    stopRequestedRef.current = true;
    queueRef.current = [];
    drainingRef.current = false;

    if (sourceRef.current) {
      try { sourceRef.current.stop(); } catch {}
      sourceRef.current = null;
    }
    if (animationFrameRef.current) cancelAnimationFrame(animationFrameRef.current);

    const buf = await fetchAudio(text, voiceId);
    if (!buf) { setSpeaking(false); return; }

    stopRequestedRef.current = false;
    setSpeaking(true);

    try {
      await playBuffer(buf);
    } finally {
      setSpeaking(false);
    }
  }, [fetchAudio, playBuffer]);

  const stop = useCallback(() => {
    stopRequestedRef.current = true;
    queueRef.current = [];
    drainingRef.current = false;

    if (sourceRef.current) {
      try { sourceRef.current.stop(); } catch {}
      sourceRef.current = null;
    }
    if (animationFrameRef.current) cancelAnimationFrame(animationFrameRef.current);
    analyzerRef.current = null;
    setSpeaking(false);
    setAudioData({ frequencyData: null, averageLevel: 0, bassLevel: 0, trebleLevel: 0 });
  }, []);

  return { speak, enqueue, stop, speaking, audioData };
}
