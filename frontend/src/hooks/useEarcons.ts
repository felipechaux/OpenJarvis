import { useEffect } from 'react';
import { useAppStore } from '../lib/store';
import { playEarcon, type Earcon } from '../lib/earcons';
import { presenceOf, type Presence } from './usePresence';

// thinking → idle can be a momentary gap (transcription finished, message
// not sent yet), so the "done" chime waits this long to be sure.
const DONE_SETTLE_MS = 450;

/// Plays the interface chimes off presence transitions, Alexa-style:
///   → listening                 wake        (wake word or mic click)
///   idle/listening → thinking   processing  (request captured / sent)
///   thinking → speaking / idle  done        (reply ready)
/// Mount once (App).  Mid-reply flips (speaking ↔ thinking while the voice
/// waits for the next sentence) stay silent so each request gets exactly
/// one processing + one done chime.
export function useEarcons() {
  useEffect(() => {
    let prev: Presence = presenceOf(useAppStore.getState());
    let awaitingReply = false;
    let doneTimer: ReturnType<typeof setTimeout> | null = null;

    const play = (kind: Earcon) => {
      if (useAppStore.getState().settings.earcons) playEarcon(kind);
    };
    const clearDone = () => {
      if (doneTimer) clearTimeout(doneTimer);
      doneTimer = null;
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
      }
    });

    return () => {
      unsubscribe();
      clearDone();
    };
  }, []);
}
