import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import {
  NOTCH_DOCK_EVENT,
  NOTCH_EVENT,
  NOTCH_LEVEL_EVENT,
  type NotchLevel,
  type NotchState,
} from './state';
import { ArcReactor } from '../components/Chat/ArcReactor';
import type { AudioAnalyzerData } from '../hooks/useTTS';
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
// Height of the top row, level with the hardware notch (matches notch.css).
const ROW_H = 38;
const REACTOR_ROW = 30;
const REACTOR_OPEN = 64;
// How long the pill celebrates catching the launch intro's reactor.
const DOCK_MS = 1700;

const PREVIEW = new URLSearchParams(window.location.search);

/// `/notch?preview=speaking&text=…&reply=…&open=1` renders a state in a plain
/// browser, with a synthetic voice level so the reactor moves.
function previewState(): NotchState {
  const presence = PREVIEW.get('preview') as NotchState['presence'] | null;
  return {
    presence: presence ?? 'idle',
    text: PREVIEW.get('text') ?? '',
    reply: PREVIEW.get('reply') ?? '',
    earcons: PREVIEW.get('earcons') !== '0',
  };
}

const fold = (s: string) =>
  s.toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '');

/// `[start, end)` of the spoken *sentence* inside *reply*, or null.  The TTS
/// caption is cleaned for speech, so match loosely: accent- and case-folded
/// (lengths stay aligned: NFD then stripping marks keeps one char per char
/// for Spanish), on its first words.
function locate(reply: string, sentence: string): [number, number] | null {
  const probe = fold(sentence.trim()).slice(0, 32);
  if (probe.length < 8) return null;
  const start = fold(reply).indexOf(probe);
  if (start < 0) return null;
  return [start, Math.min(reply.length, start + sentence.trim().length)];
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

/// Voice level from the main window (`notch:level`), read by the reactors'
/// draw loops each frame; outside Tauri a synthetic level stands in.
function useVoiceLevel(presence: NotchState['presence']) {
  const data = useRef<AudioAnalyzerData>({
    frequencyData: null,
    averageLevel: 0,
    bassLevel: 0,
    trebleLevel: 0,
  });
  const tauri = useRef(false);

  useEffect(() => {
    let unlisten = () => {};
    import('@tauri-apps/api/event')
      .then(({ listen }) =>
        listen<NotchLevel>(NOTCH_LEVEL_EVENT, ({ payload }) => {
          tauri.current = true;
          const d = data.current;
          d.averageLevel = payload.level;
          d.bassLevel = payload.level * 0.9;
          d.trebleLevel = payload.level * 0.6;
          d.frequencyData = payload.bins.length ? Uint8Array.from(payload.bins) : null;
        }),
      )
      .then((fn) => {
        unlisten = fn;
      })
      .catch(() => {});
    return () => unlisten();
  }, []);

  return useCallback((): AudioAnalyzerData => {
    const d = data.current;
    if (!tauri.current && PREVIEW.has('preview')) {
      const t = performance.now() / 1000;
      const voiced = presence === 'speaking' || presence === 'listening';
      d.averageLevel = voiced ? 0.35 + 0.3 * Math.sin(t * 5.3) * Math.sin(t * 1.7) : 0;
      d.bassLevel = d.averageLevel * 0.9;
      d.trebleLevel = d.averageLevel * 0.6;
    }
    return d;
  }, [presence]);
}

/// Dynamic-Island-style pill that grows out of the MacBook notch, with the
/// same arc reactor as companion mode.  Runs in its own window (src-tauri
/// `notch` mod) that only takes clicks over the pill, and renders what the
/// main window sends; it has no voice, chat or API state of its own.
/// Clicking opens it to show JARVIS's last reply, scrollable.
export function NotchPage() {
  const [state, setState] = useState<NotchState>(previewState);
  const [open, setOpen] = useState(() => PREVIEW.has('open'));
  const [docking, setDocking] = useState(() => PREVIEW.has('dock'));
  const pill = useRef<HTMLDivElement>(null);
  const body = useRef<HTMLDivElement>(null);
  const line = useRef<HTMLDivElement>(null);
  const reply = useRef<HTMLDivElement>(null);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [bodyH, setBodyH] = useState(0);
  const { presence, text } = state;
  const getAudioData = useVoiceLevel(presence);

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

  // The launch intro's reactor flies into the notch: catch it.
  useEffect(() => {
    let unlisten = () => {};
    let timer: ReturnType<typeof setTimeout> | undefined;
    import('@tauri-apps/api/event')
      .then(({ listen }) =>
        listen(NOTCH_DOCK_EVENT, () => {
          setDocking(true);
          clearTimeout(timer);
          timer = setTimeout(() => setDocking(false), DOCK_MS);
        }),
      )
      .then((fn) => {
        unlisten = fn;
      })
      .catch(() => {});
    return () => {
      unlisten();
      clearTimeout(timer);
    };
  }, []);

  // The pill's height follows its content (explicit px, so the spring can
  // animate it; `height: auto` cannot transition in WebKit).
  useLayoutEffect(() => {
    const el = body.current;
    if (!el) return;
    const measure = () => setBodyH(el.scrollHeight);
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    measure();
    return () => observer.disconnect();
  }, [open]);

  // Keep the native hit area in step with the pill as it animates.
  useEffect(() => {
    const el = pill.current;
    if (!el) return;
    const observer = new ResizeObserver(() => reportHitArea(el));
    observer.observe(el);
    reportHitArea(el);
    return () => observer.disconnect();
  }, []);

  // A long status line glides to its newest words.
  useEffect(() => {
    const el = line.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
  }, [text]);

  // A new reply opens at its start; while speaking, the spoken sentence
  // (highlighted) is kept in view instead.
  const spoken = presence === 'speaking' ? locate(state.reply, text) : null;
  useEffect(() => {
    reply.current?.scrollTo({ top: 0 });
  }, [state.reply, open]);
  useEffect(() => {
    // Scroll only the reply box (scrollIntoView would also shift the pill).
    const box = reply.current;
    const mark = box?.querySelector('mark');
    if (box && mark) {
      const top = mark.offsetTop - (box.clientHeight - mark.offsetHeight) / 2;
      box.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
    }
  }, [spoken?.[0], open]);

  const cancelClose = () => {
    if (closeTimer.current) clearTimeout(closeTimer.current);
    closeTimer.current = null;
  };
  const scheduleClose = () => {
    cancelClose();
    if (open) closeTimer.current = setTimeout(() => setOpen(false), AUTO_CLOSE_MS);
  };
  const toggle = () => {
    cancelClose();
    // Click-only chime (reveal / conceal); hover and auto-close stay silent.
    if (state.earcons) playEarcon(open ? 'conceal' : 'reveal');
    setOpen(!open);
  };

  const status = text || LABELS[presence];
  // Open header: a short state, never the spoken sentence (that is
  // highlighted in the reply below).
  const headStatus = presence === 'thinking' && text ? text : STATUS[presence];
  const replyText = state.reply || 'Todavía no hay respuestas en esta sesión.';
  const classes = [
    'notch',
    `notch--${presence}`,
    status ? 'notch--text' : '',
    open ? 'notch--open' : '',
    docking ? 'notch--docking' : '',
  ].join(' ');
  const height = presence === 'idle' && !open ? ROW_H : ROW_H + bodyH;

  return (
    <div
      ref={pill}
      className={classes}
      style={{ height }}
      aria-live="polite"
      onClick={toggle}
      onMouseEnter={cancelClose}
      onMouseLeave={scheduleClose}
    >
      <div className="notch__row">
        <span className="notch__reactor">
          <ArcReactor size={REACTOR_ROW} presence={presence} getAudioData={getAudioData} />
          <i className="notch__shock" aria-hidden />
        </span>
        <span className="notch__bars" aria-hidden>
          {[0, 1, 2, 3, 4].map((i) => (
            <i key={i} style={{ animationDelay: `${i * 0.11}s` }} />
          ))}
        </span>
      </div>
      <div ref={body} className="notch__body">
        {open ? (
          <div className="notch__panel">
            <div className="notch__head">
              <ArcReactor size={REACTOR_OPEN} presence={presence} getAudioData={getAudioData} />
              <div className="notch__titles">
                <div className="notch__brand">J.A.R.V.I.S</div>
                <div className="notch__status">{headStatus}</div>
              </div>
            </div>
            <div
              ref={reply}
              className="notch__reply"
              // Scrolling or selecting inside the reply must not close the pill.
              onClick={(e) => e.stopPropagation()}
            >
              {spoken ? (
                <>
                  {replyText.slice(0, spoken[0])}
                  <mark>{replyText.slice(spoken[0], spoken[1])}</mark>
                  {replyText.slice(spoken[1])}
                </>
              ) : (
                replyText
              )}
            </div>
          </div>
        ) : (
          <div ref={line} className="notch__line">
            {status}
          </div>
        )}
      </div>
    </div>
  );
}
