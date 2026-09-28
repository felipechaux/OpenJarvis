import { useEffect, useState } from 'react';
import { PictureInPicture2 } from 'lucide-react';
import { isTauri } from '../lib/api';
import { useCompanionMode } from '../hooks/useCompanionMode';

const IS_MAC = navigator.userAgent.includes('Mac');

const DAYS = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'];
const MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];

export function TitleBar() {
  const [time, setTime] = useState(() => new Date());
  const { toggle: toggleCompanion } = useCompanionMode();

  useEffect(() => {
    const t = setInterval(() => setTime(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  const hh = String(time.getHours()).padStart(2, '0');
  const mm = String(time.getMinutes()).padStart(2, '0');
  const ss = String(time.getSeconds()).padStart(2, '0');
  const dateStr = `${DAYS[time.getDay()]} ${String(time.getDate()).padStart(2, '0')} ${MONTHS[time.getMonth()]}`;

  // On macOS the native traffic lights are overlaid on the top-left of the
  // webview (titleBarStyle: Overlay) — reserve space so nothing renders
  // underneath them.
  const leftInset = isTauri() && IS_MAC ? 96 : 12;

  return (
    <div
      className="titlebar flex items-center justify-between shrink-0 select-none relative"
      style={{
        height: 38,
        padding: `0 12px 0 ${leftInset}px`,
        background: 'rgba(2, 6, 14, 0.75)',
        borderBottom: '1px solid var(--color-border)',
        zIndex: 50,
      }}
    >
      {/* Drag region — covers the whole bar; interactive children sit above it */}
      <div data-tauri-drag-region className="absolute inset-0 z-0" />

      {/* Left: status readout */}
      <div
        className="z-10 relative flex items-center gap-2"
        style={{ pointerEvents: 'none', minWidth: 110 }}
      >
        <span className="hud-heartbeat" style={{ width: 5, height: 5 }} />
        <span
          style={{
            fontFamily: 'var(--font-hud)',
            fontSize: '0.6rem',
            letterSpacing: '0.18em',
            color: 'var(--color-text-tertiary)',
          }}
        >
          SYSTEMS ONLINE
        </span>
      </div>

      {/* Center: branding */}
      <div
        className="absolute left-1/2 -translate-x-1/2 flex items-center gap-2.5 z-10"
        style={{ pointerEvents: 'none' }}
      >
        {/* Mini arc reactor */}
        <div className="relative flex items-center justify-center" style={{ width: 16, height: 16 }}>
          <svg width="16" height="16" viewBox="0 0 16 16" style={{ position: 'absolute', animation: 'jarvis-ring-cw 4s linear infinite' }}>
            <circle cx="8" cy="8" r="6" fill="none" stroke="var(--color-accent)" strokeWidth="0.75" strokeDasharray="4 2" opacity="0.6" />
          </svg>
          <svg width="16" height="16" viewBox="0 0 16 16" style={{ position: 'absolute', animation: 'jarvis-ring-ccw 3s linear infinite' }}>
            <circle cx="8" cy="8" r="4" fill="none" stroke="var(--color-accent)" strokeWidth="0.75" strokeDasharray="2 3" opacity="0.35" />
          </svg>
          <div style={{ width: 3, height: 3, borderRadius: '50%', background: 'var(--color-accent)', boxShadow: '0 0 6px var(--color-accent)' }} />
        </div>

        <span style={{
          fontFamily: 'var(--font-hud)',
          fontSize: '0.68rem',
          fontWeight: 700,
          letterSpacing: '0.25em',
          color: 'var(--color-accent)',
          textTransform: 'uppercase',
          textShadow: '0 0 14px var(--color-accent-glow)',
        }}>
          J.A.R.V.I.S
        </span>

        {/* Mirror arc reactor */}
        <div className="relative flex items-center justify-center" style={{ width: 16, height: 16 }}>
          <svg width="16" height="16" viewBox="0 0 16 16" style={{ position: 'absolute', animation: 'jarvis-ring-ccw 4s linear infinite' }}>
            <circle cx="8" cy="8" r="6" fill="none" stroke="var(--color-accent)" strokeWidth="0.75" strokeDasharray="4 2" opacity="0.6" />
          </svg>
          <svg width="16" height="16" viewBox="0 0 16 16" style={{ position: 'absolute', animation: 'jarvis-ring-cw 3s linear infinite' }}>
            <circle cx="8" cy="8" r="4" fill="none" stroke="var(--color-accent)" strokeWidth="0.75" strokeDasharray="2 3" opacity="0.35" />
          </svg>
          <div style={{ width: 3, height: 3, borderRadius: '50%', background: 'var(--color-accent)', boxShadow: '0 0 6px var(--color-accent)' }} />
        </div>
      </div>

      {/* Right: date + clock */}
      <div
        className="z-10 relative flex items-center gap-3"
        style={{
          pointerEvents: 'none',
          fontFamily: 'var(--font-hud)',
          fontSize: '0.65rem',
          letterSpacing: '0.1em',
          minWidth: 110,
          justifyContent: 'flex-end',
        }}
      >
        {isTauri() && (
          <button
            onClick={toggleCompanion}
            className="p-1 rounded-md cursor-pointer"
            style={{ pointerEvents: 'auto', color: 'var(--color-text-tertiary)', background: 'transparent', border: 'none' }}
            title={`Companion mode (${IS_MAC ? '⌘⇧J' : 'Ctrl+Shift+J'})`}
          >
            <PictureInPicture2 size={13} />
          </button>
        )}
        <span style={{ color: 'var(--color-text-tertiary)', opacity: 0.7 }}>{dateStr}</span>
        <span className="hud-mono" style={{ color: 'var(--color-text-secondary)' }}>
          {hh}:{mm}:<span style={{ color: 'var(--color-accent)' }}>{ss}</span>
        </span>
      </div>
    </div>
  );
}
