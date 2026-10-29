<p align="center"><img src="https://raw.githubusercontent.com/Twinsen81/banksman/main/docs/images/banksman-logo.png" alt="banksman" width="600"></p>

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

## Install

```bash
pipx install banksman
banksman version
banksman admin doctor
```

`uv tool install banksman` works too. To upgrade, run `pipx upgrade banksman`, then
`banksman admin doctor`: it names the saved files that you must save again. To work on
banksman from a clone, see [CONTRIBUTING.md][contributing].

## Documentation

- [docs/PROJECTS.md][projects]: use banksman from a project, and set it up for agents.
- [docs/DESIGN.md][design]: the design and the roadmap.
- [DECISIONS.md][decisions]: the design decisions and why.
- [SECURITY.md][security]: what banksman protects, and what it does not.

## License

[Apache License 2.0][license]. Contributions are welcome; see [CONTRIBUTING.md][contributing].

<!-- Full URLs, so that the links also work on PyPI. -->
[contributing]: https://github.com/Twinsen81/banksman/blob/main/CONTRIBUTING.md
[projects]: https://github.com/Twinsen81/banksman/blob/main/docs/PROJECTS.md
[design]: https://github.com/Twinsen81/banksman/blob/main/docs/DESIGN.md
[decisions]: https://github.com/Twinsen81/banksman/blob/main/DECISIONS.md
[security]: https://github.com/Twinsen81/banksman/blob/main/SECURITY.md
[license]: https://github.com/Twinsen81/banksman/blob/main/LICENSE

