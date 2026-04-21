import { useMemo } from 'react';
import type { AudioAnalyzerData } from '../../hooks/useTTS';

interface ArcReactorProps {
  size?: number;
  streaming?: boolean;
  audioData?: AudioAnalyzerData;
  className?: string;
}

export function ArcReactor({
  size = 260,
  streaming = false,
  audioData,
}: ArcReactorProps) {
  const cx = size / 2;
  const cy = size / 2;

  // Calculate voice-reactive values
  const voiceIntensity = audioData?.averageLevel ?? 0;
  const bassIntensity = audioData?.bassLevel ?? 0;
  const trebleIntensity = audioData?.trebleLevel ?? 0;
  const isVoiceActive = voiceIntensity > 0.05;

  // Dynamic speed based on streaming and voice
  const baseSpeed = streaming ? 0.45 : 1;
  const voiceSpeedMultiplier = 1 + voiceIntensity * 2; // Speed up when voice is loud
  const speed = baseSpeed / voiceSpeedMultiplier;

  // Dynamic glow intensity based on voice
  const glowIntensity = isVoiceActive
    ? 0.5 + voiceIntensity * 0.5 // Boost glow when speaking
    : streaming ? 0.4 : 0.25;

  const glow = useMemo(() => {
    const baseGlow = streaming || isVoiceActive
      ? `drop-shadow(0 0 18px rgba(0,212,255,${0.7 + voiceIntensity * 0.3})) drop-shadow(0 0 40px rgba(0,212,255,${0.4 + voiceIntensity * 0.4}))`
      : `drop-shadow(0 0 8px rgba(0,212,255,0.5)) drop-shadow(0 0 20px rgba(0,212,255,0.25))`;
    return baseGlow;
  }, [streaming, isVoiceActive, voiceIntensity]);

  // Core pulse speed based on voice
  const corePulseSpeed = isVoiceActive ? 0.6 / (1 + voiceIntensity) : 1.8;

  // Hexagon points (flat-top) centered at cx,cy with given radius
  function hex(r: number): string {
    return Array.from({ length: 6 }, (_, i) => {
      const a = (Math.PI / 180) * (60 * i - 30);
      return `${cx + r * Math.cos(a)},${cy + r * Math.sin(a)}`;
    }).join(' ');
  }

  // Triangle points
  function tri(r: number, rotate = 0): string {
    return Array.from({ length: 3 }, (_, i) => {
      const a = (Math.PI / 180) * (120 * i + rotate);
      return `${cx + r * Math.cos(a)},${cy + r * Math.sin(a)}`;
    }).join(' ');
  }

  const s = (base: number) => `${base * speed}s`;

  // Generate frequency bars for voice visualization
  const frequencyBars = useMemo(() => {
    if (!audioData?.frequencyData) return null;

    const bars = [];
    const barCount = 8;
    const radius = cx * 0.95;
    const maxBarLength = cx * 0.15;
    const freqData = audioData.frequencyData;

    for (let i = 0; i < barCount; i++) {
      const angle = (Math.PI / 180) * (45 * i - 90); // Start from top
      // Map frequency data to bars (sample from different parts of frequency spectrum)
      const freqIndex = Math.floor((i / barCount) * freqData.length * 0.5);
      const freqValue = freqData[freqIndex] ?? 0;
      const barLength = (freqValue / 255) * maxBarLength * (1 + voiceIntensity);

      const x1 = cx + radius * Math.cos(angle);
      const y1 = cy + radius * Math.sin(angle);
      const x2 = cx + (radius + barLength) * Math.cos(angle);
      const y2 = cy + (radius + barLength) * Math.sin(angle);

      bars.push(
        <line
          key={`freq-${i}`}
          x1={x1}
          y1={y1}
          x2={x2}
          y2={y2}
          stroke="rgba(0,212,255,0.8)"
          strokeWidth={2 + voiceIntensity * 2}
          strokeLinecap="round"
          style={{
            filter: `drop-shadow(0 0 ${4 + voiceIntensity * 6}px rgba(0,212,255,${0.6 + voiceIntensity * 0.4}))`,
            opacity: 0.3 + (freqValue / 255) * 0.7,
          }}
        />
      );
    }
    return bars;
  }, [audioData, voiceIntensity, cx, cy]);

  // Inner voice ring pulses
  const voiceRingRadius = cx * (0.25 + bassIntensity * 0.1);
  const voiceRingOpacity = 0.3 + voiceIntensity * 0.5;

  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      style={{ filter: glow, overflow: 'visible' }}
    >
      {/* ── Ambient glow blob ── */}
      <radialGradient id="arc-glow" cx="50%" cy="50%" r="50%">
        <stop
          offset="0%"
          stopColor={`rgba(0,212,255,${0.12 + voiceIntensity * 0.25})`}
        />
        <stop offset="100%" stopColor="rgba(0,212,255,0)" />
      </radialGradient>
      <circle cx={cx} cy={cy} r={cx * 0.95} fill="url(#arc-glow)" />

      {/* ── Voice-reactive frequency bars (outer ring) ── */}
      {isVoiceActive && frequencyBars}

      {/* ── Ring 1 — outermost, slow CW ── */}
      <g
        style={{
          animation: `jarvis-ring-cw ${s(12)} linear infinite`,
          transformOrigin: `${cx}px ${cy}px`,
        }}
      >
        <circle
          cx={cx}
          cy={cy}
          r={cx * 0.88}
          fill="none"
          stroke={`rgba(0,212,255,${0.25 + voiceIntensity * 0.3})`}
          strokeWidth={1 + trebleIntensity}
          strokeDasharray={`${8 + voiceIntensity * 8} ${6 - voiceIntensity * 3}`}
        />
      </g>

      {/* ── Ring 2 — CCW, medium ── */}
      <g
        style={{
          animation: `jarvis-ring-ccw ${s(8)} linear infinite`,
          transformOrigin: `${cx}px ${cy}px`,
        }}
      >
        <circle
          cx={cx}
          cy={cy}
          r={cx * 0.78}
          fill="none"
          stroke={`rgba(0,212,255,${0.4 + voiceIntensity * 0.35})`}
          strokeWidth={1.5 + bassIntensity}
          strokeDasharray={`${18 + voiceIntensity * 10} 4 4 4`}
        />
      </g>

      {/* ── Hex frame at ring-2 radius ── */}
      <polygon
        points={hex(cx * 0.78)}
        fill="none"
        stroke={`rgba(0,212,255,${0.12 + voiceIntensity * 0.2})`}
        strokeWidth={1 + voiceIntensity}
      />

      {/* ── Ring 3 — CW, faster ── */}
      <g
        style={{
          animation: `jarvis-ring-cw ${s(5)} linear infinite`,
          transformOrigin: `${cx}px ${cy}px`,
        }}
      >
        <circle
          cx={cx}
          cy={cy}
          r={cx * 0.66}
          fill="none"
          stroke={`rgba(0,212,255,${0.55 + voiceIntensity * 0.3})`}
          strokeWidth={1.5 + voiceIntensity}
          strokeDasharray={`${4 + voiceIntensity * 6} 3`}
        />
      </g>

      {/* ── Outer hex bracket lines (spokes) ── */}
      {Array.from({ length: 6 }, (_, i) => {
        const a = (Math.PI / 180) * (60 * i - 30);
        const r1 = cx * 0.66,
          r2 = cx * 0.78;
        return (
          <line
            key={i}
            x1={cx + r1 * Math.cos(a)}
            y1={cy + r1 * Math.sin(a)}
            x2={cx + r2 * Math.cos(a)}
            y2={cy + r2 * Math.sin(a)}
            stroke={`rgba(0,212,255,${0.2 + voiceIntensity * 0.3})`}
            strokeWidth={1 + voiceIntensity}
          />
        );
      })}

      {/* ── Ring 4 — CCW ── */}
      <g
        style={{
          animation: `jarvis-ring-ccw ${s(7)} linear infinite`,
          transformOrigin: `${cx}px ${cy}px`,
        }}
      >
        <circle
          cx={cx}
          cy={cy}
          r={cx * 0.52}
          fill="none"
          stroke={`rgba(0,212,255,${0.45 + voiceIntensity * 0.35})`}
          strokeWidth={2 + voiceIntensity * 1.5}
          strokeDasharray={`${12 + voiceIntensity * 8} 5 3 5`}
        />
      </g>

      {/* ── Inner hex (solid, faint fill) ── */}
      <polygon
        points={hex(cx * 0.42)}
        fill={`rgba(0,212,255,${0.04 + voiceIntensity * 0.1})`}
        stroke={`rgba(0,212,255,${0.35 + voiceIntensity * 0.4})`}
        strokeWidth={1.5 + voiceIntensity}
        style={{
          animation: `jarvis-ring-cw ${s(20)} linear infinite`,
          transformOrigin: `${cx}px ${cy}px`,
        }}
      />

      {/* ── Voice-reactive pulse ring ── */}
      {isVoiceActive && (
        <circle
          cx={cx}
          cy={cy}
          r={voiceRingRadius}
          fill="none"
          stroke={`rgba(0,212,255,${voiceRingOpacity})`}
          strokeWidth={2 + voiceIntensity * 3}
          style={{
            filter: `drop-shadow(0 0 ${8 + voiceIntensity * 12}px rgba(0,212,255,${0.5 + voiceIntensity * 0.5}))`,
            animation: `jarvis-pulse-ring ${0.3 + (1 - voiceIntensity) * 0.5}s ease-in-out infinite`,
          }}
        />
      )}

      {/* ── Triangle outer (CW) ── */}
      <polygon
        points={tri(cx * 0.38)}
        fill="none"
        stroke={`rgba(0,212,255,${0.5 + voiceIntensity * 0.3})`}
        strokeWidth={1.5 + voiceIntensity}
        style={{
          animation: `jarvis-ring-cw ${s(4)} linear infinite`,
          transformOrigin: `${cx}px ${cy}px`,
        }}
      />

      {/* ── Triangle inner (CCW) ── */}
      <polygon
        points={tri(cx * 0.26, 60)}
        fill="none"
        stroke={`rgba(0,212,255,${0.65 + voiceIntensity * 0.25})`}
        strokeWidth={1.5 + voiceIntensity * 0.5}
        style={{
          animation: `jarvis-ring-ccw ${s(3)} linear infinite`,
          transformOrigin: `${cx}px ${cy}px`,
        }}
      />

      {/* ── Ring 5 — innermost ring ── */}
      <g
        style={{
          animation: `jarvis-ring-cw ${s(2.5)} linear infinite`,
          transformOrigin: `${cx}px ${cy}px`,
        }}
      >
        <circle
          cx={cx}
          cy={cy}
          r={cx * (0.19 + bassIntensity * 0.03)}
          fill="none"
          stroke={`rgba(0,212,255,${0.7 + voiceIntensity * 0.3})`}
          strokeWidth={2 + voiceIntensity * 2}
          strokeDasharray={`${5 + voiceIntensity * 5} 2`}
        />
      </g>

      {/* ── Core glow layers ── */}
      <radialGradient id="core-grad" cx="50%" cy="50%" r="50%">
        <stop
          offset="0%"
          stopColor={`rgba(180,240,255,${0.85 + voiceIntensity * 0.15})`}
        />
        <stop
          offset="35%"
          stopColor={`rgba(0,212,255,${0.75 + voiceIntensity * 0.25})`}
        />
        <stop offset="100%" stopColor="rgba(0,80,180,0)" />
      </radialGradient>
      <circle
        cx={cx}
        cy={cy}
        r={cx * (0.13 + bassIntensity * 0.04)}
        fill="url(#core-grad)"
        style={{
          animation: `jarvis-pulse-ring ${corePulseSpeed}s ease-in-out infinite`,
          filter: isVoiceActive
            ? `drop-shadow(0 0 ${20 + voiceIntensity * 30}px rgba(0,212,255,${0.6 + voiceIntensity * 0.4}))`
            : 'none',
        }}
      />

      {/* ── Bright center point ── */}
      <circle
        cx={cx}
        cy={cy}
        r={cx * (0.05 + voiceIntensity * 0.03)}
        fill={`rgba(255,255,255,${0.8 + voiceIntensity * 0.2})`}
        style={{
          filter: isVoiceActive
            ? `drop-shadow(0 0 ${10 + voiceIntensity * 20}px rgba(255,255,255,${0.5 + voiceIntensity * 0.5}))`
            : 'none',
        }}
      />

      {/* ── 8 tick marks on outer ring ── */}
      {Array.from({ length: 8 }, (_, i) => {
        const a = (Math.PI / 180) * (45 * i);
        const r1 = cx * 0.88,
          r2 = cx * 0.93 + voiceIntensity * cx * 0.05;
        return (
          <line
            key={i}
            x1={cx + r1 * Math.cos(a)}
            y1={cy + r1 * Math.sin(a)}
            x2={cx + r2 * Math.cos(a)}
            y2={cy + r2 * Math.sin(a)}
            stroke={`rgba(0,212,255,${0.4 + voiceIntensity * 0.4})`}
            strokeWidth={i % 2 === 0 ? 2 + voiceIntensity : 1 + voiceIntensity * 0.5}
          />
        );
      })}

      {/* ── Voice activity indicator arcs ── */}
      {isVoiceActive && (
        <g>
          {Array.from({ length: 3 }, (_, i) => {
            const radius = cx * (0.6 + i * 0.1);
            const opacity = voiceIntensity * (0.3 - i * 0.1);
            return (
              <circle
                key={`voice-ring-${i}`}
                cx={cx}
                cy={cy}
                r={radius}
                fill="none"
                stroke={`rgba(0,212,255,${opacity})`}
                strokeWidth={1 + voiceIntensity}
                strokeDasharray={`${20 + i * 10} ${40 - i * 5}`}
                style={{
                  animation: `jarvis-ring-cw ${1 + i * 0.5}s linear infinite`,
                  transformOrigin: `${cx}px ${cy}px`,
                }}
              />
            );
          })}
        </g>
      )}
    </svg>
  );
}
