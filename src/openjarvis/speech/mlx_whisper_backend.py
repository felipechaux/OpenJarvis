"""Whisper on the Apple GPU through MLX.

On an M4 Pro, large-v3-turbo transcribes a 3 s command in about 0.8 s
here, where the same model on the CPU (faster-whisper, int8) takes 4.5 s.
That is the latency of ``small`` on the CPU with far fewer errors on
Spanish spoken through the laptop microphone (25% vs 39% word errors on
the corpus described in :mod:`openjarvis.speech.prompts`).  macOS only;
elsewhere the import fails and discovery falls back to faster-whisper.
"""

from __future__ import annotations

import tempfile
import threading
from typing import List, Optional

from openjarvis.core.registry import SpeechRegistry
from openjarvis.speech._stubs import Segment, SpeechBackend, TranscriptionResult
from openjarvis.speech.prompts import stt_prompt

try:
    import mlx_whisper
except ImportError:  # not macOS, or the speech extra is not installed
    mlx_whisper = None  # type: ignore[assignment]

try:
    # PyAV decoder shipped with faster-whisper: no ffmpeg binary needed.
    # mlx_whisper's own loader shells out to ``ffmpeg``, which the desktop
    # app's server cannot find on its PATH; the result was 30 s of silence
    # transcribed as "Gracias por ver el video".
    from faster_whisper.audio import decode_audio
except ImportError:
    decode_audio = None  # type: ignore[assignment]

SAMPLE_RATE = 16000

# One MLX call at a time for the whole process, whichever instance makes it.
# The server builds this backend twice (cli/serve.py and system/builder.py),
# and two prewarms running on the GPU at once left the model answering
# "Gracias por ver el video" to every command; once it killed the server
# with a Metal command-buffer timeout.  mlx_whisper also keeps one shared
# model cache, so there is nothing to gain from parallel decodes anyway.
_GPU_LOCK = threading.Lock()

# Whisper size names map to the MLX conversions on the Hugging Face hub;
# anything with a "/" is taken as a repo (or local path) as is.
MODEL_REPOS = {
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "turbo": "mlx-community/whisper-large-v3-turbo",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "tiny": "mlx-community/whisper-tiny",
}


def model_repo(model: str) -> str:
    name = (model or "large-v3-turbo").strip()
    return name if "/" in name else MODEL_REPOS.get(name, name)


def _decode(path: str):
    """The file as 16 kHz mono float32 samples, or the path when PyAV is
    missing (mlx_whisper then decodes it with the ffmpeg binary)."""
    if decode_audio is None:
        return path
    return decode_audio(path, sampling_rate=SAMPLE_RATE)


@SpeechRegistry.register("mlx-whisper")
class MlxWhisperBackend(SpeechBackend):
    """Local speech-to-text on the Apple GPU (mlx-whisper)."""

    backend_id = "mlx-whisper"

    def __init__(self, model: str = "large-v3-turbo") -> None:
        if mlx_whisper is None:
            raise ImportError(
                "mlx-whisper is not installed. Install with: uv sync --extra speech"
            )
        self._repo = model_repo(model)
        self._ready = False
        # Loading large-v3-turbo takes a few seconds; do it before the first
        # command rather than during it.
        threading.Thread(target=self._prewarm, daemon=True, name="mlx-prewarm").start()

    def _prewarm(self) -> None:
        try:
            import numpy as np

            with _GPU_LOCK:
                mlx_whisper.transcribe(
                    np.zeros(16000, dtype=np.float32),
                    path_or_hf_repo=self._repo,
                    language="es",
                    temperature=0.0,
                )
                self._ready = True
        except Exception:  # noqa: BLE001 - reported by the first real call
            pass

    def transcribe(
        self,
        audio: bytes,
        *,
        format: str = "wav",
        language: Optional[str] = None,
    ) -> TranscriptionResult:
        suffix = f".{format}" if not format.startswith(".") else format
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
            tmp.write(audio)
            tmp.flush()
            samples = _decode(tmp.name)
            options = {
                "path_or_hf_repo": self._repo,
                # Greedy, no temperature fallback: the resampled retries are
                # where Whisper invents text on noise, and they are slow.
                "temperature": 0.0,
                "condition_on_previous_text": False,
                "initial_prompt": stt_prompt(language),
            }
            if language:
                options["language"] = language
            with _GPU_LOCK:
                result = mlx_whisper.transcribe(samples, **options)
                self._ready = True

        raw_segments = result.get("segments") or []
        segments = [
            Segment(
                text=str(seg.get("text", "")).strip(),
                start=float(seg.get("start", 0.0)),
                end=float(seg.get("end", 0.0)),
                confidence=None,
            )
            for seg in raw_segments
        ]
        return TranscriptionResult(
            text=str(result.get("text", "")).strip(),
            language=result.get("language") or language,
            confidence=None,
            duration_seconds=max((s.end for s in segments), default=0.0),
            segments=segments,
        )

    def health(self) -> bool:
        return mlx_whisper is not None

    def supported_formats(self) -> List[str]:
        # Decoded by ffmpeg, like faster-whisper.
        return ["wav", "mp3", "m4a", "ogg", "flac", "webm"]


__all__ = ["MODEL_REPOS", "MlxWhisperBackend", "model_repo"]
