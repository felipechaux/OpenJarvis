import { useEffect, useRef, useState } from 'react';
import { NOTCH_EVENT, type NotchState } from './state';
import { playEarcon } from '../lib/earcons';
import './notch.css';

const LABELS: Record<NotchState['presence'], string> = {
  idle: '',
  listening: 'Escuchando',
  thinking: 'Pensando',
  speaking: 'Hablando',
};
const STATUS: Record<NotchState['presence'], string> = {
  idle: 'En espera',
  listening: 'Escuchando',
  thinking: 'Pensando',
  speaking: 'Hablando',
};
// An opened pill closes itself this long after the cursor leaves it.
const AUTO_CLOSE_MS = 6000;

/// `/notch?preview=thinking&text=…&open=1` renders a state in a plain browser.
function previewState(): NotchState {
  const q = new URLSearchParams(window.location.search);
  const presence = q.get('preview') as NotchState['presence'] | null;
  return {
    presence: presence ?? 'idle',
    text: q.get('text') ?? '',
    reply: q.get('reply') ?? '',
    earcons: q.get('earcons') !== '0',
  };
}

async function reportHitArea(el: HTMLElement) {
  const { width, height } = el.getBoundingClientRect();
  try {
    const { invoke } = await import('@tauri-apps/api/core');
    await invoke('notch_hit_area', { width, height });
  } catch {
    /* plain browser (preview) */
  }
}

/// Dynamic-Island-style pill that grows out of the MacBook notch.  Runs in
/// its own window (src-tauri `notch` mod) that only takes clicks over the
/// pill, and renders what the main window sends; it has no voice, chat or API
/// state of its own.  Clicking opens it to show JARVIS's last reply.
export function NotchPage() {
  const [state, setState] = useState<NotchState>(previewState);
  const [open, setOpen] = useState(() => new URLSearchParams(window.location.search).has('open'));
  const pill = useRef<HTMLDivElement>(null);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    document.documentElement.classList.add('notch-window');
    let unlisten = () => {};
    import('@tauri-apps/api/event')
      .then(({ listen }) => listen<NotchState>(NOTCH_EVENT, (e) => setState(e.payload)))
      .then((fn) => {
        unlisten = fn;
      })
      .catch(() => {}); // plain browser (preview): no Tauri events
    return () => unlisten();
  }, []);

  // Keep the native hit area in step with the pill as it animates.
  useEffect(() => {
    const el = pill.current;
    if (!el) return;
    const observer = new ResizeObserver(() => reportHitArea(el));
    observer.observe(el);
    reportHitArea(el);
    return () => observer.disconnect();
  }, []);

  const cancelClose = () => {
    if (closeTimer.current) clearTimeout(closeTimer.current);
    closeTimer.current = null;
  };
  const scheduleClose = () => {
    cancelClose();
    if (open) closeTimer.current = setTimeout(() => setOpen(false), AUTO_CLOSE_MS);
  };

  const { presence, text, reply } = state;
  const line = text || LABELS[presence];
  const classes = [
    'notch',
    `notch--${presence}`,
    line ? 'notch--text' : '',
    open ? 'notch--open' : '',
  ].join(' ');

  return (
    <div
      ref={pill}
      className={classes}
      aria-live="polite"
      onClick={() => {
        cancelClose();
        // Click-only chime (reveal / conceal); hover and auto-close stay silent.
        if (state.earcons) playEarcon(open ? 'conceal' : 'reveal');
        setOpen(!open);
      }}
      onMouseEnter={cancelClose}
      onMouseLeave={scheduleClose}
    >
      <div className="notch__row">
        <span className="notch__core" />
        <span className="notch__bars" aria-hidden>
          {[0, 1, 2, 3, 4].map((i) => (
            <i key={i} style={{ animationDelay: `${i * 0.11}s` }} />
          ))}
        </span>
      </div>
      {open ? (
        <div className="notch__panel">
          <div className="notch__status">
            J.A.R.V.I.S · {line || STATUS[presence]}
          </div>
          <div className="notch__reply">{reply || 'Todavía no hay respuestas en esta sesión.'}</div>
        </div>
      ) : (
        <div className="notch__line">{line}</div>
      )}
    </div>
  );
}
