import { useEffect } from 'react';
import { isTauri } from '../lib/api';
import { useAppStore } from '../lib/store';
import { presenceOf } from './usePresence';
import {
  NOTCH_EVENT,
  NOTCH_LABEL,
  NOTCH_LEVEL_EVENT,
  replyPreview,
  snippet,
  toolLabel,
  type NotchLevel,
  type NotchState,
} from '../notch/state';

type AppState = ReturnType<typeof useAppStore.getState>;
type EmitTo = (target: string, event: string, payload: unknown) => Promise<void>;

// Level updates to the notch: the reactor smooths between them, so ~12/s is
// enough and keeps cross-window IPC cheap.
const LEVEL_EVERY_MS = 80;
// The waveform only draws the low analyser bins (see ArcReactor drawWaveform).
const LEVEL_BINS = 21;

function notchStateOf(s: AppState): NotchState {
  const presence = presenceOf(s);
  const last = [...s.messages].reverse().find((m) => m.role === 'assistant' && m.content);
  const reply = replyPreview(last?.content ?? '');
  const earcons = s.settings.earcons;
  if (presence === 'thinking') {
    const running = s.streamState.activeToolCalls.filter((t) => t.status === 'running');
    const tool = running[running.length - 1];
    return { presence, text: tool ? toolLabel(tool.tool) : '', reply, earcons };
  }
  if (presence === 'speaking') {
    // The sentence being spoken, whole; the notch scrolls it if it is long.
    const text = s.ttsCaption || snippet(s.streamState.content || last?.content || '');
    return { presence, text, reply, earcons };
  }
  return { presence, text: '', reply, earcons };
}

function levelOf(s: AppState): NotchLevel {
  const presence = presenceOf(s);
  if (presence === 'speaking') {
    const { averageLevel, frequencyData } = s.ttsAudioData;
    const bins = frequencyData ? Array.from(frequencyData.slice(0, LEVEL_BINS)) : [];
    return { level: averageLevel, bins };
  }
  if (presence === 'listening') return { level: s.micLevel, bins: [] };
  return { level: 0, bins: [] };
}

/// Mirrors JARVIS's presence into the notch window (see src-tauri notch mod):
/// state on every change, plus the voice level while listening or speaking so
/// the notch reactor moves with the audio.  Mount once, in the main window only.
export function useNotchBridge() {
  useEffect(() => {
    if (!isTauri()) return;
    let prev = '';
    let emitTo: EmitTo | null = null;
    let levelTimer: ReturnType<typeof setInterval> | null = null;

    const sendLevel = () => {
      emitTo?.(NOTCH_LABEL, NOTCH_LEVEL_EVENT, levelOf(useAppStore.getState())).catch(() => {});
    };

    const push = (state: AppState) => {
      if (!emitTo) return;
      const next = notchStateOf(state);
      const key = `${next.presence}|${next.text}|${next.reply}|${next.earcons}`;
      if (key === prev) return;
      prev = key;
      emitTo(NOTCH_LABEL, NOTCH_EVENT, next).catch(() => {});

      const voiced = next.presence === 'listening' || next.presence === 'speaking';
      if (voiced && !levelTimer) {
        levelTimer = setInterval(sendLevel, LEVEL_EVERY_MS);
      } else if (!voiced && levelTimer) {
        clearInterval(levelTimer);
        levelTimer = null;
        sendLevel(); // settle the reactor at silence
      }
    };

    let unsubscribe = () => {};
    import('@tauri-apps/api/event').then((mod) => {
      emitTo = mod.emitTo;
      push(useAppStore.getState());
      unsubscribe = useAppStore.subscribe(push);
    });
    return () => {
      unsubscribe();
      if (levelTimer) clearInterval(levelTimer);
    };
  }, []);
}
