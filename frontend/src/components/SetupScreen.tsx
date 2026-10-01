import { useState, useEffect, useCallback } from 'react';
import { Loader2, CheckCircle2, XCircle, Cpu, Server, Database, Terminal } from 'lucide-react';
import { chooseEngine, getSetupStatus, type CliOption, type SetupStatus } from '../lib/api';

const STEPS = [
  { key: 'ollama_ready', label: 'Inference Engine', icon: Cpu, detail: 'Starting Ollama...' },
  { key: 'model_ready', label: 'AI Model', icon: Database, detail: 'Loading model...' },
  { key: 'server_ready', label: 'API Server', icon: Server, detail: 'Starting server...' },
] as const;

type StepKey = (typeof STEPS)[number]['key'];

function StepRow({
  icon: Icon,
  label,
  done,
  active,
  detail,
}: {
  icon: typeof Cpu;
  label: string;
  done: boolean;
  active: boolean;
  detail: string;
}) {
  return (
    <div
      className="flex items-center gap-4 px-5 py-4 rounded-xl transition-all"
      style={{
        background: done
          ? 'var(--color-accent-subtle)'
          : active
            ? 'var(--color-surface)'
            : 'transparent',
        border: active ? '1px solid var(--color-border)' : '1px solid transparent',
      }}
    >
      <div
        className="w-10 h-10 rounded-lg flex items-center justify-center shrink-0"
        style={{
          background: done ? 'var(--color-accent)' : 'var(--color-bg-tertiary)',
          color: done ? 'white' : 'var(--color-text-tertiary)',
        }}
      >
        <Icon size={18} />
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium" style={{ color: 'var(--color-text)' }}>
          {label}
        </div>
        <div className="text-xs truncate" style={{ color: 'var(--color-text-tertiary)', maxWidth: '280px' }}>
          {done ? 'Ready' : active ? detail : 'Waiting...'}
        </div>
      </div>
      <div className="shrink-0">
        {done ? (
          <CheckCircle2 size={18} style={{ color: 'var(--color-accent)' }} />
        ) : active ? (
          <Loader2 size={18} className="animate-spin" style={{ color: 'var(--color-accent)' }} />
        ) : (
          <div
            className="w-4 h-4 rounded-full"
            style={{ border: '2px solid var(--color-border)' }}
          />
        )}
      </div>
    </div>
  );
}

function EngineChoice({
  options,
  onChoose,
}: {
  options: CliOption[];
  onChoose: (key: string) => void;
}) {
  const [picked, setPicked] = useState<string | null>(null);
  const pick = (key: string) => {
    if (picked) return;
    setPicked(key);
    onChoose(key);
  };
  const choices = [
    ...options.map((o) => ({ key: o.key, label: o.label, detail: `Usa tu suscripción · ${o.model}`, icon: Terminal })),
    { key: 'ollama', label: 'Modelo local (Ollama)', detail: 'Descarga un modelo pequeño y corre sin internet', icon: Cpu },
  ];
  return (
    <div className="flex flex-col gap-2 mb-8">
      <p className="text-sm mb-2 text-center" style={{ color: 'var(--color-text-secondary)' }}>
        Encontré estos CLIs instalados. ¿Con cuál quieres que piense JARVIS?
      </p>
      {choices.map(({ key, label, detail, icon: Icon }) => (
        <button
          key={key}
          type="button"
          disabled={picked !== null}
          onClick={() => pick(key)}
          className="flex items-center gap-4 px-5 py-4 rounded-xl text-left transition-all disabled:opacity-60"
          style={{
            background: picked === key ? 'var(--color-accent-subtle)' : 'var(--color-surface)',
            border: '1px solid var(--color-border)',
            cursor: picked ? 'default' : 'pointer',
          }}
        >
          <div
            className="w-10 h-10 rounded-lg flex items-center justify-center shrink-0"
            style={{ background: 'var(--color-bg-tertiary)', color: 'var(--color-accent)' }}
          >
            <Icon size={18} />
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-sm font-medium" style={{ color: 'var(--color-text)' }}>
              {label}
            </div>
            <div className="text-xs truncate" style={{ color: 'var(--color-text-tertiary)' }}>
              {detail}
            </div>
          </div>
          {picked === key && (
            <Loader2 size={18} className="animate-spin shrink-0" style={{ color: 'var(--color-accent)' }} />
          )}
        </button>
      ))}
      <p className="text-xs mt-2 text-center" style={{ color: 'var(--color-text-tertiary)' }}>
        Puedes cambiar de modelo después desde el chat.
      </p>
    </div>
  );
}

export function SetupScreen({ onReady }: { onReady: () => void }) {
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const choosing = status?.phase === 'choose_engine' && !!status.cli_options?.length;
  const poll = useCallback(async () => {
    const s = await getSetupStatus();
    if (s) setStatus(s);
    if (s?.phase === 'ready') {
      setTimeout(() => onReady(), 600);
    }
  }, [onReady]);

  useEffect(() => {
    poll();
    const interval = setInterval(poll, 800);
    return () => clearInterval(interval);
  }, [poll]);

  const activeStep: StepKey | null =
    status && !status.ollama_ready
      ? 'ollama_ready'
      : status && !status.model_ready
        ? 'model_ready'
        : status && !status.server_ready
          ? 'server_ready'
          : null;

  return (
    <div
      className="fixed inset-0 flex items-center justify-center"
      style={{ background: 'var(--color-bg)' }}
    >
      <div className="w-full max-w-md px-6">
        {/* Logo */}
        <div className="text-center mb-10">
          <div
            className="w-16 h-16 rounded-2xl flex items-center justify-center mx-auto mb-4"
            style={{ background: 'var(--color-accent-subtle)', color: 'var(--color-accent)' }}
          >
            <Cpu size={32} />
          </div>
          <h1 className="text-2xl font-bold mb-1" style={{ color: 'var(--color-text)' }}>
            OpenJarvis
          </h1>
          <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
            Setting up your local AI...
          </p>
        </div>

        {choosing && (
          <EngineChoice options={status!.cli_options!} onChoose={(key) => void chooseEngine(key)} />
        )}

        {/* Steps */}
        {!choosing && (
        <div className="flex flex-col gap-2 mb-8">
          {STEPS.map((step) => (
            <StepRow
              key={step.key}
              icon={step.icon}
              label={step.label}
              done={status?.[step.key] ?? false}
              active={activeStep === step.key}
              detail={
                activeStep === step.key && status?.detail
                  ? status.detail
                  : step.detail
              }
            />
          ))}
        </div>
        )}

        {/* Error */}
        {status?.error && (
          <div
            className="flex items-start gap-3 px-4 py-3 rounded-xl text-sm"
            style={{
              background: 'color-mix(in srgb, var(--color-error) 10%, transparent)',
              border: '1px solid color-mix(in srgb, var(--color-error) 20%, transparent)',
              color: 'var(--color-error)',
            }}
          >
            <XCircle size={16} className="shrink-0 mt-0.5" />
            <span style={{ wordBreak: 'break-word', overflowWrap: 'anywhere' }}>{status.error}</span>
          </div>
        )}

        {/* Progress bar */}
        {!status?.error && !choosing && (
          <div
            className="h-1 rounded-full overflow-hidden"
            style={{ background: 'var(--color-bg-tertiary)' }}
          >
            <div
              className="h-full rounded-full transition-all duration-500"
              style={{
                background: 'var(--color-accent)',
                width: `${
                  ((status?.ollama_ready ? 1 : 0) +
                    (status?.model_ready ? 1 : 0) +
                    (status?.server_ready ? 1 : 0)) *
                  33.33
                }%`,
              }}
            />
          </div>
        )}
      </div>
    </div>
  );
}
