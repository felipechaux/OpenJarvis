import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import {
  Check,
  Copy,
  FileDown,
  Maximize2,
  Minimize2,
  Mic,
  Pause,
  Play,
  SendHorizontal,
  ShieldAlert,
  SkipBack,
  SkipForward,
  Square,
  SquareTerminal,
} from 'lucide-react';
import { useNowPlaying, type MediaAction, type NowPlaying } from './media';
import {
  answerPrompt,
  cliLabel,
  messageSession,
  openSession,
  shortDuration,
  useLiveSessions,
  type LiveSession,
  type PromptOption,
} from './sessions';
import {
  MAIN_LABEL,
  NOTCH_COMMAND_EVENT,
  NOTCH_DOCK_EVENT,
  NOTCH_EVENT,
  NOTCH_GAZE_EVENT,
  NOTCH_HOVER_EVENT,
  NOTCH_SHORTCUT,
  NOTCH_LEVEL_EVENT,
  fileMessage,
  type NotchCommand,
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
// How long a dropped file's name shows while JARVIS takes it.
const SWALLOW_MS = 2200;
// A click on the reactor waits this long before opening/closing the pill, so
// a double or triple click (which pokes it) does not move it mid-click.
const MULTI_CLICK_MS = 380;

const PREVIEW = new URLSearchParams(window.location.search);

/// `/notch?preview=speaking&text=…&reply=…&open=1` renders a state in a plain
/// browser, with a synthetic voice level so the reactor moves (`hover=1` and
/// `drop=1` show the peek and the drop zone).
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

/// Sends a command to the main window, which owns voice and chat.
async function command(cmd: NotchCommand) {
  try {
    const { emitTo } = await import('@tauri-apps/api/event');
    await emitTo(MAIN_LABEL, NOTCH_COMMAND_EVENT, cmd);
  } catch {
    console.log('[notch] command (preview):', cmd);
  }
}

/// Opens or hides JARVIS's main window (history, settings…): the
/// "advanced" view.
async function setMainWindow(visible: boolean) {
  try {
    const { invoke } = await import('@tauri-apps/api/core');
    await invoke('main_window', { visible });
  } catch {
    /* plain browser (preview) */
  }
}

/// Whether the main window is open (src-tauri sends `notch:main-window`).
function useMainWindowOpen(): boolean {
  const [isOpen, setIsOpen] = useState(false);
  useEffect(() => {
    let unlisten = () => {};
    import('@tauri-apps/api/event')
      .then(({ listen }) => listen<boolean>('notch:main-window', (e) => setIsOpen(e.payload)))
      .then((fn) => {
        unlisten = fn;
      })
      .catch(() => {});
    return () => unlisten();
  }, []);
  return isOpen;
}

/// Lets the text box take the keyboard, or hands it back to the user's app.
async function keyboard(focus: boolean) {
  try {
    const { invoke } = await import('@tauri-apps/api/core');
    await invoke('notch_keyboard', { focus });
  } catch {
    /* plain browser (preview) */
  }
}

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // WebKit refuses the async clipboard in an unfocused window.
    const area = document.createElement('textarea');
    area.value = text;
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand('copy');
    area.remove();
    return ok;
  }
}

/// Files dragged over the pill (Tauri's webview drag-drop: the window takes
/// the drag only while the cursor is over the pill, see src-tauri `notch`).
function useFileDrop(onDrop: (paths: string[]) => void) {
  const [over, setOver] = useState(() => PREVIEW.has('drop'));
  const latest = useRef(onDrop);
  latest.current = onDrop;

  useEffect(() => {
    let unlisten = () => {};
    let disposed = false;
    import('@tauri-apps/api/webview')
      .then(({ getCurrentWebview }) =>
        getCurrentWebview().onDragDropEvent(({ payload }) => {
          if (payload.type === 'enter') setOver(payload.paths.length > 0);
          else if (payload.type === 'leave') setOver(false);
          else if (payload.type === 'drop') {
            setOver(false);
            if (payload.paths.length) latest.current(payload.paths);
          }
        }),
      )
      .then((fn) => {
        if (disposed) fn();
        else unlisten = fn;
      })
      .catch(() => {});
    return () => {
      disposed = true;
      unlisten();
    };
  }, []);
  return over;
}

const baseName = (path: string) => path.split('/').filter(Boolean).pop() ?? path;

/// Claude Code's permission questions, in Spanish; anything else as is.
function askText(question: string): string {
  if (/^do you want to proceed\?$/i.test(question)) return '¿Continúo?';
  const edit = question.match(/^do you want to make this edit to (.+)\?$/i);
  if (edit) return `¿Edito ${edit[1]}?`;
  const create = question.match(/^do you want to create (.+)\?$/i);
  if (create) return `¿Creo ${create[1]}?`;
  return question;
}

function sessionStatus(s: LiveSession): string {
  const edited = s.edited.length ? ` · ${s.edited.length} archivo${s.edited.length === 1 ? '' : 's'}` : '';
  if (s.status === 'prompt') return `espera respuesta${edited}`;
  if (s.status === 'working') return `${shortDuration(s.elapsed_s) || 'trabajando'}${edited}`;
  return `lista${edited}`;
}

/// A coding session's menu (permission or question) with one button per option.
function PromptCard({
  session,
  onAnswer,
}: {
  session: LiveSession;
  onAnswer: (session: LiveSession, option: PromptOption) => void;
}) {
  const prompt = session.prompt!;
  return (
    <div className="notch__prompt" onClick={(e) => e.stopPropagation()}>
      <div className="notch__prompt-head">
        <ShieldAlert size={14} />
        <span className="notch__prompt-who">
          {cliLabel(session.cli)} · {session.project}
        </span>
        <span className="notch__prompt-q">{askText(prompt.question)}</span>
      </div>
      {prompt.detail && <pre className="notch__prompt-detail">{prompt.detail}</pre>}
      <div className="notch__prompt-actions">
        {prompt.options.map((o) => (
          <button
            key={o.key}
            type="button"
            title={o.title}
            className={[
              'notch__choice',
              o.key === '1' ? 'notch__choice--yes' : '',
              o.key === 'Escape' ? 'notch__choice--no' : '',
            ].join(' ')}
            onClick={() => onAnswer(session, o)}
          >
            {o.label}
          </button>
        ))}
      </div>
    </div>
  );
}

/// One chip per live session.  Clicking a chip points the text box at that
/// session (again: back to JARVIS); its terminal button opens the session.
function SessionChips({
  sessions,
  target,
  onTarget,
}: {
  sessions: LiveSession[];
  target: string;
  onTarget: (name: string) => void;
}) {
  if (!sessions.length) return null;
  return (
    <div className="notch__sessions" onClick={(e) => e.stopPropagation()}>
      {sessions.map((s) => (
        <span
          key={s.name}
          role="button"
          tabIndex={-1}
          className={[
            'notch__chip',
            `notch__chip--${s.status}`,
            s.name === target ? 'notch__chip--target' : '',
          ].join(' ')}
          title={
            (s.name === target ? 'Volver a hablar con JARVIS' : `Escribirle a ${s.project}`) +
            (s.edited.length ? ` · Editados: ${s.edited.join(', ')}` : '')
          }
          onClick={() => onTarget(s.name === target ? '' : s.name)}
        >
          <i aria-hidden />
          <b>{s.project}</b>
          {sessionStatus(s)}
          <button
            type="button"
            className="notch__chip-open"
            title="Abrir la terminal"
            onClick={(e) => {
              e.stopPropagation();
              openSession(s.name);
            }}
          >
            <SquareTerminal size={12} />
          </button>
        </span>
      ))}
    </div>
  );
}

/// Spotify's current track with previous / play-pause / next.
function NowPlayingRow({ now, onControl }: { now: NowPlaying; onControl: (a: MediaAction) => void }) {
  if (now.state === 'none') return null;
  const playing = now.state === 'playing';
  return (
    <div className="notch__media" onClick={(e) => e.stopPropagation()}>
      {now.art ? (
        <img className="notch__media-art" src={now.art} alt="" />
      ) : (
        <span className="notch__media-art notch__media-art--blank" aria-hidden />
      )}
      <div className="notch__media-text">
        <b>{now.title || 'Spotify'}</b>
        <span>{now.artist}</span>
      </div>
      <span className={`notch__eq${playing ? ' notch__eq--on' : ''}`} aria-hidden>
        <i />
        <i />
        <i />
      </span>
      <button type="button" className="notch__btn" title="Anterior" onClick={() => onControl('previous')}>
        <SkipBack size={14} />
      </button>
      <button
        type="button"
        className="notch__btn"
        title={playing ? 'Pausar' : 'Reproducir'}
        onClick={() => onControl(playing ? 'pause' : 'play')}
      >
        {playing ? <Pause size={14} /> : <Play size={14} />}
      </button>
      <button type="button" className="notch__btn" title="Siguiente" onClick={() => onControl('next')}>
        <SkipForward size={14} />
      </button>
    </div>
  );
}

/// Dynamic-Island-style pill that grows out of the MacBook notch, with the
/// same arc reactor as the chat.  Runs in its own window (src-tauri
/// `notch` mod) that only takes clicks over the pill.  It renders what the
/// main window sends and sends commands back (`notch:command`): hovering
/// peeks, clicking opens JARVIS's last reply with controls (talk, stop, copy)
/// and a text box, and a file dropped on it goes to JARVIS to look at.
export function NotchPage() {
  const [state, setState] = useState<NotchState>(previewState);
  const [open, setOpen] = useState(() => PREVIEW.has('open'));
  const [docking, setDocking] = useState(() => PREVIEW.has('dock'));
  const [hover, setHover] = useState(() => PREVIEW.has('hover'));
  const [draft, setDraft] = useState('');
  const [typing, setTyping] = useState(false);
  const [copied, setCopied] = useState(false);
  // Name of the file just dropped, shown while the pill "swallows" it.
  const [swallowed, setSwallowed] = useState('');
  const pill = useRef<HTMLDivElement>(null);
  const body = useRef<HTMLDivElement>(null);
  const line = useRef<HTMLDivElement>(null);
  const reply = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [bodyH, setBodyH] = useState(0);
  const { presence, text } = state;
  const getAudioData = useVoiceLevel(presence);
  const mainOpen = useMainWindowOpen();
  const sessions = useLiveSessions();
  // Prompts already answered here, hidden until the next poll drops them.
  const [answered, setAnswered] = useState<string[]>([]);
  const asking = sessions.find((s) => s.prompt && !answered.includes(s.prompt.id));
  const sessionsWorking = sessions.filter((s) => s.status === 'working').length;
  const [now, mediaControl] = useNowPlaying();
  const music = now.state === 'playing';
  // The session the text box writes to ('' = JARVIS); dropped when it ends.
  const [target, setTarget] = useState('');
  const targetSession = sessions.find((s) => s.name === target);
  const [sendError, setSendError] = useState('');
  // The reactor's reaction to being poked: one click squishes, three spin it.
  const [mood, setMood] = useState<'' | 'squish' | 'dizzy'>('');
  const moodTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const clickTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const dragging = useFileDrop((paths) => {
    const [path] = paths;
    if (state.earcons) playEarcon('shutter');
    setSwallowed(baseName(path));
    setTimeout(() => setSwallowed(''), SWALLOW_MS);
    command({ type: 'send', text: fileMessage(path) });
  });

  // A session asking something chimes once; one finishing makes the pill
  // celebrate like the launch dock.  Answered prompts that left the screen
  // are forgotten, so the same dialog showing up again is shown again.
  const seen = useRef<{ prompts: Set<string>; status: Map<string, LiveSession['status']> }>({
    prompts: new Set(),
    status: new Map(),
  });
  useEffect(() => {
    const prev = seen.current;
    const prompts = new Set<string>();
    const status = new Map<string, LiveSession['status']>();
    let chime: 'session' | 'done' | null = null;
    for (const s of sessions) {
      status.set(s.name, s.status);
      if (s.prompt) {
        prompts.add(s.prompt.id);
        if (!prev.prompts.has(s.prompt.id)) chime = 'session';
      }
      if (s.status === 'idle' && prev.status.get(s.name) === 'working') {
        chime = chime ?? 'done';
        setDocking(true);
        setTimeout(() => setDocking(false), DOCK_MS);
      }
    }
    seen.current = { prompts, status };
    if (chime && state.earcons) playEarcon(chime);
    setAnswered((a) => (a.some((id) => !prompts.has(id)) ? a.filter((id) => prompts.has(id)) : a));
  }, [sessions]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (target && !targetSession) setTarget('');
  }, [target, targetSession]);

  // The reactor leans towards the cursor (src-tauri sends `notch:gaze` while
  // the cursor is near the notch); written as CSS variables, no re-render.
  useEffect(() => {
    let unlisten = () => {};
    import('@tauri-apps/api/event')
      .then(({ listen }) =>
        listen<[number, number]>(NOTCH_GAZE_EVENT, ({ payload: [x, y] }) => {
          pill.current?.style.setProperty('--gx', x.toFixed(3));
          pill.current?.style.setProperty('--gy', y.toFixed(3));
        }),
      )
      .then((fn) => {
        unlisten = fn;
      })
      .catch(() => {});
    return () => unlisten();
  }, []);

  // ⌥Space from anywhere: open the pill with the cursor in the text box
  // (again while typing: close it and hand the keyboard back).
  const shortcutRef = useRef(() => {});
  shortcutRef.current = () => {
    if (open && typing) {
      done();
      setOpen(false);
      return;
    }
    if (state.earcons && !open) playEarcon('reveal');
    setOpen(true);
    keyboard(true);
    setTimeout(() => input.current?.focus(), 80);
  };
  useEffect(() => {
    let active = true;
    import('@tauri-apps/plugin-global-shortcut')
      .then(async ({ register, unregister }) => {
        await unregister(NOTCH_SHORTCUT).catch(() => {});
        if (!active) return;
        await register(NOTCH_SHORTCUT, (e) => {
          if (e.state === 'Pressed') shortcutRef.current();
        });
      })
      .catch((err) => console.warn('[notch] shortcut not registered', err));
    return () => {
      active = false;
      import('@tauri-apps/plugin-global-shortcut')
        .then(({ unregister }) => unregister(NOTCH_SHORTCUT))
        .catch(() => {});
    };
  }, []);

  const poke = (next: 'squish' | 'dizzy') => {
    clearTimeout(moodTimer.current);
    setMood('');
    requestAnimationFrame(() => setMood(next)); // restart the animation
    moodTimer.current = setTimeout(() => setMood(''), next === 'dizzy' ? 1400 : 600);
    if (next === 'dizzy' && state.earcons) playEarcon('scan');
  };

  const answer = async (session: LiveSession, option: PromptOption) => {
    const id = session.prompt!.id;
    setAnswered((a) => [...a, id]);
    if (state.earcons) playEarcon(option.key === 'Escape' ? 'dismiss' : 'done');
    if (!(await answerPrompt(session.name, id, option.key))) {
      setAnswered((a) => a.filter((x) => x !== id));
      if (state.earcons) playEarcon('error');
    }
  };

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

  useEffect(() => {
    let unlisten = () => {};
    import('@tauri-apps/api/event')
      .then(({ listen }) => listen<boolean>(NOTCH_HOVER_EVENT, (e) => setHover(e.payload)))
      .then((fn) => {
        unlisten = fn;
      })
      .catch(() => {});
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

  // An opened pill closes itself a while after the cursor leaves it, unless
  // something is being typed.
  const keepOpen = typing || draft.trim() !== '';
  useEffect(() => {
    if (!open || hover || keepOpen) return;
    closeTimer.current = setTimeout(() => setOpen(false), AUTO_CLOSE_MS);
    return () => {
      if (closeTimer.current) clearTimeout(closeTimer.current);
    };
  }, [open, hover, keepOpen]);

  const toggle = () => {
    // Click-only chime (reveal / conceal); hover and auto-close stay silent.
    if (state.earcons) playEarcon(open ? 'conceal' : 'reveal');
    if (open && typing) {
      input.current?.blur();
    }
    setOpen(!open);
  };

  // Leave the text box and give the keyboard back to the user's app.
  const done = () => {
    input.current?.blur();
    keyboard(false);
  };
  const send = async () => {
    const message = draft.trim();
    if (!message) return;
    setSendError('');
    if (!target) {
      command({ type: 'send', text: message });
      setDraft('');
      done();
      return;
    }
    setDraft('');
    if (await messageSession(target, message)) {
      if (state.earcons) playEarcon('session');
      done();
    } else {
      setDraft(message); // keep it to retry
      setSendError(
        targetSession?.status === 'prompt'
          ? 'La sesión espera que respondas su pregunta primero.'
          : 'No pude escribir en la sesión.',
      );
      if (state.earcons) playEarcon('error');
    }
  };

  const copy = async () => {
    if (!state.reply) return;
    if (await copyText(state.reply)) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    }
  };

  const busy = presence === 'thinking' || presence === 'speaking';
  const peek = hover && !open && !dragging && !swallowed && !asking && presence === 'idle';
  const track = music && now.title ? `♪ ${now.title}${now.artist ? ` · ${now.artist}` : ''} · ` : '';
  const sessionsHint = sessions.length
    ? `${sessions.length} sesión${sessions.length === 1 ? '' : 'es'}` +
      (sessionsWorking ? ` · ${sessionsWorking} trabajando` : '') +
      ' · '
    : '';
  const status = swallowed
    ? `Revisando ${swallowed}`
    : peek
      ? `${track}${sessionsHint}Clic para abrir · ⌥Espacio`
      : text || LABELS[presence];
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
    peek ? 'notch--peek' : '',
    dragging ? 'notch--drop' : '',
    swallowed ? 'notch--swallow' : '',
    asking && !open && !dragging ? 'notch--asking' : '',
    sessionsWorking ? 'notch--sessions-busy' : '',
    music ? 'notch--music' : '',
    mood ? `notch--${mood}` : '',
  ].join(' ');
  const collapsed = presence === 'idle' && !open && !peek && !dragging && !swallowed && !asking;
  const height = collapsed ? ROW_H : ROW_H + bodyH;

  return (
    <div
      ref={pill}
      className={classes}
      style={{ height }}
      aria-live="polite"
      onClick={(e) => {
        // Poking the reactor: one click squishes it (and, unless more clicks
        // follow, opens/closes the pill); a triple click spins it.
        if ((e.target as HTMLElement).closest('.notch__reactor, .notch__head .arc-reactor')) {
          clearTimeout(clickTimer.current);
          if (e.detail === 1) {
            poke('squish');
            clickTimer.current = setTimeout(toggle, MULTI_CLICK_MS);
          } else if (e.detail === 3) {
            poke('dizzy');
          }
          return;
        }
        if (e.detail === 1) toggle();
      }}
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
        {dragging ? (
          <div className="notch__dropzone">
            <FileDown size={18} strokeWidth={1.8} />
            Suelta el archivo y JARVIS lo revisa
          </div>
        ) : asking && !open ? (
          <PromptCard session={asking} onAnswer={answer} />
        ) : open ? (
          <div className="notch__panel">
            <div className="notch__head">
              <ArcReactor size={REACTOR_OPEN} presence={presence} getAudioData={getAudioData} />
              <div className="notch__titles">
                <div className="notch__brand">J.A.R.V.I.S</div>
                <div className="notch__status">{headStatus}</div>
              </div>
              <button
                type="button"
                className="notch__btn notch__expand"
                title={mainOpen ? 'Ocultar la ventana de JARVIS (⌘⇧J)' : 'Abrir JARVIS completo (⌘⇧J)'}
                onClick={(e) => {
                  e.stopPropagation();
                  setOpen(false);
                  setMainWindow(!mainOpen);
                }}
              >
                {mainOpen ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
              </button>
            </div>
            {asking && <PromptCard session={asking} onAnswer={answer} />}
            <SessionChips sessions={sessions} target={target} onTarget={setTarget} />
            <NowPlayingRow now={now} onControl={mediaControl} />
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
            <div className="notch__controls" onClick={(e) => e.stopPropagation()}>
              <button
                type="button"
                className="notch__btn"
                title="Hablar"
                disabled={presence === 'listening'}
                onClick={() => command({ type: 'listen' })}
              >
                <Mic size={15} />
              </button>
              <button
                type="button"
                className="notch__btn notch__btn--stop"
                title="Detener"
                disabled={!busy}
                onClick={() => command({ type: 'stop' })}
              >
                <Square size={13} />
              </button>
              <button
                type="button"
                className="notch__btn"
                title={copied ? 'Copiado' : 'Copiar respuesta'}
                disabled={!state.reply}
                onClick={copy}
              >
                {copied ? <Check size={15} /> : <Copy size={15} />}
              </button>
              <form
                className="notch__ask"
                onSubmit={(e) => {
                  e.preventDefault();
                  send();
                }}
              >
                <input
                  ref={input}
                  value={draft}
                  placeholder={
                    targetSession ? `Escríbele a ${targetSession.project}…` : 'Escríbele a JARVIS…'
                  }
                  spellCheck={false}
                  onChange={(e) => {
                    setDraft(e.target.value);
                    setSendError('');
                  }}
                  onMouseDown={() => keyboard(true)}
                  onFocus={() => setTyping(true)}
                  onBlur={() => setTyping(false)}
                  onKeyDown={(e) => {
                    if (e.key === 'Escape') done();
                  }}
                />
                <button type="submit" className="notch__btn" title="Enviar" disabled={!draft.trim()}>
                  <SendHorizontal size={15} />
                </button>
              </form>
            </div>
            {sendError && <div className="notch__error">{sendError}</div>}
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
