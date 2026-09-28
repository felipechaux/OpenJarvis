import { Maximize2 } from 'lucide-react';
import { InputArea } from '../components/Chat/InputArea';
import { PresenceCore } from '../components/Chat/PresenceCore';
import { useAppStore } from '../lib/store';
import { isTauri } from '../lib/api';
import { usePresence, usePresenceLabel } from '../hooks/usePresence';
import { useCompanionMode } from '../hooks/useCompanionMode';

const IS_MAC = navigator.userAgent.includes('Mac');

/// Floating companion panel: the presence core, the latest exchange and the
/// input.  Rendered in the shrunk, always-on-top main window.
export function CompanionPage() {
  const presence = usePresence();
  const label = usePresenceLabel(presence);
  const messages = useAppStore((s) => s.messages);
  const { toggle } = useCompanionMode();

  const lastUser = [...messages].reverse().find((m) => m.role === 'user');
  const lastAssistant = [...messages].reverse().find((m) => m.role === 'assistant');

  return (
    <div className="companion flex flex-col h-full w-full overflow-hidden relative">
      <div className="hud-backdrop" aria-hidden="true" />

      {/* Drag strip — clears the native traffic lights on macOS */}
      <div
        className="relative flex items-center justify-end shrink-0 z-10"
        style={{ height: 38, paddingLeft: isTauri() && IS_MAC ? 96 : 12, paddingRight: 8 }}
      >
        <div data-tauri-drag-region className="absolute inset-0 z-0" />
        <button
          onClick={toggle}
          className="relative z-10 p-1.5 rounded-md cursor-pointer"
          style={{ color: 'var(--color-text-tertiary)', background: 'transparent', border: 'none' }}
          title={`${IS_MAC ? '⌘⇧J' : 'Ctrl+Shift+J'} · Expand`}
        >
          <Maximize2 size={14} />
        </button>
      </div>

      {/* Presence */}
      <div
        data-tauri-drag-region
        className="flex flex-col items-center shrink-0 pt-1 pb-3 z-10 select-none"
      >
        <PresenceCore size={176} />
        <span className={`companion-status companion-status-${presence}`}>{label}</span>
      </div>

      {/* Latest exchange */}
      <div className="companion-exchange flex-1 min-h-0 overflow-y-auto px-5 z-10">
        {lastUser && <p className="companion-user">{lastUser.content}</p>}
        {lastAssistant?.content && (
          <p key={lastAssistant.id} className="companion-reply">
            {lastAssistant.content}
          </p>
        )}
      </div>

      <div className="shrink-0 z-10">
        <InputArea />
      </div>
    </div>
  );
}
