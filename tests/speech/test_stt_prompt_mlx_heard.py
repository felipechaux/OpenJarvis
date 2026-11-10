"""Prompt per language, the MLX backend (mlx_whisper faked), the heard log."""

from __future__ import annotations

import stat
import types

import pytest

from openjarvis.speech import heard_log
from openjarvis.speech import mlx_whisper_backend as mb
from openjarvis.speech.prompts import STT_PROMPTS, stt_prompt


def test_prompt_follows_the_forced_language():
    assert stt_prompt("es") == STT_PROMPTS["es"]
    assert stt_prompt("es-CO") == STT_PROMPTS["es"]
    assert stt_prompt("EN") == STT_PROMPTS["en"]


def test_no_prompt_without_a_language_or_for_unknown_ones():
    assert stt_prompt(None) is None
    assert stt_prompt("") is None
    assert stt_prompt("fr") is None


def test_prompts_name_topics_not_commands():
    # Whisper copies example commands over what was said.
    commands = ("pon la", "apaga", "turn on", "crea una")
    for prompt in STT_PROMPTS.values():
        assert not any(w in prompt.lower() for w in commands)


def test_model_repo_mapping():
    assert mb.model_repo("large-v3-turbo") == "mlx-community/whisper-large-v3-turbo"
    assert mb.model_repo("") == "mlx-community/whisper-large-v3-turbo"
    assert mb.model_repo("someone/custom-mlx") == "someone/custom-mlx"


class FakeMlx:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio, **options):
        self.calls.append((audio, options))
        return {
            "text": " Pon la luz azul. ",
            "language": options.get("language", "xx"),
            "segments": [{"start": 0.0, "end": 1.8, "text": " Pon la luz azul. "}],
        }


@pytest.fixture
def fake_mlx(monkeypatch):
    fake = FakeMlx()
    monkeypatch.setattr(mb, "mlx_whisper", fake)
    monkeypatch.setattr(mb, "decode_audio", lambda path, sampling_rate: ("pcm", path))
    # The prewarm thread must not race the assertions.
    idle = types.SimpleNamespace(start=lambda: None)
    monkeypatch.setattr(mb.threading, "Thread", lambda *a, **k: idle)
    return fake


def test_backend_refuses_to_start_without_mlx(monkeypatch):
    monkeypatch.setattr(mb, "mlx_whisper", None)
    with pytest.raises(ImportError):
        mb.MlxWhisperBackend()


def test_backend_transcribes_with_language_and_prompt(fake_mlx):
    backend = mb.MlxWhisperBackend("large-v3-turbo")
    result = backend.transcribe(b"\x00" * 100, format="webm", language="es")
    assert result.text == "Pon la luz azul."
    assert result.language == "es"
    assert result.duration_seconds == 1.8
    (kind, path), options = fake_mlx.calls[-1]
    assert kind == "pcm" and path.endswith(".webm")  # decoded in-process
    assert options["language"] == "es"
    assert options["initial_prompt"] == STT_PROMPTS["es"]
    assert options["temperature"] == 0.0
    assert options["condition_on_previous_text"] is False
    assert options["path_or_hf_repo"] == "mlx-community/whisper-large-v3-turbo"


def test_backend_auto_detects_without_a_prompt(fake_mlx):
    backend = mb.MlxWhisperBackend()
    backend.transcribe(b"\x00" * 100, language=None)
    _, options = fake_mlx.calls[-1]
    assert "language" not in options and options["initial_prompt"] is None


def test_heard_log_is_private_and_readable(monkeypatch, tmp_path):
    monkeypatch.setattr(heard_log, "HEARD_LOG", tmp_path / "logs" / "heard.log")
    heard_log.record("heard", "pon la luz\nazul", audio="1.8s", lang="es")
    heard_log.record("wake", "oye jarvis.")
    text = heard_log.HEARD_LOG.read_text()
    lines = text.splitlines()
    assert len(lines) == 2
    assert lines[0].endswith(" heard audio=1.8s lang=es | pon la luz azul")
    assert lines[1].endswith(" wake | oye jarvis.")
    assert stat.S_IMODE(heard_log.HEARD_LOG.stat().st_mode) == 0o600


def test_heard_log_rotates(monkeypatch, tmp_path):
    monkeypatch.setattr(heard_log, "HEARD_LOG", tmp_path / "heard.log")
    monkeypatch.setattr(heard_log, "MAX_BYTES", 10)
    heard_log.record("heard", "a long enough line")
    heard_log.record("heard", "second")
    assert (tmp_path / "heard.log.1").exists()
    assert "second" in (tmp_path / "heard.log").read_text()


def test_heard_log_never_raises(monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(heard_log, "HEARD_LOG", blocker / "heard.log")
    heard_log.record("heard", "nothing happens")
