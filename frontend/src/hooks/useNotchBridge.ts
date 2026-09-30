import { useEffect } from 'react';
import { isTauri } from '../lib/api';
import { useAppStore } from '../lib/store';
import { presenceOf } from './usePresence';
import {
  NOTCH_EVENT,
  NOTCH_LABEL,
  replyPreview,
  snippet,
  toolLabel,
  type NotchState,
} from '../notch/state';

type AppState = ReturnType<typeof useAppStore.getState>;

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
    return { presence, text: snippet(s.streamState.content || last?.content || ''), reply, earcons };
  }
  return { presence, text: '', reply, earcons };
}

/// Mirrors JARVIS's presence into the notch window (see src-tauri notch mod).
/// Mount once, in the main window only.
export function useNotchBridge() {
  useEffect(() => {
    if (!isTauri()) return;
    let prev = '';
    let emitTo: ((target: string, event: string, payload: unknown) => Promise<void>) | null =
      null;

    const push = (state: AppState) => {
      if (!emitTo) return;
      const next = notchStateOf(state);
      const key = `${next.presence}|${next.text}|${next.reply}|${next.earcons}`;
      if (key === prev) return;
      prev = key;
      emitTo(NOTCH_LABEL, NOTCH_EVENT, next).catch(() => {});
    };

    let unsubscribe = () => {};
    import('@tauri-apps/api/event').then((mod) => {
      emitTo = mod.emitTo;
      push(useAppStore.getState());
      unsubscribe = useAppStore.subscribe(push);
    });
    return () => unsubscribe();
  }, []);
}
