import { useEffect, useRef, useState } from 'react';
import { getBase } from '../lib/api';

/// One option of a menu a coding session waits on (server/session_watcher.py
/// `parse_prompt`): `key` is what gets pressed, `title` the CLI's own text.
export interface PromptOption {
  key: string;
  label: string;
  title: string;
}

export interface SessionPrompt {
  id: string;
  question: string;
  detail: string;
  options: PromptOption[];
}

/// A live `jarvis-*` tmux session (GET /v1/coding-sessions/live).
export interface LiveSession {
  name: string;
  project: string;
  cli: 'claude' | 'antigravity';
  status: 'prompt' | 'working' | 'idle';
  elapsed_s: number | null;
  edited: string[];
  prompt: SessionPrompt | null;
}

const POLL_MS = 2000;
// No sessions: poll slowly, nothing can need an answer.
const IDLE_POLL_MS = 6000;

export const cliLabel = (cli: LiveSession['cli']) => (cli === 'antigravity' ? 'Antigravity' : 'Claude');

export function shortDuration(seconds: number | null): string {
  if (seconds == null) return '';
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

const PREVIEW = new URLSearchParams(window.location.search);

/// `/notch?sessions=demo` shows sample sessions (one asking permission).
function demoSessions(): LiveSession[] {
  return [
    {
      name: 'jarvis-openjarvis',
      project: 'openjarvis',
      cli: 'claude',
      status: 'prompt',
      elapsed_s: null,
      edited: ['lib.rs', 'NotchPage.tsx'],
      prompt: {
        id: 'demo',
        question: 'Do you want to proceed?',
        detail: 'Bash command\nnpm run build\nBuild the frontend',
        options: [
          { key: '1', label: 'Sí', title: 'Yes' },
          { key: '2', label: 'Sí, siempre', title: "Yes, and don't ask again" },
          { key: 'Escape', label: 'No', title: 'No (esc)' },
        ],
      },
    },
    {
      name: 'jarvis-dadomatch',
      project: 'dadomatch',
      cli: 'claude',
      status: 'working',
      elapsed_s: 312,
      edited: ['App.kt'],
      prompt: null,
    },
  ];
}

/// Live coding sessions, polled from the JARVIS server while the notch runs.
export function useLiveSessions(): LiveSession[] {
  const [sessions, setSessions] = useState<LiveSession[]>(() =>
    PREVIEW.get('sessions') === 'demo' ? demoSessions() : [],
  );
  const count = useRef(0);

  useEffect(() => {
    if (PREVIEW.get('sessions') === 'demo') return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;
    const tick = async () => {
      try {
        const res = await fetch(`${getBase()}/v1/coding-sessions/live`);
        if (res.ok) {
          const data = (await res.json()) as { sessions: LiveSession[] };
          if (!stopped) {
            count.current = data.sessions.length;
            setSessions(data.sessions);
          }
        }
      } catch {
        /* server not up yet */
      }
      if (!stopped) timer = setTimeout(tick, count.current ? POLL_MS : IDLE_POLL_MS);
    };
    tick();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, []);

  return sessions;
}

/// Answers a session's menu; false when it is no longer showing.
export async function answerPrompt(session: string, promptId: string, key: string): Promise<boolean> {
  if (promptId === 'demo') return true;
  try {
    const res = await fetch(`${getBase()}/v1/coding-sessions/${encodeURIComponent(session)}/answer`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt_id: promptId, key }),
    });
    const data = (await res.json()) as { ok: boolean };
    return data.ok;
  } catch {
    return false;
  }
}
