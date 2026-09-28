// Voice-activity gate for hands-free (wake-word) recordings.
//
// Fed one mic RMS reading per tick, it decides when the user has finished
// their command ('stop'), never spoke at all ('cancel' — the audio is
// discarded, not transcribed), or is still going ('listening').
//
// Thresholds are relative to the background.  Fixed thresholds worked in
// a quiet room but not with music playing: the music never fell below the
// "silence" level, so after a (false) wake the mic stayed open for the full
// 30 s cap — macOS ducking the music the whole time — and the song was then
// transcribed as a command.  The floor is the 25th percentile of the last
// few seconds, which tracks the music (or room noise) even while the user
// talks, because speech has gaps between words.  In a quiet room the
// absolute minimums below dominate, so behaviour there is unchanged.
//
// Pure (no DOM / Web Audio) so it can be replayed over recorded RMS traces.

export type VadVerdict = 'listening' | 'stop' | 'cancel';

export interface VadTunables {
  /** Clearly-speaking floor in a silent room (normal voice ≈ 0.05-0.15). */
  speechRms: number;
  /** Clearly-silent ceiling in a silent room (MacBook mic noise ≈ 0.005-0.012). */
  silenceRms: number;
  /** Speech must exceed the background floor by this factor. */
  speechOverFloor: number;
  /** Below this factor × floor counts as "back to background". */
  silenceOverFloor: number;
  /** Consecutive loud ticks needed before speech counts as started, so a
   * drum hit or the wake chime tail doesn't open the command. */
  speechOnsetTicks: number;
  /** Background after speech → stop. */
  silenceHoldMs: number;
  /** No speech at all by then → cancel. */
  noSpeechTimeoutMs: number;
  /** Hard cap on the whole recording. */
  maxRecordingMs: number;
  /** Ignore the start: the wake chime plays as the mic opens. */
  armDelayMs: number;
  /** Background window for the floor percentile. */
  floorWindowMs: number;
  /** Readings needed before the floor is trusted. */
  minFloorSamples: number;
  tickMs: number;
}

export const VAD_DEFAULTS: VadTunables = {
  speechRms: 0.030,
  silenceRms: 0.012,
  speechOverFloor: 2.2,
  silenceOverFloor: 1.6,
  speechOnsetTicks: 3,
  silenceHoldMs: 1200,
  noSpeechTimeoutMs: 6000,
  maxRecordingMs: 30000,
  armDelayMs: 550,
  floorWindowMs: 3000,
  minFloorSamples: 4,
  tickMs: 80,
};

export class VadGate {
  readonly t: VadTunables;
  speechStarted = false;
  maxRms = 0;
  /** Time spent above the speech threshold since speech started (for
   * logs — music with vocals scores like a short command, so it can't
   * gate the stop without dropping real ones). */
  speechMs = 0;
  private recent: number[] = [];
  private loudTicks = 0;
  private silenceStart = -1;

  constructor(tunables: Partial<VadTunables> = {}) {
    this.t = { ...VAD_DEFAULTS, ...tunables };
  }

  /** Background level (25th percentile of the recent window), 0 if unknown. */
  get floor(): number {
    if (this.recent.length < this.t.minFloorSamples) return 0;
    const sorted = [...this.recent].sort((a, b) => a - b);
    return sorted[Math.floor(sorted.length * 0.25)];
  }

  get speechThreshold(): number {
    return Math.max(this.t.speechRms, this.floor * this.t.speechOverFloor);
  }

  get silenceThreshold(): number {
    return Math.max(this.t.silenceRms, this.floor * this.t.silenceOverFloor);
  }

  /** Silence (ms) accumulated since speech ended, for logging. */
  silenceMs(elapsedMs: number): number {
    return this.silenceStart < 0 ? 0 : elapsedMs - this.silenceStart;
  }

  push(rms: number, elapsedMs: number): VadVerdict {
    if (rms > this.maxRms) this.maxRms = rms;

    if (elapsedMs > this.t.maxRecordingMs) return this.finish();
    if (elapsedMs < this.t.armDelayMs) return 'listening';

    // Thresholds come from the background *before* this reading.
    const speechT = this.speechThreshold;
    const silenceT = this.silenceThreshold;

    this.recent.push(rms);
    const keep = Math.max(1, Math.round(this.t.floorWindowMs / this.t.tickMs));
    if (this.recent.length > keep) this.recent.shift();

    if (rms > speechT) {
      this.loudTicks += 1;
      if (!this.speechStarted && this.loudTicks >= this.t.speechOnsetTicks) {
        this.speechStarted = true;
        this.speechMs = this.loudTicks * this.t.tickMs;
      } else if (this.speechStarted) {
        this.speechMs += this.t.tickMs;
      }
      if (this.speechStarted) this.silenceStart = -1;
      return 'listening';
    }
    this.loudTicks = 0;

    if (!this.speechStarted) {
      return elapsedMs > this.t.noSpeechTimeoutMs ? 'cancel' : 'listening';
    }

    // Between the thresholds: uncertain — hold state (hysteresis).
    if (rms >= silenceT) return 'listening';

    if (this.silenceStart < 0) {
      this.silenceStart = elapsedMs;
    } else if (elapsedMs - this.silenceStart > this.t.silenceHoldMs) {
      return this.finish();
    }
    return 'listening';
  }

  private finish(): VadVerdict {
    return this.speechStarted ? 'stop' : 'cancel';
  }
}
