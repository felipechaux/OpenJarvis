import { useEffect } from 'react';
import { ChatArea } from '../components/Chat/ChatArea';
import { SystemPanel } from '../components/Chat/SystemPanel';
import { useAppStore, generateId } from '../lib/store';
import { streamChat } from '../lib/sse';
import { useTTS } from '../hooks/useTTS';
import { INTERRUPT_EVENT } from '../hooks/useWakeWord';

// Module-level flag: survives StrictMode remounts, resets only on full page reload
let _greetingComplete = false;

export function ChatPage() {
  const systemPanelOpen = useAppStore((s) => s.systemPanelOpen);
  const greeted = useAppStore((s) => s.greeted);
  const setGreeted = useAppStore((s) => s.setGreeted);
  const messages = useAppStore((s) => s.messages);
  const activeId = useAppStore((s) => s.activeId);
  const createConversation = useAppStore((s) => s.createConversation);
  const addMessage = useAppStore((s) => s.addMessage);
  const updateLastAssistant = useAppStore((s) => s.updateLastAssistant);
  const selectedModel = useAppStore((s) => s.selectedModel);
  const setStreamState = useAppStore((s) => s.setStreamState);
  const resetStream = useAppStore((s) => s.resetStream);
  const ttsEnabled = useAppStore((s) => s.settings.ttsEnabled);

  // Voice and UI Sync
  const { enqueue: enqueueSpeech, stop: stopSpeaking, speaking: ttsSpeaking, audioData: ttsAudioData } = useTTS();
  const setTTSSpeaking = useAppStore((s) => s.setTTSSpeaking);
  const setTTSAudioData = useAppStore((s) => s.setTTSAudioData);

  useEffect(() => {
    setTTSSpeaking(ttsSpeaking);
  }, [ttsSpeaking, setTTSSpeaking]);

  useEffect(() => {
    setTTSAudioData(ttsAudioData);
  }, [ttsAudioData, setTTSAudioData]);

  useEffect(() => {
    if (_greetingComplete || !selectedModel) return;

    const controller = new AbortController();

    // The wake word fires INTERRUPT_EVENT when the user says "Jarvis"
    // mid-greeting.  Abort the stream + kill TTS so we don't keep paying
    // for tool calls the user already cut off, and so the briefing audio
    // doesn't keep playing over their command.
    const onInterrupt = () => {
      if (controller.signal.aborted) return;
      _greetingComplete = true;
      controller.abort();
      stopSpeaking();
      resetStream();
    };
    window.addEventListener(INTERRUPT_EVENT, onInterrupt);

    async function triggerGreeting() {
      setGreeted(true);
      
      // Always start a fresh conversation for the morning briefing
      const convId = createConversation(selectedModel);

      const now = new Date();
      // Localised time string for the briefing — Spanish locale so day-of-week
      // and month names already arrive in Spanish for the LLM to quote verbatim
      // ("lunes 23 de mayo de 2026, 7:42 a. m.") rather than translating itself
      // and risking subtle errors.
      const localTime = now.toLocaleString('es-CO', {
        timeZone: 'America/Bogota',
        weekday: 'long', year: 'numeric', month: 'long', day: 'numeric',
        hour: 'numeric', minute: '2-digit', hour12: true,
      });
      const greetingPrompt = `JARVIS_WELCOME_TRIGGER: DEBES llamar a 'get_weather' y 'digest_collect' (sources: ['gcalendar', 'gmail']) inmediatamente.
Formato: Reporta CADA correo y evento de calendario que aparezca en las salidas de las herramientas. Saluda al usuario como "señor Chaux".
Hora local: ${localTime}.

INSTRUCCIONES DE BRIEFING:
- Enumera todos los correos sin leer que devuelva la herramienta.
- Enumera todos los eventos próximos para hoy y mañana.
- Menciona el impacto del clima de Bogotá en la agenda.

REGLAS ABSOLUTAS:
- DEBES reportar los correos específicos encontrados. NUNCA digas que faltan datos si las herramientas los devolvieron.
- CERO ALUCINACIÓN. Si una herramienta falla, dilo claramente.
- Mantente en personaje como JARVIS.
- Responde TODO en español, con un tono británico formal traducido al castellano (trata al usuario de "usted").`;

      const assistantMsgId = generateId();
      addMessage(convId, {
        id: assistantMsgId,
        role: 'assistant',
        content: '',
        timestamp: Date.now(),
      });

      setStreamState({
        isStreaming: true,
        phase: 'Inicializando sistemas...',
        content: '',
      });

      let accumulated = '';
      let ttsBuffer = '';
      try {
        for await (const event of streamChat({
          model: selectedModel,
          messages: [{ role: 'user', content: greetingPrompt }],
          stream: true,
          temperature: 0.7,
        }, controller.signal)) {
          if (event.event === 'tool_call_start') {
             try {
                const data = JSON.parse(event.data);
                if (data.tool === 'get_weather') setStreamState({ phase: 'Consultando datos meteorológicos...' });
                if (data.tool === 'digest_collect') setStreamState({ phase: 'Sincronizando su informe...' });
             } catch {
                setStreamState({ phase: 'Procesando...' });
             }
          } else if (event.event === 'tool_call_end') {
             setStreamState({ phase: 'Sintetizando informe...' });
          } else {
            try {
              const data = JSON.parse(event.data);
              const delta = data.choices?.[0]?.delta?.content;
              if (delta) {
                accumulated += delta;
                ttsBuffer += delta;
                setStreamState({ content: accumulated });
                updateLastAssistant(convId!, accumulated);

                // Speak each complete sentence as it arrives (skip abbreviation periods)
                if (ttsEnabled && !controller.signal.aborted) {
                  const match = ttsBuffer.match(/^([\s\S]*?(?<!\b(?:Mr|Mrs|Ms|Dr|Prof|St|Jr|Sr|vs|etc|No|Fig))[.!?;:])\s+/);
                  if (match) {
                    enqueueSpeech(match[1].trim());
                    ttsBuffer = ttsBuffer.slice(match[0].length);
                  }
                }
              }
              if (data.choices?.[0]?.finish_reason === 'stop') break;
            } catch {}
          }
        }

        // Speak any remaining text after the stream ends
        if (ttsEnabled && !controller.signal.aborted) {
          const remaining = ttsBuffer.trim();
          if (remaining) enqueueSpeech(remaining);
        }

      } catch (err) {
        if (err instanceof Error && err.name === 'AbortError') return;
        console.error('Greeting failed:', err);
      } finally {
        if (!controller.signal.aborted) {
          _greetingComplete = true;
          resetStream();
        }
      }
    }

    triggerGreeting();

    return () => {
      window.removeEventListener(INTERRUPT_EVENT, onInterrupt);
      controller.abort();
      stopSpeaking();
      resetStream();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedModel]);

  return (
    <div className="flex h-full overflow-hidden">
      <div className="flex-1 min-w-0">
        <ChatArea />
      </div>
      {systemPanelOpen && <SystemPanel />}
    </div>
  );
}
