"""Remove terminal control sequences from text before it is printed."""

from __future__ import annotations

import re

# CSI sequences, OSC sequences, and other escape sequences, then single control characters.
_SEQUENCES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-_]?")
_CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def clean(text: str) -> str:
    return _CONTROLS.sub("", _SEQUENCES.sub("", text))

