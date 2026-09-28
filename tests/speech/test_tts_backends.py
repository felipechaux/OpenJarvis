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
    from openjarvis.speech.edge_tts_backend import EdgeTTSBackend

    TTSRegistry.register_value("edge_tts", EdgeTTSBackend)
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
            rate="-6%",
            pitch="-8Hz",
        )

        # 2. Spanish text + English male voice -> switches to Latin-American Spanish
        backend.synthesize(
            "He consultado su correo, señor.", voice_id="en-GB-RyanNeural"
        )
        mock_comm.assert_called_with(
            "He consultado su correo, señor.",
            voice="es-MX-JorgeNeural",
            rate="-6%",
            pitch="-8Hz",
        )

        # 3. Spanish text + English female voice -> switches to Spain Spanish
        backend.synthesize("Buenos días sir.", voice_id="en-GB-SoniaNeural")
        mock_comm.assert_called_with(
            "Buenos días sir.",
            voice="es-ES-ElviraNeural",
            rate="-6%",
            pitch="-8Hz",
        )




def test_edge_tts_default_prosody_is_calm_jarvis():
    from openjarvis.speech.edge_tts_backend import EdgeTTSBackend

    backend = EdgeTTSBackend()
    # Measured butler delivery: slower and lower than the stock voice.
    assert backend.default_rate == "-6%"
    assert backend.default_pitch == "-8Hz"
    assert backend.default_voice == "en-GB-RyanNeural"


def test_edge_tts_instance_prosody_and_voice_from_config():
    from openjarvis.speech.edge_tts_backend import EdgeTTSBackend

    backend = EdgeTTSBackend(
        voice_id="es-ES-AlvaroNeural", rate="-10%", pitch="-12Hz"
    )
    with patch("edge_tts.Communicate") as mock_comm:
        result = backend.synthesize("Buenas noches, señor.")
    mock_comm.assert_called_with(
        "Buenas noches, señor.",
        voice="es-ES-AlvaroNeural",
        rate="-10%",
        pitch="-12Hz",
    )
    assert result.metadata["rate"] == "-10%"
    assert result.metadata["pitch"] == "-12Hz"


def test_edge_tts_per_call_prosody_override():
    from openjarvis.speech.edge_tts_backend import EdgeTTSBackend

    backend = EdgeTTSBackend()
    with patch("edge_tts.Communicate") as mock_comm:
        backend.synthesize(
            "Todos los sistemas operativos.",
            voice_id="es-ES-AlvaroNeural",
            rate="+4%",
            pitch="-2Hz",
        )
    mock_comm.assert_called_with(
        "Todos los sistemas operativos.",
        voice="es-ES-AlvaroNeural",
        rate="+4%",
        pitch="-2Hz",
    )


def test_edge_tts_speed_folds_into_rate():
    from openjarvis.speech.edge_tts_backend import EdgeTTSBackend

    backend = EdgeTTSBackend()
    with patch("edge_tts.Communicate") as mock_comm:
        backend.synthesize("Hola.", voice_id="es-ES-AlvaroNeural", speed=1.1)
    # -6% default + 10% speed-up
    assert mock_comm.call_args.kwargs["rate"] == "+4%"


def test_edge_tts_invalid_prosody_falls_back():
    from openjarvis.speech.edge_tts_backend import (
        _RATE_RE,
        EdgeTTSBackend,
        _normalize_prosody,
    )

    backend = EdgeTTSBackend(rate="fast", pitch="deep")
    assert backend.default_rate == "-6%"
    assert backend.default_pitch == "-8Hz"

    assert _normalize_prosody("5%", _RATE_RE, "-6%") == "+5%"
    assert _normalize_prosody("", _RATE_RE, "-6%") == "-6%"
    assert _normalize_prosody(None, _RATE_RE, "-6%") == "-6%"

    with patch("edge_tts.Communicate") as mock_comm:
        backend.synthesize(
            "Hola.", voice_id="es-ES-AlvaroNeural", rate="nope", pitch="1000Hz!"
        )
    assert mock_comm.call_args.kwargs["rate"] == "-6%"
    assert mock_comm.call_args.kwargs["pitch"] == "-8Hz"


def test_speech_package_registers_edge_tts(monkeypatch):
    """The text_to_speech tool (digest audio) resolves backends via the
    registry after ``import openjarvis.speech`` — edge_tts must be there."""
    import importlib
    import sys

    import openjarvis.speech

    # Force a fresh import of the submodule so its @register runs against
    # the (per-test cleared) registry; monkeypatch restores the original.
    monkeypatch.delitem(sys.modules, "openjarvis.speech.edge_tts_backend")
    importlib.reload(openjarvis.speech)

    assert TTSRegistry.contains("edge_tts")
