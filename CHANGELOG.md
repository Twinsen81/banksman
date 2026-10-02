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
- Design document ([docs/DESIGN.md](docs/DESIGN.md)), recorded decisions
  ([DECISIONS.md](DECISIONS.md)), security policy and threat model
  ([SECURITY.md](SECURITY.md)), and contribution guide.

