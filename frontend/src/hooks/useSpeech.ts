import { useState, useCallback, useRef, useEffect } from 'react';
import { transcribeAudio, fetchSpeechHealth } from '../lib/api';
import { useAppStore } from '../lib/store';
import { VadGate } from '../lib/vad';

export type SpeechState = 'idle' | 'recording' | 'transcribing';

export interface StartRecordingOptions {
  /// When true, monitor mic levels and stop automatically after a brief
  /// pause once the user has started speaking.  Used by the wake-word
  /// flow so the user doesn't have to click stop after saying their
  /// command.
  autoStop?: boolean;
  /// Invoked with the transcript when ``autoStop`` triggers and
  /// transcription completes.  Not called for manual stops (the caller
  /// awaits ``stopRecording`` for those).
  onAutoResult?: (text: string) => void;
  /// Invoked when ``autoStop`` triggers but no speech is captured
  /// (silence timeout after wake), so the UI can reset cleanly.
  onAutoCancel?: () => void;
}

// Hands-free recordings stop via VadGate (lib/vad.ts): thresholds relative
// to the background so music doesn't hold the mic open, and a recording
// with no speech is discarded instead of transcribed.
// Log every Nth tick so the console doesn't flood but we can still see VAD
// progress (RMS, speechStarted, elapsed) when debugging "the mic opened
// but never closed" bugs in WKWebView.
const VAD_LOG_EVERY_N_TICKS = 12;
// RMS that maps to a full-scale mic level (normal voice ≈ 0.05-0.15).
const METER_FULL_SCALE_RMS = 0.18;

/// Publish a smoothed 0..1 mic level to the store while recording so
/// presence visuals can follow the user's voice.  Independent of the VAD
/// so it also runs for manual (click-to-talk) recordings.
function startMicMeter(stream: MediaStream): () => void {
  const setMicLevel = useAppStore.getState().setMicLevel;
  const ctx = new AudioContext();
  ctx.resume().catch(() => {});
  const source = ctx.createMediaStreamSource(stream);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 512;
  source.connect(analyser);
  const buffer = new Uint8Array(analyser.fftSize);
  let level = 0;
  let raf = 0;
  const loop = () => {
    analyser.getByteTimeDomainData(buffer);
    let sumSquares = 0;
    for (let i = 0; i < buffer.length; i++) {
      const sample = (buffer[i] - 128) / 128;
      sumSquares += sample * sample;
    }
    const target = Math.min(Math.sqrt(sumSquares / buffer.length) / METER_FULL_SCALE_RMS, 1);
    // Fast attack, slow release so the visual doesn't flicker between words.
    level += (target - level) * (target > level ? 0.5 : 0.12);
    setMicLevel(level);
    raf = requestAnimationFrame(loop);
  };
  raf = requestAnimationFrame(loop);
  return () => {
    cancelAnimationFrame(raf);
    try { source.disconnect(); } catch {}
    ctx.close().catch(() => {});
    setMicLevel(0);
  };
}

export function useSpeech() {
  const [state, setState] = useState<SpeechState>('idle');
  const [error, setError] = useState<string | null>(null);
  const [available, setAvailable] = useState(false);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const vadCleanupRef = useRef<(() => void) | null>(null);
  const meterCleanupRef = useRef<(() => void) | null>(null);

  const stopMeter = useCallback(() => {
    meterCleanupRef.current?.();
    meterCleanupRef.current = null;
  }, []);

  // Mirror capture state into the store for presence visuals.
  useEffect(() => {
    useAppStore.getState().setSpeechState(state);
  }, [state]);

  useEffect(() => stopMeter, [stopMeter]);

  // Check if speech backend is available on mount
  useEffect(() => {
    fetchSpeechHealth()
      .then((health) => setAvailable(health.available))
      .catch(() => setAvailable(false));
  }, []);

  /// Stop capture and throw the audio away — no transcription.  Used when
  /// a wake-triggered recording heard no speech (music, a false wake):
  /// Whisper would otherwise turn the song or the silence into a command.
  const discardRecording = useCallback(() => {
    const recorder = mediaRecorderRef.current;
    stopMeter();
    if (recorder) {
      recorder.ondataavailable = null;
      recorder.onstop = null;
      if (recorder.state === 'recording') {
        try { recorder.stop(); } catch {}
      }
    }
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    mediaRecorderRef.current = null;
    chunksRef.current = [];
    setState('idle');
  }, [stopMeter]);

  const finalizeRecording = useCallback(async (): Promise<string> => {
    const recorder = mediaRecorderRef.current;
    if (!recorder) throw new Error('Not recording');

    return new Promise((resolve, reject) => {
      recorder.onstop = async () => {
        stopMeter();
        setState('transcribing');

        // Stop all audio tracks
        streamRef.current?.getTracks().forEach((track) => track.stop());
        streamRef.current = null;

        const blob = new Blob(chunksRef.current, { type: recorder.mimeType || 'audio/webm' });
        chunksRef.current = [];

        const transcribeStart = Date.now();
        console.log('[useSpeech] transcribe start', {
          blobBytes: blob.size,
          mimeType: blob.type,
        });

        try {
          // Force the configured language so Whisper doesn't mis-detect and
          // return the wrong-language transcript; 'auto' keeps detection on.
          const lang = useAppStore.getState().settings.language;
          const result = await transcribeAudio(
            blob,
            undefined,
            lang === 'auto' ? undefined : lang,
          );
          console.log('[useSpeech] transcribe done', {
            ms: Date.now() - transcribeStart,
            text: result.text,
          });
          setState('idle');
          resolve(result.text);
        } catch (err) {
          console.warn('[useSpeech] transcribe failed', {
            ms: Date.now() - transcribeStart,
            err,
          });
          setState('idle');
          const msg = err instanceof Error ? err.message : 'Transcription failed';
          setError(msg);
          reject(err);
        }
      };

      if (recorder.state === 'recording') {
        recorder.stop();
      } else {
        // Already stopped — fire onstop manually
        recorder.onstop?.(new Event('stop'));
      }
    });
  }, [stopMeter]);

  const startRecording = useCallback(async (options: StartRecordingOptions = {}): Promise<void> => {
    setError(null);

    if (!navigator.mediaDevices?.getUserMedia) {
      setError('Microphone not supported in this browser');
      return;
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;

      const recorder = new MediaRecorder(stream);
      chunksRef.current = [];

      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };

      recorder.start();
      mediaRecorderRef.current = recorder;
      setState('recording');
      stopMeter();
      meterCleanupRef.current = startMicMeter(stream);

      if (options.autoStop) {
        // Voice Activity Detection: stop automatically after the user
        // pauses post-speech, or after a no-speech timeout.  This is the
        // hands-free path used when a wake word triggered recording.
        const audioCtx = new AudioContext();
        // WebKit (and therefore Tauri's WKWebView) starts AudioContext in
        // "suspended" state when there's no user gesture — and a wake-word
        // event is NOT a gesture from the browser's POV.  Without resume()
        // the analyser returns 128 for every sample (silence), RMS stays
        // at 0, speechStarted never flips true, and the mic stays open
        // forever.  resume() unblocks the audio graph.
        audioCtx.resume().catch((err) => {
          console.warn('[useSpeech] AudioContext.resume() failed:', err);
        });
        const source = audioCtx.createMediaStreamSource(stream);
        const analyser = audioCtx.createAnalyser();
        analyser.fftSize = 1024;
        source.connect(analyser);
        const buffer = new Uint8Array(analyser.fftSize);

        const recordingStart = Date.now();
        const gate = new VadGate();
        let stopped = false;
        let tickCount = 0;

        console.log('[useSpeech] autoStop VAD armed', {
          ctxState: audioCtx.state,
          sampleRate: audioCtx.sampleRate,
          tracks: stream.getAudioTracks().map((t) => ({
            label: t.label,
            enabled: t.enabled,
            muted: t.muted,
            readyState: t.readyState,
          })),
        });

        const triggerStop = async (cancelled: boolean) => {
          if (stopped) return;
          stopped = true;
          console.log('[useSpeech] VAD triggerStop', {
            cancelled,
            elapsedMs: Date.now() - recordingStart,
            speechStarted: gate.speechStarted,
            speechMs: gate.speechMs,
            maxRmsSeen: Number(gate.maxRms.toFixed(4)),
            floor: Number(gate.floor.toFixed(4)),
          });
          cleanup();
          if (cancelled) {
            discardRecording();
            options.onAutoCancel?.();
            return;
          }
          try {
            const text = await finalizeRecording();
            if (!text.trim()) {
              options.onAutoCancel?.();
            } else {
              options.onAutoResult?.(text);
            }
          } catch (err) {
            console.warn('[useSpeech] finalizeRecording failed', err);
            options.onAutoCancel?.();
          }
        };

        const tick = window.setInterval(() => {
          if (stopped) return;
          analyser.getByteTimeDomainData(buffer);
          let sumSquares = 0;
          for (let i = 0; i < buffer.length; i++) {
            const sample = (buffer[i] - 128) / 128;
            sumSquares += sample * sample;
          }
          const rms = Math.sqrt(sumSquares / buffer.length);
          const elapsed = Date.now() - recordingStart;
          const verdict = gate.push(rms, elapsed);

          tickCount += 1;
          if (tickCount % VAD_LOG_EVERY_N_TICKS === 0) {
            console.log('[useSpeech] VAD tick', {
              elapsedMs: elapsed,
              rms: Number(rms.toFixed(4)),
              floor: Number(gate.floor.toFixed(4)),
              speechThreshold: Number(gate.speechThreshold.toFixed(4)),
              speechStarted: gate.speechStarted,
              silenceMs: gate.silenceMs(elapsed),
              ctxState: audioCtx.state,
            });
          }

          if (verdict !== 'listening') triggerStop(verdict === 'cancel');
        }, gate.t.tickMs);

        // Watchdog: even if the tick interval breaks (clearInterval racing,
        // tab throttled, etc.) we guarantee the recording can't outlive
        // the max recording time + 1s.  Without this the "animation never
        // ends" bug is impossible to recover from short of a page reload.
        const watchdog = window.setTimeout(() => {
          if (stopped) return;
          console.warn('[useSpeech] watchdog forcing stop after max recording');
          triggerStop(!gate.speechStarted);
        }, gate.t.maxRecordingMs + 1000);

        const cleanup = () => {
          clearInterval(tick);
          clearTimeout(watchdog);
          try { source.disconnect(); } catch {}
          audioCtx.close().catch(() => {});
        };

        vadCleanupRef.current = cleanup;
      }
    } catch {
      stopMeter();
      setError('Microphone access denied');
      setState('idle');
    }
  }, [finalizeRecording, discardRecording, stopMeter]);

  const stopRecording = useCallback(async (): Promise<string> => {
    // If VAD was running, cancel it — we're stopping manually.
    vadCleanupRef.current?.();
    vadCleanupRef.current = null;
    return finalizeRecording();
  }, [finalizeRecording]);

  return {
    state,
    error,
    available,
    startRecording,
    stopRecording,
    isRecording: state === 'recording',
    isTranscribing: state === 'transcribing',
  };
}
