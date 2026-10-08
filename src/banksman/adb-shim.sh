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
# For every call, banksman guard adb checks the leases, and then runs the adb of the Android SDK
# in the configuration. Without banksman on the PATH, this script runs the adb below, which was
# that adb when banksman printed this script, and says that the call is not checked. It uses
# only a banksman in an absolute directory of the PATH, so a file in a project cannot stand in
# for it.

adb=@ADB@

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
if [ -n "$banksman" ]; then
    exec "$banksman" guard adb -- "$@"
fi
echo "banksman: adb guard: banksman is not on the PATH, so this adb call is not checked" >&2
exec "$adb" "$@"
