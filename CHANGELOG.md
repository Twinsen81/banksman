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
- Build slots for Gradle. `banksman admin gradle-init` prints a Gradle init script that the
  operator saves in `~/.gradle/init.d/`. Every Gradle build of the user then holds a slot of
  the counted kind `build` while it runs, also when Gradle reuses the configuration cache, and
  gives it back when it ends. The owner process of the slot is the process that runs the
  build, so the slot is free as soon as a daemon ends. A build waits for a slot for 30
  minutes, or for the Gradle property `banksman.buildSlotWait`, and then fails with a message
  that says that this is a limit of the machine. Without `banksman` on the `PATH`, the script
  does nothing.
- `acquire --wait` says on standard error when it starts to wait, and stops waiting when its
  owner process ends.
- Design document ([docs/DESIGN.md](docs/DESIGN.md)), recorded decisions
  ([DECISIONS.md](DECISIONS.md)), security policy and threat model
  ([SECURITY.md](SECURITY.md)), and contribution guide.

