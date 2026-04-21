import { useEffect, useState } from 'react';
import { isTauri } from '../lib/api';

export function TitleBar() {
  const [time, setTime] = useState(() => new Date());

  useEffect(() => {
    const t = setInterval(() => setTime(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  const close = async () => {
    if (!isTauri()) return;
    const { getCurrentWindow } = await import('@tauri-apps/api/window');
    getCurrentWindow().close();
  };

  const minimize = async () => {
    if (!isTauri()) return;
    const { getCurrentWindow } = await import('@tauri-apps/api/window');
    getCurrentWindow().minimize();
  };

  const maximize = async () => {
    if (!isTauri()) return;
    const { getCurrentWindow } = await import('@tauri-apps/api/window');
    const win = getCurrentWindow();
    if (await win.isMaximized()) win.unmaximize();
    else win.maximize();
  };

  const hh = String(time.getHours()).padStart(2, '0');
  const mm = String(time.getMinutes()).padStart(2, '0');
  const ss = String(time.getSeconds()).padStart(2, '0');

  return (
    <div
      className="titlebar flex items-center justify-between shrink-0 select-none"
      data-tauri-drag-region
      style={{
        height: 38,
        padding: '0 12px',
        background: 'rgba(2, 6, 14, 0.92)',
        borderBottom: '1px solid var(--color-border)',
      }}
    >
      {/* Left: window controls (Tauri only) or spacer */}
      {isTauri() ? (
        <div className="flex items-center gap-1.5 z-10" style={{ minWidth: 56 }}>
          <button onClick={close} className="jarvis-wbtn" style={{ background: '#ff5f57' }} title="Close" />
          <button onClick={minimize} className="jarvis-wbtn" style={{ background: '#febc2e' }} title="Minimize" />
          <button onClick={maximize} className="jarvis-wbtn" style={{ background: '#28c840' }} title="Maximize" />
        </div>
      ) : (
        <div style={{ minWidth: 56 }} />
      )}

      {/* Center: branding — drag region */}
      <div
        className="absolute left-1/2 -translate-x-1/2 flex items-center gap-2.5"
        data-tauri-drag-region
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

      {/* Right: clock */}
      <div style={{
        fontFamily: 'var(--font-hud)',
        fontSize: '0.65rem',
        color: 'var(--color-text-tertiary)',
        letterSpacing: '0.1em',
        minWidth: 70,
        textAlign: 'right',
      }}>
        {hh}:{mm}:{ss}
      </div>
    </div>
  );
}
