# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Project scaffold: the `banksman` Python package (standard library only, Python 3.11+),
  a `banksman` command with `version` and an empty `status`, both with JSON output that
  carries a `schema` number, a pytest suite, and CI on macOS and Linux.
- The lease core: one lease file per resource in `/tmp/banksman-<uid>`, changed only
  under a file lock that is held for milliseconds; the states `booting`, `ready`,
  `draining`, and `quarantined`; and the void triggers (restart, boot deadline, hard cap,
  ended owner, idle), counted in awake time. The reaper runs at the start of every command
  that reads or changes leases.
- `banksman reap`, and a `banksman status` that lists the leases.
- Resource kinds as configuration, in `~/.config/banksman/config.toml`: counted kinds,
  kinds that list their instances by name (for example port numbers), the timeouts
  for all kinds and for each kind, and an `on_void` hook that the reaper runs to end a
  void instance. A hook that fails puts the lease in quarantine. A hook program is an
  absolute path, and a hook never outlives the command that runs it. banksman refuses a
  configuration file that a user other than the user and root owns, or that group or
  others can write to. If the take-back of a quarantined lease from an earlier boot fails
  again, the lease stays quarantined.
- Fencing in both directions. Scripts run `banksman enter`, `check`, and `leave` with the
  lease id as a token: `enter` registers a script and the process group that it created for
  its work, `check` touches the lease and exits with status 3 when it is lost, and `leave`
  refuses while the script's work still runs. A void lease drains until its scripts have
  ended, confirmed by pid and start time, and it is quarantined when they still run at its
  drain timeout (`drain_timeout`, 5 minutes by default). A release while scripts still run
  drains the lease too. By default banksman sends no signal; `stop = true` for a kind lets
  the reaper send SIGTERM, then SIGKILL, but only to processes that the lease's owner process
  started, never to the agent, a program above it, or the reaper itself. A take-back that 3
  reapers started and none finished quarantines the lease. Lease files carry the registered
  scripts, so their schema is now 2.
- Quarantined leases are also kept in `~/.local/state/banksman/quarantine`, so that a
  quarantine outlives cleaners of temporary files and restarts, and
  `banksman admin release --force` removes a lease in any state.
- Holder identity for any agent. A lease records its owner (the worktree, found from the
  `.git` entry without running `git`), the issue id (from the branch name or the worktree
  directory name, through `issue_pattern`), the agent (the nearest ancestor process whose
  program is in `agents`, `claude` and `codex` by default), the agent's process as the owner
  process, a session id when the agent gives one, a purpose, and an expected hold time.
  The `[holder]` table of the configuration sets `agents` and `issue_pattern`. `status`
  shows the holder as, for example, `codex · #123 · verify the tablet layout`, and
  `status --json` carries the purpose only with `--verbose`. `banksman whoami` shows the
  holder that banksman would record. Lease files carry the holder, so their schema is now 3.
- Discovery and the allowlist. `banksman admin discover` runs the `discover` hook or the
  preset of each discovered kind, asks which instances and accounts agents may use, and
  writes `~/.config/banksman/inventory.toml`. It selects at first only what the `preselect`
  patterns match, refuses what a person leaves unselected, and never allows a refused name
  again on its own, also not with `--all` or as an instance of another kind. `--yes` and `--json` make it scriptable. It warns
  about an account that is signed in on several selected instances, and an instance whose
  accounts are not known is not offered for work that needs an account. The presets
  `android-emulator` and `android-device` find AVDs, running emulators, and physical
  devices with their facts (such as `manufacturer`, `model`, `codename`, and the
  `display_name` of an AVD) and Google accounts; they only read, and they find the SDK in a
  fixed place or in `[android]`, never through `$ANDROID_HOME`.
- Requests by properties. `banksman acquire --where <clause>` grants a permitted resource
  that is present now and matches every clause, with the operators `=`, `!=`, `~`, `>=`, and
  `<=`, and prints `RESOURCE`, `KIND`, `LEASE`, `STATE`, and `SERIAL` as `KEY=value` lines,
  or JSON with `--json`. Facts come from discovery, and the presets now also give `running`.
  banksman sets `kind`, `account` (whether an allowed account is signed in), and `tag`
  itself, and the operator assigns tags under `[tags]` in the inventory. An unknown
  attribute or kind is an error. banksman chooses the caller's lease that it keeps with
  `--lease`, then the kind with the lowest `rank` (1 for the `android-device` preset, 0 for
  other kinds), then an instance that runs before one that does not run. banksman starts
  nothing: every grant is `ready`, and the holder starts an instance that does not run.
  `--wait` waits while every matching resource is held, and exit status 4 means that they
  are all in use.
- The `on_acquire` hook of a kind resets an instance before a run gets it. When it fails,
  the lease is given back. The lease names the process that runs the hook, and no other
  command takes the resource back or hands it on until that process has ended. After the
  reset, `acquire` reads the facts of the instance again, so that it prints the current
  serial.
- `banksman touch` and `banksman release` take the lease id. `touch` records the agent
  process as the owner process, and `release --all` gives back the leases of the caller's
  agent process. A held resource is never granted again without its lease id, also not to
  the same worktree, because several agents can work in one worktree.
- Joint acquire. `banksman acquire --as <part> ...` grants several resources in one call, all
  or nothing, and prints the keys of each part with the part name as a prefix, for example
  `PHONE_RESOURCE`. A request whose parts could not all be met even with every resource
  free fails at once. Each lease has its own id, which is printed for each part, for example
  `PHONE_LEASE`, and the leases of one `acquire` form one holding: `check`, `enter`, and
  `touch` touch all of them, and `touch --lease` and `release --lease` without `--resource`
  act on all of them, with the id of any of them. The JSON of `acquire` has a list `parts`.
  While a request resets instances, all its new leases are booting and name the `acquire`,
  so that no other process hands one on before `acquire` returns, and each boot deadline
  counts the time of the resets before it.
- Accounts leased with their device. `acquire --accounts N` grants a resource on which N
  allowed accounts are signed in now, together with those accounts, and prints them as
  `ACCOUNTS`. No request gets an account while a lease in any state lists it, so two runs
  never use one account on two devices at the same time. `status` shows the accounts of each
  lease. Lease files carry the accounts and the holding, so their schema is now 4.
- The console. `banksman status` shows every resource that agents may use, free ones
  included, with its form and API level, its state, since when it is held, its last use, when
  it can be free (expected, at the latest, and if abandoned), its accounts, and its holder. It
  never shows a discovered instance that the inventory does not allow, and it shows the notes
  of discovery only as a count, because they can name such an instance; `acquire` does the
  same. `status --json` carries the same data in a new shape, a list `resources` and `notes`.
  `banksman watch` shows the table again every few seconds.
- `banksman log` shows the history of acquire, release, void, reap, quarantine, and forced
  release events, kept in `~/.local/state/banksman/log.jsonl`, with `--since` and
  `--resource`. A release and a void record how long the lease was held and the longest time
  between two touches; a quarantine records the scripts that still ran and whether stopping
  was on for the kind. Lease files carry the time of the grant and the longest time between
  two touches, so their schema is now 5.
- Optional build slots for Gradle, an example of a counted kind that is not a device. A
  project that leases devices does not need them. `banksman admin gradle-init` prints a
  Gradle init script that the operator saves in `~/.gradle/init.d/`, and `banksman admin gradle-init --build-slot` prints
  the build-slot part that it applies, which the operator saves in `~/.gradle/banksman/`.
  Every Gradle build of the user then holds a slot of the counted kind `build` while it runs,
  also when Gradle reuses the configuration cache, and gives it back when it ends. The owner
  process of the slot is the process that runs the build, so the slot is free as soon as a
  daemon ends. A build waits for a slot for 30 minutes, or for the Gradle property
  `banksman.buildSlotWait`, and then fails with a message that says that this is a limit of
  the machine. Without `banksman` on the `PATH`, the build-slot part does nothing. On a Gradle
  older than 7.4, a build runs as it does without banksman.
- `acquire --wait` says on standard error when it starts to wait, and stops waiting when its
  owner process ends.
- `banksman run --lease <id> --resource <name> -- <command>` runs a command under a lease, in
  a process group of its own that is registered with the lease before the command starts. It
  checks the lease while the command runs, stops the group when the lease is lost (SIGTERM,
  then SIGKILL), and leaves the lease only after the group has ended. It exits with the
  status of the command, or with 3 when the lease was lost, also when the command failed after
  that. When it ends first, the command ends too. When its input is the terminal, the command
  gets the terminal, so it can read input, and Ctrl-C and Ctrl-Z work as for any command. With `--where` instead of `--lease`, it leases a resource for
  the command and gives it back after the command, so a counted kind with `count = 1` is a
  mutex for one command at a time. The command gets `BANKSMAN_RESOURCE`, `BANKSMAN_KIND`, and
  `BANKSMAN_LEASE` in its environment, and `BANKSMAN_SERIAL` and `BANKSMAN_ACCOUNTS` when the
  grant has them. Neither the command nor a hook inherits these variables from the
  environment of banksman.
- `acquire` prints `KEPT` for each part: `true` for a lease of the caller's holding that
  `--lease` kept, which can have another id than the one that the caller passed. A lease whose
  owner process still runs and started the caller keeps that owner when the caller keeps or
  touches it, so a script inside `banksman run --where` cannot move the lease of the run to
  the agent.
- The serial in the lease. A lease records the serial of its instance, for example
  `emulator-5554`, from discovery only: at the grant, after an `on_acquire` reset, and
  whenever `acquire`, `run`, `status`, or `watch` runs discovery. An instance that stops loses
  its serial, a slow discovery never undoes a newer one, and one serial belongs to one held
  lease at most. `status` shows the serial in a column and in its JSON. Hooks get
  `BANKSMAN_SERIAL`; the `on_void` hook gets the serial that discovery finds at the take-back.
  `banksman run` asks discovery for the serial before its command starts, also with `--lease`,
  and gives its command only a serial that discovery confirms then, in `BANKSMAN_SERIAL`. For a kind with an Android preset, `run` and the hooks
  also set `ANDROID_SERIAL`, or a value that no device has while the serial is not known. `run`
  replaces each argument that is exactly `{serial}` or `{resource}`, and fails before the
  command starts when it needs a serial that is not known. The `android-emulator` preset finds
  the serial of an emulator that still boots. Lease files carry the serial, so their schema is
  now 6: lease files of an earlier banksman cannot be read after the upgrade, and
  `banksman admin release --force --resource <name>` frees their resources.
- The optional adb guard. `banksman admin adb-shim` prints an `sh` wrapper that the operator
  saves as `adb` in a directory first on the `PATH` of the agents. For every call, it runs
  `banksman guard adb`, which only decides: it refuses an agent's device command with exit
  status 3 when another holding leases the device, or prints the adb of the SDK of the
  configuration, which the wrapper then runs in its own place. A call passes for a person, for a device without a lease, for the holding of the lease
  id in `BANKSMAN_LEASE` or of an owner process above the caller, and for the hooks of the
  `acquire` or the reaper that the lease names. The guard finds the device as adb does, from
  `-t`, `-s`, `-d`, `-e`, `ANDROID_SERIAL`, or the only device, and for `adb emu` by the
  console port or the only emulator. It also refuses `adb kill-server`, `adb reconnect offline`,
  `adb disconnect` without an address, and `adb forward --remove-all` while an instance of
  another holder runs. `strict = true` in the new `[guard]` table also refuses a
  device that no lease of the caller has. The guard reads the lease files without the lock,
  reads the process list and runs `adb devices` only when it must, and lets the call run with
  a warning when it cannot decide. When the configuration names no `sdk` or cannot be read, the
  guard names the adb that the wrapper saved, and when banksman cannot start or fails, the
  wrapper runs that adb itself. The `banksman` command now starts the guard without loading
  the rest of the command line.
- Two schema numbers. The lease schema is the version of the lease file format, and it
  changes on every change of the format, as before. The output schema is the version of the
  JSON that the commands print and of the lines of the log. A new field does not change it,
  and a reader ignores the fields that it does not know. It changes when a field is removed
  or renamed, or changes its type or its meaning. Both are 6 now, so a reader of schema 6
  sees no change. `banksman version` shows both numbers, and `version --json` has the new
  field `lease_schema`. `banksman log` also reads the lines of schema 5 again, so the history
  from before the upgrade to schema 6 is back. The test suite records the shape of every JSON
  document, so a change of the output fails the tests until its shape is recorded.
- Remote devices of Android Device Streaming, with the new preset `android-remote`. An
  instance is a model of the catalogue of the service, such as `tokay:34`. `banksman admin
  discover` fetches the catalogue with the `android` command line tool, whose path is the new
  key `cli` in `[android]`, and the new kind key `project` names the Google Cloud project.
  Every other command reads only local files and adb, never the network. The
  `android-device` preset no longer offers a device whose serial starts with `localhost:`.
- Paid kinds. The new kind key `paid` marks a kind whose every use costs money; it is true by
  default for `android-remote`. `acquire` and `run --where` offer a paid resource only with
  the new flag `--paid`, and after every resource that costs nothing. A request whose matches
  are all paid fails at once and says why. `status` shows `remote (paid)`, and
  [docs/PROJECTS.md](docs/PROJECTS.md) has rules that ask the user before every `--paid`.
- `acquire --start` and `run --where --start`: banksman reserves and connects a remote device
  that does not run, extends the reservation to the hard cap of the lease, and grants it when
  adb lists it. The lease is `booting` meanwhile, under the boot deadline. The lease records
  the id of the reservation as soon as the tool prints it; `acquire` prints it as `HANDLE`,
  and `run` and the hooks get it in `BANKSMAN_HANDLE`. `run` and `acquire --lease` connect a
  dropped remote device again. A release keeps the reservation for the next request, and the
  reaper ends a reservation of a void lease only when banksman created it. Lease files carry
  the id, so the lease schema is now 7: lease files of an earlier banksman cannot be read
  after the upgrade, and `banksman admin release --force --resource <name>` frees their
  resources. The output schema stays 6: `status --json` and the grant only get the new fields
  `paid`, `handle`, and `ends`.
- Named holdings. `acquire --holding <label>` keeps the holding of the caller's agent process
  that has this label, as `--lease` does, also a paid one without a new `--paid`; without such
  a holding, it starts one with the label. The caller chooses the label, so an agent that lost
  its lease id, for example after a compaction of its context, gets the same device again
  with the same command. `run`, `touch`, and `release` take `--holding` instead of `--lease`,
  and `run --holding` needs `--resource` only when the holding has several resources. A
  holding that has a booting lease, for example of an `acquire` that a time limit stopped
  during its reset, is not kept, by a label or by a lease id: the request waits as for a
  resource in use, `run --holding` refuses it, and `release` gives it back. The new
  command `banksman held` lists the held leases of the agent process with their labels, lease
  ids, resources, and serials. `status --json --verbose` shows the label. Lease files carry the
  label, so the lease schema is now 8: lease files of an earlier banksman cannot be read after
  the upgrade, and `banksman admin release --force --resource <name>` frees their resources.
  The output schema stays 6.
- `banksman agent-guide` prints a guide for agents in the format of a skill: how to get a
  device with a label, run work on it, and give it back, the exit statuses, the time limits of
  the shell tools of agents, and the rules for paid devices and for `banksman admin`. Agents
  may run it, so the text always matches the installed banksman, and the operator can save it
  as a skill. The examples in the documentation now wait 5 minutes, less than the longest time
  for which Claude Code lets a command of its Bash tool run.
- [docs/PROJECTS.md](docs/PROJECTS.md): how a project uses banksman. A POSIX `sh` helper that
  a project copies into its repository and that does nothing without banksman, a stub of
  `banksman` for the tests of the project's scripts, deny rules for Claude Code and Codex
  that refuse `banksman admin`, the guide for agents, and an example of the test accounts of
  a server as a kind. Section 15 of the design has an example configuration for an Android
  project.
- Design document ([docs/DESIGN.md](docs/DESIGN.md)), recorded decisions
  ([DECISIONS.md](DECISIONS.md)), security policy and threat model
  ([SECURITY.md](SECURITY.md)), and contribution guide.

