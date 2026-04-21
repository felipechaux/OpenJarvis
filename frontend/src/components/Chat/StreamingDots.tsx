interface Props {
  phase: string;
}

const EQ_ANIMATIONS = [
  'jarvis-eq-1 0.7s ease-in-out infinite',
  'jarvis-eq-2 0.7s ease-in-out infinite 0.1s',
  'jarvis-eq-3 0.7s ease-in-out infinite 0.2s',
  'jarvis-eq-4 0.7s ease-in-out infinite 0.05s',
  'jarvis-eq-5 0.7s ease-in-out infinite 0.15s',
];

export function StreamingDots({ phase }: Props) {
  return (
    <div className="flex items-center gap-3 py-2">
      {/* Arc reactor ring + equalizer combo */}
      <div className="relative flex items-center justify-center" style={{ width: 28, height: 28 }}>
        {/* Outer spinning ring */}
        <svg
          width="28" height="28"
          style={{ position: 'absolute', animation: 'jarvis-ring-cw 3s linear infinite' }}
          viewBox="0 0 28 28"
        >
          <circle
            cx="14" cy="14" r="12"
            fill="none"
            stroke="var(--color-accent)"
            strokeWidth="1"
            strokeDasharray="6 3"
            opacity="0.5"
          />
        </svg>
        {/* Inner counter-rotating ring */}
        <svg
          width="28" height="28"
          style={{ position: 'absolute', animation: 'jarvis-ring-ccw 2s linear infinite' }}
          viewBox="0 0 28 28"
        >
          <circle
            cx="14" cy="14" r="8"
            fill="none"
            stroke="var(--color-accent)"
            strokeWidth="1"
            strokeDasharray="3 4"
            opacity="0.35"
          />
        </svg>
        {/* Center dot */}
        <div
          style={{
            width: 5,
            height: 5,
            borderRadius: '50%',
            background: 'var(--color-accent)',
            boxShadow: '0 0 8px var(--color-accent), 0 0 16px var(--color-accent-glow)',
            animation: 'jarvis-pulse-ring 1.5s ease-in-out infinite',
          }}
        />
      </div>

      {/* Equalizer bars */}
      <div className="flex items-end gap-0.5" style={{ height: 20 }}>
        {EQ_ANIMATIONS.map((anim, i) => (
          <div
            key={i}
            style={{
              width: 3,
              borderRadius: 2,
              background: 'var(--color-accent)',
              opacity: 0.75,
              boxShadow: '0 0 4px var(--color-accent-glow)',
              animation: anim,
              alignSelf: 'flex-end',
              height: 4,
            }}
          />
        ))}
      </div>

      {/* Phase label */}
      {phase && (
        <span
          className="text-xs font-mono tracking-wide"
          style={{ color: 'var(--color-accent)', opacity: 0.7 }}
        >
          {phase || 'PROCESSING'}
          <span className="hud-caret" style={{ marginLeft: 2 }} />
        </span>
      )}
    </div>
  );
}
