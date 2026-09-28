import { useCallback, useEffect, useRef } from 'react';
import { useLocation, useNavigate } from 'react-router';
import { isTauri } from '../lib/api';
import {
  COMPANION_ROUTE,
  COMPANION_SHORTCUT,
  enterCompanionWindow,
  exitCompanionWindow,
} from '../lib/companion';

type ShortcutApi = typeof import('@tauri-apps/plugin-global-shortcut');

// Register/unregister are async; StrictMode's mount → unmount → mount would
// otherwise interleave them and leave the shortcut unregistered.  Run every
// operation strictly in order.
let shortcutChain: Promise<void> = Promise.resolve();
function enqueueShortcutOp(op: (api: ShortcutApi) => Promise<void>) {
  shortcutChain = shortcutChain
    .then(() => import('@tauri-apps/plugin-global-shortcut'))
    .then(op)
    .catch((err) => console.warn('[Companion] shortcut op failed', err));
}

/// Toggle between the full app and the floating companion panel.  Mount
/// once with ``registerShortcut`` (App) to bind the system-wide shortcut so
/// JARVIS can be summoned (or sent back to the full window) from any app.
export function useCompanionMode({ registerShortcut = false } = {}) {
  const navigate = useNavigate();
  const location = useLocation();
  const inCompanion = location.pathname === COMPANION_ROUTE;
  const inCompanionRef = useRef(inCompanion);
  inCompanionRef.current = inCompanion;
  const busyRef = useRef(false);

  const toggle = useCallback(async () => {
    if (busyRef.current) return;
    busyRef.current = true;
    try {
      if (inCompanionRef.current) {
        navigate('/');
        await exitCompanionWindow();
      } else {
        navigate(COMPANION_ROUTE);
        await enterCompanionWindow();
      }
    } catch (err) {
      console.warn('[Companion] toggle failed', err);
    } finally {
      busyRef.current = false;
    }
  }, [navigate]);

  const toggleRef = useRef(toggle);
  toggleRef.current = toggle;

  useEffect(() => {
    if (!registerShortcut || !isTauri()) return;
    enqueueShortcutOp(async ({ register, unregister }) => {
      // A dev reload can leave the previous registration alive.
      await unregister(COMPANION_SHORTCUT).catch(() => {});
      await register(COMPANION_SHORTCUT, (event) => {
        if (event.state === 'Pressed') toggleRef.current();
      });
    });
    return () => {
      enqueueShortcutOp(({ unregister }) => unregister(COMPANION_SHORTCUT));
    };
  }, [registerShortcut]);

  return { inCompanion, toggle };
}
