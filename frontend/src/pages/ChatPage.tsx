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
      const localTime = now.toLocaleString('en-US', {
        timeZone: 'America/Bogota',
        weekday: 'long', year: 'numeric', month: 'long', day: 'numeric',
        hour: 'numeric', minute: '2-digit', hour12: true,
      });
      const greetingPrompt = `JARVIS_WELCOME_TRIGGER: You MUST call 'get_weather' and 'digest_collect' (sources: ['gcalendar', 'gmail']) immediately. 
Format: Report EVERY email and calendar event found in the tool outputs. Greet the user as Mr. Chaux. 
Local time: ${localTime}.

INSIGHT COMMANDS:
- List all unread emails from the tool output.
- List all upcoming events for today and tomorrow.
- Mention Bogotá weather impact on schedule.

ABSOLUTE RULES:
- YOU MUST report the specific emails found. NEVER say you are missing data if the tools return data.
- ZERO HALLUCINATION. If a tool fails, say it failed.
- Stay in character as JARVIS.`;

      const assistantMsgId = generateId();
      addMessage(convId, {
        id: assistantMsgId,
        role: 'assistant',
        content: '',
        timestamp: Date.now(),
      });

      setStreamState({
        isStreaming: true,
        phase: 'Initializing systems...',
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
                if (data.tool === 'get_weather') setStreamState({ phase: 'Checking meteorological data...' });
                if (data.tool === 'digest_collect') setStreamState({ phase: 'Synchronizing your briefing...' });
             } catch {
                setStreamState({ phase: 'Processing...' });
             }
          } else if (event.event === 'tool_call_end') {
             setStreamState({ phase: 'Synthesizing briefing...' });
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
