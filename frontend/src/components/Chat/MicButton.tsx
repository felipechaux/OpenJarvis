import { useState } from 'react';
import type { SpeechState } from '../../hooks/useSpeech';

interface MicButtonProps {
  state: SpeechState;
  onClick: () => void;
  disabled?: boolean;
  reason?: 'not-enabled' | 'no-backend' | 'streaming';
}

const WAVE_ANIMS = [
  'jarvis-wave-1 0.4s ease-in-out infinite',
  'jarvis-wave-2 0.4s ease-in-out infinite 0.08s',
  'jarvis-wave-3 0.4s ease-in-out infinite 0.16s',
  'jarvis-wave-4 0.4s ease-in-out infinite 0.24s',
  'jarvis-wave-5 0.4s ease-in-out infinite 0.32s',
];

export function MicButton({ state, onClick, disabled, reason }: MicButtonProps) {
  const [showTooltip, setShowTooltip] = useState(false);

  const tooltipText =
    reason === 'not-enabled'
      ? 'Enable in Settings'
      : reason === 'no-backend'
        ? 'Speech backend not configured'
        : reason === 'streaming'
          ? 'Wait for response'
          : state === 'recording'
            ? 'Stop recording'
            : state === 'transcribing'
              ? 'Transcribing...'
              : 'Voice input';

  const isInactive = disabled || state === 'transcribing';

  return (
    <div
      className="relative"
      onMouseEnter={() => setShowTooltip(true)}
      onMouseLeave={() => setShowTooltip(false)}
    >
      <button
        onClick={onClick}
        disabled={isInactive}
        className="flex items-center justify-center rounded-xl transition-all shrink-0"
        style={{
          width: 36,
          height: 36,
          background: state === 'recording'
            ? 'color-mix(in srgb, var(--color-error) 15%, transparent)'
            : 'transparent',
          border: state === 'recording'
            ? '1px solid color-mix(in srgb, var(--color-error) 40%, transparent)'
            : '1px solid transparent',
          color: state === 'recording'
            ? 'var(--color-error)'
            : isInactive
              ? 'var(--color-text-tertiary)'
              : 'var(--color-text-secondary)',
          cursor: isInactive ? 'default' : 'pointer',
          opacity: isInactive ? 0.35 : 1,
          boxShadow: state === 'recording'
            ? '0 0 12px -3px color-mix(in srgb, var(--color-error) 50%, transparent)'
            : 'none',
        }}
      >
        {state === 'recording' ? (
          /* Live waveform bars */
          <div className="flex items-end gap-px" style={{ height: 16 }}>
            {WAVE_ANIMS.map((anim, i) => (
              <div
                key={i}
                style={{
                  width: 2.5,
                  borderRadius: 2,
                  background: 'var(--color-error)',
                  boxShadow: '0 0 4px var(--color-error)',
                  animation: anim,
                  alignSelf: 'flex-end',
                  height: 3,
                }}
              />
            ))}
          </div>
        ) : state === 'transcribing' ? (
          <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
            <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="2" strokeDasharray="28" strokeDashoffset="10">
              <animateTransform attributeName="transform" type="rotate" from="0 8 8" to="360 8 8" dur="1s" repeatCount="indefinite" />
            </circle>
          </svg>
        ) : (
          <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
            <path d="M5 3a3 3 0 0 1 6 0v5a3 3 0 0 1-6 0V3z" />
            <path d="M3.5 6.5A.5.5 0 0 1 4 7v1a4 4 0 0 0 8 0V7a.5.5 0 0 1 1 0v1a5 5 0 0 1-4.5 4.975V15h3a.5.5 0 0 1 0 1h-7a.5.5 0 0 1 0-1h3v-2.025A5 5 0 0 1 3 8V7a.5.5 0 0 1 .5-.5z" />
          </svg>
        )}
      </button>

      {showTooltip && (
        <div
          className="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 px-2.5 py-1.5 rounded-lg text-xs whitespace-nowrap pointer-events-none z-50"
          style={{
            background: 'var(--color-bg-tertiary)',
            color: 'var(--color-text)',
            border: '1px solid var(--color-border)',
            boxShadow: 'var(--shadow-md)',
          }}
        >
          {tooltipText}
        </div>
      )}
    </div>
  );
}
