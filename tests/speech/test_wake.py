"""Tests for wake-word matching and Whisper hallucination filters."""

from __future__ import annotations

from types import SimpleNamespace
from typing import List

import pytest

from openjarvis.speech.wake import (
    detect_wake,
    is_hallucination,
    is_prompt_echo,
    is_wake,
    matches_wake,
)


@pytest.mark.parametrize(
    "text",
    [
        "hey jarvis",
        "oye jarvis, ¿qué hora es?",
        "¡hola jarvis', pausa la música!",
        "jarvis.",
        "ey yarvis",
        "[jarvis]",
    ],
)
def test_matches_wake(text):
    assert matches_wake(text)


@pytest.mark.parametrize("text", ["tarjeeves", "harvester", "travis", "drivers", ""])
def test_matches_wake_rejects_lookalikes(text):
    assert not matches_wake(text)


@pytest.mark.parametrize(
    "text",
    [
        # Real false wakes recorded on music with the tiny model.
        "the user is talking to an ai assistant named jarvis. they may speak "
        "english or spanish and say,",
        "so, he is talking to an ai assistant named jarvis.",
    ],
)
def test_prompt_echo_is_not_a_wake(text):
    assert matches_wake(text)
    assert is_prompt_echo(text)
    assert not is_wake(text)


@pytest.mark.parametrize(
    "text",
    [
        "Gracias por ver el video.",
        "¡Gracias por ver!",
        "Subtítulos realizados por la comunidad de Amara.org",
        "Thanks for watching!",
        "♪♪",
        "[Música]",
        "(music)",
        "¡Suscríbete al canal!",
        "The user is dictating commands or questions to an AI assistant named Jarvis.",
    ],
)
def test_is_hallucination(text):
    assert is_hallucination(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "gracias",
        "pon música de Feid",
        "reproduce la música en Spotify",
        "¿qué hora es?",
        "thank you",
    ],
)
def test_real_commands_are_not_hallucinations(text):
    assert not is_hallucination(text)


class FakeModel:
    """Stands in for WhisperModel: returns queued transcripts."""

    def __init__(self, texts: List[str]) -> None:
        self.texts = list(texts)
        self.calls: List[dict] = []

    def transcribe(self, path, **kwargs):
        self.calls.append(kwargs)
        return [SimpleNamespace(text=self.texts.pop(0))], None


def test_detect_wake_skips_verify_when_screen_misses():
    screen, verify = FakeModel(["la la la"]), FakeModel([])
    assert detect_wake(screen, verify, "x.webm") == (False, "la la la")
    assert verify.calls == []


def test_detect_wake_requires_verify_confirmation():
    screen = FakeModel(["jarvis, where are we?"])
    verify = FakeModel(["y dónde estamos"])
    assert detect_wake(screen, verify, "x.webm") == (False, "y dónde estamos")
    assert verify.calls[0]["beam_size"] == 5


def test_detect_wake_confirmed():
    screen, verify = FakeModel([" Hey Jarvis!"]), FakeModel([" Hey Jarvis."])
    assert detect_wake(screen, verify, "x.webm") == (True, "hey jarvis.")


def test_detect_wake_rejects_prompt_echo_without_verify():
    screen = FakeModel(["The user is talking to an AI assistant named Jarvis."])
    assert detect_wake(screen, None, "x.webm")[0] is False


def test_detect_wake_without_verify_model_trusts_screen():
    assert detect_wake(FakeModel(["oye jarvis"]), None, "x.webm") == (
        True,
        "oye jarvis",
    )


def test_wake_decoding_is_greedy_and_deterministic():
    screen = FakeModel(["nada"])
    detect_wake(screen, None, "x.webm")
    kwargs = screen.calls[0]
    assert kwargs["temperature"] == 0.0
    assert kwargs["vad_filter"] is True
    assert kwargs["beam_size"] == 1
