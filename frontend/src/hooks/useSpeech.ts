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

// VAD tunables — generous defaults that prefer "keep listening" over
// "cut the user off."  Tighten silence if the assistant feels sluggish.
const VAD_SPEECH_RMS = 0.008;        // RMS threshold to count as "speaking" (~0.8% of full scale)
const VAD_SILENCE_HOLD_MS = 1800;    // silence after speech → stop
const VAD_NO_SPEECH_TIMEOUT_MS = 8000; // hard timeout if user never speaks at all
const VAD_MAX_RECORDING_MS = 30000;  // hard cap on total recording
const VAD_TICK_MS = 80;

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

        try {
          const result = await transcribeAudio(blob);
          setState('idle');
          resolve(result.text);
        } catch (err) {
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
        const source = audioCtx.createMediaStreamSource(stream);
        const analyser = audioCtx.createAnalyser();
        analyser.fftSize = 1024;
        source.connect(analyser);
        const buffer = new Uint8Array(analyser.fftSize);

        const recordingStart = Date.now();
        let speechStarted = false;
        let silenceStart = 0;
        let stopped = false;

        const triggerStop = async (cancelled: boolean) => {
          if (stopped) return;
          stopped = true;
          cleanup();
          try {
            const text = await finalizeRecording();
            if (cancelled || !text.trim()) {
              options.onAutoCancel?.();
            } else {
              options.onAutoResult?.(text);
            }
          } catch {
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
          const now = Date.now();
          const elapsed = now - recordingStart;

          // Hard cap on total recording duration
          if (elapsed > VAD_MAX_RECORDING_MS) {
            triggerStop(false);
            return;
          }

          if (rms > VAD_SPEECH_RMS) {
            speechStarted = true;
            silenceStart = 0;
            return;
          }

          if (!speechStarted) {
            // Still waiting for the user to start speaking
            if (elapsed > VAD_NO_SPEECH_TIMEOUT_MS) {
              // No speech captured — cancel quietly
              triggerStop(true);
            }
            return;
          }

          // We've heard speech; now timing the post-speech silence
          if (silenceStart === 0) {
            silenceStart = now;
          } else if (now - silenceStart > VAD_SILENCE_HOLD_MS) {
            triggerStop(false);
          }
        }, VAD_TICK_MS);

        const cleanup = () => {
          clearInterval(tick);
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
