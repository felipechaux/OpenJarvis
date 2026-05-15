import { useEffect, useRef, useCallback } from 'react';
import { getBase, isTauri } from '../lib/api';

const CHUNK_MS = 2500;
const WAKE_EVENT = 'jarvis-wake';
const NATIVE_READY_TIMEOUT_MS = 5000;

/// Subscribe to the native macOS wake-word sidecar.  Resolves to ``null``
/// when:
///   - Tauri isn't reachable (browser preview / non-Tauri build)
///   - The sidecar emits an error before becoming ready
///   - The sidecar doesn't emit "ready" within NATIVE_READY_TIMEOUT_MS
/// In all those cases the caller falls back to the MediaRecorder +
/// Whisper polling loop below so the user never ends up with a silent,
/// non-functional wake word.
async function subscribeToNativeWake(
  onDetected: (text: string) => void,
): Promise<(() => void) | null> {
  if (!isTauri()) return null;
  try {
    const { listen } = await import('@tauri-apps/api/event');

    // The Swift sidecar emits "ready" once the audio engine + recogniser
    // are armed.  Until then (or if it fails), we can't trust it as the
    // sole wake source.  Race: wait for the first ready/error/wake event
    // up to NATIVE_READY_TIMEOUT_MS, otherwise treat it as unavailable.
    const wakeUnlisten = await listen<string>(WAKE_EVENT, (event) => {
      const text = event.payload || '';
      console.log('[WakeWord] Native detect:', text);
      onDetected(text);
    });

    let resolveReady: (ok: boolean) => void = () => {};
    const readyProm = new Promise<boolean>((r) => { resolveReady = r; });

    const readyUnlisten = await listen('jarvis-wake-ready', () => {
      console.log('[WakeWord] Native sidecar ready');
      resolveReady(true);
    });
    const errorUnlisten = await listen<string>('jarvis-wake-error', (ev) => {
      console.warn('[WakeWord] Native sidecar error:', ev.payload);
      resolveReady(false);
    });
    const timeout = setTimeout(() => resolveReady(false), NATIVE_READY_TIMEOUT_MS);

    const ready = await readyProm;
    clearTimeout(timeout);
    readyUnlisten();
    errorUnlisten();

    if (!ready) {
      wakeUnlisten();
      return null;
    }
    return wakeUnlisten;
  } catch (err) {
    console.warn('[WakeWord] Tauri event subscription failed:', err);
    return null;
  }
}

async function focusWindow() {
  if (!isTauri()) return;
  try {
    const { getCurrentWindow } = await import('@tauri-apps/api/window');
    const win = getCurrentWindow();
    await win.show();
    await win.unminimize();
    await win.setFocus();
  } catch {}
}

export function useWakeWord(enabled: boolean) {
  const streamRef = useRef<MediaStream | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const processingRef = useRef(false);
  const activeRef = useRef(false);
  const mimeRef = useRef('audio/webm');
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const processChunk = useCallback(async (blob: Blob) => {
    if (processingRef.current || blob.size < 1500) return;
    processingRef.current = true;
    try {
      const form = new FormData();
      form.append('file', blob, 'wake.webm');
      const res = await fetch(`${getBase()}/v1/speech/wake-word/detect`, {
        method: 'POST',
        body: form,
      });
      if (!res.ok) return;
      const data = await res.json();
      if (data.detected) {
        console.log('[WakeWord] Detected:', data.text);
        await focusWindow();
        window.dispatchEvent(new CustomEvent(WAKE_EVENT, { detail: data.text }));
      }
    } catch {
      // Silent — backend may not be ready yet
    } finally {
      processingRef.current = false;
    }
  }, []);

  // Stop the current recorder, collect its final chunk, then start a fresh one.
  // Each stop→start cycle produces a self-contained WebM file with proper headers
  // that ffmpeg/Whisper can decode independently.
  const cycleRecorder = useCallback(() => {
    if (!activeRef.current || !streamRef.current) return;

    const mime = mimeRef.current;
    const recorder = new MediaRecorder(streamRef.current, { mimeType: mime });
    recorderRef.current = recorder;

    const chunks: Blob[] = [];
    recorder.ondataavailable = (e) => {
      if (e.data.size > 0) chunks.push(e.data);
    };

    recorder.onstop = () => {
      if (chunks.length > 0 && activeRef.current) {
        processChunk(new Blob(chunks, { type: mime }));
      }
      // Start the next cycle immediately after this one stops
      if (activeRef.current) {
        timerRef.current = setTimeout(cycleRecorder, 0);
      }
    };

    recorder.start();
    // Stop after CHUNK_MS to trigger onstop → collect chunk → restart
    timerRef.current = setTimeout(() => {
      if (recorder.state === 'recording') recorder.stop();
    }, CHUNK_MS);
  }, [processChunk]);

  const start = useCallback(async () => {
    if (streamRef.current) return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      mimeRef.current = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
        ? 'audio/webm;codecs=opus'
        : 'audio/webm';
      activeRef.current = true;
      cycleRecorder();
      console.log('[WakeWord] Whisper fallback active');
    } catch (err) {
      console.warn('[WakeWord] Mic permission denied; wake word unavailable', err);
    }
  }, [cycleRecorder]);

  const stop = useCallback(() => {
    activeRef.current = false;
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null; }
    if (recorderRef.current?.state === 'recording') {
      try { recorderRef.current.stop(); } catch {}
    }
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    recorderRef.current = null;
  }, []);

  useEffect(() => {
    if (!enabled) return;

    let nativeUnlisten: (() => void) | null = null;
    let cancelled = false;

    const onWake = async (text: string) => {
      await focusWindow();
      window.dispatchEvent(new CustomEvent(WAKE_EVENT, { detail: text }));
    };

    // Prefer the native macOS Swift sidecar.  If Tauri isn't available,
    // the sidecar errored out, or it didn't become ready within the
    // timeout, fall back to the cross-platform MediaRecorder + Whisper
    // polling loop so the wake word still works.
    (async () => {
      nativeUnlisten = await subscribeToNativeWake(onWake);
      if (cancelled) {
        nativeUnlisten?.();
        return;
      }
      if (!nativeUnlisten) {
        console.log('[WakeWord] Falling back to Whisper polling');
        start();
      }
    })();

    return () => {
      cancelled = true;
      nativeUnlisten?.();
      stop();
    };
  }, [enabled, start, stop]);
}

export { WAKE_EVENT };
