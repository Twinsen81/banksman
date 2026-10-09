# Security policy

## Supported versions

banksman has no release yet. Security fixes go to the `main` branch until the first
release; after that, to the latest release.

## Reporting a vulnerability

**Please do not open a public GitHub issue for security problems.**

Report them privately through
[GitHub private vulnerability reporting](https://github.com/Twinsen81/banksman/security/advisories/new).
Include the version or commit, your platform, the steps to reproduce, and what an attacker
could do. You will get an answer within a week. A fix is released in a new version,
documented in [`CHANGELOG.md`](CHANGELOG.md), and credited to you unless you prefer
otherwise.

## Threat model

banksman runs on one machine, as the user who calls it. It has no daemon, no listening
port, and no network access. The design is in [docs/DESIGN.md](docs/DESIGN.md).

**The lease state belongs to one user.** When the operator turns stopping on, the reaper
sends signals to the processes and process groups that a lease file lists; by default it
sends none. If another local user could write lease files, that user could make the reaper
kill the owner's processes. So the state directory, `/tmp/banksman-<uid>`, and the directory
that keeps quarantines, `~/.local/state/banksman/quarantine`, are created with mode `0700`,
and banksman refuses to use a directory, or a lease file, that another user owns or that
group or others can write to. The kernel also checks every signal, so the reaper can never
signal another user's processes. The kernel does not protect the user's own programs, so
the reaper signals only processes that the lease's owner process started, and only a
process group that a script created for its own work. It never signals a group that
contains the agent, a program above it, or the reaper itself. Without a known owner process,
it signals nothing. Another local user can create the state directory first; banksman then
refuses to run. That is a denial of service, not a takeover.

**Configuration runs as code.** A kind's hooks are commands that banksman runs as the
user. So banksman refuses a configuration file that a user other than the user and root
owns, or that group or others can write to. The reaper runs at the start of most commands,
so a command that an agent runs can run the `on_void` hook of a void lease, in the
environment of that command. Which hooks exist is the operator's choice: banksman ships no
hook that ends anything.

**Agents are clients, not operators.** Agents acquire, touch, check, and release leases.
Discovery, which decides what agents may use, and forced release, which takes a resource
from another holder, are operator commands under `banksman admin`. An agent's permission
rules can refuse all of them with one pattern; [docs/PROJECTS.md](docs/PROJECTS.md) gives
such rules for Claude Code and Codex. The inventory that discovery writes,
`~/.config/banksman/inventory.toml`, decides what agents may use, so banksman refuses an
inventory that another user owns or that group or others can write to, as for the
configuration file. A new scan never allows a name that a person refused. That is a guardrail against a careless
agent, not a boundary against a determined one: an agent that runs as the same user can
edit files that user can edit. Running agents under a separate user account is the
stronger option. Such agents use that account's state directory, so banksman does not
coordinate their leases with the leases of the operator's own account.

**Holder text is untrusted.** The purpose that a caller gives with `--for` is written by
an agent and read by people and by other agents, so it is a channel for prompt injection
between agents. The JSON that agents read by default carries only validated fields. The
purpose appears in the human console, and in JSON only with `--verbose`. banksman removes
terminal control sequences from all output and limits the length of the purpose. The other
holder fields have fixed character sets: an issue id has only letters, digits, and `#._/-`,
so a branch name that anybody can choose gives a valid issue id or none.

**The log is history, not evidence.** `~/.local/state/banksman/log.jsonl` keeps the holder of
every lease, the purpose included. banksman creates it with mode `0600` in a directory with
mode `0700`, and refuses a log that another user owns or that group or others can write to.
Every program of the user can change it, an agent too, so banksman checks each line when it
reads it, as it checks a lease file, and leaves out a line that is not a valid event. The JSON
of `banksman log` carries the purpose only with `--verbose`, as `status` does.

**Discovered values are untrusted.** Device names, models, and notes come from devices and
from discover hooks. banksman refuses an instance name that is not a valid resource name,
keeps only short facts without control characters, removes terminal control sequences from
notes, and never shows an account name that it does not accept. banksman sets the attributes
`kind`, `account`, and `tag` itself, so a hook cannot make an instance look like one of
another kind or like one with an allowed account. A request grants only what the
configuration declares or the inventory allows, also when it names a resource, and an
account only when the inventory allows it. `status` shows only the resources that the configuration
declares or the inventory allows, and the resources that have a lease, so it never shows an
instance that the operator did not allow, which can be personal. A note of discovery can name
such an instance, so `status` and `acquire` show only why a discovery failed and how many other
notes it has; only `banksman admin discover` shows the notes.

**A grant is safe to read in a shell.** The `KEY=value` lines of `acquire` carry only the
resource name, the kind, the lease id, the state, whether the lease was kept, a serial, and
the granted accounts, and each
value has only the characters of a resource name; several accounts are separated by commas.
The name of a part, which is the prefix of its keys, has only lowercase letters, digits, and
`_`. A serial with other characters is not printed, and no other fact is printed.

**The Gradle init script runs in every build of the user.** This applies only when the
operator installs the optional build slots (DESIGN.md, section 11). On Gradle 7.4 and later,
the init script applies the build-slot part from `~/.gradle/banksman/build-slot.gradle`, so that file is as
trusted as the init script in `~/.gradle/init.d/`: whoever can write it runs code in every
build of the user. The build-slot part runs the `banksman` that the `PATH` of the build finds,
and it looks only in absolute directories, so a file in a project cannot stand in for it. The
purpose of a build slot is `gradle` and the requested tasks, which come from the command line
of the build: like any purpose, it is untrusted holder text. At the start and at the end of
each build, the build-slot part gives back every lease whose owner process is the process
that runs the build. So a lease that another caller gives that owner process, with
`--owner-pid`, does not outlive the build.

**No credentials.** For accounts on devices, banksman stores identifiers such as email
addresses, never passwords or tokens. The account is already signed in on the device; a
run needs the address, not a secret.

**A supervised run signals only the group that it created.** `banksman run` starts the
leader of a new process group for its command, and signals that group only while the leader,
its own child, is not yet reaped, so that the id of the group cannot name another group. It
passes on to the group the SIGHUP, SIGINT, SIGQUIT, and SIGTERM that it gets, and it stops the
group when the lease is lost. The command gets the resource, the kind, and the lease id in its
environment, and the serial and the accounts of its lease, all with the characters of a
resource name only. An argument that is exactly `{serial}` or `{resource}` is replaced with one
of these values; no other text of the command changes, and no shell reads it.

**The serial comes from discovery only.** `run` and the hooks act on the serial in a lease:
they put it into `BANKSMAN_SERIAL` and, for a kind with an Android preset, into
`ANDROID_SERIAL`. The holder cannot set it, so an agent cannot point the commands of its lease
at the device of another holder. A serial that is not a valid resource name is not recorded.
While the serial is not known, `ANDROID_SERIAL` names no device, so `adb` without `-s` fails
instead of choosing a device. A hook never gets the `ANDROID_SERIAL` of the command that runs
it. Before the `on_void` hook runs, the reaper asks discovery for the serial now, because the
serial in the lease can be old. The reaper runs inside any command, so a command that an agent
runs, such as `check`, can run discovery and the `discover` hook of a kind while it takes a
lease back.

**Fencing covers the scripts, not every command.** A script that registers as a user of a
lease stops itself when the lease is lost, or the reaper stops it when the operator turns
stopping on. A raw device command that an agent types, for example `adb -s <serial> ...`,
is not registered and is not stopped. The lease id that scripts pass back is not a secret:
it keeps a script of an earlier lease from acting on a newer one, and it is no protection
against a process that reads the lease files.

**The adb guard is a guardrail, not a boundary.** When the operator installs it (DESIGN.md,
section 12), an `adb` wrapper on the `PATH` refuses an agent's device command on the device of
another holder. It stops a careless agent or script, not a determined one: a call through the
full path of adb, a tool that talks to the adb server itself, a second connection to an
emulator, and a process that no agent process is above all pass, and the lease id that lets a
call pass is the same token as for the scripts. The wrapper runs only a `banksman` in an
absolute directory of the `PATH`, so a file in a project cannot stand in for it, and the guard
runs the adb of the SDK of the configuration, or the adb that the wrapper saved when the
configuration cannot be read, never one from the `PATH`. A refusal names the
holder as `status` does, with the purpose, which is untrusted text, and with terminal control
sequences removed. When banksman cannot decide, the call runs with a warning: the guard fails
open, so a broken configuration costs protection, not every device.

