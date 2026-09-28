import { useCallback, useRef } from 'react';
import { ArcReactor } from './ArcReactor';
import { useAppStore } from '../../lib/store';
import { usePresence } from '../../hooks/usePresence';
import type { AudioAnalyzerData } from '../../hooks/useTTS';

interface PresenceCoreProps {
  size?: number;
}

/// The arc reactor as a living presence: breathes while idle, follows the
/// user's voice while listening, spins up while thinking and draws the TTS
/// waveform while speaking.
///
/// Only re-renders when the presence state changes — the reactor pulls audio
/// levels from the store inside its own animation loop.
export function PresenceCore({ size = 260 }: PresenceCoreProps) {
  const presence = usePresence();
  const micData = useRef<AudioAnalyzerData>({
    frequencyData: null,
    averageLevel: 0,
    bassLevel: 0,
    trebleLevel: 0,
  });

  const getAudioData = useCallback((): AudioAnalyzerData | undefined => {
    const s = useAppStore.getState();
    if (presence === 'speaking') return s.ttsAudioData;
    if (presence === 'listening') {
      const m = micData.current;
      m.averageLevel = s.micLevel;
      m.bassLevel = s.micLevel * 0.9;
      m.trebleLevel = s.micLevel * 0.6;
      return m;
    }
    return undefined;
  }, [presence]);

  return (
    <div className={`presence-core presence-${presence}`} style={{ width: size, height: size }}>
      <div className="presence-reactor">
        <ArcReactor size={size} presence={presence} getAudioData={getAudioData} />
      </div>
    </div>
  );
}
