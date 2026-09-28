"""Edge TTS backend — Microsoft neural voices via edge-tts package.

Requires: pip install edge-tts
No API key needed. Uses the same engine as Microsoft Edge browser's read-aloud.
Default voice: en-GB-RyanNeural (British male, JARVIS-like).

Prosody (rate / pitch) defaults to a calm, measured J.A.R.V.I.S. delivery:
slightly slower and slightly lower than the stock voice.  Both can be
overridden per backend instance (``[speech] tts_rate`` / ``tts_pitch`` in
config.toml) or per call.
"""

from __future__ import annotations

import asyncio
import io
import re
from typing import List, Optional

from openjarvis.core.registry import TTSRegistry
from openjarvis.speech.tts import TTSBackend, TTSResult

# JARVIS-inspired defaults: calm, measured butler delivery — a touch slower
# and a touch lower than the stock voice.  Small offsets on purpose: larger
# pitch drops (> ~-15Hz) start to sound processed on the neural voices.
_DEFAULT_VOICE = "en-GB-RyanNeural"
_DEFAULT_RATE = "-6%"    # measured, unhurried
_DEFAULT_PITCH = "-8Hz"  # slightly deeper, more authoritative

# edge-tts accepts signed percentages for rate and signed Hz for pitch.
_RATE_RE = re.compile(r"^[+-]\d{1,3}%$")
_PITCH_RE = re.compile(r"^[+-]\d{1,3}Hz$")


def _normalize_prosody(value: Optional[str], pattern: re.Pattern, fallback: str) -> str:
    """Return *value* when it is a valid edge-tts prosody string, else *fallback*.

    Accepts unsigned values ("6%", "8Hz") by assuming "+".  Anything
    malformed falls back so a bad config never breaks speech entirely.
    """
    if value is None:
        return fallback
    v = str(value).strip()
    if not v:
        return fallback
    if v[0] not in "+-":
        v = "+" + v
    return v if pattern.match(v) else fallback


def _is_spanish(text: str) -> bool:
    """Helper to detect if the input text contains significant Spanish content."""
    import re

    # Check for Spanish-specific accented characters
    if any(char in text for char in "¿¡ñáéíóúüÑÁÉÍÓÚÜ"):
        return True

    # Check ratio of common Spanish stop words (case-insensitive)
    spanish_stopwords = {
        "el", "la", "los", "las", "un", "una", "y", "en", "que", "de", "con",
        "para", "por", "su", "sus", "como", "este", "esta", "es", "son",
        "correo", "calendario", "hola", "buenos", "días", "tardes", "noches",
        "señor", "sí", "pero", "más", "mi", "mis", "al", "del", "lo", "tengo",
        "tiene", "he", "ha", "hay", "esta", "está", "estoy", "cómo", "hola",
        "gracias", "por", "favor", "su", "correo", "calendario"
    }

    words = re.findall(r"\b[a-zA-ZáéíóúüñÑ]+\b", text.lower())
    if not words:
        return False

    spanish_count = sum(1 for w in words if w in spanish_stopwords)
    return (spanish_count / len(words)) >= 0.15


@TTSRegistry.register("edge_tts")
class EdgeTTSBackend(TTSBackend):
    """Microsoft Edge neural TTS — high-quality British voices,
    free, offline-capable.
    """

    backend_id = "edge_tts"

    def __init__(
        self,
        *,
        voice_id: Optional[str] = None,
        rate: Optional[str] = None,
        pitch: Optional[str] = None,
    ) -> None:
        self.default_voice = (voice_id or "").strip() or _DEFAULT_VOICE
        self.default_rate = _normalize_prosody(rate, _RATE_RE, _DEFAULT_RATE)
        self.default_pitch = _normalize_prosody(pitch, _PITCH_RE, _DEFAULT_PITCH)

    _VOICES = [
        "en-GB-RyanNeural",    # British male — JARVIS default
        "en-GB-ThomasNeural",  # British male — alternate
        "en-GB-SoniaNeural",   # British female
        "en-GB-LibbyNeural",   # British female
        "en-US-GuyNeural",     # American male
        "en-US-AriaNeural",    # American female
        "es-CO-GonzaloNeural", # Colombian male
        "es-CO-SalomeNeural",  # Colombian female
        "es-ES-AlvaroNeural",  # Spanish male
        "es-ES-ElviraNeural",  # Spanish female
        "es-MX-JorgeNeural",   # Mexican male
    ]

    def synthesize(
        self,
        text: str,
        *,
        voice_id: str = "",
        speed: float = 1.0,
        output_format: str = "mp3",
        rate: Optional[str] = None,
        pitch: Optional[str] = None,
    ) -> TTSResult:
        import edge_tts

        # Resolve actual voice. If the selected voice is English but the text
        # is Spanish, fallback to a premium Spanish voice (respecting gender
        # mapping if possible).
        actual_voice = voice_id or self.default_voice
        if actual_voice.startswith("en-") and _is_spanish(text):
            if any(n in actual_voice for n in ("Sonia", "Libby", "Aria")):
                actual_voice = "es-ES-ElviraNeural"
            else:
                actual_voice = "es-ES-AlvaroNeural"


        # Per-call prosody overrides the instance defaults (which already
        # fold in config); invalid values fall back to the instance default.
        base_rate = _normalize_prosody(rate, _RATE_RE, self.default_rate)
        pitch_str = _normalize_prosody(pitch, _PITCH_RE, self.default_pitch)

        # Fold the speed multiplier into the rate string (e.g. 1.1 → +10%)
        rate_pct = round((speed - 1.0) * 100)
        total_pct = int(base_rate.rstrip("%")) + rate_pct
        rate_str = f"+{total_pct}%" if total_pct >= 0 else f"{total_pct}%"

        buf = io.BytesIO()

        async def _run():
            comm = edge_tts.Communicate(
                text,
                voice=actual_voice,
                rate=rate_str,
                pitch=pitch_str,
            )
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    buf.write(chunk["data"])

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    ex.submit(lambda: asyncio.run(_run())).result()
            else:
                loop.run_until_complete(_run())
        except RuntimeError:
            asyncio.run(_run())

        audio_bytes = buf.getvalue()
        return TTSResult(
            audio=audio_bytes,
            format="mp3",
            voice_id=actual_voice,
            metadata={"backend": "edge_tts", "rate": rate_str, "pitch": pitch_str},
        )


    def available_voices(self) -> List[str]:
        return self._VOICES

    def health(self) -> bool:
        try:
            import edge_tts  # noqa: F401
            return True
        except ImportError:
            return False
