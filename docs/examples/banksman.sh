# banksman.sh: lease a device with banksman when banksman is installed, and work as before when
# it is not. Copy this file into your project, and source it from the scripts that use a device:
#
#     . "$(dirname "$0")/banksman.sh"
#     banksman_acquire --where form=phone --for "UI tests" --wait 5m || exit
#     status=0
#     banksman_run ./gradlew connectedCheck || status=$?
#     banksman_release
#     exit "$status"
#
# POSIX sh. Without banksman on PATH, banksman_acquire and banksman_release do nothing, and
# banksman_run runs the command as it is. https://github.com/Twinsen81/banksman

# The variables are for the scripts that source this file.
# shellcheck disable=SC2034

# Lease a resource: banksman acquire with these options, for a request with one part. Sets
# BANKSMAN_RESOURCE, BANKSMAN_KIND, and BANKSMAN_LEASE, and BANKSMAN_SERIAL, BANKSMAN_HANDLE,
# and BANKSMAN_ACCOUNTS when the grant has them. When BANKSMAN_LEASE is set already, for example by
# the caller of the script, acquire keeps a lease of that holding that matches. Returns the
# status of acquire: 4 when every matching resource is in use. Without banksman, returns 0.
banksman_acquire() {
    command -v banksman >/dev/null 2>&1 || return 0
    if [ -n "${BANKSMAN_LEASE:-}" ]; then
        set -- --lease "$BANKSMAN_LEASE" "$@"
    fi
    _banksman_grant=$(banksman acquire "$@") || return
    BANKSMAN_SERIAL=
    BANKSMAN_HANDLE=
    BANKSMAN_ACCOUNTS=
    _banksman_kept=
    # Every value has only the characters of a resource name, so it needs no quotes.
    while IFS='=' read -r _banksman_key _banksman_value; do
        case $_banksman_key in
            RESOURCE) BANKSMAN_RESOURCE=$_banksman_value ;;
            KIND) BANKSMAN_KIND=$_banksman_value ;;
            LEASE) BANKSMAN_LEASE=$_banksman_value ;;
            SERIAL) BANKSMAN_SERIAL=$_banksman_value ;;
            HANDLE) BANKSMAN_HANDLE=$_banksman_value ;;
            ACCOUNTS) BANKSMAN_ACCOUNTS=$_banksman_value ;;
            KEPT) _banksman_kept=$_banksman_value ;;
        esac
    done <<EOF
$_banksman_grant
EOF
    # A kept lease stays with the caller, also when it is another lease of the caller's
    # holding, with an id of its own; banksman_release gives back only a new lease.
    if [ "$_banksman_kept" != true ]; then
        _banksman_new=$BANKSMAN_LEASE
        _banksman_new_resource=$BANKSMAN_RESOURCE
    fi
}

# Run a command under the lease: banksman run registers it with the lease, and stops it when the
# lease is lost. Returns the status of the command, or 3 when the lease was lost. Without
# banksman, or without a lease, runs the command as it is.
banksman_run() {
    if [ -n "${BANKSMAN_LEASE:-}" ] && command -v banksman >/dev/null 2>&1; then
        banksman run --lease "$BANKSMAN_LEASE" --resource "$BANKSMAN_RESOURCE" -- "$@"
    else
        "$@"
    fi
}

# Give back the new lease that banksman_acquire got in this script. Without banksman, or when
# acquire kept a lease of the caller, does nothing.
banksman_release() {
    if [ -n "${_banksman_new:-}" ] && command -v banksman >/dev/null 2>&1; then
        banksman release --resource "$_banksman_new_resource" --lease "$_banksman_new" >&2 ||
            return
        _banksman_new=
    fi
}

