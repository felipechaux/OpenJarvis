import { useEffect } from 'react';
import { getBase } from '../lib/api';
import { useAppStore } from '../lib/store';
import { useTTS } from './useTTS';
import { presenceOf } from './usePresence';

const POLL_MS = 4000;

interface SessionEvent {
  id: number;
  kind: string;
  project: string;
  text: string;
}

/// Speaks events from the coding sessions JARVIS started ("Claude terminó
/// en openjarvis", "Claude pide permiso en openjarvis").  Claude Code hooks
/// POST them to the backend; this polls and announces each one once JARVIS
/// is idle, so it never talks over a reply or cuts off the user.
/// Mount once (App).
export function useSessionAnnouncements(enabled: boolean) {
  const { speak } = useTTS();

  useEffect(() => {
    if (!enabled) return;
    let lastId: number | null = null; // null → first poll only syncs
    const pending: SessionEvent[] = [];
    let cancelled = false;
    // This hook's TTS instance doesn't drive the global ttsSpeaking flag,
    // so guard its own playback to keep announcements from overlapping.
    let announcing = false;

    const tick = async () => {
      try {
        const res = await fetch(
          `${getBase()}/v1/coding-sessions/events?after=${lastId ?? 0}`,
        );
        if (res.ok) {
          const data: { events: SessionEvent[]; last_id: number } = await res.json();
          // Skip whatever happened before the app opened.
          if (lastId !== null) pending.push(...data.events);
          lastId = Math.max(lastId ?? 0, data.last_id);
        }
      } catch {
        // backend not ready yet
      }

      const state = useAppStore.getState();
      if (cancelled || announcing || !pending.length) return;
      if (presenceOf(state) !== 'idle') return;
      const event = pending.shift()!;
      state.addLogEntry({
        timestamp: Date.now(), level: 'info', category: 'tool',
        message: event.text,
      });
      if (!state.settings.ttsEnabled) return;
      announcing = true;
      try {
        await speak(event.text);
      } finally {
        announcing = false;
      }
    };

    const timer = setInterval(tick, POLL_MS);
    tick();
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [enabled, speak]);
}
