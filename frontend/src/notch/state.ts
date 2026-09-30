import type { Presence } from '../hooks/usePresence';

/// What the main window tells the notch pill (Tauri event `notch:state`).
export interface NotchState {
  presence: Presence;
  /// Status line: tool in progress, or the sentence being spoken.
  text: string;
  /// JARVIS's last reply (paragraphs kept), scrollable when the pill is open.
  reply: string;
  /// Interface chimes on (settings.earcons); the notch window has no store.
  earcons: boolean;
}

export const NOTCH_EVENT = 'notch:state';
/// Voice level for the notch reactor, sent ~12×/s while listening/speaking.
export const NOTCH_LEVEL_EVENT = 'notch:level';

export interface NotchLevel {
  /// 0..1 loudness.
  level: number;
  /// Low analyser bins (0..255), the part of the spectrum the waveform draws.
  bins: number[];
}

export const NOTCH_LABEL = 'notch';

const TOOL_LABELS: Record<string, string> = {
  look_at_screen: 'Mirando tu pantalla',
  browser_task: 'Navegando en la web',
  chrome_tabs: 'Revisando pestañas',
  start_coding_session: 'Abriendo sesión de código',
  send_to_session: 'Hablando con la sesión',
  coding_sessions: 'Revisando sesiones',
  spotify: 'Spotify',
  knowledge_search: 'Buscando',
  digest_collect: 'Preparando el resumen',
  get_weather: 'Consultando el clima',
  apple_notes: 'Notas',
  open_app: 'Abriendo',
  open_url: 'Abriendo enlace',
  model_switch: 'Cambiando de modelo',
};

export function toolLabel(tool: string): string {
  return TOOL_LABELS[tool] ?? 'Trabajando';
}

/// First sentence (≤ 70 chars) of a reply, for the speaking state.
export function snippet(text: string): string {
  const clean = text.replace(/[*_`#>]/g, '').replace(/\s+/g, ' ').trim();
  const first = clean.split(/(?<=[.!?])\s/)[0] ?? '';
  return first.length > 70 ? `${first.slice(0, 67).trimEnd()}…` : first;
}

/// Reply text for the expanded pill: markdown stripped, line breaks kept
/// (shown with `white-space: pre-line`), capped.
export function replyPreview(text: string, max = 4000): string {
  const clean = text
    .replace(/[*_`#>]/g, '')
    .replace(/[ \t]+/g, ' ')
    .replace(/ ?\n ?/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
  return clean.length > max ? `${clean.slice(0, max - 1).trimEnd()}…` : clean;
}
