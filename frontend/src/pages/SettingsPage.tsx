import { useState, useEffect, useRef } from 'react';
import {
  Palette,
  Globe,
  Cpu,
  Database,
  Info,
  Check,
  Sun,
  Moon,
  Monitor,
  Download,
  Upload,
  Trash2,
  Mic,
  Key,
  Search,
  Brain,
} from 'lucide-react';
import { useAppStore, type ThemeMode } from '../lib/store';
import { checkHealth, fetchSpeechHealth, getMemoryStats, getBase } from '../lib/api';

function OllamaModelList() {
  const [models, setModels] = useState<Array<{ name: string; size: number }>>([]);
  useEffect(() => {
    fetch('http://localhost:11434/api/tags')
      .then(r => r.json())
      .then(data => setModels((data.models || []).map((m: any) => ({ name: m.name, size: m.size }))))
      .catch(() => setModels([]));
  }, []);
  if (models.length === 0) return <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>No models loaded</span>;
  return (
    <div className="flex flex-wrap gap-1">
      {models.map(m => (
        <span key={m.name} className="flex items-center gap-1 px-2 py-0.5 rounded text-[10px]"
          style={{ background: 'var(--color-bg-tertiary)', color: 'var(--color-text)' }}>
          <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--color-success)', display: 'inline-block' }} />
          {m.name} ({(m.size / 1e9).toFixed(1)} GB)
        </span>
      ))}
    </div>
  );
}

const _STORAGE_KEY_TO_ENV: Record<string, string> = {
  'openjarvis-openai-key': 'OPENAI_API_KEY',
  'openjarvis-anthropic-key': 'ANTHROPIC_API_KEY',
  'openjarvis-gemini-key': 'GEMINI_API_KEY',
  'openjarvis-openrouter-key': 'OPENROUTER_API_KEY',
  'openjarvis-minimax-key': 'MINIMAX_API_KEY',
  'openjarvis-nvidia-key': 'NVIDIA_API_KEY',
};

async function persistKeyToBackend(envName: string, value: string) {
  try {
    await fetch(`${getBase()}/v1/cloud/keys`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ env_name: envName, key_value: value }),
    });
  } catch { /* best-effort */ }
}

function ApiKeyInput({ storageKey, placeholder }: { storageKey: string; placeholder: string }) {
  const [value, setValue] = useState(() => {
    try { return localStorage.getItem(storageKey) || ''; } catch { return ''; }
  });
  const [saved, setSaved] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const handleChange = (v: string) => {
    setValue(v);
    try { if (v) localStorage.setItem(storageKey, v); else localStorage.removeItem(storageKey); } catch {}
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(async () => {
      const envName = _STORAGE_KEY_TO_ENV[storageKey];
      if (envName) await persistKeyToBackend(envName, v);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    }, 600);
  };

  return (
    <div className="flex items-center gap-2">
      <input type="password" value={value} onChange={e => handleChange(e.target.value)} placeholder={placeholder}
        className="w-48 px-2 py-1 rounded text-xs"
        style={{ background: 'var(--color-bg)', border: '1px solid var(--color-border)', color: 'var(--color-text)' }} />
      {saved && <span className="text-[10px]" style={{ color: 'var(--color-success)' }}>Saved</span>}
    </div>
  );
}

function CloudProviderStatus({ label, storageKey }: { label: string; storageKey: string }) {
  const [hasKey, setHasKey] = useState(() => {
    try { return !!localStorage.getItem(storageKey); } catch { return false; }
  });
  useEffect(() => {
    fetch(`${getBase()}/v1/cloud/keys/status`)
      .then(r => r.json())
      .then((status: Record<string, boolean>) => {
        const envName = _STORAGE_KEY_TO_ENV[storageKey];
        if (envName) setHasKey(!!status[envName]);
      })
      .catch(() => {
        try { setHasKey(!!localStorage.getItem(storageKey)); } catch { setHasKey(false); }
      });
  }, [storageKey]);
  return (
    <span className="flex items-center gap-1 text-xs" style={{ color: 'var(--color-text-secondary)' }}>
      <span style={{
        width: 6, height: 6, borderRadius: '50%', display: 'inline-block',
        background: hasKey ? 'var(--color-success)' : 'var(--color-text-tertiary)',
      }} />
      {label}
    </span>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div
      className="rounded-xl p-5"
      style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)' }}
    >
      <h3 className="text-sm font-semibold mb-4" style={{ color: 'var(--color-text)' }}>
        {title}
      </h3>
      {children}
    </div>
  );
}

function SettingRow({ label, description, children }: { label: string; description?: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between py-3" style={{ borderBottom: '1px solid var(--color-border-subtle)' }}>
      <div>
        <div className="text-sm" style={{ color: 'var(--color-text)' }}>{label}</div>
        {description && (
          <div className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>{description}</div>
        )}
      </div>
      <div>{children}</div>
    </div>
  );
}

const themeOptions: { value: ThemeMode; label: string; icon: typeof Sun }[] = [
  { value: 'light', label: 'Light', icon: Sun },
  { value: 'dark', label: 'Dark', icon: Moon },
  { value: 'system', label: 'System', icon: Monitor },
];

function Switch({ on, onToggle, disabled }: { on: boolean; onToggle: () => void; disabled?: boolean }) {
  return (
    <button
      onClick={onToggle}
      disabled={disabled}
      className="relative w-11 h-6 rounded-full transition-colors cursor-pointer disabled:opacity-40 disabled:cursor-default"
      style={{ background: on ? 'var(--color-accent)' : 'var(--color-bg-tertiary)' }}
    >
      <span
        className="absolute top-0.5 left-0.5 w-5 h-5 rounded-full transition-transform bg-white"
        style={{
          transform: on ? 'translateX(20px)' : 'translateX(0)',
          boxShadow: '0 1px 3px rgba(0,0,0,0.2)',
        }}
      />
    </button>
  );
}

/// Launch at login (tauri-plugin-autostart: a LaunchAgent for this app's
/// executable).  A dev build would register target/debug, which needs vite
/// running, so it is only offered in the installed app.
function LaunchAtLoginRow() {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [error, setError] = useState('');
  const dev = import.meta.env.DEV;

  useEffect(() => {
    import('@tauri-apps/plugin-autostart')
      .then(({ isEnabled }) => isEnabled())
      .then(setEnabled)
      .catch(() => setEnabled(false));
  }, []);

  const toggle = async () => {
    setError('');
    try {
      const api = await import('@tauri-apps/plugin-autostart');
      if (enabled) await api.disable();
      else await api.enable();
      setEnabled(await api.isEnabled());
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <SettingRow
      label="Iniciar con el Mac / Launch at login"
      description={
        dev
          ? 'Disponible en la app instalada (en desarrollo apuntaría al binario de debug, que necesita vite).'
          : error || 'JARVIS arranca en el notch al encender o iniciar sesión en tu Mac.'
      }
    >
      <Switch on={!!enabled} onToggle={toggle} disabled={dev || enabled === null} />
    </SettingRow>
  );
}

interface PairPayload { urls: string[]; expires_at: number; name: string }
interface MobileDevice { id: string; name: string; platform: string; last_seen: number; push: { provider?: string } }

/// Pair the JARVIS phone app: shows the one-time QR from
/// ``POST /v1/mobile/pair/start`` (same as ``jarvis mobile pair``) and the
/// paired devices.  Loopback requests from this app need no token.
function MobilePairingSection() {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [qr, setQr] = useState<{ svg: string | null; payload: PairPayload } | null>(null);
  const [devices, setDevices] = useState<MobileDevice[]>([]);
  const [now, setNow] = useState(() => Date.now() / 1000);
  const [error, setError] = useState('');

  const loadDevices = () =>
    fetch(`${getBase()}/v1/mobile/devices`)
      .then((r) => (r.ok ? r.json() : { devices: [] }))
      .then((d) => setDevices(d.devices ?? []))
      .catch(() => {});

  useEffect(() => {
    fetch(`${getBase()}/v1/mobile/hello`)
      .then((r) => {
        setEnabled(r.ok);
        if (r.ok) loadDevices();
      })
      .catch(() => setEnabled(false));
  }, []);

  // Countdown for the code; refresh the device list while the QR is up so
  // a freshly paired phone appears without reloading.
  useEffect(() => {
    if (!qr) return;
    const id = setInterval(() => {
      setNow(Date.now() / 1000);
      loadDevices();
    }, 2000);
    return () => clearInterval(id);
  }, [qr]);

  const generate = async () => {
    setError('');
    try {
      const r = await fetch(`${getBase()}/v1/mobile/pair/start`, { method: 'POST' });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const body = await r.json();
      setQr({ svg: body.qr_svg, payload: body.payload });
      setNow(Date.now() / 1000);
    } catch (e) {
      setError(String(e));
    }
  };

  const revoke = async (id: string) => {
    await fetch(`${getBase()}/v1/mobile/devices/${id}`, { method: 'DELETE' }).catch(() => {});
    loadDevices();
  };

  const remaining = qr ? Math.max(0, Math.round(qr.payload.expires_at - now)) : 0;
  const muted = { color: 'var(--color-text-tertiary)' };

  return (
    <Section title="Teléfono / Mobile app">
      {enabled === false && (
        <div className="text-xs" style={muted}>
          La app móvil está desactivada. Ejecuta <code>jarvis mobile enable</code> y reinicia JARVIS.
        </div>
      )}
      {enabled && (
        <>
          <SettingRow
            label="Emparejar un teléfono"
            description="Escanea el código con la app JARVIS. Vale 5 minutos y sirve una sola vez."
          >
            <button
              onClick={generate}
              className="text-sm px-3 py-1.5 rounded-lg cursor-pointer"
              style={{ background: 'var(--color-accent)', color: 'white' }}
            >
              {qr && remaining > 0 ? 'Nuevo código' : 'Mostrar QR'}
            </button>
          </SettingRow>
          {error && <div className="text-xs mt-2" style={{ color: 'var(--color-error)' }}>{error}</div>}
          {qr && remaining > 0 && (
            <div className="flex flex-col items-center gap-2 py-4">
              {qr.svg ? (
                <div className="rounded-lg overflow-hidden bg-white p-2" dangerouslySetInnerHTML={{ __html: qr.svg }} />
              ) : (
                <div className="text-xs" style={muted}>
                  Instala el extra <code>mobile</code> para ver el QR (uv sync --extra mobile).
                </div>
              )}
              <div className="text-xs" style={muted}>
                Expira en {Math.floor(remaining / 60)}:{String(remaining % 60).padStart(2, '0')} ·{' '}
                {qr.payload.urls.join(' · ') || 'sin dirección accesible'}
              </div>
            </div>
          )}
          {devices.map((d) => (
            <SettingRow
              key={d.id}
              label={`${d.name} (${d.platform})`}
              description={`${d.push.provider ? `push ${d.push.provider}` : 'sin push'} · ${
                d.last_seen ? `visto ${new Date(d.last_seen * 1000).toLocaleString()}` : 'nunca visto'
              }`}
            >
              <button
                onClick={() => revoke(d.id)}
                className="text-xs px-2 py-1 rounded-lg cursor-pointer"
                style={{ color: 'var(--color-error)', border: '1px solid var(--color-border)' }}
              >
                Desvincular
              </button>
            </SettingRow>
          ))}
        </>
      )}
    </Section>
  );
}

export function SettingsPage() {
  const settings = useAppStore((s) => s.settings);
  const updateSettings = useAppStore((s) => s.updateSettings);
  const conversations = useAppStore((s) => s.conversations);
  const serverInfo = useAppStore((s) => s.serverInfo);
  const [healthy, setHealthy] = useState<boolean | null>(null);
  const [speechBackendAvailable, setSpeechBackendAvailable] = useState<boolean | null>(null);
  const [saved, setSaved] = useState(false);

  const [memoryStats, setMemoryStats] = useState<{ entries: number; backend: string } | null>(null);
  const [memoryEnabled, setMemoryEnabled] = useState(() => {
    try { return localStorage.getItem('openjarvis-memory-enabled') !== 'false'; } catch { return true; }
  });
  const [memoryBackend, setMemoryBackend] = useState(() => {
    try { return localStorage.getItem('openjarvis-memory-backend') || 'sqlite'; } catch { return 'sqlite'; }
  });
  const [memoryTopK, setMemoryTopK] = useState(() => {
    try { return parseInt(localStorage.getItem('openjarvis-memory-top-k') || '5'); } catch { return 5; }
  });
  const [memoryMinScore, setMemoryMinScore] = useState(() => {
    try { return parseFloat(localStorage.getItem('openjarvis-memory-min-score') || '0.1'); } catch { return 0.1; }
  });
  const [memoryMaxTokens, setMemoryMaxTokens] = useState(() => {
    try { return parseInt(localStorage.getItem('openjarvis-memory-max-tokens') || '2048'); } catch { return 2048; }
  });

  useEffect(() => {
    checkHealth().then(setHealthy);
    fetchSpeechHealth()
      .then((h) => setSpeechBackendAvailable(h.available))
      .catch(() => setSpeechBackendAvailable(false));
    getMemoryStats()
      .then(setMemoryStats)
      .catch(() => setMemoryStats(null));

    // Sync any API keys already in localStorage to the backend file.
    fetch(`${getBase()}/v1/cloud/keys/status`)
      .then(r => r.json())
      .then(async (status: Record<string, boolean>) => {
        for (const [storageKey, envName] of Object.entries(_STORAGE_KEY_TO_ENV)) {
          if (!status[envName]) {
            const stored = localStorage.getItem(storageKey);
            if (stored) await persistKeyToBackend(envName, stored);
          }
        }
      })
      .catch(() => {});
  }, []);

  const showSaved = () => {
    setSaved(true);
    setTimeout(() => setSaved(false), 1500);
  };

  const handleExport = () => {
    const data = localStorage.getItem('openjarvis-conversations') || '{}';
    const blob = new Blob([data], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `openjarvis-export-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleImport = () => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.json';
    input.onchange = (e) => {
      const file = (e.target as HTMLInputElement).files?.[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = (ev) => {
        try {
          const data = JSON.parse(ev.target?.result as string);
          if (data.version === 1) {
            localStorage.setItem('openjarvis-conversations', JSON.stringify(data));
            useAppStore.getState().loadConversations();
            showSaved();
          }
        } catch {}
      };
      reader.readAsText(file);
    };
    input.click();
  };

  const [confirmClear, setConfirmClear] = useState(false);
  const handleClear = () => {
    if (!confirmClear) {
      setConfirmClear(true);
      setTimeout(() => setConfirmClear(false), 3000);
      return;
    }
    localStorage.removeItem('openjarvis-conversations');
    useAppStore.getState().loadConversations();
    setConfirmClear(false);
    showSaved();
  };

  return (
    <div className="flex-1 overflow-y-auto px-6 py-10">
      <div className="max-w-2xl mx-auto">
        <header className="mb-6">
          <div className="flex items-center justify-between gap-3">
            <h1 className="text-lg font-semibold" style={{ color: 'var(--color-text)' }}>
              Settings
            </h1>
            {saved && (
              <span className="flex items-center gap-1 text-xs px-2 py-1 rounded-full" style={{
                background: 'var(--color-accent-subtle)',
                color: 'var(--color-success)',
              }}>
                <Check size={12} /> Saved
              </span>
            )}
          </div>
          <p className="text-sm mt-2 max-w-2xl" style={{ color: 'var(--color-text-secondary)' }}>
            App preferences — appearance, model defaults, keyboard shortcuts, and data management.
          </p>
        </header>

        <div className="flex flex-col gap-4">
          {/* Appearance */}
          <Section title="Appearance">
            <SettingRow label="Theme" description="Choose how OpenJarvis looks">
              <div className="flex gap-1 p-0.5 rounded-lg" style={{ background: 'var(--color-bg-secondary)' }}>
                {themeOptions.map((opt) => {
                  const isActive = settings.theme === opt.value;
                  return (
                    <button
                      key={opt.value}
                      onClick={() => { updateSettings({ theme: opt.value }); showSaved(); }}
                      className="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-colors cursor-pointer"
                      style={{
                        background: isActive ? 'var(--color-surface)' : 'transparent',
                        color: isActive ? 'var(--color-text)' : 'var(--color-text-tertiary)',
                        boxShadow: isActive ? 'var(--shadow-sm)' : 'none',
                      }}
                    >
                      <opt.icon size={14} />
                      {opt.label}
                    </button>
                  );
                })}
              </div>
            </SettingRow>
            <SettingRow label="Font size">
              <select
                value={settings.fontSize}
                onChange={(e) => { updateSettings({ fontSize: e.target.value as any }); showSaved(); }}
                className="text-sm px-3 py-1.5 rounded-lg outline-none cursor-pointer"
                style={{
                  background: 'var(--color-bg-secondary)',
                  color: 'var(--color-text)',
                  border: '1px solid var(--color-border)',
                }}
              >
                <option value="small">Small</option>
                <option value="default">Default</option>
                <option value="large">Large</option>
              </select>
            </SettingRow>
          </Section>

          {/* Connection */}
          <Section title="Connection">
            <SettingRow label="Server status" description={serverInfo ? `${serverInfo.engine} / ${serverInfo.model}` : 'Not connected'}>
              <div className="flex items-center gap-2">
                <span
                  className="w-2 h-2 rounded-full"
                  style={{ background: healthy === true ? 'var(--color-success)' : healthy === false ? 'var(--color-error)' : 'var(--color-text-tertiary)' }}
                />
                <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                  {healthy === true ? 'Connected' : healthy === false ? 'Disconnected' : 'Checking...'}
                </span>
              </div>
            </SettingRow>
            <SettingRow label="API URL" description="Set if backend runs on a different port or host">
              <input
                type="text"
                value={settings.apiUrl}
                onChange={(e) => { updateSettings({ apiUrl: e.target.value }); showSaved(); }}
                placeholder="http://localhost:8000"
                className="text-sm px-3 py-1.5 rounded-lg outline-none w-56"
                style={{
                  background: 'var(--color-bg-secondary)',
                  color: 'var(--color-text)',
                  border: '1px solid var(--color-border)',
                }}
              />
            </SettingRow>
          </Section>

          <MobilePairingSection />

          {/* Models */}
          <Section title="Models">
            <SettingRow label="Local models (Ollama)" description="Models available for local inference">
              <OllamaModelList />
            </SettingRow>
            <div className="text-xs mt-2 px-1" style={{ color: 'var(--color-text-tertiary)' }}>
              Run <code className="px-1 py-0.5 rounded text-[11px]" style={{ background: 'var(--color-bg-tertiary)' }}>ollama pull &lt;model-name&gt;</code> in your terminal to add more models
            </div>
            <SettingRow label="Cloud providers" description="Green dot means API key is configured">
              <div className="flex flex-wrap gap-3">
                <CloudProviderStatus label="OpenAI" storageKey="openjarvis-openai-key" />
                <CloudProviderStatus label="Anthropic" storageKey="openjarvis-anthropic-key" />
                <CloudProviderStatus label="Google" storageKey="openjarvis-gemini-key" />
                <CloudProviderStatus label="OpenRouter" storageKey="openjarvis-openrouter-key" />
                <CloudProviderStatus label="MiniMax" storageKey="openjarvis-minimax-key" />
                <CloudProviderStatus label="NVIDIA" storageKey="openjarvis-nvidia-key" />
              </div>
            </SettingRow>
          </Section>

          {/* API Keys */}
          <Section title="API Keys">
            <SettingRow label="OpenAI" description="GPT-4, GPT-3.5, etc.">
              <ApiKeyInput storageKey="openjarvis-openai-key" placeholder="sk-..." />
            </SettingRow>
            <SettingRow label="Anthropic" description="Claude models">
              <ApiKeyInput storageKey="openjarvis-anthropic-key" placeholder="sk-ant-..." />
            </SettingRow>
            <SettingRow label="Google" description="Gemini models">
              <ApiKeyInput storageKey="openjarvis-gemini-key" placeholder="AI..." />
            </SettingRow>
            <SettingRow label="OpenRouter" description="Multi-provider routing">
              <ApiKeyInput storageKey="openjarvis-openrouter-key" placeholder="sk-or-..." />
            </SettingRow>
            <SettingRow label="MiniMax" description="MiniMax models">
              <ApiKeyInput storageKey="openjarvis-minimax-key" placeholder="API key..." />
            </SettingRow>
            <SettingRow label="NVIDIA" description="NVIDIA models">
              <ApiKeyInput storageKey="openjarvis-nvidia-key" placeholder="nvapi-..." />
            </SettingRow>
          </Section>

          {/* Tools */}
          <Section title="Tools">
            <SettingRow label="Web Search" description="SerpAPI or Tavily key for web search tool">
              <ApiKeyInput storageKey="openjarvis-search-key" placeholder="API key..." />
            </SettingRow>
          </Section>

          {/* Memory */}
          <Section title="Memory">
            <SettingRow label="Memory status" description={memoryStats ? `${memoryStats.backend} backend — ${memoryStats.entries} entries` : 'Unable to reach memory service'}>
              <div className="flex items-center gap-2">
                <Brain size={14} style={{ color: memoryStats ? 'var(--color-accent)' : 'var(--color-text-tertiary)' }} />
                <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                  {memoryStats ? `${memoryStats.entries} entries` : 'Unavailable'}
                </span>
              </div>
            </SettingRow>
            <SettingRow label="Use memory context" description="Automatically inject relevant memories into conversations">
              <button
                onClick={() => {
                  const next = !memoryEnabled;
                  setMemoryEnabled(next);
                  try { localStorage.setItem('openjarvis-memory-enabled', String(next)); } catch {}
                  showSaved();
                }}
                className="relative w-11 h-6 rounded-full transition-colors cursor-pointer"
                style={{
                  background: memoryEnabled ? 'var(--color-accent)' : 'var(--color-bg-tertiary)',
                }}
              >
                <span
                  className="absolute top-0.5 left-0.5 w-5 h-5 rounded-full transition-transform bg-white"
                  style={{
                    transform: memoryEnabled ? 'translateX(20px)' : 'translateX(0)',
                    boxShadow: '0 1px 3px rgba(0,0,0,0.2)',
                  }}
                />
              </button>
            </SettingRow>
            <SettingRow label="Memory backend" description="Which retrieval engine to use">
              <select
                value={memoryBackend}
                onChange={(e) => {
                  setMemoryBackend(e.target.value);
                  try { localStorage.setItem('openjarvis-memory-backend', e.target.value); } catch {}
                  showSaved();
                }}
                className="text-sm px-3 py-1.5 rounded-lg outline-none cursor-pointer"
                style={{
                  background: 'var(--color-bg-secondary)',
                  color: 'var(--color-text)',
                  border: '1px solid var(--color-border)',
                }}
              >
                <option value="sqlite">sqlite</option>
                <option value="faiss">faiss</option>
                <option value="bm25">bm25</option>
                <option value="colbert">colbert</option>
                <option value="hybrid">hybrid</option>
              </select>
            </SettingRow>
            <SettingRow label="Results to inject" description={`${memoryTopK}`}>
              <input
                type="range"
                min="1"
                max="20"
                step="1"
                value={memoryTopK}
                onChange={(e) => {
                  const v = parseInt(e.target.value);
                  setMemoryTopK(v);
                  try { localStorage.setItem('openjarvis-memory-top-k', String(v)); } catch {}
                  showSaved();
                }}
                className="w-32 cursor-pointer accent-[var(--color-accent)]"
              />
            </SettingRow>
            <SettingRow label="Min relevance score" description={`${memoryMinScore}`}>
              <input
                type="range"
                min="0"
                max="1"
                step="0.05"
                value={memoryMinScore}
                onChange={(e) => {
                  const v = parseFloat(e.target.value);
                  setMemoryMinScore(v);
                  try { localStorage.setItem('openjarvis-memory-min-score', String(v)); } catch {}
                  showSaved();
                }}
                className="w-32 cursor-pointer accent-[var(--color-accent)]"
              />
            </SettingRow>
            <SettingRow label="Max context tokens" description={`${memoryMaxTokens}`}>
              <input
                type="range"
                min="256"
                max="8192"
                step="256"
                value={memoryMaxTokens}
                onChange={(e) => {
                  const v = parseInt(e.target.value);
                  setMemoryMaxTokens(v);
                  try { localStorage.setItem('openjarvis-memory-max-tokens', String(v)); } catch {}
                  showSaved();
                }}
                className="w-32 cursor-pointer accent-[var(--color-accent)]"
              />
            </SettingRow>
          </Section>

          {/* Model defaults */}
          <Section title="Model Defaults">
            <SettingRow label="Temperature" description={`${settings.temperature}`}>
              <input
                type="range"
                min="0"
                max="2"
                step="0.1"
                value={settings.temperature}
                onChange={(e) => { updateSettings({ temperature: parseFloat(e.target.value) }); showSaved(); }}
                className="w-32 cursor-pointer accent-[var(--color-accent)]"
              />
            </SettingRow>
            <SettingRow label="Max tokens" description={`${settings.maxTokens}`}>
              <input
                type="range"
                min="256"
                max="32768"
                step="256"
                value={settings.maxTokens}
                onChange={(e) => { updateSettings({ maxTokens: parseInt(e.target.value) }); showSaved(); }}
                className="w-32 cursor-pointer accent-[var(--color-accent)]"
              />
            </SettingRow>
          </Section>

          {/* Speech */}
          <Section title="Speech">
            <SettingRow
              label="Idioma / Language"
              description="Primary language for voice, transcription and replies"
            >
              <select
                value={settings.language}
                onChange={(e) => { updateSettings({ language: e.target.value as any }); showSaved(); }}
                className="text-sm px-3 py-1.5 rounded-lg outline-none cursor-pointer"
                style={{
                  background: 'var(--color-bg-secondary)',
                  color: 'var(--color-text)',
                  border: '1px solid var(--color-border)',
                }}
              >
                <option value="es">Español (principal)</option>
                <option value="en">English</option>
                <option value="auto">Auto-detect</option>
              </select>
            </SettingRow>
            <SettingRow label="Speech-to-Text" description="Enable microphone input for voice dictation">
              <button
                onClick={() => { updateSettings({ speechEnabled: !settings.speechEnabled }); showSaved(); }}
                className="relative w-11 h-6 rounded-full transition-colors cursor-pointer"
                style={{
                  background: settings.speechEnabled ? 'var(--color-accent)' : 'var(--color-bg-tertiary)',
                }}
              >
                <span
                  className="absolute top-0.5 left-0.5 w-5 h-5 rounded-full transition-transform bg-white"
                  style={{
                    transform: settings.speechEnabled ? 'translateX(20px)' : 'translateX(0)',
                    boxShadow: '0 1px 3px rgba(0,0,0,0.2)',
                  }}
                />
              </button>
            </SettingRow>
            <SettingRow label="Text-to-Speech" description="Jarvis speaks responses aloud using Kokoro (local, open-source)">
              <button
                onClick={() => { updateSettings({ ttsEnabled: !settings.ttsEnabled }); showSaved(); }}
                className="relative w-11 h-6 rounded-full transition-colors cursor-pointer"
                style={{ background: settings.ttsEnabled ? 'var(--color-accent)' : 'var(--color-bg-tertiary)' }}
              >
                <span
                  className="absolute top-0.5 left-0.5 w-5 h-5 rounded-full transition-transform bg-white"
                  style={{
                    transform: settings.ttsEnabled ? 'translateX(20px)' : 'translateX(0)',
                    boxShadow: '0 1px 3px rgba(0,0,0,0.2)',
                  }}
                />
              </button>
            </SettingRow>
            <SettingRow
              label="Voz J.A.R.V.I.S. / Voice FX"
              description="Subtle holographic polish on spoken replies (EQ, light compression, faint reverb)"
            >
              <button
                onClick={() => { updateSettings({ voiceFx: !settings.voiceFx }); showSaved(); }}
                className="relative w-11 h-6 rounded-full transition-colors cursor-pointer"
                style={{ background: settings.voiceFx ? 'var(--color-accent)' : 'var(--color-bg-tertiary)' }}
              >
                <span
                  className="absolute top-0.5 left-0.5 w-5 h-5 rounded-full transition-transform bg-white"
                  style={{
                    transform: settings.voiceFx ? 'translateX(20px)' : 'translateX(0)',
                    boxShadow: '0 1px 3px rgba(0,0,0,0.2)',
                  }}
                />
              </button>
            </SettingRow>
            <SettingRow
              label="Efectos de sonido / Sound FX"
              description="Interface chimes when JARVIS wakes up, starts processing and has a reply"
            >
              <button
                onClick={() => { updateSettings({ earcons: !settings.earcons }); showSaved(); }}
                className="relative w-11 h-6 rounded-full transition-colors cursor-pointer"
                style={{ background: settings.earcons ? 'var(--color-accent)' : 'var(--color-bg-tertiary)' }}
              >
                <span
                  className="absolute top-0.5 left-0.5 w-5 h-5 rounded-full transition-transform bg-white"
                  style={{
                    transform: settings.earcons ? 'translateX(20px)' : 'translateX(0)',
                    boxShadow: '0 1px 3px rgba(0,0,0,0.2)',
                  }}
                />
              </button>
            </SettingRow>
            <SettingRow
              label="HUD al hablar / Speaking HUD"
              description="Full-screen Iron Man HUD while JARVIS speaks (Esc hides it for that reply)"
            >
              <button
                onClick={() => { updateSettings({ speakingHud: !settings.speakingHud }); showSaved(); }}
                className="relative w-11 h-6 rounded-full transition-colors cursor-pointer"
                style={{ background: settings.speakingHud ? 'var(--color-accent)' : 'var(--color-bg-tertiary)' }}
              >
                <span
                  className="absolute top-0.5 left-0.5 w-5 h-5 rounded-full transition-transform bg-white"
                  style={{
                    transform: settings.speakingHud ? 'translateX(20px)' : 'translateX(0)',
                    boxShadow: '0 1px 3px rgba(0,0,0,0.2)',
                  }}
                />
              </button>
            </SettingRow>
            <SettingRow
              label="Intro al iniciar / Launch intro"
              description="Arc reactor power-up with its sound when JARVIS starts (click or Esc skips it)"
            >
              <button
                onClick={() => { updateSettings({ bootIntro: !settings.bootIntro }); showSaved(); }}
                className="relative w-11 h-6 rounded-full transition-colors cursor-pointer"
                style={{ background: settings.bootIntro ? 'var(--color-accent)' : 'var(--color-bg-tertiary)' }}
              >
                <span
                  className="absolute top-0.5 left-0.5 w-5 h-5 rounded-full transition-transform bg-white"
                  style={{
                    transform: settings.bootIntro ? 'translateX(20px)' : 'translateX(0)',
                    boxShadow: '0 1px 3px rgba(0,0,0,0.2)',
                  }}
                />
              </button>
            </SettingRow>
            <SettingRow
              label="Abrir la ventana al iniciar / Open window on launch"
              description="Apagado: JARVIS arranca solo en el notch; esta ventana se abre con ⌘⇧J, el botón ⤢ del notch o el menú de la barra"
            >
              <Switch
                on={settings.openWindowOnLaunch}
                onToggle={() => { updateSettings({ openWindowOnLaunch: !settings.openWindowOnLaunch }); showSaved(); }}
              />
            </SettingRow>
            <LaunchAtLoginRow />
            <SettingRow label="Backend status" description="Requires Whisper, Deepgram, or another speech backend">
              <div className="flex items-center gap-2">
                <span
                  className="w-2 h-2 rounded-full"
                  style={{
                    background: speechBackendAvailable === true ? 'var(--color-success)'
                      : speechBackendAvailable === false ? 'var(--color-text-tertiary)'
                      : 'var(--color-text-tertiary)',
                  }}
                />
                <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                  {speechBackendAvailable === null ? 'Checking...'
                    : speechBackendAvailable ? 'Available'
                    : 'Not configured'}
                </span>
              </div>
            </SettingRow>
            {!speechBackendAvailable && speechBackendAvailable !== null && (
              <div className="text-xs mt-2 px-1" style={{ color: 'var(--color-text-tertiary)' }}>
                Set up a speech backend to use voice input.
                See the <a href="https://open-jarvis.github.io/OpenJarvis/user-guide/tools/" target="_blank" rel="noopener noreferrer" style={{ color: 'var(--color-accent)' }}>documentation</a> for details.
              </div>
            )}
          </Section>

          {/* Data */}
          <Section title="Data">
            <SettingRow label="Conversations" description={`${conversations.length} stored locally`}>
              <div className="flex gap-2">
                <button
                  onClick={handleExport}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors cursor-pointer"
                  style={{ background: 'var(--color-bg-secondary)', color: 'var(--color-text-secondary)', border: '1px solid var(--color-border)' }}
                  onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--color-bg-tertiary)')}
                  onMouseLeave={(e) => (e.currentTarget.style.background = 'var(--color-bg-secondary)')}
                >
                  <Download size={12} /> Export
                </button>
                <button
                  onClick={handleImport}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors cursor-pointer"
                  style={{ background: 'var(--color-bg-secondary)', color: 'var(--color-text-secondary)', border: '1px solid var(--color-border)' }}
                  onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--color-bg-tertiary)')}
                  onMouseLeave={(e) => (e.currentTarget.style.background = 'var(--color-bg-secondary)')}
                >
                  <Upload size={12} /> Import
                </button>
              </div>
            </SettingRow>
            <SettingRow label="Clear all data" description="Permanently delete all conversations">
              <button
                onClick={handleClear}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors cursor-pointer"
                style={{
                  color: confirmClear ? 'white' : 'var(--color-error)',
                  background: confirmClear ? 'var(--color-error)' : 'transparent',
                  border: '1px solid var(--color-error)',
                }}
                onMouseEnter={(e) => { if (!confirmClear) e.currentTarget.style.background = 'rgba(220,38,38,0.1)'; }}
                onMouseLeave={(e) => { if (!confirmClear) e.currentTarget.style.background = 'transparent'; }}
              >
                <Trash2 size={12} /> {confirmClear ? 'Click again to confirm' : 'Clear'}
              </button>
            </SettingRow>
          </Section>

          {/* About */}
          <Section title="About">
            <div className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
              <p className="mb-2">
                <span className="font-semibold" style={{ color: 'var(--color-text)' }}>OpenJarvis</span> — Programming abstractions for on-device AI.
              </p>
              <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                Part of Intelligence Per Watt, a research initiative at Stanford SAIL.
              </p>
              <div className="flex gap-3 mt-3 text-xs">
                <a
                  href="https://scalingintelligence.stanford.edu/blogs/openjarvis/"
                  target="_blank"
                  rel="noopener noreferrer"
                  style={{ color: 'var(--color-accent)' }}
                >
                  Project site
                </a>
                <a
                  href="https://open-jarvis.github.io/OpenJarvis/"
                  target="_blank"
                  rel="noopener noreferrer"
                  style={{ color: 'var(--color-accent)' }}
                >
                  Documentation
                </a>
              </div>
            </div>
          </Section>
        </div>
      </div>
    </div>
  );
}
