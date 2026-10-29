<p align="center"><img src="docs/images/banksman-logo.png" alt="banksman" width="600"></p>

# banksman

Lease shared Android devices and emulators to parallel coding agents on one machine.

> **Status: pre-alpha.** The commands and the file formats can still change.

## The problem

Run two or three coding agents in parallel worktrees of one app, and they reach for the same
devices. One agent installs its build while another is in the middle of a UI test. A lock does
not help, because it stays taken forever when its run dies.

## What it does

- **Leases, not locks.** A lease ends when its agent process is gone, when nobody uses it for
  too long, or at a hard limit. Nothing needs to be renewed.
- **Works with your setup.** Your AVDs, your phones on USB or Wi-Fi, the default adb server, and
  your scripts stay as they are. Scripts behave as before on a machine without banksman.
- **Requests by properties.** `banksman acquire --where form=tablet --where 'api>=33'`. Several
  devices and test accounts come in one call, all or nothing.
- **Work that stops with its lease.** `banksman run` runs a command with `ANDROID_SERIAL` set to
  the leased device, and stops the command when the lease is lost.
- **An optional guard on adb and Gradle.** It refuses a call on a device that another agent
  holds, also in scripts that do not know banksman, and tells the agent how to get a device.
- **Closed by default.** Agents get only the devices that the operator allows.
- **Emulators on request.** `--start` boots a leased emulator. banksman ends it when its holder
  is gone, on `release --end`, or after a timeout that the operator sets.
- **One view.** `banksman status` shows who holds each device, why, and until when.
- **Small.** Python standard library only, no daemon.

## Requirements

- macOS or Linux.
- Python 3.11 or newer.
- The Android SDK platform tools and emulator.

## Trying it

```bash
python3 -m venv ~/.venvs/banksman
~/.venvs/banksman/bin/pip install -e .
~/.venvs/banksman/bin/banksman version
~/.venvs/banksman/bin/banksman admin doctor
```

## Documentation

- [docs/PROJECTS.md](docs/PROJECTS.md): use banksman from a project, and set it up for agents.
- [docs/DESIGN.md](docs/DESIGN.md): the design and the roadmap.
- [DECISIONS.md](DECISIONS.md): the design decisions and why.
- [SECURITY.md](SECURITY.md): what banksman protects, and what it does not.

## License

[Apache License 2.0](LICENSE). Contributions are welcome; see
[CONTRIBUTING.md](CONTRIBUTING.md).

