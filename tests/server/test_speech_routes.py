"""Tests for speech API endpoints."""

from unittest.mock import MagicMock

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from openjarvis.speech._stubs import TranscriptionResult  # noqa: E402


@pytest.fixture
def mock_speech_backend():
    backend = MagicMock()
    backend.backend_id = "mock"
    backend.health.return_value = True
    backend.transcribe.return_value = TranscriptionResult(
        text="Hello world",
        language="en",
        confidence=0.95,
        duration_seconds=1.5,
        segments=[],
    )
    return backend


@pytest.fixture
def app_with_speech(mock_speech_backend):
    from fastapi import FastAPI

    from openjarvis.server.api_routes import speech_router

    app = FastAPI()
    app.state.speech_backend = mock_speech_backend
    app.include_router(speech_router)
    return app


@pytest.fixture
def client(app_with_speech):
    return TestClient(app_with_speech)


def test_transcribe_endpoint(client, mock_speech_backend):
    response = client.post(
        "/v1/speech/transcribe",
        files={"file": ("test.wav", b"fake audio data", "audio/wav")},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["text"] == "Hello world"
    assert data["language"] == "en"
    assert data["confidence"] == 0.95
    assert data["duration_seconds"] == 1.5


def test_transcribe_no_file(client):
    response = client.post("/v1/speech/transcribe")
    assert response.status_code == 400 or response.status_code == 422


def test_health_endpoint(client):
    response = client.get("/v1/speech/health")
    assert response.status_code == 200
    data = response.json()
    assert data["available"] is True
    assert data["backend"] == "mock"


def test_health_no_backend():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openjarvis.server.api_routes import speech_router

    app = FastAPI()
    app.state.speech_backend = None
    app.include_router(speech_router)
    client = TestClient(app)

    response = client.get("/v1/speech/health")
    assert response.status_code == 200
    data = response.json()
    assert data["available"] is False


# ---------------------------------------------------------------------------
# /v1/speech/synthesize
# ---------------------------------------------------------------------------


def _synth_client(backend_id: str = "edge_tts", default_voice: str = ""):
    from fastapi import FastAPI

    from openjarvis.server.api_routes import speech_router
    from openjarvis.speech.tts import TTSResult

    tts = MagicMock()
    tts.backend_id = backend_id
    tts.default_voice = default_voice
    tts.synthesize.return_value = TTSResult(audio=b"mp3-bytes", format="mp3")

    app = FastAPI()
    app.state.tts_backend = tts
    app.include_router(speech_router)
    return TestClient(app), tts


def test_synthesize_backward_compatible_text_and_voice_only():
    client, tts = _synth_client()
    response = client.post(
        "/v1/speech/synthesize",
        json={"text": "Buenas noches, señor.", "voice_id": "es-ES-AlvaroNeural"},
    )
    assert response.status_code == 200
    assert response.content == b"mp3-bytes"
    assert response.headers["content-type"] == "audio/mpeg"
    # No prosody kwargs → backend applies its configured JARVIS defaults.
    tts.synthesize.assert_called_once_with(
        "Buenas noches, señor.",
        voice_id="es-ES-AlvaroNeural",
        speed=1.0,
        output_format="mp3",
    )


def test_synthesize_passes_optional_prosody():
    client, tts = _synth_client()
    response = client.post(
        "/v1/speech/synthesize",
        json={
            "text": "Sistemas operativos.",
            "voice_id": "es-ES-AlvaroNeural",
            "rate": "-10%",
            "pitch": "-12Hz",
        },
    )
    assert response.status_code == 200
    kwargs = tts.synthesize.call_args.kwargs
    assert kwargs["rate"] == "-10%"
    assert kwargs["pitch"] == "-12Hz"


def test_synthesize_uses_backend_default_voice():
    client, tts = _synth_client(default_voice="es-ES-AlvaroNeural")
    response = client.post("/v1/speech/synthesize", json={"text": "Hola."})
    assert response.status_code == 200
    assert tts.synthesize.call_args.kwargs["voice_id"] == "es-ES-AlvaroNeural"


def test_synthesize_ignores_prosody_for_non_edge_backend():
    client, tts = _synth_client(backend_id="kokoro")
    response = client.post(
        "/v1/speech/synthesize",
        json={"text": "Hello.", "rate": "-10%", "pitch": "-12Hz"},
    )
    assert response.status_code == 200
    kwargs = tts.synthesize.call_args.kwargs
    assert "rate" not in kwargs and "pitch" not in kwargs
    assert kwargs["voice_id"] == "af_heart"
    assert kwargs["output_format"] == "wav"


def test_synthesize_missing_text():
    client, _ = _synth_client()
    response = client.post("/v1/speech/synthesize", json={"text": "  "})
    assert response.status_code == 400


def test_transcribe_drops_whisper_hallucination(client, mock_speech_backend):
    """Silence / music captions must not come back as a command."""
    mock_speech_backend.transcribe.return_value = TranscriptionResult(
        text="Subtítulos realizados por la comunidad de Amara.org",
        language="es",
        confidence=0.4,
        duration_seconds=6.0,
        segments=[],
    )
    response = client.post(
        "/v1/speech/transcribe",
        files={"file": ("test.webm", b"fake audio data", "audio/webm")},
    )
    assert response.status_code == 200
    assert response.json()["text"] == ""


def test_wake_word_detect_uses_two_stage_detector(app_with_speech, monkeypatch):
    """The endpoint returns what detect_wake decides (verify model wired in)."""
    import openjarvis.speech.wake as wake

    tiny, base = object(), object()
    app_with_speech.state._wake_word_model = tiny
    app_with_speech.state._wake_verify_model = base
    seen = {}

    def fake_detect(model, verify_model, path):
        seen["models"] = (model, verify_model)
        return False, "jarvis, como porque tiene la música"

    monkeypatch.setattr(wake, "detect_wake", fake_detect)
    response = TestClient(app_with_speech).post(
        "/v1/speech/wake-word/detect",
        files={"file": ("wake.webm", b"x" * 2000, "audio/webm")},
    )
    assert response.json() == {
        "detected": False,
        "text": "jarvis, como porque tiene la música",
    }
    assert seen["models"] == (tiny, base)
