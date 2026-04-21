import { useEffect } from 'react';
import { ChatArea } from '../components/Chat/ChatArea';
import { SystemPanel } from '../components/Chat/SystemPanel';
import { useAppStore, generateId } from '../lib/store';
import { streamChat } from '../lib/sse';
import { useTTS } from '../hooks/useTTS';

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
      const greetingPrompt = `JARVIS_WELCOME_TRIGGER: Current local time in Bogotá is ${localTime}. The user has just arrived. Please perform these steps: 1) Use 'get_weather' for Bogotá. 2) Use 'digest_collect' with sources ['gcalendar', 'gmail', 'oura'] to check for today's events and status. 3) Greet the user by their name (Felipe) with a time-appropriate salutation (good morning / good afternoon / good evening based on the time above) and provide a concise, proactive briefing. Stay in your signature Paul Bettany-esque character.`;

      const assistantMsgId = generateId();
      addMessage(convId, {
        id: assistantMsgId,
        role: 'assistant',
        content: '',
        timestamp: Date.now(),
      });

      setStreamState({
        isStreaming: true,
        phase: 'Jarvis is waking up...',
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
                if (data.tool === 'get_weather') setStreamState({ phase: 'Checking the weather...' });
                if (data.tool === 'digest_collect') setStreamState({ phase: 'Syncing your calendar...' });
             } catch {
                setStreamState({ phase: 'Processing...' });
             }
          } else if (event.event === 'tool_call_end') {
             setStreamState({ phase: 'Generating briefing...' });
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
