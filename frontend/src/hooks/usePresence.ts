import { useAppStore } from '../lib/store';

/// What JARVIS is doing right now, derived from mic, stream and TTS state.
/// Drives every "alive" visual (reactor, companion status line).
export type Presence = 'idle' | 'listening' | 'thinking' | 'speaking';

type AppState = ReturnType<typeof useAppStore.getState>;

/// Pure presence derivation, shared by the hook and non-React subscribers
/// (e.g. the interface chimes in useEarcons).
export function presenceOf(s: AppState): Presence {
  if (s.speechState === 'recording') return 'listening';
  if (s.ttsSpeaking) return 'speaking';
  if (s.speechState === 'transcribing' || s.streamState.isStreaming) return 'thinking';
  return 'idle';
}

export function usePresence(): Presence {
  return useAppStore(presenceOf);
}

const LABELS: Record<'es' | 'en', Record<Presence, string>> = {
  es: { idle: 'EN ESPERA', listening: 'ESCUCHANDO', thinking: 'PENSANDO', speaking: 'HABLANDO' },
  en: { idle: 'STANDING BY', listening: 'LISTENING', thinking: 'THINKING', speaking: 'SPEAKING' },
};

export function usePresenceLabel(presence: Presence): string {
  const language = useAppStore((s) => s.settings.language);
  return LABELS[language === 'en' ? 'en' : 'es'][presence];
}
