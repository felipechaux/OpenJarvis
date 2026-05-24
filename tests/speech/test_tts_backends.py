"""Tests for TTS backend infrastructure."""

from __future__ import annotations

from unittest.mock import patch

from openjarvis.core.registry import TTSRegistry
from openjarvis.speech.tts import TTSResult

# ---------------------------------------------------------------------------
# TTSResult tests
# ---------------------------------------------------------------------------


def test_tts_result_dataclass():
    result = TTSResult(
        audio=b"fake-audio-bytes",
        format="mp3",
        duration_seconds=3.5,
        voice_id="jarvis-v1",
    )
    assert result.audio == b"fake-audio-bytes"
    assert result.format == "mp3"
    assert result.duration_seconds == 3.5


def test_tts_result_save(tmp_path):
    result = TTSResult(audio=b"fake-mp3-data", format="mp3")
    out = result.save(tmp_path / "test.mp3")
    assert out.exists()
    assert out.read_bytes() == b"fake-mp3-data"


# ---------------------------------------------------------------------------
# Cartesia backend tests
# ---------------------------------------------------------------------------


def test_cartesia_registered():
    from openjarvis.speech.cartesia_tts import CartesiaTTSBackend

    TTSRegistry.register_value("cartesia", CartesiaTTSBackend)
    assert TTSRegistry.contains("cartesia")


def test_cartesia_synthesize():
    from openjarvis.speech.cartesia_tts import CartesiaTTSBackend

    backend = CartesiaTTSBackend(api_key="fake-key")

    with patch(
        "openjarvis.speech.cartesia_tts._cartesia_synthesize",
        return_value=b"fake-audio-mp3-bytes",
    ):
        result = backend.synthesize("Hello world", voice_id="test-voice")

    assert result.audio == b"fake-audio-mp3-bytes"
    assert result.format == "mp3"
    assert result.voice_id == "test-voice"


# ---------------------------------------------------------------------------
# Kokoro backend tests
# ---------------------------------------------------------------------------


def test_kokoro_registered():
    from openjarvis.speech.kokoro_tts import KokoroTTSBackend

    TTSRegistry.register_value("kokoro", KokoroTTSBackend)
    assert TTSRegistry.contains("kokoro")


def test_kokoro_health_false_without_package():
    import sys
    from unittest import mock

    from openjarvis.speech.kokoro_tts import KokoroTTSBackend

    backend = KokoroTTSBackend()
    with mock.patch.dict(sys.modules, {"kokoro": None}):
        # Without kokoro installed, health returns False
        assert backend.health() is False


# ---------------------------------------------------------------------------
# OpenAI TTS backend tests
# ---------------------------------------------------------------------------


def test_openai_tts_registered():
    from openjarvis.speech.openai_tts import OpenAITTSBackend

    TTSRegistry.register_value("openai_tts", OpenAITTSBackend)
    assert TTSRegistry.contains("openai_tts")


def test_openai_tts_synthesize():
    from openjarvis.speech.openai_tts import OpenAITTSBackend

    backend = OpenAITTSBackend(api_key="fake-key")

    with patch(
        "openjarvis.speech.openai_tts._openai_tts_request",
        return_value=b"fake-openai-audio",
    ):
        result = backend.synthesize("Hello", voice_id="nova")

    assert result.audio == b"fake-openai-audio"
    assert result.voice_id == "nova"


# ---------------------------------------------------------------------------
# Edge TTS backend tests
# ---------------------------------------------------------------------------


def test_edge_tts_registered():
    from openjarvis.speech.edge_tts_backend import EdgeTTSBackend  # noqa: F401

    assert TTSRegistry.contains("edge_tts")


def test_edge_tts_is_spanish_helper():
    from openjarvis.speech.edge_tts_backend import _is_spanish

    assert _is_spanish("He consultado su correo y calendario.") is True
    assert _is_spanish("Buenos días señor, ¿cómo está?") is True
    assert _is_spanish("Hello sir, how are you today?") is False
    assert _is_spanish("I am your helpful AI assistant.") is False


def test_edge_tts_synthesize_autoswitch():
    from openjarvis.speech.edge_tts_backend import EdgeTTSBackend

    backend = EdgeTTSBackend()

    # We mock edge_tts Communicate to prevent actual network calls during test
    with patch("edge_tts.Communicate") as mock_comm:
        # 1. English text -> stays English voice
        backend.synthesize("Hello sir, how are you?", voice_id="en-GB-RyanNeural")
        mock_comm.assert_called_with(
            "Hello sir, how are you?",
            voice="en-GB-RyanNeural",
            rate="+8%",
            pitch="-5Hz",
        )

        # 2. Spanish text + English male voice -> switches to Colombian Spanish
        backend.synthesize(
            "He consultado su correo, señor.", voice_id="en-GB-RyanNeural"
        )
        mock_comm.assert_called_with(
            "He consultado su correo, señor.",
            voice="es-CO-GonzaloNeural",
            rate="+8%",
            pitch="-5Hz",
        )

        # 3. Spanish text + English female voice -> switches to Colombian Spanish
        backend.synthesize("Buenos días sir.", voice_id="en-GB-SoniaNeural")
        mock_comm.assert_called_with(
            "Buenos días sir.",
            voice="es-CO-SalomeNeural",
            rate="+8%",
            pitch="-5Hz",
        )


