import { useState, useCallback, useRef, useEffect } from 'react';
import { transcribeAudio, fetchSpeechHealth } from '../lib/api';

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

// VAD tunables — hysteresis-based.  A MacBook's built-in mic has a noise
// floor around 0.005-0.012 RMS even in a "quiet" room (fan, breathing,
// ventilation, distant traffic).  A single threshold always loses:
// adaptive-calibration approaches drift either too low (background keeps
// resetting silence) or too high (user gets cut off).
//
// Two-threshold hysteresis instead:
//   - RMS > VAD_SPEECH_RMS         → user is clearly speaking
//   - RMS < VAD_SILENCE_RMS        → room is clearly silent
//   - middle band (uncertain)      → hold current state, change nothing
//
// This way ambient noise that sits at 0.014 (between SILENCE and SPEECH)
// neither falsely advances the silence timer nor falsely resets it once
// the user stops talking.
const VAD_SPEECH_RMS = 0.030;         // clearly-speaking floor (normal voice ≈ 0.05-0.15)
const VAD_SILENCE_RMS = 0.012;        // clearly-silent ceiling (above this is "uncertain")
const VAD_SILENCE_HOLD_MS = 1200;     // silence after speech → stop
const VAD_NO_SPEECH_TIMEOUT_MS = 6000; // hard timeout if user never speaks at all
const VAD_MAX_RECORDING_MS = 30000;   // hard cap on total recording
const VAD_TICK_MS = 80;
// Log every Nth tick so the console doesn't flood but we can still see VAD
// progress (RMS, speechStarted, elapsed) when debugging "the mic opened
// but never closed" bugs in WKWebView.
const VAD_LOG_EVERY_N_TICKS = 12;

export function useSpeech() {
  const [state, setState] = useState<SpeechState>('idle');
  const [error, setError] = useState<string | null>(null);
  const [available, setAvailable] = useState(false);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const vadCleanupRef = useRef<(() => void) | null>(null);

  // Check if speech backend is available on mount
  useEffect(() => {
    fetchSpeechHealth()
      .then((health) => setAvailable(health.available))
      .catch(() => setAvailable(false));
  }, []);

  const finalizeRecording = useCallback(async (): Promise<string> => {
    const recorder = mediaRecorderRef.current;
    if (!recorder) throw new Error('Not recording');

    return new Promise((resolve, reject) => {
      recorder.onstop = async () => {
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
          const result = await transcribeAudio(blob);
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
  }, []);

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
        let speechStarted = false;
        let silenceStart = 0;
        let stopped = false;
        let tickCount = 0;
        let maxRmsSeen = 0;

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
            speechStarted,
            maxRmsSeen,
          });
          cleanup();
          try {
            const text = await finalizeRecording();
            if (cancelled || !text.trim()) {
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
          if (rms > maxRmsSeen) maxRmsSeen = rms;
          const now = Date.now();
          const elapsed = now - recordingStart;

          tickCount += 1;
          if (tickCount % VAD_LOG_EVERY_N_TICKS === 0) {
            console.log('[useSpeech] VAD tick', {
              elapsedMs: elapsed,
              rms: Number(rms.toFixed(4)),
              maxRmsSeen: Number(maxRmsSeen.toFixed(4)),
              speechStarted,
              silenceMs: silenceStart === 0 ? 0 : now - silenceStart,
              ctxState: audioCtx.state,
            });
          }

          // Hard cap on total recording duration
          if (elapsed > VAD_MAX_RECORDING_MS) {
            triggerStop(false);
            return;
          }

          // Two-threshold hysteresis:
          //   rms > VAD_SPEECH_RMS  → clearly speaking, reset silence timer
          //   rms < VAD_SILENCE_RMS → clearly silent, advance silence timer
          //   in between            → uncertain, hold current state
          if (rms > VAD_SPEECH_RMS) {
            speechStarted = true;
            silenceStart = 0;
            return;
          }

          if (rms >= VAD_SILENCE_RMS) {
            // Middle "uncertain" band — neither clearly speaking nor
            // clearly silent.  Don't reset silenceStart, don't start
            // counting either.  Just wait for the audio to commit one
            // way or the other.  This is the whole point of hysteresis.
            return;
          }

          // rms < VAD_SILENCE_RMS — room is clearly silent.
          if (!speechStarted) {
            if (elapsed > VAD_NO_SPEECH_TIMEOUT_MS) {
              // No speech captured at all — cancel quietly
              triggerStop(true);
            }
            return;
          }

          if (silenceStart === 0) {
            silenceStart = now;
          } else if (now - silenceStart > VAD_SILENCE_HOLD_MS) {
            triggerStop(false);
          }
        }, VAD_TICK_MS);

        // Watchdog: even if the tick interval breaks (clearInterval racing,
        // tab throttled, etc.) we guarantee the recording can't outlive
        // VAD_MAX_RECORDING_MS + 1s.  Without this the "animation never
        // ends" bug is impossible to recover from short of a page reload.
        const watchdog = window.setTimeout(() => {
          if (stopped) return;
          console.warn('[useSpeech] watchdog forcing stop after max recording');
          triggerStop(false);
        }, VAD_MAX_RECORDING_MS + 1000);

        const cleanup = () => {
          clearInterval(tick);
          clearTimeout(watchdog);
          try { source.disconnect(); } catch {}
          audioCtx.close().catch(() => {});
        };

        vadCleanupRef.current = cleanup;
      }
    } catch {
      setError('Microphone access denied');
      setState('idle');
    }
  }, [finalizeRecording]);

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
