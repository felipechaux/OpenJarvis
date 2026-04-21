import { useCallback, useRef, useState, useEffect } from 'react';
import { getBase } from '../lib/api';

export interface AudioAnalyzerData {
  frequencyData: Uint8Array | number[] | null;
  averageLevel: number;
  bassLevel: number;
  trebleLevel: number;
}

const ABBREVS = /\b(Mr|Mrs|Ms|Dr|Prof|St|Jr|Sr|vs|etc|No|Fig)\./g;

function cleanForSpeech(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, '')
    .replace(/`[^`]+`/g, '')
    .replace(/\*\*?([^*]+)\*\*?/g, '$1')
    .replace(/#{1,6}\s/g, '')
    .replace(ABBREVS, '$1') // strip periods from abbreviations so TTS doesn't pause
    .trim();
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

  // Fetch audio bytes only — no playback
  const fetchAudio = useCallback(async (text: string, voiceId = 'en-GB-RyanNeural'): Promise<ArrayBuffer | null> => {
    const clean = cleanForSpeech(text);
    if (!clean) return null;
    try {
      const res = await fetch(`${getBase()}/v1/speech/synthesize`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: clean, voice_id: voiceId }),
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

    const analyzer = ctx.createAnalyser();
    analyzer.fftSize = 64;
    analyzer.smoothingTimeConstant = 0.8;
    analyzerRef.current = analyzer;
    dataArrayRef.current = new Uint8Array(analyzer.frequencyBinCount);

    source.connect(analyzer);
    analyzer.connect(ctx.destination);
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

  // Immediate speak — interrupts queue
  const speak = useCallback(async (text: string, voiceId = 'en-GB-RyanNeural') => {
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
