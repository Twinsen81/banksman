"""The `banksman` command.

The adb guard runs before every adb call, so `banksman guard` starts without importing the rest
of the command line, which it does not need.
"""

from __future__ import annotations

import sys


def main() -> int:
    arguments = sys.argv[1:]
    if arguments[:1] == ["guard"]:
        from banksman import guard

        return guard.main(arguments[1:])
    from banksman import cli

    return cli.main(arguments)
