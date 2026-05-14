import { useRef, useEffect, useState } from 'react';
import { useNavigate } from 'react-router';
import { MessageBubble } from './MessageBubble';
import { InputArea } from './InputArea';
import { StreamingDots } from './StreamingDots';
import { ArcReactor } from './ArcReactor';
import { useAppStore } from '../../lib/store';
import { PanelRightOpen, PanelRightClose, Database, MessageSquare, X } from 'lucide-react';
import { listConnectors } from '../../lib/connectors-api';

function getGreeting(): string {
  const hour = new Date().getHours();
  if (hour < 12) return 'Good morning';
  if (hour < 18) return 'Good afternoon';
  return 'Good evening';
}

export function ChatArea() {
  const messages = useAppStore((s) => s.messages);
  const streamState = useAppStore((s) => s.streamState);
  const systemPanelOpen = useAppStore((s) => s.systemPanelOpen);
  const toggleSystemPanel = useAppStore((s) => s.toggleSystemPanel);
  const ttsSpeaking = useAppStore((s) => s.ttsSpeaking);
  const ttsAudioData = useAppStore((s) => s.ttsAudioData);
  const navigate = useNavigate();
  const listRef = useRef<HTMLDivElement>(null);
  const shouldAutoScroll = useRef(true);

  // Check if any data sources are connected
  const [hasConnectedSources, setHasConnectedSources] = useState<boolean | null>(null);
  const [bannerDismissed, setBannerDismissed] = useState(false);

  useEffect(() => {
    listConnectors()
      .then((list) => setHasConnectedSources(list.some((c) => c.connected)))
      .catch(() => setHasConnectedSources(null));
  }, []);

  useEffect(() => {
    if (shouldAutoScroll.current && listRef.current) {
      listRef.current.scrollTop = listRef.current.scrollHeight;
    }
  }, [messages, streamState.content]);

  const handleScroll = () => {
    if (!listRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = listRef.current;
    shouldAutoScroll.current = scrollHeight - scrollTop - clientHeight < 100;
  };

  const isEmpty = messages.length === 0 && !streamState.isStreaming;

  const PanelIcon = systemPanelOpen ? PanelRightClose : PanelRightOpen;

  return (
    <div className="flex flex-col h-full">
      {/* Toggle bar */}
      <div className="flex items-center justify-end px-3 py-1.5 shrink-0">
        <button
          onClick={toggleSystemPanel}
          className="p-1.5 rounded-md transition-colors cursor-pointer"
          style={{ color: 'var(--color-text-tertiary)' }}
          title={`${systemPanelOpen ? 'Hide' : 'Show'} system panel (${navigator.platform.includes('Mac') ? '⌘' : 'Ctrl'}+I)`}
        >
          <PanelIcon size={16} />
        </button>
      </div>

      {/* Data sources banner */}
      {hasConnectedSources === false && !bannerDismissed && (
        <div
          className="mx-4 mb-2 flex items-center gap-3 px-4 py-3 rounded-lg text-sm shrink-0"
          style={{
            background: 'var(--color-accent-subtle)',
            border: '1px solid var(--color-border)',
          }}
        >
          <Database size={16} style={{ color: 'var(--color-accent)', flexShrink: 0 }} />
          <span style={{ color: 'var(--color-text-secondary)', flex: 1 }}>
            Connect your data sources (Gmail, iMessage, Slack, etc.) to get personalized answers.
          </span>
          <button
            onClick={() => navigate('/data-sources')}
            className="px-3 py-1 rounded text-xs font-medium cursor-pointer"
            style={{ background: 'var(--color-accent)', color: 'var(--color-on-accent)', border: 'none' }}
          >
            Connect
          </button>
          <button
            onClick={() => setBannerDismissed(true)}
            className="p-1 rounded cursor-pointer"
            style={{ color: 'var(--color-text-tertiary)', background: 'transparent', border: 'none' }}
          >
            <X size={14} />
          </button>
        </div>
      )}
      <div
        ref={listRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto"
      >
        {isEmpty ? (
          /* ── Empty state: full-center arc reactor ── */
          <div className="flex flex-col items-center justify-center h-full px-4 select-none">
            <div style={{ marginBottom: 32, position: 'relative' }}>
              <ArcReactor size={260} streaming={streamState.isStreaming} audioData={ttsAudioData} />
              {/* Greeting floats below the reactor */}
            </div>

            <div className="flex flex-col items-center gap-1 mb-8" style={{ animation: 'jarvis-msg-in 0.5s ease-out both 0.2s', opacity: 0 }}>
              <h2
                style={{
                  fontFamily: 'var(--font-hud)',
                  fontSize: '1.05rem',
                  fontWeight: 700,
                  letterSpacing: '0.18em',
                  color: 'var(--color-accent)',
                  textShadow: '0 0 20px var(--color-accent-glow)',
                  textTransform: 'uppercase',
                }}
              >
                {getGreeting()}
              </h2>
              <p className="text-xs text-center max-w-xs" style={{ color: 'var(--color-text-tertiary)', letterSpacing: '0.05em' }}>
                ALL SYSTEMS OPERATIONAL · AWAITING INPUT
              </p>
            </div>

            {/* Quick action hints */}
            <div className="flex gap-3" style={{ animation: 'jarvis-msg-in 0.5s ease-out both 0.4s', opacity: 0 }}>
              <button
                onClick={() => navigate('/data-sources')}
                className="flex items-center gap-2 px-4 py-2.5 rounded-lg text-xs cursor-pointer transition-all"
                style={{
                  background: 'rgba(0,212,255,0.04)',
                  border: '1px solid rgba(0,212,255,0.18)',
                  color: 'var(--color-text-secondary)',
                  fontFamily: 'var(--font-hud)',
                  letterSpacing: '0.06em',
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.borderColor = 'rgba(0,212,255,0.5)';
                  e.currentTarget.style.background = 'rgba(0,212,255,0.08)';
                  e.currentTarget.style.boxShadow = '0 0 16px -4px rgba(0,212,255,0.3)';
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.borderColor = 'rgba(0,212,255,0.18)';
                  e.currentTarget.style.background = 'rgba(0,212,255,0.04)';
                  e.currentTarget.style.boxShadow = 'none';
                }}
              >
                <Database size={13} style={{ color: 'var(--color-accent)' }} />
                DATA SOURCES
              </button>
              <button
                onClick={() => { navigate('/data-sources'); setTimeout(() => window.dispatchEvent(new CustomEvent('switch-tab', { detail: 'messaging' })), 100); }}
                className="flex items-center gap-2 px-4 py-2.5 rounded-lg text-xs cursor-pointer transition-all"
                style={{
                  background: 'rgba(0,212,255,0.04)',
                  border: '1px solid rgba(0,212,255,0.18)',
                  color: 'var(--color-text-secondary)',
                  fontFamily: 'var(--font-hud)',
                  letterSpacing: '0.06em',
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.borderColor = 'rgba(0,212,255,0.5)';
                  e.currentTarget.style.background = 'rgba(0,212,255,0.08)';
                  e.currentTarget.style.boxShadow = '0 0 16px -4px rgba(0,212,255,0.3)';
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.borderColor = 'rgba(0,212,255,0.18)';
                  e.currentTarget.style.background = 'rgba(0,212,255,0.04)';
                  e.currentTarget.style.boxShadow = 'none';
                }}
              >
                <MessageSquare size={13} style={{ color: 'var(--color-accent)' }} />
                CHANNELS
              </button>
            </div>
          </div>
        ) : (
          /* ── Chat view: reactor lives as a faint centered background ── */
          <div className="relative">
            {/* Background reactor — always centered, fades behind messages */}
            <div
              style={{
                position: 'fixed',
                top: '50%',
                left: '50%',
                transform: 'translate(-50%, -50%)',
                opacity: streamState.isStreaming || ttsSpeaking ? 0.22 : 0.07,
                pointerEvents: 'none',
                zIndex: 0,
                transition: 'opacity 1s ease',
              }}
            >
              <ArcReactor size={340} streaming={streamState.isStreaming || ttsSpeaking} audioData={ttsAudioData} />
            </div>

            <div className="relative z-10 max-w-[var(--chat-max-width)] mx-auto px-4 py-6">
              {messages.map((msg) => (
                <MessageBubble key={msg.id} message={msg} />
              ))}
              {streamState.isStreaming && streamState.content === '' && (
                <div className="flex justify-start mb-4">
                  <StreamingDots phase={streamState.phase} />
                </div>
              )}
            </div>
          </div>
        )}
      </div>
      <InputArea />
    </div>
  );
}
