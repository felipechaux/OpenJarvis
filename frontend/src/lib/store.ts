import { create } from 'zustand';
import type {
  Conversation,
  ChatMessage,
  LogEntry,
  ModelInfo,
  MessageTelemetry,
  SavingsData,
  ServerInfo,
  StreamState,
  ToolCallInfo,
  TokenUsage,
} from '../types';
import type { ManagedAgent } from './api';
import type { AudioAnalyzerData } from '../hooks/useTTS';
import type { SpeechState } from '../hooks/useSpeech';

export interface CachedConnector {
  connector_id: string;
  display_name: string;
  connected: boolean;
  chunks: number;
}

export interface AgentEvent {
  type: string;
  timestamp: number;
  data: Record<string, unknown>;
}

// ── localStorage persistence ──────────────────────────────────────────

const CONVERSATIONS_KEY = 'openjarvis-conversations';
const SETTINGS_KEY = 'openjarvis-settings';
const SELECTED_MODEL_KEY = 'openjarvis-selected-model';
const OPTIN_KEY = 'openjarvis-optin';
const OPTIN_NAME_KEY = 'openjarvis-display-name';
const OPTIN_EMAIL_KEY = 'openjarvis-email';
const OPTIN_ANONID_KEY = 'openjarvis-anon-id';
const OPTIN_SEEN_KEY = 'openjarvis-optin-seen';

interface ConversationStore {
  version: 1;
  conversations: Record<string, Conversation>;
  activeId: string | null;
}

function generateId(): string {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

function loadConversations(): ConversationStore {
  try {
    const raw = localStorage.getItem(CONVERSATIONS_KEY);
    if (!raw) return { version: 1, conversations: {}, activeId: null };
    const parsed = JSON.parse(raw);
    if (parsed.version === 1) return parsed;
    return { version: 1, conversations: {}, activeId: null };
  } catch {
    return { version: 1, conversations: {}, activeId: null };
  }
}

function saveConversations(store: ConversationStore): void {
  localStorage.setItem(CONVERSATIONS_KEY, JSON.stringify(store));
}

export type ThemeMode = 'light' | 'dark' | 'system';

/** Primary assistant language: fixed Spanish, fixed English, or per-text auto-detect. */
export type AssistantLanguage = 'es' | 'en' | 'auto';

interface Settings {
  theme: ThemeMode;
  apiUrl: string;
  fontSize: 'small' | 'default' | 'large';
  defaultModel: string;
  defaultAgent: string;
  temperature: number;
  maxTokens: number;
  speechEnabled: boolean;
  ttsEnabled: boolean;
  /** Subtle J.A.R.V.I.S.-style polish on spoken replies (EQ, light
   *  compression, faint holographic reverb) applied in useTTS. */
  voiceFx: boolean;
  /** J.A.R.V.I.S.-style interface chimes on wake, processing and reply
   *  (played by useEarcons). */
  earcons: boolean;
  /** Full-screen Iron Man HUD while JARVIS is speaking (JarvisHud). */
  speakingHud: boolean;
  /** Arc-reactor power-up intro with its sound when the app launches (BootIntro). */
  bootIntro: boolean;
  /** Show the main window when JARVIS starts; off = only the notch. */
  openWindowOnLaunch: boolean;
  language: AssistantLanguage;
}

function loadSettings(): Settings {
  const defaults: Settings = {
    theme: 'system',
    apiUrl: '',
    fontSize: 'default',
    defaultModel: '',
    defaultAgent: '',
    temperature: 0.7,
    maxTokens: 4096,
    speechEnabled: true,
    ttsEnabled: true,
    voiceFx: true,
    earcons: true,
    speakingHud: true,
    bootIntro: true,
    openWindowOnLaunch: false,
    language: 'es',
  };
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    if (!raw) return defaults;
    return { ...defaults, ...JSON.parse(raw) };
  } catch {
    return defaults;
  }
}

function saveSettings(settings: Settings): void {
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
}

// ── Store ─────────────────────────────────────────────────────────────

const INITIAL_STREAM: StreamState = {
  isStreaming: false,
  phase: '',
  elapsedMs: 0,
  activeToolCalls: [],
  content: '',
};

interface AppState {
  // Conversations
  conversations: Conversation[];
  activeId: string | null;
  messages: ChatMessage[];
  streamState: StreamState;

  // Models & server
  models: ModelInfo[];
  modelsLoading: boolean;
  selectedModel: string;
  /** Model that actually answered the last turn ('' before any turn).
   *  Tiered routing can answer an Opus selection with Haiku. */
  activeModel: string;
  serverInfo: ServerInfo | null;
  savings: SavingsData | null;

  // Settings
  settings: Settings;

  // Command palette
  commandPaletteOpen: boolean;

  // Sidebar
  sidebarOpen: boolean;

  // System panel
  systemPanelOpen: boolean;

  // Opt-in sharing
  optInEnabled: boolean;
  optInDisplayName: string;
  optInEmail: string;
  optInAnonId: string;
  optInModalSeen: boolean;
  optInModalOpen: boolean;

  // Actions: conversations
  loadConversations: () => void;
  createConversation: (model?: string) => string;
  selectConversation: (id: string) => void;
  deleteConversation: (id: string) => void;
  loadMessages: (conversationId: string | null) => void;
  addMessage: (conversationId: string, message: ChatMessage) => void;
  updateLastAssistant: (
    conversationId: string,
    content: string,
    toolCalls?: ToolCallInfo[],
    usage?: TokenUsage,
    telemetry?: MessageTelemetry,
    audio?: { url: string },
  ) => void;
  setStreamState: (state: Partial<StreamState>) => void;
  resetStream: () => void;

  // Actions: models & server
  setModels: (models: ModelInfo[]) => void;
  setModelsLoading: (loading: boolean) => void;
  setSelectedModel: (model: string) => void;
  setActiveModel: (model: string) => void;
  setServerInfo: (info: ServerInfo | null) => void;
  setSavings: (data: SavingsData | null) => void;

  // Actions: settings
  updateSettings: (partial: Partial<Settings>) => void;

  // Actions: UI
  setCommandPaletteOpen: (open: boolean) => void;
  toggleSidebar: () => void;
  setSidebarOpen: (open: boolean) => void;
  toggleSystemPanel: () => void;
  setSystemPanelOpen: (open: boolean) => void;

  // Data sources (cached between visits to avoid empty-state flicker)
  cachedConnectors: CachedConnector[] | null;
  setCachedConnectors: (list: CachedConnector[] | null) => void;

  // Agents
  managedAgents: ManagedAgent[];
  managedAgentsLoading: boolean;
  selectedAgentId: string | null;

  // Actions: agents
  setManagedAgents: (agents: ManagedAgent[]) => void;
  setManagedAgentsLoading: (loading: boolean) => void;
  setSelectedAgentId: (id: string | null) => void;

  // Agent events (live stream)
  agentEvents: AgentEvent[];
  addAgentEvent: (event: AgentEvent) => void;
  clearAgentEvents: () => void;

  // Actions: opt-in sharing
  setOptIn: (enabled: boolean, displayName: string, email: string) => void;
  setOptInModalOpen: (open: boolean) => void;
  markOptInModalSeen: () => void;

  // Logs
  logEntries: LogEntry[];
  addLogEntry: (entry: LogEntry) => void;
  clearLogs: () => void;

  // Model loading
  modelLoading: boolean;
  setModelLoading: (loading: boolean) => void;

  // TTS / Voice state
  ttsSpeaking: boolean;
  ttsAudioData: AudioAnalyzerData;
  /** Sentence currently being spoken — the HUD's subtitle line. */
  ttsCaption: string;
  setTTSCaption: (text: string) => void;
  setTTSSpeaking: (speaking: boolean) => void;
  setTTSAudioData: (data: AudioAnalyzerData) => void;

  // Mic capture state — mirrored from useSpeech so presence visuals
  // (ArcReactor, notch) can react to the user's voice.
  speechState: SpeechState;
  micLevel: number;
  setSpeechState: (state: SpeechState) => void;
  setMicLevel: (level: number) => void;

  // Initial greeting
  greeted: boolean;
  setGreeted: (greeted: boolean) => void;
}

export const useAppStore = create<AppState>((set, get) => {
  const initial = loadConversations();
  const convList = Object.values(initial.conversations).sort(
    (a, b) => b.updatedAt - a.updatedAt,
  );

  return {
    conversations: convList,
    activeId: initial.activeId,
    messages:
      initial.activeId && initial.conversations[initial.activeId]
        ? initial.conversations[initial.activeId].messages
        : [],
    streamState: INITIAL_STREAM,

    models: [],
    modelsLoading: true,
    selectedModel: localStorage.getItem(SELECTED_MODEL_KEY) || '',
    activeModel: '',
    serverInfo: null,
    savings: null,

    settings: loadSettings(),

    commandPaletteOpen: false,
    sidebarOpen: true,
    systemPanelOpen: true,

    optInEnabled: localStorage.getItem(OPTIN_KEY) === 'true',
    optInDisplayName: localStorage.getItem(OPTIN_NAME_KEY) || '',
    optInEmail: localStorage.getItem(OPTIN_EMAIL_KEY) || '',
    optInAnonId: localStorage.getItem(OPTIN_ANONID_KEY) || crypto.randomUUID(),
    optInModalSeen: localStorage.getItem(OPTIN_SEEN_KEY) === 'true',
    optInModalOpen: false,

    // ── Conversations ───────────────────────────────────────────────

    loadConversations: () => {
      const store = loadConversations();
      set({
        conversations: Object.values(store.conversations).sort(
          (a, b) => b.updatedAt - a.updatedAt,
        ),
        activeId: store.activeId,
      });
    },

    createConversation: (model?: string) => {
      const store = loadConversations();
      const conv: Conversation = {
        id: generateId(),
        title: 'New chat',
        createdAt: Date.now(),
        updatedAt: Date.now(),
        model: model || get().selectedModel || 'default',
        messages: [],
      };
      store.conversations[conv.id] = conv;
      store.activeId = conv.id;
      saveConversations(store);
      set({
        conversations: Object.values(store.conversations).sort(
          (a, b) => b.updatedAt - a.updatedAt,
        ),
        activeId: conv.id,
        messages: [],
      });
      return conv.id;
    },

    selectConversation: (id: string) => {
      const store = loadConversations();
      store.activeId = id;
      saveConversations(store);
      const conv = store.conversations[id];
      set({
        activeId: id,
        messages: conv ? conv.messages : [],
      });
    },

    deleteConversation: (id: string) => {
      const store = loadConversations();
      delete store.conversations[id];
      if (store.activeId === id) {
        const remaining = Object.keys(store.conversations);
        store.activeId = remaining.length > 0 ? remaining[0] : null;
      }
      saveConversations(store);
      const convList = Object.values(store.conversations).sort(
        (a, b) => b.updatedAt - a.updatedAt,
      );
      const activeConv = store.activeId
        ? store.conversations[store.activeId]
        : null;
      set({
        conversations: convList,
        activeId: store.activeId,
        messages: activeConv ? activeConv.messages : [],
      });
    },

    loadMessages: (conversationId: string | null) => {
      if (!conversationId) {
        set({ messages: [] });
        return;
      }
      const store = loadConversations();
      const conv = store.conversations[conversationId];
      set({ messages: conv ? conv.messages : [] });
    },

    addMessage: (conversationId: string, message: ChatMessage) => {
      const store = loadConversations();
      const conv = store.conversations[conversationId];
      if (!conv) return;
      conv.messages.push(message);
      conv.updatedAt = Date.now();
      if (message.role === 'user' && conv.title === 'New chat') {
        conv.title =
          message.content.slice(0, 50) +
          (message.content.length > 50 ? '...' : '');
      }
      saveConversations(store);
      set({
        messages: [...conv.messages],
        conversations: Object.values(store.conversations).sort(
          (a, b) => b.updatedAt - a.updatedAt,
        ),
      });
    },

    updateLastAssistant: (
      conversationId: string,
      content: string,
      toolCalls?: ToolCallInfo[],
      usage?: TokenUsage,
      telemetry?: MessageTelemetry,
      audio?: { url: string },
    ) => {
      const store = loadConversations();
      const conv = store.conversations[conversationId];
      if (!conv) return;
      const lastMsg = conv.messages[conv.messages.length - 1];
      if (lastMsg && lastMsg.role === 'assistant') {
        lastMsg.content = content;
        if (toolCalls) lastMsg.toolCalls = toolCalls;
        if (usage) lastMsg.usage = usage;
        if (telemetry) lastMsg.telemetry = telemetry;
        if (audio) lastMsg.audio = audio;
        conv.updatedAt = Date.now();
        saveConversations(store);
        set({ messages: [...conv.messages] });
      }
    },

    setStreamState: (partial: Partial<StreamState>) => {
      set((s) => ({ streamState: { ...s.streamState, ...partial } }));
    },

    resetStream: () => {
      set({ streamState: INITIAL_STREAM });
    },

    // ── Models & server ────────────────────────────────────────────

    setModels: (models: ModelInfo[]) => set({ models }),
    setModelsLoading: (loading: boolean) => set({ modelsLoading: loading }),
    setSelectedModel: (model: string) => {
      try {
        if (model) localStorage.setItem(SELECTED_MODEL_KEY, model);
        else localStorage.removeItem(SELECTED_MODEL_KEY);
      } catch { /* ignore quota errors */ }
      set({ selectedModel: model });
    },
    setActiveModel: (model: string) => set({ activeModel: model }),
    setServerInfo: (info: ServerInfo | null) => set({ serverInfo: info }),
    setSavings: (data: SavingsData | null) => set({ savings: data }),

    cachedConnectors: null,
    setCachedConnectors: (list) => set({ cachedConnectors: list }),

    // ── Settings ───────────────────────────────────────────────────

    updateSettings: (partial: Partial<Settings>) => {
      const updated = { ...get().settings, ...partial };
      saveSettings(updated);
      set({ settings: updated });
    },

    // ── UI ──────────────────────────────────────────────────────────

    setCommandPaletteOpen: (open: boolean) => set({ commandPaletteOpen: open }),
    toggleSidebar: () => set((s) => ({ sidebarOpen: !s.sidebarOpen })),
    setSidebarOpen: (open: boolean) => set({ sidebarOpen: open }),
    toggleSystemPanel: () => set((s) => ({ systemPanelOpen: !s.systemPanelOpen })),
    setSystemPanelOpen: (open: boolean) => set({ systemPanelOpen: open }),

    // ── Agents ─────────────────────────────────────────────────────

    managedAgents: [],
    managedAgentsLoading: false,
    selectedAgentId: null,

    setManagedAgents: (agents) => set({ managedAgents: agents }),
    setManagedAgentsLoading: (loading) => set({ managedAgentsLoading: loading }),
    setSelectedAgentId: (id) => set({ selectedAgentId: id }),

    agentEvents: [],
    addAgentEvent: (event) => set((s) => ({
      agentEvents: [...s.agentEvents.slice(-99), event],
    })),
    clearAgentEvents: () => set({ agentEvents: [] }),

    // ── Logs ────────────────────────────────────────────────────────
    logEntries: [],
    addLogEntry: (entry) => set((s) => ({
      logEntries: [...s.logEntries.slice(-499), entry],
    })),
    clearLogs: () => set({ logEntries: [] }),

    // ── Model loading ───────────────────────────────────────────────
    modelLoading: false,
    setModelLoading: (loading) => set({ modelLoading: loading }),

    // ── TTS / Voice ─────────────────────────────────────────────────
    ttsSpeaking: false,
    ttsAudioData: {
      frequencyData: null,
      averageLevel: 0,
      bassLevel: 0,
      trebleLevel: 0,
    },
    setTTSSpeaking: (speaking) => set({ ttsSpeaking: speaking }),
    setTTSAudioData: (data) => set({ ttsAudioData: data }),
    ttsCaption: '',
    setTTSCaption: (text) => set({ ttsCaption: text }),

    // ── Mic ──────────────────────────────────────────────────────────
    speechState: 'idle',
    micLevel: 0,
    setSpeechState: (speechState) => set({ speechState }),
    setMicLevel: (micLevel) => set({ micLevel }),

    // ── Initial Greeting ─────────────────────────────────────────────
    greeted: false,
    setGreeted: (greeted: boolean) => set({ greeted }),

    // ── Opt-in sharing ──────────────────────────────────────────────

    setOptIn: (enabled: boolean, displayName: string, email: string) => {
      const anonId = get().optInAnonId;
      localStorage.setItem(OPTIN_KEY, String(enabled));
      localStorage.setItem(OPTIN_NAME_KEY, displayName);
      localStorage.setItem(OPTIN_EMAIL_KEY, email);
      localStorage.setItem(OPTIN_ANONID_KEY, anonId);
      set({ optInEnabled: enabled, optInDisplayName: displayName, optInEmail: email });
    },
    setOptInModalOpen: (open: boolean) => set({ optInModalOpen: open }),
    markOptInModalSeen: () => {
      localStorage.setItem(OPTIN_SEEN_KEY, 'true');
      set({ optInModalSeen: true });
    },
  };
});

export { generateId };
