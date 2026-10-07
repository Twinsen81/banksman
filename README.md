# banksman

Lease shared devices and emulators to parallel coding agents on one machine, so that two
agents never use the same device at the same time, and a dead run never holds one forever.

> **Status: pre-alpha, not usable yet.** This repository has the design, the lease core
> (lease files, void triggers, and the reaper), resource kinds as configuration, fencing,
> discovery with the allowlist, holder identity, requests by properties, joint acquire with
> accounts, the console (`status`, `watch`, and `log`), the supervised run (`banksman run`)
> with a helper for the scripts of a project, and optional build slots for Gradle.
> `explain` and the queue of the callers that wait are not written yet. The design is in
> [docs/DESIGN.md](docs/DESIGN.md).

A *banksman* is the person on a building site who directs crane lifts and tells each
operator when it is safe to move. This tool does that job for agents that share a machine:
it decides who may use which device, records who holds it and why, and takes it back when
the holder is gone.

## The problem

Run two or three coding agents in parallel worktrees of one mobile app, and they reach for
the same devices: a phone on USB, and a small number of emulators. One agent installs its
build while another is in the middle of a UI test. A lock held by "whoever started it" does
not help, because it stays taken forever when that run dies.

banksman is made for devices and emulators. The lease core does not depend on the type of
resource, so an operator can also declare other resources, such as host ports or a mutex for
`sdkmanager`. These are optional, and a project that only shares devices does not need them.

## How it works

- **Leases, not locks.** A lease becomes void when its holder is gone, when it has been
  idle too long, or when it has been held longer than a hard limit. A dead run cannot keep
  a device.
- **Requests by properties.** A run asks for what it needs, for example
  `banksman acquire --where form=tablet --where 'api>=33'`, not for a device by name.
- **Several resources at once.** A run that needs a phone and a tablet, or a device with a
  signed-in test account, gets them in one call, all or nothing. Two runs never use the same
  account at the same time, also not on two different devices.
- **Work that stops when its lease is lost.** `banksman run` runs a command in a process
  group of its own under a lease, and stops it when the lease is lost. With `--where`, it
  leases a resource just for that command, so a kind with one instance is a mutex.
- **Automatic holder identity.** banksman records the worktree, the issue, and the agent
  process that holds each lease. It works the same for any coding agent, and for a person
  at a terminal.
- **One view of everything.** `banksman status` shows every resource, who holds it, why,
  and when it will be free at the latest. Agents read the same data as JSON.
- **Closed by default.** An agent can only get what the operator allows. A personal phone
  or emulator on the same machine is never offered.
- **Knows no app.** banksman leases devices. What a run installs or clears on a device is
  the run's own business.
- **Other resources, when the operator wants them.** A counted kind is a number of
  interchangeable slots. For example, an operator who wants to limit parallel Gradle builds
  can install a Gradle init script that makes every build hold a build slot. This is one
  use of a counted kind. It is not part of the device setup, and a project does not need it.
- **Optional.** Scripts that call banksman behave exactly as before on a machine where it
  is not installed. [docs/PROJECTS.md](docs/PROJECTS.md) has a helper that a project copies,
  a stub for its tests, and permission rules for its agents.

## Requirements

- macOS or Linux.
- Python 3.11 or newer.
- For Android devices and emulators: the Android SDK platform tools and emulator.

## Trying it

```bash
python3 -m venv ~/.venvs/banksman
~/.venvs/banksman/bin/pip install -e .
~/.venvs/banksman/bin/banksman version
~/.venvs/banksman/bin/banksman status --json
```

## License

[Apache License 2.0](LICENSE). Contributions are welcome; see
[CONTRIBUTING.md](CONTRIBUTING.md).

