# banksman

Lease shared devices, emulators, and build slots to parallel coding agents on one machine,
so that two agents never use the same device at the same time, and a dead run never holds
one forever.

> **Status: pre-alpha, not usable yet.** This repository has the design and the lease core:
> lease files, void triggers, and the reaper. Resource kinds, discovery, requests, and the
> console are not written yet, so no command can take a lease yet. The design is in
> [docs/DESIGN.md](docs/DESIGN.md).

A *banksman* is the person on a building site who directs crane lifts and tells each
operator when it is safe to move. This tool does that job for agents that share a machine:
it decides who may use which device, records who holds it and why, and takes it back when
the holder is gone.

## The problem

Run two or three coding agents in parallel worktrees of one mobile app, and they reach for
the same things: a phone on USB, a small number of emulators, and memory for heavy builds.
One agent installs its build while another is in the middle of a UI test. A lock held by
"whoever started it" does not help, because it stays taken forever when that run dies.

## How it works

- **Leases, not locks.** A lease becomes void when its holder is gone, when it has been
  idle too long, or when it has been held longer than a hard limit. A dead run cannot keep
  a device.
- **Requests by properties.** A run asks for what it needs, for example
  `banksman acquire --where form=tablet --where api>=33`, not for a device by name.
- **Automatic holder identity.** banksman records the worktree, the issue, and the agent
  process that holds each lease. It works the same for any coding agent, and for a person
  at a terminal.
- **One view of everything.** `banksman status` shows every resource, who holds it, why,
  and when it will be free at the latest. Agents read the same data as JSON.
- **Closed by default.** An agent can only get what the operator allows. A personal phone
  or emulator on the same machine is never offered.
- **Knows no app.** banksman leases devices. What a run installs or clears on a device is
  the run's own business.
- **Optional.** Scripts that call banksman behave exactly as before on a machine where it
  is not installed.

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

