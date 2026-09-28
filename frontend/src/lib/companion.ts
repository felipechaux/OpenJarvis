import { isTauri } from './api';

// Companion mode shrinks the main window into a small floating panel that
// stays on top of every app and Space — JARVIS as an ambient presence
// instead of a full-screen app.  Same window, same webview, so voice, TTS
// and wake word keep working without a second pipeline.

export const COMPANION_ROUTE = '/companion';
export const COMPANION_SHORTCUT = 'CommandOrControl+Shift+J';

const COMPANION_SIZE = { width: 360, height: 560 };
const COMPANION_MIN = { width: 300, height: 420 };
const MAIN_MIN = { width: 900, height: 600 };
const MAIN_DEFAULT = { width: 1340, height: 860 };
const SCREEN_MARGIN = 24;
const BOUNDS_KEY = 'openjarvis-main-bounds';

interface Bounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

function loadBounds(): Bounds | null {
  try {
    const raw = localStorage.getItem(BOUNDS_KEY);
    return raw ? (JSON.parse(raw) as Bounds) : null;
  } catch {
    return null;
  }
}

function saveBounds(bounds: Bounds) {
  try {
    localStorage.setItem(BOUNDS_KEY, JSON.stringify(bounds));
  } catch {}
}

export async function enterCompanionWindow(): Promise<void> {
  if (!isTauri()) return;
  const { getCurrentWindow, currentMonitor, LogicalSize, LogicalPosition } =
    await import('@tauri-apps/api/window');
  const win = getCurrentWindow();

  // Remember the full-size bounds so leaving companion mode restores them.
  const scale = await win.scaleFactor();
  const pos = (await win.outerPosition()).toLogical(scale);
  const size = (await win.outerSize()).toLogical(scale);
  saveBounds({ x: pos.x, y: pos.y, width: size.width, height: size.height });

  if (await win.isMaximized()) await win.unmaximize();
  await win.setMinSize(new LogicalSize(COMPANION_MIN.width, COMPANION_MIN.height));
  await win.setSize(new LogicalSize(COMPANION_SIZE.width, COMPANION_SIZE.height));

  // Dock to the top-right corner of the current screen's work area.
  const monitor = await currentMonitor();
  if (monitor) {
    const area = monitor.workArea.position.toLogical(monitor.scaleFactor);
    const areaSize = monitor.workArea.size.toLogical(monitor.scaleFactor);
    await win.setPosition(
      new LogicalPosition(
        area.x + areaSize.width - COMPANION_SIZE.width - SCREEN_MARGIN,
        area.y + SCREEN_MARGIN,
      ),
    );
  }

  await win.setAlwaysOnTop(true);
  await win.setVisibleOnAllWorkspaces(true);
  await win.show();
}

export async function exitCompanionWindow(): Promise<void> {
  if (!isTauri()) return;
  const { getCurrentWindow, LogicalSize, LogicalPosition } = await import('@tauri-apps/api/window');
  const win = getCurrentWindow();

  await win.setAlwaysOnTop(false);
  await win.setVisibleOnAllWorkspaces(false);
  await win.setMinSize(new LogicalSize(MAIN_MIN.width, MAIN_MIN.height));

  const bounds = loadBounds();
  if (bounds) {
    await win.setSize(new LogicalSize(bounds.width, bounds.height));
    await win.setPosition(new LogicalPosition(bounds.x, bounds.y));
  } else {
    await win.setSize(new LogicalSize(MAIN_DEFAULT.width, MAIN_DEFAULT.height));
    await win.center();
  }
  await win.setFocus();
}
