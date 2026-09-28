import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ArcReactor } from './ArcReactor';
import { useAppStore } from '../../lib/store';
import { usePresence } from '../../hooks/usePresence';
import type { AudioAnalyzerData } from '../../hooks/useTTS';

// Speaking ↔ thinking flips between sentences while the reply streams; keep
// the HUD up through those gaps and only fold it away once JARVIS is done.
const HIDE_AFTER_MS = 1400;

const getTTSAudio = (): AudioAnalyzerData | undefined => useAppStore.getState().ttsAudioData;

function useViewport() {
  const [vp, setVp] = useState({ w: window.innerWidth, h: window.innerHeight });
  useEffect(() => {
    const onResize = () => setVp({ w: window.innerWidth, h: window.innerHeight });
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);
  return vp;
}

function useClock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  return now;
}

/// Iron Man-style full-window HUD shown while JARVIS speaks: the reactor
/// large at the centre riding the voice, telemetry panels either side, a
/// live spectrum, a rolling voice trace and the spoken sentence as a
/// subtitle.  Esc or the ✕ button folds it away for the rest of the reply
/// (a stray click on the HUD itself does nothing).
export function JarvisHud() {
  const presence = usePresence();
  const enabled = useAppStore((s) => s.settings.speakingHud);
  const [visible, setVisible] = useState(false);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    if (presence === 'speaking') {
      setVisible(true);
      return;
    }
    if (presence === 'listening') {
      // The user took the floor — get out of the way immediately.
      setVisible(false);
      setDismissed(false);
      return;
    }
    const id = setTimeout(() => {
      setVisible(false);
      setDismissed(false);
    }, HIDE_AFTER_MS);
    return () => clearTimeout(id);
  }, [presence]);

  useEffect(() => {
    if (!visible || dismissed) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setDismissed(true);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [visible, dismissed]);

  if (!enabled || !visible || dismissed) return null;
  // Portal to <body>: the routed page keeps a transform from its entrance
  // animation, which would otherwise trap `position: fixed` inside the chat.
  return createPortal(<HudScreen onDismiss={() => setDismissed(true)} />, document.body);
}

function HudScreen({ onDismiss }: { onDismiss: () => void }) {
  const { w, h } = useViewport();
  const now = useClock();
  const caption = useAppStore((s) => s.ttsCaption);
  const model = useAppStore((s) => s.selectedModel);
  const language = useAppStore((s) => s.settings.language);

  const rootRef = useRef<HTMLDivElement>(null);
  const spectrumRef = useRef<HTMLCanvasElement>(null);
  const traceRef = useRef<HTMLCanvasElement>(null);
  const hexRef = useRef<HTMLPreElement>(null);

  const compact = w < 820;
  const reactorSize = Math.round(Math.max(200, Math.min(h * 0.5, w * (compact ? 0.62 : 0.36), 440)));

  // ── One rAF loop for every live widget ──
  useEffect(() => {
    const root = rootRef.current;
    const spectrum = spectrumRef.current;
    const trace = traceRef.current;
    if (!root) return;
    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);

    const fit = (c: HTMLCanvasElement | null) => {
      if (!c) return null;
      const r = c.getBoundingClientRect();
      c.width = Math.max(1, Math.round(r.width * dpr));
      c.height = Math.max(1, Math.round(r.height * dpr));
      return c.getContext('2d');
    };
    const sctx = fit(spectrum);
    const tctx = fit(trace);

    const HISTORY = 160;
    const history = new Float32Array(HISTORY);
    const bars = new Float32Array(24);
    let avg = 0;
    let bass = 0;
    let treble = 0;
    let raf = 0;
    let last = 0;

    const frame = (t: number) => {
      const d = useAppStore.getState().ttsAudioData;
      avg += ((d?.averageLevel ?? 0) - avg) * 0.3;
      bass += ((d?.bassLevel ?? 0) - bass) * 0.3;
      treble += ((d?.trebleLevel ?? 0) - treble) * 0.3;
      root.style.setProperty('--hud-avg', avg.toFixed(3));
      root.style.setProperty('--hud-bass', bass.toFixed(3));
      root.style.setProperty('--hud-treble', treble.toFixed(3));

      // Voice trace: push a sample ~every 33ms so it scrolls at a steady pace.
      if (t - last > 33) {
        history.copyWithin(0, 1);
        history[HISTORY - 1] = avg;
        last = t;
      }

      const accent = '0, 212, 255';
      if (sctx && spectrum) {
        const W = spectrum.width;
        const H = spectrum.height;
        sctx.clearRect(0, 0, W, H);
        const freq = d?.frequencyData;
        const n = bars.length;
        const gap = W / n;
        for (let i = 0; i < n; i++) {
          const raw = freq && freq.length ? Number(freq[Math.floor((i / n) * freq.length * 0.8)] ?? 0) / 255 : 0;
          bars[i] += (raw - bars[i]) * (raw > bars[i] ? 0.5 : 0.15);
          const bh = Math.max(1.5 * dpr, bars[i] * H);
          // Segmented bars — the HUD "LED ladder" look.
          const seg = 3 * dpr;
          for (let y = H; y > H - bh; y -= seg + dpr) {
            const k = (H - y) / H;
            sctx.fillStyle = k > 0.8 ? 'rgba(255, 159, 0, 0.9)' : `rgba(${accent}, ${0.35 + k * 0.6})`;
            sctx.fillRect(i * gap + gap * 0.18, y - seg, gap * 0.64, seg);
          }
        }
      }

      if (tctx && trace) {
        const W = trace.width;
        const H = trace.height;
        const mid = H / 2;
        tctx.clearRect(0, 0, W, H);
        tctx.strokeStyle = `rgba(${accent}, 0.18)`;
        tctx.lineWidth = dpr;
        tctx.beginPath();
        tctx.moveTo(0, mid);
        tctx.lineTo(W, mid);
        tctx.stroke();

        // Mirrored voice envelope with a carrier wobble — the movie's
        // "voice line" under the subtitles.
        tctx.shadowColor = `rgba(${accent}, 0.8)`;
        tctx.shadowBlur = 8 * dpr;
        tctx.strokeStyle = `rgba(${accent}, 0.95)`;
        tctx.lineWidth = 1.4 * dpr;
        tctx.beginPath();
        for (let i = 0; i < HISTORY; i++) {
          const x = (i / (HISTORY - 1)) * W;
          const fade = Math.sin((i / (HISTORY - 1)) * Math.PI);
          const wobble = reduced ? 1 : Math.sin(i * 0.9 + t / 90);
          const y = mid + history[i] * wobble * fade * (H * 0.46);
          if (i === 0) tctx.moveTo(x, y);
          else tctx.lineTo(x, y);
        }
        tctx.stroke();
        tctx.shadowBlur = 0;
      }

      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);

    // Decorative telemetry stream, as in the suit HUD.
    let hexTimer: ReturnType<typeof setInterval> | null = null;
    if (hexRef.current && !reduced) {
      const lines: string[] = [];
      const rnd = (n: number) => Math.floor(Math.random() * 16 ** n).toString(16).toUpperCase().padStart(n, '0');
      hexTimer = setInterval(() => {
        lines.push(`${rnd(4)}:${rnd(4)}  ${rnd(2)} ${rnd(2)} ${rnd(2)} ${rnd(2)}  ${(Math.random() * 99).toFixed(2)}`);
        if (lines.length > 9) lines.shift();
        if (hexRef.current) hexRef.current.textContent = lines.join('\n');
      }, 160);
    }

    return () => {
      cancelAnimationFrame(raf);
      if (hexTimer) clearInterval(hexTimer);
    };
  }, [compact]);

  const es = language !== 'en';
  const time = now.toLocaleTimeString(es ? 'es-CO' : 'en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  const date = now.toLocaleDateString(es ? 'es-CO' : 'en-GB', { weekday: 'short', day: '2-digit', month: 'short', year: 'numeric' }).toUpperCase();

  return (
    <div ref={rootRef} className="jhud" role="dialog" aria-label="J.A.R.V.I.S.">
      <div className="jhud-grid" aria-hidden="true" />
      <div className="jhud-scanlines" aria-hidden="true" />
      <div className="jhud-vignette" aria-hidden="true" />
      <span className="jhud-corner jhud-tl" aria-hidden="true" />
      <span className="jhud-corner jhud-tr" aria-hidden="true" />
      <span className="jhud-corner jhud-bl" aria-hidden="true" />
      <span className="jhud-corner jhud-br" aria-hidden="true" />

      {/* Top bar */}
      <header className="jhud-top" data-tauri-drag-region>
        <div className="jhud-title">
          <span className="jhud-title-main">J.A.R.V.I.S.</span>
          <span className="jhud-title-sub">JUST A RATHER VERY INTELLIGENT SYSTEM</span>
        </div>
        <button
          type="button"
          className="jhud-close"
          onClick={onDismiss}
          aria-label={es ? 'Ocultar HUD' : 'Hide HUD'}
          title={es ? 'Ocultar HUD (Esc)' : 'Hide HUD (Esc)'}
        >
          ✕
        </button>
        <div className="jhud-clock">
          <span className="jhud-clock-time">{time}</span>
          <span className="jhud-clock-date">{date}</span>
        </div>
      </header>

      {/* Side panels */}
      {!compact && (
        <aside className="jhud-panel jhud-left" aria-hidden="true">
          <div className="jhud-panel-title">{es ? 'DIAGNÓSTICO' : 'DIAGNOSTICS'}</div>
          <Meter label={es ? 'SALIDA VOZ' : 'VOICE OUT'} cssVar="--hud-avg" />
          <Meter label={es ? 'GRAVES' : 'LOW BAND'} cssVar="--hud-bass" />
          <Meter label={es ? 'AGUDOS' : 'HIGH BAND'} cssVar="--hud-treble" />
          <dl className="jhud-kv">
            <dt>{es ? 'ENLACE' : 'UPLINK'}</dt><dd className="jhud-ok">{es ? 'EN LÍNEA' : 'ONLINE'}</dd>
            <dt>{es ? 'NÚCLEO' : 'CORE'}</dt><dd>{(model || '—').toUpperCase()}</dd>
            <dt>{es ? 'PROTOCOLO' : 'PROTOCOL'}</dt><dd>{es ? 'VOZ · ES-LAT' : 'VOICE · EN-GB'}</dd>
          </dl>
          <pre ref={hexRef} className="jhud-hex" />
        </aside>
      )}

      {!compact && (
        <aside className="jhud-panel jhud-right" aria-hidden="true">
          <div className="jhud-panel-title">{es ? 'ESPECTRO' : 'SPECTRUM'}</div>
          <canvas ref={spectrumRef} className="jhud-spectrum" />
          <div className="jhud-radar">
            <div className="jhud-radar-sweep" />
            <span className="jhud-radar-blip" style={{ top: '30%', left: '62%' }} />
            <span className="jhud-radar-blip" style={{ top: '64%', left: '36%', animationDelay: '1.1s' }} />
          </div>
          <div className="jhud-status">
            <span className="jhud-dot" /> {es ? 'TRANSMITIENDO' : 'TRANSMITTING'}
          </div>
        </aside>
      )}

      {/* Reactor */}
      <div className="jhud-center">
        <div className="jhud-reactor-ring" style={{ width: reactorSize * 1.22, height: reactorSize * 1.22 }} aria-hidden="true" />
        <ArcReactor size={reactorSize} presence="speaking" getAudioData={getTTSAudio} />
      </div>

      {/* Subtitle + voice trace */}
      <footer className="jhud-bottom">
        <div className="jhud-caption-label">{es ? '▸ TRANSMISIÓN DE VOZ' : '▸ VOICE TRANSMISSION'}</div>
        <p key={caption} className="jhud-caption">{caption ? `« ${caption} »` : ' '}</p>
        <canvas ref={traceRef} className="jhud-trace" />
        <div className="jhud-hint">{es ? 'ESC · OCULTAR HUD' : 'ESC · HIDE HUD'}</div>
      </footer>
    </div>
  );
}

function Meter({ label, cssVar }: { label: string; cssVar: string }) {
  return (
    <div className="jhud-meter">
      <div className="jhud-meter-head">
        <span>{label}</span>
      </div>
      <div className="jhud-meter-track">
        <div className="jhud-meter-fill" style={{ transform: `scaleX(var(${cssVar}, 0))` }} />
      </div>
    </div>
  );
}
