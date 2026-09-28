import { useEffect, useId, useMemo, useRef, type ReactNode } from 'react';
import type { AudioAnalyzerData } from '../../hooks/useTTS';
import type { Presence } from '../../hooks/usePresence';

interface ArcReactorProps {
  size?: number;
  streaming?: boolean;
  audioData?: AudioAnalyzerData;
  /// Per-frame audio source, read inside the draw loop so callers don't have
  /// to re-render every frame. Takes precedence over `audioData`.
  getAudioData?: () => AudioAnalyzerData | undefined;
  /// Explicit presence state; inferred from `streaming`/audio when omitted.
  presence?: Presence;
  className?: string;
}

/// The JARVIS HUD core.
///
/// Layout (all geometry in a 200×200 viewBox, so every size shares it):
///   - static rings live in separate <svg> layers, each rotated by CSS on its
///     own composited <div> — constant rotation never repaints the SVG;
///   - a single <canvas> draws the voice-driven waveform ring (speaking) or
///     the mic ripple + level gauge (listening) from a rAF loop that only
///     runs while there's audio to show;
///   - the loop writes `--level` on the root for the CSS core/halo, so
///     nothing re-renders in React per frame.
export function ArcReactor({
  size = 260,
  streaming = false,
  audioData,
  getAudioData,
  presence,
  className = '',
}: ArcReactorProps) {
  const mode: Presence = presence ?? (streaming ? 'thinking' : audioData ? 'speaking' : 'idle');
  const compact = size < 220;
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, '');

  const rootRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const audioRef = useRef(audioData);
  audioRef.current = audioData;
  const getterRef = useRef(getAudioData);
  getterRef.current = getAudioData;
  // What the canvas last drew, so a fade-out keeps the same shape.
  const lastModeRef = useRef<Presence>('idle');

  const layers = useMemo(() => <HudLayers uid={uid} compact={compact} />, [uid, compact]);

  // ── Voice canvas loop ──
  useEffect(() => {
    const canvas = canvasRef.current;
    const root = rootRef.current;
    const ctx = canvas?.getContext('2d');
    if (!canvas || !root || !ctx) return;

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(size * dpr);
    canvas.height = Math.round(size * dpr);
    const unit = (size * dpr) / 200;
    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
    const active = mode === 'speaking' || mode === 'listening';
    if (active) lastModeRef.current = mode;
    const drawMode = lastModeRef.current;

    const POINTS = 96;
    const smooth = new Float32Array(POINTS);
    let level = 0;
    let vis = 0;
    let lastLevelVar = -1;
    let raf = 0;
    const t0 = performance.now();

    const clear = () => {
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, canvas.width, canvas.height);
    };

    const frame = (now: number) => {
      const t = (now - t0) / 1000;
      const data = getterRef.current ? getterRef.current() : audioRef.current;
      const target = active ? Math.min(1, data?.averageLevel ?? 0) : 0;
      level += (target - level) * (target > level ? 0.45 : 0.12);
      vis += ((active ? 1 : 0) - vis) * 0.12;

      if (Math.abs(level - lastLevelVar) > 0.004) {
        root.style.setProperty('--level', level.toFixed(3));
        lastLevelVar = level;
      }

      clear();
      if (!active && vis < 0.01) {
        root.style.setProperty('--level', '0');
        raf = 0;
        return;
      }

      ctx.setTransform(unit, 0, 0, unit, 0, 0);
      ctx.translate(100, 100);
      ctx.globalAlpha = vis;

      if (drawMode === 'speaking') {
        drawWaveform(ctx, data?.frequencyData ?? null, level, smooth, t, reduced);
      } else {
        drawListening(ctx, level, t, reduced);
      }
      raf = requestAnimationFrame(frame);
    };

    raf = requestAnimationFrame(frame);
    return () => {
      if (raf) cancelAnimationFrame(raf);
    };
  }, [mode, size]);

  return (
    <div
      ref={rootRef}
      className={`arc-reactor arc-${mode} ${compact ? 'arc-compact' : ''} ${className}`}
      style={{ width: size, height: size }}
      aria-hidden="true"
    >
      <div className="arc-layer arc-ambient" />
      {/* Listening halo — swells with the user's voice */}
      <div className="arc-layer arc-halo" />
      {layers}
      {/* Thinking scanner — a sweep orbiting the waveform track */}
      <div className="arc-layer arc-scan" />
      <canvas ref={canvasRef} className="arc-layer arc-canvas" />
      <div className="arc-layer arc-core" />
    </div>
  );
}

// ── Geometry helpers (viewBox 200, centre 100, 0° = top, clockwise) ──

function polar(r: number, deg: number): [number, number] {
  const a = ((deg - 90) * Math.PI) / 180;
  return [100 + r * Math.cos(a), 100 + r * Math.sin(a)];
}

function arc(r: number, from: number, to: number): string {
  const [x1, y1] = polar(r, from);
  const [x2, y2] = polar(r, to);
  const large = to - from > 180 ? 1 : 0;
  return `M${x1.toFixed(2)},${y1.toFixed(2)} A${r},${r} 0 ${large} 1 ${x2.toFixed(2)},${y2.toFixed(2)}`;
}

function ticks(r1: number, r2: number, count: number, every = 1, r2Major = r2): string {
  let d = '';
  for (let i = 0; i < count; i++) {
    const deg = (360 / count) * i;
    const [x1, y1] = polar(r1, deg);
    const [x2, y2] = polar(i % every === 0 ? r2Major : r2, deg);
    d += `M${x1.toFixed(2)},${y1.toFixed(2)}L${x2.toFixed(2)},${y2.toFixed(2)}`;
  }
  return d;
}

// ── Static HUD rings ──

interface SpinProps {
  /// Seconds per turn at rest.
  dur: number;
  /// Seconds per extra turn while thinking.
  boost: number;
  ccw?: boolean;
  className?: string;
  children: ReactNode;
}

/// One composited ring layer: base rotation plus a "boost" rotation that only
/// runs while thinking. Pausing (instead of changing duration) keeps the
/// angle continuous, so speed changes never jump.
function Spin({ dur, boost, ccw, className = '', children }: SpinProps) {
  return (
    <div
      className={`arc-layer arc-spin ${ccw ? 'arc-ccw' : ''} ${className}`}
      style={{ ['--dur' as string]: `${dur}s`, ['--boost' as string]: `${boost}s` }}
    >
      <div className="arc-layer arc-boost">
        <svg viewBox="0 0 200 200" width="100%" height="100%">
          {children}
        </svg>
      </div>
    </div>
  );
}

function HudLayers({ uid, compact }: { uid: string; compact: boolean }) {
  // Hairlines disappear at companion size — thicken them a touch.
  const hair = compact ? 0.8 : 0.5;
  const textPathId = `arc-text-${uid}`;
  const C = '#00d4ff';

  return (
    <>
      {/* Outer tick bezel — slowest */}
      <Spin dur={200} boost={14} className="arc-dim">
        <circle cx="100" cy="100" r="98" fill="none" stroke={C} strokeOpacity="0.22" strokeWidth={hair} />
        <path
          d={ticks(95, 97.5, compact ? 72 : 120, compact ? 6 : 10, 92.5)}
          stroke={C}
          strokeOpacity="0.45"
          strokeWidth={hair}
        />
      </Spin>

      {/* Segmented arc band */}
      <Spin dur={110} boost={9} ccw>
        {[[-38, 52], [62, 118], [128, 214], [226, 302]].map(([a, b]) => (
          <path key={a} d={arc(89, a, b)} fill="none" stroke={C} strokeOpacity="0.55" strokeWidth="2.2" />
        ))}
        {[[-38, -30], [110, 118], [226, 234]].map(([a, b]) => (
          <path key={`c${a}`} d={arc(85.5, a, b)} fill="none" stroke={C} strokeOpacity="0.8" strokeWidth="1.4" />
        ))}
      </Spin>

      {/* Readout text + compass labels (hidden at companion size) */}
      {!compact && (
        <Spin dur={260} boost={20} className="arc-dim arc-readout">
          <defs>
            <path id={textPathId} d={`${arc(81, -150, 170)}`} />
          </defs>
          <text className="arc-text" fill={C} fillOpacity="0.55">
            <textPath href={`#${textPathId}`}>
              J.A.R.V.I.S · CORE 7.2 · NEURAL LINK STABLE · SPECTRUM 20-8K · LAT 0.02 · SYNC OK
            </textPath>
          </text>
        </Spin>
      )}

      {/* Waveform baseline track + fixed notches (static) */}
      <div className="arc-layer">
        <svg viewBox="0 0 200 200" width="100%" height="100%">
          <circle cx="100" cy="100" r="72" fill="none" stroke={C} strokeOpacity="0.28" strokeWidth={hair} />
          <path d={ticks(69, 70.5, 4)} stroke={C} strokeOpacity="0.7" strokeWidth="1" />
          <circle cx="100" cy="100" r="46" fill="none" stroke={C} strokeOpacity="0.18" strokeWidth={hair} />
        </svg>
      </div>

      {/* Dense gear ring */}
      <Spin dur={48} boost={4} className="arc-dim">
        <circle
          cx="100" cy="100" r="62" fill="none" stroke={C} strokeOpacity="0.32"
          strokeWidth="3.5" strokeDasharray="0.8 2.4"
        />
        <circle cx="100" cy="100" r="58.5" fill="none" stroke={C} strokeOpacity="0.45" strokeWidth={hair} />
      </Spin>

      {/* Heavy arc pair */}
      <Spin dur={30} boost={3} ccw>
        <path d={arc(53, 18, 142)} fill="none" stroke={C} strokeOpacity="0.7" strokeWidth="3" />
        <path d={arc(53, 198, 322)} fill="none" stroke={C} strokeOpacity="0.7" strokeWidth="3" />
        <path d={arc(53, 150, 160)} fill="none" stroke={C} strokeOpacity="0.4" strokeWidth="3" />
        <path d={arc(53, 330, 340)} fill="none" stroke={C} strokeOpacity="0.4" strokeWidth="3" />
      </Spin>

      {/* Inner segmented ring */}
      <Spin dur={20} boost={2.2}>
        <circle
          cx="100" cy="100" r="39" fill="none" stroke={C} strokeOpacity="0.8"
          strokeWidth="1.6" strokeDasharray={`${(2 * Math.PI * 39) / 12 - 2.4} 2.4`}
        />
      </Spin>

      {/* Innermost dashed ring */}
      <Spin dur={12} boost={1.6} ccw className="arc-dim">
        <circle
          cx="100" cy="100" r="32.5" fill="none" stroke={C} strokeOpacity="0.55"
          strokeWidth="0.9" strokeDasharray="9 3 2 3"
        />
      </Spin>
    </>
  );
}

// ── Canvas drawing ──

const TRACK = 72;

/// Circular spectrum: the TTS analyser bins mirrored left/right around the
/// track so the voice draws a closed, symmetric ring (lows at the top).
function drawWaveform(
  ctx: CanvasRenderingContext2D,
  freq: Uint8Array | number[] | null,
  level: number,
  smooth: Float32Array,
  t: number,
  reduced: boolean,
) {
  const n = smooth.length;
  // Voice energy sits in the lower bins of the 32-bin analyser.
  const bins = freq ? Math.min(freq.length - 1, 20) : 0;
  for (let i = 0; i < n; i++) {
    const u = i / n; // 0..1 around the circle
    const m = 1 - Math.abs(u * 2 - 1); // 0 at top → 1 at bottom, mirrored
    let v: number;
    if (bins > 1 && freq) {
      const f = 1 + m * (bins - 1);
      const lo = Math.floor(f);
      const hi = Math.min(lo + 1, bins);
      v = ((freq[lo] ?? 0) + ((freq[hi] ?? 0) - (freq[lo] ?? 0)) * (f - lo)) / 255;
      v = Math.pow(v, 1.4); // keep quiet bins quiet, let peaks spike
    } else {
      // No spectrum available — synthesise movement from the overall level.
      v = level * (0.55 + 0.45 * Math.sin(i * 0.9 + t * 7) * Math.sin(i * 0.37 - t * 3));
    }
    smooth[i] += (v - smooth[i]) * (v > smooth[i] ? 0.6 : 0.2);
  }

  const outAmp = reduced ? 10 : 20;
  const inAmp = reduced ? 3 : 7;
  if (!reduced) ctx.rotate(t * 0.12);

  const outer = (i: number) => TRACK + 0.8 + smooth[i % n] * outAmp;
  const inner = (i: number) => TRACK - 0.8 - smooth[i % n] * inAmp;

  // Filled band between the outer and inner curves.
  ctx.beginPath();
  traceClosed(ctx, n, outer);
  traceClosed(ctx, n, inner, true);
  ctx.fillStyle = `rgba(0,212,255,${0.14 + level * 0.18})`;
  ctx.fill('evenodd');

  // Radial spectral hairlines.
  ctx.beginPath();
  for (let i = 0; i < n; i += 2) {
    const a = (i / n) * Math.PI * 2 - Math.PI / 2;
    const r1 = inner(i);
    const r2 = outer(i) + smooth[i] * 3;
    ctx.moveTo(Math.cos(a) * r1, Math.sin(a) * r1);
    ctx.lineTo(Math.cos(a) * r2, Math.sin(a) * r2);
  }
  ctx.strokeStyle = 'rgba(0,212,255,0.28)';
  ctx.lineWidth = 0.5;
  ctx.stroke();

  // Outer line: soft wide pass for glow, then a crisp bright pass.
  ctx.beginPath();
  traceClosed(ctx, n, outer);
  ctx.strokeStyle = `rgba(0,212,255,${0.16 + level * 0.2})`;
  ctx.lineWidth = 4;
  ctx.stroke();
  ctx.strokeStyle = 'rgba(170,240,255,0.95)';
  ctx.lineWidth = 1;
  ctx.stroke();

  ctx.beginPath();
  traceClosed(ctx, n, inner);
  ctx.strokeStyle = 'rgba(0,212,255,0.5)';
  ctx.lineWidth = 0.6;
  ctx.stroke();
}

/// Listening: a soft ripple on the track plus twin level gauges on the bezel.
function drawListening(ctx: CanvasRenderingContext2D, level: number, t: number, reduced: boolean) {
  const n = 72;
  const wob = reduced ? 0 : 1;
  ctx.beginPath();
  traceClosed(ctx, n, (i) => {
    const a = (i / n) * Math.PI * 2;
    return TRACK + level * 7 * (0.55 + 0.45 * wob * Math.sin(3 * a + t * 2.1) * Math.sin(2 * a - t * 1.3));
  });
  ctx.strokeStyle = `rgba(0,212,255,${0.18 + level * 0.25})`;
  ctx.lineWidth = 3;
  ctx.stroke();
  ctx.strokeStyle = `rgba(170,240,255,${0.45 + level * 0.5})`;
  ctx.lineWidth = 0.9;
  ctx.stroke();

  // Gauges grow out from 3 and 9 o'clock.
  const span = 0.08 + level * 1.05;
  ctx.lineCap = 'round';
  ctx.strokeStyle = `rgba(0,212,255,${0.55 + level * 0.4})`;
  ctx.lineWidth = 2.4;
  for (const c of [0, Math.PI]) {
    ctx.beginPath();
    ctx.arc(0, 0, 93.5, c - span / 2, c + span / 2);
    ctx.stroke();
  }
  ctx.lineCap = 'butt';
}

/// Smooth closed curve through `n` polar points (quadratic via midpoints).
function traceClosed(
  ctx: CanvasRenderingContext2D,
  n: number,
  radius: (i: number) => number,
  reverse = false,
) {
  const pt = (k: number): [number, number] => {
    const i = reverse ? (n - (k % n)) % n : k % n;
    const a = (i / n) * Math.PI * 2 - Math.PI / 2;
    const r = radius(i);
    return [Math.cos(a) * r, Math.sin(a) * r];
  };
  const [px, py] = pt(0);
  const [qx, qy] = pt(1);
  ctx.moveTo((px + qx) / 2, (py + qy) / 2);
  for (let k = 1; k <= n; k++) {
    const [cx, cy] = pt(k);
    const [nx, ny] = pt(k + 1);
    ctx.quadraticCurveTo(cx, cy, (cx + nx) / 2, (cy + ny) / 2);
  }
  ctx.closePath();
}
