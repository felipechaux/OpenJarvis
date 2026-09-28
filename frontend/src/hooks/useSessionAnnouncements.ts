import { useEffect } from 'react';
import { getBase } from '../lib/api';
import { useAppStore } from '../lib/store';
import { useTTS } from './useTTS';
import { presenceOf } from './usePresence';

const POLL_MS = 4000;

interface SessionEvent {
  id: number;
  kind: string; // 'progress' | 'stop' | 'notification'
  project: string;
  session_id: string;
  text: string;
}

/// A progress update is stale once a newer event from the same session is
/// queued (a later progress, or the final "terminó") — skip it.
function isSuperseded(event: SessionEvent, queue: SessionEvent[]): boolean {
  return event.kind === 'progress'
    && queue.some((e) => e.id > event.id
      && (e.session_id === event.session_id || e.project === event.project));
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
        // client=app lets the backend report when the app last polled, its
        // presence and queue (GET /v1/coding-sessions/status) for debugging.
        const st = useAppStore.getState();
        const diag = new URLSearchParams({
          after: String(lastId ?? 0),
          client: 'app',
          presence: presenceOf(st),
          pending: String(pending.length),
          tts: st.settings.ttsEnabled ? '1' : '0',
        });
        const res = await fetch(`${getBase()}/v1/coding-sessions/events?${diag}`);
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
      let event = pending.shift()!;
      while (isSuperseded(event, pending)) event = pending.shift()!;
      state.addLogEntry({
        timestamp: Date.now(), level: 'info', category: 'tool',
        message: event.text,
      });
      if (!state.settings.ttsEnabled) return;
      announcing = true;
      try {
        await speak(event.text);
        // Tell the backend it was spoken (delivery can then be verified).
        fetch(`${getBase()}/v1/coding-sessions/events/${event.id}/announced`, {
          method: 'POST',
        }).catch(() => {});
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
