"""Tests for the text_to_speech tool."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from openjarvis.core.registry import ToolRegistry
from openjarvis.speech.tts import TTSResult


def test_tts_tool_registered():
    from openjarvis.tools.text_to_speech import TextToSpeechTool

    ToolRegistry.register_value("text_to_speech", TextToSpeechTool)
    assert ToolRegistry.contains("text_to_speech")


def test_tts_tool_execute(tmp_path):
    from openjarvis.tools.text_to_speech import TextToSpeechTool

    tool = TextToSpeechTool()
    mock_result = TTSResult(
        audio=b"fake-audio-data",
        format="mp3",
        voice_id="jarvis",
        duration_seconds=2.5,
    )

    with patch("openjarvis.tools.text_to_speech.TTSRegistry") as mock_registry:
        mock_backend_cls = MagicMock()
        mock_backend_cls.return_value.synthesize.return_value = mock_result
        mock_registry.contains.return_value = True
        mock_registry.get.return_value = mock_backend_cls

        result = tool.execute(
            text="Good morning sir.",
            voice_id="jarvis",
            backend="cartesia",
            output_dir=str(tmp_path),
        )

    assert result.success is True
    assert "digest.mp3" in result.content
    assert (tmp_path / "digest.mp3").exists()
    assert (tmp_path / "digest.mp3").read_bytes() == b"fake-audio-data"


def test_tts_tool_empty_text():
    from openjarvis.tools.text_to_speech import TextToSpeechTool

    tool = TextToSpeechTool()
    result = tool.execute(text="")
    assert result.success is False


def _run_tool_with_mock_backend(tmp_path, **params):
    from openjarvis.tools.text_to_speech import TextToSpeechTool

    mock_result = TTSResult(audio=b"a", format="mp3")
    with patch("openjarvis.tools.text_to_speech.TTSRegistry") as mock_registry:
        mock_backend_cls = MagicMock()
        mock_backend_cls.return_value.synthesize.return_value = mock_result
        mock_registry.contains.return_value = True
        mock_registry.get.return_value = mock_backend_cls
        result = TextToSpeechTool().execute(output_dir=str(tmp_path), **params)
    return result, mock_backend_cls.return_value.synthesize


def test_tts_tool_passes_prosody_to_edge_tts(tmp_path):
    result, synth = _run_tool_with_mock_backend(
        tmp_path,
        text="Buenos días, señor.",
        voice_id="es-ES-AlvaroNeural",
        backend="edge_tts",
        rate="-6%",
        pitch="-8Hz",
    )
    assert result.success is True
    synth.assert_called_once_with(
        "Buenos días, señor.",
        voice_id="es-ES-AlvaroNeural",
        speed=1.0,
        rate="-6%",
        pitch="-8Hz",
    )


def test_tts_tool_omits_empty_or_unsupported_prosody(tmp_path):
    # Empty prosody → backend defaults (no kwargs).
    _, synth = _run_tool_with_mock_backend(
        tmp_path, text="Hola.", backend="edge_tts", rate="", pitch=""
    )
    assert "rate" not in synth.call_args.kwargs
    assert "pitch" not in synth.call_args.kwargs

    # Non-edge backends never receive prosody kwargs.
    _, synth = _run_tool_with_mock_backend(
        tmp_path, text="Hello.", backend="cartesia", rate="-6%", pitch="-8Hz"
    )
    assert "rate" not in synth.call_args.kwargs
    assert "pitch" not in synth.call_args.kwargs
