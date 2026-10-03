import { useEffect, useState } from 'react';
import { getBase } from '../lib/api';

/// What Spotify (web player, in the user's browser) is playing
/// (GET /v1/media/now-playing, server/media_router.py).
export interface NowPlaying {
  state: 'playing' | 'paused' | 'none';
  title?: string;
  artist?: string;
  art?: string;
}

export type MediaAction = 'play' | 'pause' | 'next' | 'previous';

const POLL_MS = 3000;
const NONE: NowPlaying = { state: 'none' };

const PREVIEW = new URLSearchParams(window.location.search);

/// Now playing, polled while the notch runs.  `/notch?music=demo` fakes it.
export function useNowPlaying(): [NowPlaying, (action: MediaAction) => void] {
  const [now, setNow] = useState<NowPlaying>(() =>
    PREVIEW.get('music') === 'demo'
      ? { state: 'playing', title: 'Luna', artist: 'Feid, ATL Jacob', art: '' }
      : NONE,
  );
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (PREVIEW.get('music') === 'demo') return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const res = await fetch(`${getBase()}/v1/media/now-playing`);
        if (res.ok && !stopped) setNow((await res.json()) as NowPlaying);
      } catch {
        /* server not up yet */
      }
      if (!stopped) timer = setTimeout(poll, POLL_MS);
    };
    poll();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [tick]);

  const control = (action: MediaAction) => {
    // Optimistic play/pause so the button answers at once.
    if (action === 'play' || action === 'pause') {
      setNow((n) => ({ ...n, state: action === 'play' ? 'playing' : 'paused' }));
    }
    if (PREVIEW.get('music') === 'demo') return;
    fetch(`${getBase()}/v1/media/${action}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    })
      .catch(() => {})
      .finally(() => setTick((t) => t + 1)); // re-poll now: new track / state
  };

  return [now, control];
}
