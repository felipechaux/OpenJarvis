import { useAppStore } from '../lib/store';

/// What JARVIS is doing right now, derived from mic, stream and TTS state.
/// Drives every "alive" visual (reactor, companion status line).
export type Presence = 'idle' | 'listening' | 'thinking' | 'speaking';

export function usePresence(): Presence {
  const speechState = useAppStore((s) => s.speechState);
  const streaming = useAppStore((s) => s.streamState.isStreaming);
  const speaking = useAppStore((s) => s.ttsSpeaking);
  if (speechState === 'recording') return 'listening';
  if (speaking) return 'speaking';
  if (speechState === 'transcribing' || streaming) return 'thinking';
  return 'idle';
}

const LABELS: Record<'es' | 'en', Record<Presence, string>> = {
  es: { idle: 'EN ESPERA', listening: 'ESCUCHANDO', thinking: 'PENSANDO', speaking: 'HABLANDO' },
  en: { idle: 'STANDING BY', listening: 'LISTENING', thinking: 'THINKING', speaking: 'SPEAKING' },
};

export function usePresenceLabel(presence: Presence): string {
  const language = useAppStore((s) => s.settings.language);
  return LABELS[language === 'en' ? 'en' : 'es'][presence];
}
