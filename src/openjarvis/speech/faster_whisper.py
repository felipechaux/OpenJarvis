"""Faster-Whisper speech-to-text backend (local, CTranslate2-based)."""

from __future__ import annotations

import tempfile
from typing import List, Optional

from openjarvis.core.registry import SpeechRegistry
from openjarvis.speech._stubs import Segment, SpeechBackend, TranscriptionResult

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None  # type: ignore[assignment, misc]


@SpeechRegistry.register("faster-whisper")
class FasterWhisperBackend(SpeechBackend):
    """Local speech-to-text using Faster-Whisper (CTranslate2)."""

    backend_id = "faster-whisper"

    # General-purpose prompt that biases Whisper toward an assistant-style
    # bilingual dictation context.  Hands the model a hint about the speaker
    # and the wake word so it doesn't transcribe "Jarvis" as "Jervis" /
    # "Travis", and it understands ES/EN code-switching.
    _DEFAULT_PROMPT = (
        "The user is dictating commands or questions to an AI assistant "
        "named Jarvis. They speak both English and Spanish and may switch "
        "between them mid-sentence."
    )

    def __init__(
        self,
        model_size: str = "base",
        device: str = "auto",
        compute_type: str = "auto",
    ) -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._model: Optional[WhisperModel] = None
        # Pre-warm the model in a background thread.  ``large-v3-turbo``
        # takes ~5-15s to load into RAM the first time after the server
        # starts; without pre-warming the first wake-word→transcribe
        # round-trip eats that latency and the user sees the spinner sit
        # for "way too long."  We fire-and-forget here because failures
        # are surfaced again when ``_ensure_model`` is called by the
        # first real transcribe request.
        if WhisperModel is not None:
            import threading

            def _prewarm() -> None:
                try:
                    self._ensure_model()
                except Exception:
                    pass

            threading.Thread(target=_prewarm, daemon=True, name="whisper-prewarm").start()

    def _ensure_model(self) -> WhisperModel:
        """Lazy-load the Whisper model on first use."""
        if self._model is None:
            if WhisperModel is None:
                raise ImportError(
                    "faster-whisper is not installed. "
                    "Install with: uv sync --extra speech"
                )
            compute_type = self._compute_type
            if compute_type == "auto":
                compute_type = "int8"
            for ct in [compute_type, "int8", "float32"]:
                try:
                    self._model = WhisperModel(
                        self._model_size,
                        device=self._device,
                        compute_type=ct,
                    )
                    break
                except (ValueError, RuntimeError):
                    continue
            if self._model is None:
                raise RuntimeError("Could not initialize Whisper model with any supported compute type")
        return self._model

    def transcribe(
        self,
        audio: bytes,
        *,
        format: str = "wav",
        language: Optional[str] = None,
    ) -> TranscriptionResult:
        """Transcribe audio bytes using Faster-Whisper."""
        model = self._ensure_model()

        # Write audio to a temp file (faster-whisper needs a file path)
        suffix = f".{format}" if not format.startswith(".") else format
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
            tmp.write(audio)
            tmp.flush()

            # Latency-tuned decoding parameters for interactive voice use.
            # The wake-word UX needs responses in ~1-2s end-to-end; the
            # earlier "SuperWhisper-style" settings (beam_size=5, best_of=5,
            # six-step temperature fallback chain) traded ~5-10x more
            # decoding work for marginal quality gains, which made the
            # transcribing-spinner sit for 10-30s and felt like the mic
            # never stopped listening.
            #
            # Greedy decoding on large-v3-turbo is already very accurate
            # (the "turbo" model is purpose-built for low-latency runs),
            # and ``vad_filter`` + the bilingual ``initial_prompt`` keep
            # the main hallucination sources (silence, ES/EN code-switch)
            # under control without the extra beam search cost.
            kwargs = {
                "beam_size": 1,
                "best_of": 1,
                "temperature": 0.0,
                "condition_on_previous_text": False,
                "no_speech_threshold": 0.6,
                "vad_filter": True,
                "vad_parameters": {"min_silence_duration_ms": 500},
                "initial_prompt": self._DEFAULT_PROMPT,
            }
            if language:
                kwargs["language"] = language

            segments_iter, info = model.transcribe(tmp.name, **kwargs)
            segments_list = list(segments_iter)

        # Build result
        text = "".join(seg.text for seg in segments_list).strip()
        segments = [
            Segment(
                text=seg.text.strip(),
                start=seg.start,
                end=seg.end,
                confidence=None,
            )
            for seg in segments_list
        ]

        return TranscriptionResult(
            text=text,
            language=getattr(info, "language", None),
            confidence=getattr(info, "language_probability", None),
            duration_seconds=getattr(info, "duration", 0.0),
            segments=segments,
        )

    def health(self) -> bool:
        """Check if model is loaded or loadable."""
        if self._model is not None:
            return True
        return WhisperModel is not None

    def supported_formats(self) -> List[str]:
        """Supported audio formats (same as ffmpeg/Whisper)."""
        return ["wav", "mp3", "m4a", "ogg", "flac", "webm"]
