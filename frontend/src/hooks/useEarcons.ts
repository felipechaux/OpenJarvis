import { useEffect } from 'react';
import { useAppStore } from '../lib/store';
import { playEarcon, type Earcon } from '../lib/earcons';
import { presenceOf, type Presence } from './usePresence';

// thinking → idle can be a momentary gap (transcription finished, message
// not sent yet), so the "done" chime waits this long to be sure.
const DONE_SETTLE_MS = 450;
// listening → idle only means "heard nothing" if it stays idle this long
// (a recording that is about to be transcribed moves on to thinking).
const DISMISS_SETTLE_MS = 350;

/// Plays the interface chimes off presence transitions, Alexa-style:
///   → listening                 wake        (wake word or mic click)
///   idle/listening → thinking   processing  (request captured / sent)
///   thinking → speaking / idle  done        (reply ready)
///   listening → idle (settled)  dismiss     (nothing heard / cancelled)
/// Mount once (App).  Mid-reply flips (speaking ↔ thinking while the voice
/// waits for the next sentence) stay silent so each request gets exactly
/// one processing + one done chime.
export function useEarcons() {
  useEffect(() => {
    let prev: Presence = presenceOf(useAppStore.getState());
    let awaitingReply = false;
    let doneTimer: ReturnType<typeof setTimeout> | null = null;
    let dismissTimer: ReturnType<typeof setTimeout> | null = null;

    const play = (kind: Earcon) => {
      if (useAppStore.getState().settings.earcons) playEarcon(kind);
    };
    const clearDone = () => {
      if (doneTimer) clearTimeout(doneTimer);
      doneTimer = null;
      if (dismissTimer) clearTimeout(dismissTimer);
      dismissTimer = null;
    };

    const unsubscribe = useAppStore.subscribe((state) => {
      const cur = presenceOf(state);
      if (cur === prev) return;
      const from = prev;
      prev = cur;

      if (cur === 'listening') {
        clearDone();
        awaitingReply = false;
        play('wake');
      } else if (cur === 'thinking' && (from === 'idle' || from === 'listening')) {
        clearDone();
        if (!awaitingReply) {
          awaitingReply = true;
          play('processing');
        }
      } else if (from === 'thinking' && cur === 'speaking' && awaitingReply) {
        clearDone();
        awaitingReply = false;
        play('done');
      } else if (from === 'thinking' && cur === 'idle' && awaitingReply) {
        clearDone();
        doneTimer = setTimeout(() => {
          doneTimer = null;
          awaitingReply = false;
          play('done');
        }, DONE_SETTLE_MS);
      } else if (from === 'listening' && cur === 'idle') {
        clearDone();
        dismissTimer = setTimeout(() => {
          dismissTimer = null;
          if (presenceOf(useAppStore.getState()) === 'idle') play('dismiss');
        }, DISMISS_SETTLE_MS);
      }
    });

    return () => {
      unsubscribe();
      clearDone();
    };
  }, []);
}

// Tools with their own chime when they start.
const TOOL_EARCONS: Record<string, Earcon> = {
  look_at_screen: 'shutter',
  model_switch: 'switch',
};
// Any browser_* tool (navigate, click, type, …): one scan chime per turn.
const BROWSER_TOOL = /^browser/;
// native_react prefixes this when a subscription ran out mid-turn.
const QUOTA_NOTICE = /sin cuota hasta/;

/// Event chimes beyond the presence ones: the screen shutter and model
/// switches when those tools start, a scan chime when a turn first reaches
/// for the browser, a switch chime when a reply announces a
/// quota fallback, and a soft error tone when a request fails.  Mount once.
export function useInteractionEarcons() {
  useEffect(() => {
    const seenTools = new Set<string>();
    let browsing = false;
    // The log is capped (oldest dropped), so track the newest entry, not the length.
    let lastLog = useAppStore.getState().logEntries.slice(-1)[0];
    let lastMessages = useAppStore.getState().messages.length;

    const play = (kind: Earcon) => {
      if (useAppStore.getState().settings.earcons) playEarcon(kind);
    };

    return useAppStore.subscribe((state) => {
      for (const tc of state.streamState.activeToolCalls) {
        if (seenTools.has(tc.id)) continue;
        seenTools.add(tc.id);
        const kind = TOOL_EARCONS[tc.tool];
        if (kind) play(kind);
        else if (BROWSER_TOOL.test(tc.tool) && !browsing) {
          browsing = true;
          play('scan');
        }
      }
      if (!state.streamState.isStreaming && state.streamState.activeToolCalls.length === 0) {
        seenTools.clear();
        browsing = false;
      }

      const newest = state.logEntries[state.logEntries.length - 1];
      if (newest && newest !== lastLog) {
        lastLog = newest;
        if (newest.level === 'error' && newest.category === 'chat') play('error');
      }

      const msgs = state.messages;
      if (msgs.length !== lastMessages) {
        const added = msgs.length > lastMessages ? msgs.slice(lastMessages) : [];
        lastMessages = msgs.length;
        if (added.some((m) => m.role === 'assistant' && QUOTA_NOTICE.test(m.content))) {
          play('switch');
        }
      }
    });
  }, []);
}
