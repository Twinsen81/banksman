"""The files that banksman prints for the operator to save: the adb wrapper, the Gradle
scripts of the build slots and of the adb guard, and the guide for agents.

`banksman admin doctor` compares the saved copies with this text. A comparison of the content
needs no version stamp in the files, and every development install has the same version.
"""

from __future__ import annotations

import shlex
from importlib import resources

from banksman import __version__

# The Gradle scripts in this package: the init script, and the build-slot part that it applies
# on Gradle 7.4 and later to take a build slot for every build.
GRADLE_INIT_SCRIPT = "gradle-init.gradle"
GRADLE_BUILD_SLOT_SCRIPT = "build-slot.gradle"
# The Gradle scripts of the adb guard: the init script, and the part that it applies on Gradle
# 7.4 and later to check the device tasks of a build.
GRADLE_GUARD_INIT_SCRIPT = "gradle-guard-init.gradle"
GRADLE_GUARD_SCRIPT = "gradle-guard.gradle"
# The adb wrapper that runs the adb guard, and the text in it that becomes the path of the real
# adb.
ADB_SHIM_SCRIPT = "adb-shim.sh"
ADB_SHIM_PATH = "@ADB@"
# The guide for agents, in the format of a skill, and the text in it that becomes the version.
AGENT_GUIDE = "agent-guide.md"
AGENT_GUIDE_VERSION = "@VERSION@"


def gradle_init() -> str:
    return _text(GRADLE_INIT_SCRIPT)


def build_slot() -> str:
    return _text(GRADLE_BUILD_SLOT_SCRIPT)


def gradle_guard_init() -> str:
    return _text(GRADLE_GUARD_INIT_SCRIPT)


def gradle_guard() -> str:
    return _text(GRADLE_GUARD_SCRIPT)


def adb_shim(adb: str) -> str:
    return _text(ADB_SHIM_SCRIPT).replace(ADB_SHIM_PATH, shlex.quote(adb))


def adb_shim_marker() -> str:
    """Return the line that names a file as an adb wrapper of banksman, of any version."""
    return _text(ADB_SHIM_SCRIPT).splitlines()[1]


def saved_adb(text: str) -> str | None:
    """Return the adb that a saved adb wrapper runs, or None when the rest of the wrapper is not
    what this banksman prints."""
    before, _, after = _text(ADB_SHIM_SCRIPT).partition(ADB_SHIM_PATH)
    if not text.startswith(before) or not text.endswith(after):
        return None
    try:
        words = shlex.split(text[len(before) : len(text) - len(after)])
    except ValueError:
        return None
    return words[0] if len(words) == 1 and adb_shim(words[0]) == text else None


def agent_guide() -> str:
    # The text of the installed version, so that an agent that reads it never follows rules of
    # another version. A copy that the operator saved names the version that printed it.
    return _text(AGENT_GUIDE).replace(AGENT_GUIDE_VERSION, __version__)


def _text(name: str) -> str:
    return resources.files("banksman").joinpath(name).read_text(encoding="utf-8")

