# Contributing to banksman

Thanks for your interest in improving banksman. This guide covers setup, tests, the project
layout, coding standards, the sign-off requirement, and the pull-request process. By
contributing you agree that your work is licensed under the project's
[Apache License 2.0](LICENSE).

## Prerequisites

- Python 3.11 or newer.
- macOS or Linux. The test suite needs no Android SDK, device, or emulator.

## Setup and tests

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
pytest -q
```

Tests must not need a real device, a real emulator, `adb`, or network access. Anything that
runs an external program goes through a small seam that the tests replace with a fake.
Processes that the tests start and signal are the tests' own children, never processes
that already run on the machine.

## Project layout

- `src/banksman/`: the package. `cli.py` is the entry point, `store.py` keeps the lease
  files, `config.py` reads the configuration file, `fencing.py` has the rules for the
  scripts that use a resource and for the signals that the reaper may send, `hooks.py` is
  the only module that runs hook programs (through `supervisor.py`), and `system.py` is the
  only module that reads the process list and the boot id, and that sends signals.
- `tests/`: the pytest suite.
- `docs/DESIGN.md`: architecture and behavior. Read it before proposing structural changes.
- `DECISIONS.md`: resolved design decisions and how to change them.

## Coding standards

- **No runtime dependencies.** The standard library only. Test-only dependencies are fine.
- **One module per external tool.** Only one module may run `adb`, only one may run the
  emulator, and so on. Everything else works on plain data.
- **banksman knows no app.** No package names, app data, app servers, test accounts, or
  app ports in the code, the presets, or the examples.
- **Treat holder text as untrusted.** Never print it without removing terminal control
  sequences, and keep it out of the default JSON output.
- **Operator-only commands go under `banksman admin`.**
- Type hints on public functions. Comments explain why, not what.

## Developer Certificate of Origin (DCO)

All commits must be signed off. banksman uses the
[Developer Certificate of Origin 1.1](https://developercertificate.org/): by adding a
`Signed-off-by` line, you certify that you wrote the contribution or otherwise have the
right to submit it under the project's open-source license.

Sign off automatically when you commit:

```bash
git commit -s -m "Your message"
```

This appends a trailer like:

```
Signed-off-by: Your Name <you@example.com>
```

Use your real name and an email address where you can be reached. Every commit in a pull
request must carry a sign-off line.

## Pull request process

1. Fork and branch from `main`. Keep changes focused: one logical change per PR.
2. Add or update tests for your change.
3. Update [`CHANGELOG.md`](CHANGELOG.md) under `## [Unreleased]`.
4. Update `docs/DESIGN.md`, `DECISIONS.md`, or `SECURITY.md` when behavior, a decision, or
   the threat model changes.
5. Make sure `pytest -q` passes.
6. Open the PR, fill in the template, and confirm that every commit is signed off. CI must
   be green before review.

## Compatibility expectations

banksman follows [Semantic Versioning](https://semver.org/). Before `1.0.0`, anything may
change between minor versions. The surfaces that will be treated as public API are the CLI
(commands, flags, exit codes, and `KEY=value` output), the JSON output and its `schema`
number, the lease file format, the configuration files, and the hook contract.

## Code of Conduct

By participating you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md).

