"""The context Whisper is given before it hears a command.

Whisper reads ``initial_prompt`` as the preceding transcript, so it sets
the language and the vocabulary.  Measured on 20 commands recorded through
the MacBook microphone (two synthetic Spanish voices, Spanish forced):

* the old English prompt made every model drift into English, even
  translating ("Pon la luz en tono frío" → "The light is cold and
  illuminating"): 53% word errors with ``small``, 68% with large-v3-turbo;
* no prompt: 56% and 35%;
* the Spanish prompt below: 39% and 25%, at the same latency.

Full example commands in the prompt do better still, but Whisper copies
them over what was said ("crea una carpeta en Developer" became "crea una
carpeta en el escritorio"), so the prompt names topics, never commands.
"""

from __future__ import annotations

from typing import Optional

STT_PROMPTS = {
    "es": (
        "Hola Jarvis. Te hablo en español, con órdenes cortas para la casa "
        "y el computador: cosas como la luz, el brillo, los correos, las "
        "notas, las carpetas, Spotify y el clima."
    ),
    "en": (
        "Hi Jarvis. I'm giving you short commands for the house and the "
        "computer: things like the lights, email, notes, folders, Spotify "
        "and the weather."
    ),
}


def stt_prompt(language: Optional[str]) -> Optional[str]:
    """The prompt for a forced *language*; ``None`` when auto-detecting.

    With no language fixed, any prompt pulls the detection toward the
    prompt's language, which is how Spanish audio came out in English.
    """
    if not language:
        return None
    return STT_PROMPTS.get(language.lower().split("-")[0])


__all__ = ["STT_PROMPTS", "stt_prompt"]
