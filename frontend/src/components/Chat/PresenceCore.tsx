import { useMemo } from 'react';
import { ArcReactor } from './ArcReactor';
import { useAppStore } from '../../lib/store';
import { usePresence } from '../../hooks/usePresence';
import type { AudioAnalyzerData } from '../../hooks/useTTS';

interface PresenceCoreProps {
  size?: number;
}

/// The arc reactor as a living presence: breathes while idle, follows the
/// user's voice while listening, spins up while thinking and rides the TTS
/// audio while speaking.
export function PresenceCore({ size = 260 }: PresenceCoreProps) {
  const presence = usePresence();
  const ttsAudioData = useAppStore((s) => s.ttsAudioData);
  const micLevel = useAppStore((s) => (presence === 'listening' ? s.micLevel : 0));

  const audioData = useMemo<AudioAnalyzerData | undefined>(() => {
    if (presence === 'speaking') return ttsAudioData;
    if (presence === 'listening') {
      return {
        frequencyData: null,
        averageLevel: micLevel,
        bassLevel: micLevel * 0.9,
        trebleLevel: micLevel * 0.6,
      };
    }
    return undefined;
  }, [presence, ttsAudioData, micLevel]);

  return (
    <div
      className={`presence-core presence-${presence}`}
      style={{ width: size, height: size, ['--mic-level' as string]: micLevel }}
    >
      {/* Listening halo — swells with the user's voice */}
      <div className="presence-halo" aria-hidden="true" />
      {/* Thinking scanner — a sweep orbiting the reactor */}
      <div className="presence-scan" aria-hidden="true" />
      <div className="presence-reactor">
        <ArcReactor size={size} streaming={presence === 'thinking'} audioData={audioData} />
      </div>
    </div>
  );
}
