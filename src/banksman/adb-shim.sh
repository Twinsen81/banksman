#!/bin/sh
# banksman adb guard: an adb wrapper that refuses an agent's device command when another holder
# leases the device.
#
# Save it as adb in a directory of its own, make it executable, and put that directory first on
# the PATH of the agents, before the Android SDK:
#
#     mkdir -p ~/.local/share/banksman/bin
#     banksman admin adb-shim > ~/.local/share/banksman/bin/adb
#     chmod +x ~/.local/share/banksman/bin/adb
#
# After an upgrade of banksman, or a change of sdk in [android], save it again.
#
# For every call, banksman guard adb checks the leases and only decides: it prints the adb to run,
# or refuses the call with exit status 3. This script then runs adb itself, so that a banksman
# that cannot start, for example after an upgrade of Python, never blocks adb. The adb below was
# the adb of the configuration when banksman printed this script. This script runs it, and says
# that the call is not checked, when banksman is not on the PATH or fails. It uses only a banksman
# in an absolute directory of the PATH, so a file in a project cannot stand in for it.

adb=@ADB@

# This script sets the variable for the adb that it runs. When it is set here, the adb of the SDK
# is this script itself, and the two would run each other without end.
if [ -n "${BANKSMAN_GUARD:-}" ]; then
    echo "banksman: adb guard: the adb of the Android SDK runs this wrapper again. Set sdk in" \
        "[android] to the Android SDK, not to the directory of the adb wrapper" >&2
    exit 1
fi

# Not `command -v`: some shells give a program in a relative directory as an absolute path.
banksman=
rest=$PATH:
while [ -n "$rest" ]; do
    directory=${rest%%:*}
    rest=${rest#*:}
    case $directory in
    /*)
        if [ -f "$directory/banksman" ] && [ -x "$directory/banksman" ]; then
            banksman=$directory/banksman
            break
        fi
        ;;
    esac
done

if [ -z "$banksman" ]; then
    echo "banksman: adb guard: banksman is not on the PATH, so this adb call is not checked" >&2
else
    checked=$("$banksman" guard adb --fallback-adb "$adb" -- "$@")
    case $? in
    0)
        # Anything but an absolute path is not an answer of the guard.
        case $checked in
        /*) adb=$checked ;;
        *) echo "banksman: adb guard: this adb call is not checked" >&2 ;;
        esac
        ;;
    3) exit 3 ;;
    # Ctrl-C during the check ends the call.
    130) exit 130 ;;
    *) echo "banksman: adb guard: this adb call is not checked" >&2 ;;
    esac
fi
BANKSMAN_GUARD=1
export BANKSMAN_GUARD
exec "$adb" "$@"
