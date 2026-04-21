"""Edge TTS backend — Microsoft neural voices via edge-tts package.

Requires: pip install edge-tts
No API key needed. Uses the same engine as Microsoft Edge browser's read-aloud.
Default voice: en-GB-RyanNeural (British male, JARVIS-like).
"""

from __future__ import annotations

import asyncio
import io
from typing import List

from openjarvis.core.registry import TTSRegistry
from openjarvis.speech.tts import TTSBackend, TTSResult

# JARVIS-inspired defaults: British male, slightly faster, slightly lower pitch
_DEFAULT_VOICE = "en-GB-RyanNeural"
_DEFAULT_RATE = "+8%"    # a touch crisper/faster
_DEFAULT_PITCH = "-5Hz"  # slightly lower, more authoritative


@TTSRegistry.register("edge_tts")
class EdgeTTSBackend(TTSBackend):
    """Microsoft Edge neural TTS — high-quality British voices, free, offline-capable."""

    backend_id = "edge_tts"

    _VOICES = [
        "en-GB-RyanNeural",    # British male — JARVIS default
        "en-GB-ThomasNeural",  # British male — alternate
        "en-GB-SoniaNeural",   # British female
        "en-GB-LibbyNeural",   # British female
        "en-US-GuyNeural",     # American male
        "en-US-AriaNeural",    # American female
    ]

    def synthesize(
        self,
        text: str,
        *,
        voice_id: str = _DEFAULT_VOICE,
        speed: float = 1.0,
        output_format: str = "mp3",
    ) -> TTSResult:
        import edge_tts

        # Convert speed multiplier to edge-tts rate string (e.g. 1.1 → "+10%")
        rate_pct = round((speed - 1.0) * 100)
        base_rate_pct = int(_DEFAULT_RATE.rstrip("%").lstrip("+"))
        total_pct = base_rate_pct + rate_pct
        rate_str = f"+{total_pct}%" if total_pct >= 0 else f"{total_pct}%"

        buf = io.BytesIO()

        async def _run():
            comm = edge_tts.Communicate(
                text,
                voice=voice_id or _DEFAULT_VOICE,
                rate=rate_str,
                pitch=_DEFAULT_PITCH,
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
            voice_id=voice_id or _DEFAULT_VOICE,
            metadata={"backend": "edge_tts"},
        )

    def available_voices(self) -> List[str]:
        return self._VOICES

    def health(self) -> bool:
        try:
            import edge_tts  # noqa: F401
            return True
        except ImportError:
            return False
