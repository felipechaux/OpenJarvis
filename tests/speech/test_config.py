"""Tests for speech configuration."""

from openjarvis.core.config import JarvisConfig, SpeechConfig


def test_speech_config_defaults():
    cfg = SpeechConfig()
    assert cfg.backend == "auto"
    assert cfg.model == "large-v3-turbo"
    assert cfg.language == ""
    assert cfg.device == "auto"
    assert cfg.compute_type == "float16"


def test_jarvis_config_has_speech():
    cfg = JarvisConfig()
    assert hasattr(cfg, "speech")
    assert isinstance(cfg.speech, SpeechConfig)
    assert cfg.speech.backend == "auto"


def test_jarvis_system_has_speech_backend():
    """JarvisSystem has a speech_backend attribute."""
    from openjarvis.system import JarvisSystem

    assert "speech_backend" in JarvisSystem.__dataclass_fields__


def test_speech_config_tts_prosody_defaults_empty():
    """Empty TTS voice/prosody → backend JARVIS defaults apply."""
    from openjarvis.core.config import DigestConfig, SpeechConfig

    cfg = SpeechConfig()
    assert cfg.tts_voice_id == ""
    assert cfg.tts_rate == ""
    assert cfg.tts_pitch == ""

    dc = DigestConfig()
    assert dc.voice_rate == ""
    assert dc.voice_pitch == ""
