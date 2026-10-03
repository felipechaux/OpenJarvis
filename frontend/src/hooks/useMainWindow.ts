import { useEffect, useRef } from 'react';
import { useNavigate } from 'react-router';
import { isTauri } from '../lib/api';
import { useAppStore } from '../lib/store';

/// Rust's reports about the main window (src-tauri `WINDOW_EVENT`).
const WINDOW_EVENT = 'jarvis-window';
// Once per app launch: a webview reload (dev) must not hide an open window.
const LAUNCH_KEY = 'jarvis-launch-window-done';

function launchDone(): boolean {
  try {
    return sessionStorage.getItem(LAUNCH_KEY) === '1';
  } catch {
    return false;
  }
}

/// Opens (true), hides (false) or toggles (undefined) the main window.
export async function setMainWindow(visible?: boolean): Promise<void> {
  try {
    const { invoke } = await import('@tauri-apps/api/core');
    await invoke('main_window', { visible: visible ?? null });
  } catch {
    /* plain browser */
  }
}

/// JARVIS lives in the notch; this window is the "advanced" view.  It starts
/// hidden (tauri.conf.json) and is shown only for the launch intro, when
/// setup needs the user (engine choice, an error), or when
/// `openWindowOnLaunch` is on.  Waiting for the backend to boot does not
/// count: that happens on every launch and the notch covers it.  Once hidden it goes
/// back to the chat, the page that runs the notch's commands (InputArea).
export function useMainWindow({
  setupDone,
  setupAttention,
  introPlaying,
}: {
  setupDone: boolean;
  setupAttention: boolean;
  introPlaying: boolean;
}) {
  const navigate = useNavigate();
  const navigateRef = useRef(navigate);
  navigateRef.current = navigate;
  const launch = useRef({ handled: launchDone(), shownForSetup: false });

  useEffect(() => {
    if (!isTauri()) return;
    let unlisten = () => {};
    let disposed = false;
    import('@tauri-apps/api/event')
      .then(({ listen }) =>
        listen<string>(WINDOW_EVENT, ({ payload }) => {
          if (payload === 'hidden') navigateRef.current('/');
          else if (payload === 'settings') navigateRef.current('/settings');
        }),
      )
      .then((fn) => {
        if (disposed) fn();
        else unlisten = fn;
      })
      .catch(() => {});
    return () => {
      disposed = true;
      unlisten();
    };
  }, []);

  // The intro plays in this window: show it for the intro.
  useEffect(() => {
    if (isTauri() && introPlaying) setMainWindow(true);
  }, [introPlaying]);

  // Setup needs the user: show its screen (and keep the window afterwards).
  useEffect(() => {
    if (!isTauri() || !setupAttention || setupDone) return;
    launch.current.shownForSetup = true;
    setMainWindow(true);
  }, [setupAttention, setupDone]);

  // Intro over (or none): leave JARVIS in the notch, even while the backend
  // is still booting, unless the user wants the window or setup needs them.
  useEffect(() => {
    const state = launch.current;
    if (!isTauri() || state.handled || introPlaying) return;
    state.handled = true;
    try {
      sessionStorage.setItem(LAUNCH_KEY, '1');
    } catch {
      /* storage unavailable: a reload may hide the window again */
    }
    const keepOpen = useAppStore.getState().settings.openWindowOnLaunch || state.shownForSetup;
    setMainWindow(keepOpen);
  }, [introPlaying]);
}
