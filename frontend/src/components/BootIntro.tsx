import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { BOOT_TIMELINE as T, playBootSound, playCustomBootSound } from '../lib/bootSound';
import { NOTCH_DOCK_EVENT, NOTCH_LABEL } from '../notch/state';
import './bootIntro.css';

const COILS = 10;

type Lang = 'es' | 'en';

function greeting(lang: Lang, hour = new Date().getHours()): string {
  if (lang === 'en') {
    if (hour < 12) return 'Good morning, sir.';
    if (hour < 19) return 'Good afternoon, sir.';
    return 'Good evening, sir.';
  }
  if (hour < 12) return 'Buenos días, señor.';
  if (hour < 19) return 'Buenas tardes, señor.';
  return 'Buenas noches, señor.';
}

const SYSTEMS: Record<Lang, string[]> = {
  es: ['Núcleo', 'Voz', 'Enlace neuronal', 'Memoria'],
  en: ['Core', 'Voice', 'Neural link', 'Memory'],
};
const ONLINE: Record<Lang, string> = { es: 'en línea', en: 'online' };
const TAGLINE = 'Just A Rather Very Intelligent System';
// The reactor reaches the notch this far into its flight (bootIntro.css
// boot-dock), in step with the score's dock clank.
const ARRIVAL_S = 0.55;

/// Aim the dock flight at the real notch: the vector (CSS px) from the
/// reactor's centre to the notch, both in global screen points.  Null
/// outside Tauri or off macOS; the CSS then falls back to flying straight up.
async function notchVector(el: HTMLElement): Promise<[number, number] | null> {
  try {
    const { invoke } = await import('@tauri-apps/api/core');
    const { getCurrentWindow } = await import('@tauri-apps/api/window');
    const anchor = await invoke<[number, number] | null>('notch_anchor');
    if (!anchor) return null;
    const win = getCurrentWindow();
    const scale = await win.scaleFactor();
    const inner = (await win.innerPosition()).toLogical(scale);
    const r = el.getBoundingClientRect();
    const cx = inner.x + r.left + r.width / 2;
    const cy = inner.y + r.top + r.height / 2;
    return [anchor[0] - cx, anchor[1] - cy];
  } catch {
    return null;
  }
}

/// Tell the notch window the reactor has arrived.
async function signalDock() {
  try {
    const { emitTo } = await import('@tauri-apps/api/event');
    await emitTo(NOTCH_LABEL, NOTCH_DOCK_EVENT, null);
  } catch {
    /* plain browser */
  }
}

/// The launch intro: an arc reactor powering up in the dark, a greeting,
/// then the reactor docking into the notch.  Timed to the synthesized score
/// in lib/bootSound.ts (same BOOT_TIMELINE).  Click or Esc skips it.
/// `onDock` fires as the reactor reaches the notch: the window can go then.
export function BootIntro({
  lang,
  sound,
  onDone,
  onDock,
}: {
  lang: Lang;
  sound: boolean;
  onDone: () => void;
  onDock?: () => void;
}) {
  const [leaving, setLeaving] = useState(false);
  const [dock, setDock] = useState<[number, number] | null>(null);
  const reactor = useRef<HTMLDivElement>(null);
  const done = useRef(false);
  const stopSound = useRef<() => void>(() => {});
  const reduced = useMemo(
    () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false,
    [],
  );
  const text = useMemo(() => greeting(lang), [lang]);
  const onDockRef = useRef(onDock);
  onDockRef.current = onDock;

  useEffect(() => {
    const finish = () => {
      if (done.current) return;
      done.current = true;
      onDone();
    };
    const skip = () => {
      if (done.current) return;
      stopSound.current();
      setLeaving(true);
      setTimeout(finish, 350);
    };
    // The user's own sound when there is one, else the synthesized score;
    // either way in step with the animation, which starts now.
    let cancelled = false;
    if (sound && !reduced) {
      const started = performance.now();
      playCustomBootSound(
        () => (performance.now() - started) / 1000,
        () => !cancelled,
      ).then((stop) => {
        if (cancelled) {
          stop?.();
          return;
        }
        stopSound.current = stop ?? playBootSound();
      });
    }
    const end = setTimeout(finish, (reduced ? 1.6 : T.end) * 1000);
    // Aim before the flight starts (layout is settled by then).
    const aim = setTimeout(() => {
      if (reactor.current) notchVector(reactor.current).then(setDock);
    }, (T.dock - 0.5) * 1000);
    const arrive = setTimeout(() => {
      signalDock();
      onDockRef.current?.();
    }, (reduced ? 0.8 : T.dock + ARRIVAL_S) * 1000);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' || e.key === 'Enter' || e.key === ' ') skip();
    };
    window.addEventListener('keydown', onKey);
    window.addEventListener('pointerdown', skip);
    return () => {
      cancelled = true;
      clearTimeout(end);
      clearTimeout(aim);
      clearTimeout(arrive);
      window.removeEventListener('keydown', onKey);
      window.removeEventListener('pointerdown', skip);
    };
  }, [onDone, sound, reduced]);

  // Every animation delay comes from the shared timeline.
  const vars = {
    '--t-ignite': `${T.ignite}s`,
    '--t-coils': `${T.coilsStart}s`,
    '--t-step': `${T.coilStep}s`,
    '--t-lock': `${T.lock}s`,
    '--t-greet': `${T.greet}s`,
    '--t-scan': `${T.scan}s`,
    '--t-dock': `${T.dock}s`,
    '--t-end': `${T.end}s`,
    ...(dock && { '--dock-x': `${dock[0]}px`, '--dock-y': `${dock[1]}px` }),
  } as CSSProperties;

  return (
    <div
      className={`boot ${leaving ? 'boot--leaving' : ''} ${reduced ? 'boot--reduced' : ''}`}
      style={vars}
      role="presentation"
    >
      <div className="boot__brand">
        <div className="boot__name">J.A.R.V.I.S</div>
        <div className="boot__tagline">{TAGLINE}</div>
      </div>

      <div className="boot__stage">
        <div ref={reactor} className={`boot__reactor ${dock ? 'boot__reactor--aimed' : ''}`}>
          <div className="boot__bloom" />
          <div className="boot__scan" />
          <svg viewBox="-100 -100 200 200" className="boot__svg" aria-hidden>
            <defs>
              <radialGradient id="boot-core">
                <stop offset="0%" stopColor="#ffffff" />
                <stop offset="30%" stopColor="#c8f6ff" />
                <stop offset="65%" stopColor="#00d4ff" stopOpacity="0.55" />
                <stop offset="100%" stopColor="#00d4ff" stopOpacity="0" />
              </radialGradient>
            </defs>

            {/* Rings draw themselves in, outermost last. */}
            <g className="boot__spin boot__spin--slow">
              <circle className="boot__ring boot__ring--ticks" r="92" pathLength={1} style={{ '--i': 3 } as CSSProperties} />
            </g>
            <g className="boot__spin boot__spin--rev">
              <circle className="boot__ring boot__ring--seg" r="80" pathLength={1} style={{ '--i': 2 } as CSSProperties} />
            </g>
            <circle className="boot__ring" r="66" pathLength={1} style={{ '--i': 1 } as CSSProperties} />
            <g className="boot__spin boot__spin--fast">
              <circle className="boot__ring boot__ring--dash" r="56" pathLength={1} style={{ '--i': 0 } as CSSProperties} />
            </g>

            {/* The ten coils light one by one, clockwise. */}
            <g className="boot__coils">
              {Array.from({ length: COILS }, (_, i) => (
                <rect
                  key={i}
                  className="boot__coil"
                  x="-5"
                  y="-47"
                  width="10"
                  height="16"
                  rx="2"
                  transform={`rotate(${(360 / COILS) * i})`}
                  style={{ '--i': i } as CSSProperties}
                />
              ))}
            </g>

            <circle className="boot__inner" r="26" />
            <circle className="boot__core" r="24" fill="url(#boot-core)" />
            <circle className="boot__flash" r="100" />
          </svg>
        </div>

        <div className="boot__greeting" aria-live="polite">
          {Array.from(text).map((ch, i) => (
            <span key={i} style={{ '--i': i } as CSSProperties}>
              {ch === ' ' ? ' ' : ch}
            </span>
          ))}
        </div>
      </div>

      <ul className="boot__systems">
        {SYSTEMS[lang].map((name, i) => (
          <li key={name} style={{ '--i': i } as CSSProperties}>
            <span>{name}</span>
            <i />
            <b>{ONLINE[lang]}</b>
          </li>
        ))}
      </ul>
    </div>
  );
}
