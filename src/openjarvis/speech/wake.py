"""Wake-word matching and Whisper hallucination filters.

The wake loop sends 2.5 s mic chunks to ``/v1/speech/wake-word/detect``,
which transcribes them with a tiny Whisper model.  With music playing that
model regularly "hears" Jarvis: it recites its own ``initial_prompt`` ("the
user is talking to an AI assistant named Jarvis…") or invents a line that
starts with "Jarvis".  Each false wake opened the command mic — macOS ducks
the music — and then transcribed the song.  Measured on 7.5 min of Spanish
songs cut into 2.5 s chunks: 5 false wakes with the original tiny-only
detector, 0 with the prompt-echo filter, greedy decoding (no temperature
fallback) and a ``base``-model confirmation (~0.6 s, paid only when the
tiny model fires) — with the same hit rate on spoken wake phrases.
"""

from __future__ import annotations

import re
from typing import Any

WAKE_PROMPT = (
    "The user is talking to an AI assistant named Jarvis. "
    "They may speak English or Spanish and say 'Jarvis', "
    "'hey Jarvis', 'hi Jarvis', 'hello Jarvis', "
    "'oye Jarvis', 'hola Jarvis', or 'ey Jarvis' "
    "to get its attention."
)

# Common mishearings the tiny Whisper model produces for "Jarvis" across
# English and Spanish accents.  Excluded: near-homophones that are also
# common words ("travis", "drivers") to keep false positives low.
WAKE_TOKENS = (
    "jarvis", "jervis", "javis", "jarvey", "jarvi",
    # Spanish-accent mishearings
    "yarvis", "yarbis", "jarbis", "harvis", "charvis",
    "yarvi", "harvi", "jarvys", "jarvees",
)

# Fragments of the wake / STT prompts.  Whisper recites its prompt on
# music and noise; a transcript containing one is never the user.
_PROMPT_ECHOES = (
    "assistant named",
    "talking to an ai",
    "may speak english",
    "to get its attention",
    "dictating commands",
    "switch between them",
)

# Stock captions Whisper emits for silence / music (it learned them from
# subtitled videos).  Matched against the whole normalised transcript.
_HALLUCINATION_EXACT = {
    "gracias por ver",
    "gracias por ver el video",
    "gracias por ver el vídeo",
    "muchas gracias por ver",
    "muchas gracias por ver el video",
    "thanks for watching",
    "thank you for watching",
    "thank you so much for watching",
    "subtítulos",
    "subtitulos",
    "música",
    "musica",
    "music",
}
# ...and fragments that never belong to a spoken command.
_HALLUCINATION_FRAGMENTS = (
    "amara.org",
    "subtítulos realizados por",
    "subtitulos realizados por",
    "subtítulos por la comunidad",
    "subtitulado por",
    "suscríbete",
    "suscribete",
    "like and subscribe",
    "please subscribe",
)


def _norm(text: str) -> str:
    """Lowercase, drop brackets/notes/punctuation, collapse spaces."""
    text = text.lower()
    text = re.sub(r"[\[\]()♪♫¡!¿?.,;:\"'…*-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def matches_wake(text: str) -> bool:
    """True when a wake token starts a word ("tarjeeves" doesn't count)."""
    padded = f" {_norm(text)} "
    return any(f" {tok}" in padded for tok in WAKE_TOKENS)


def is_prompt_echo(text: str) -> bool:
    lowered = text.lower()
    return any(frag in lowered for frag in _PROMPT_ECHOES)


def is_wake(text: str) -> bool:
    return matches_wake(text) and not is_prompt_echo(text)


def is_hallucination(text: str) -> bool:
    """True for transcripts Whisper produces from silence or music."""
    lowered = text.lower()
    norm = _norm(text)
    if not norm:  # only notes / punctuation, e.g. "♪♪"
        return bool(text.strip())
    if is_prompt_echo(lowered):
        return True
    if norm in _HALLUCINATION_EXACT:
        return True
    return any(frag in lowered for frag in _HALLUCINATION_FRAGMENTS)


def transcribe_wake(model: Any, path: str, *, beam_size: int = 1) -> str:
    """Transcribe a wake chunk with the settings both stages share."""
    segments, _ = model.transcribe(
        path,
        # Language auto-detected so bilingual ES/EN speakers aren't forced
        # into English (which mangles a Spanish-accented "Jarvis").
        beam_size=beam_size,
        best_of=1,
        # No temperature fallback: the resampled retries are where the
        # model invents "Jarvis." over music, and they make it random.
        temperature=0.0,
        condition_on_previous_text=False,
        # Skip non-speech chunks entirely; on silence the model
        # hallucinates, and with the prompt it tends to echo "Jarvis".
        vad_filter=True,
        # Bias toward the wake word — the tiny model otherwise writes
        # "Jervis" / "Travis" / "drivers" for it.
        initial_prompt=WAKE_PROMPT,
    )
    return "".join(s.text for s in segments).strip().lower()


def detect_wake(model: Any, verify_model: Any, path: str) -> tuple[bool, str]:
    """Two-stage wake detection → ``(detected, text)``.

    The fast model screens every chunk; only its hits are re-checked by the
    larger ``verify_model`` (beam search), which rejects the song lines the
    tiny model turns into "Jarvis …".  Without a verify model the screen's
    verdict stands.
    """
    text = transcribe_wake(model, path)
    if not is_wake(text):
        return False, text
    if verify_model is None:
        return True, text
    confirmed = transcribe_wake(verify_model, path, beam_size=5)
    return is_wake(confirmed), confirmed
