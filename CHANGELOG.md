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
- Design document ([docs/DESIGN.md](docs/DESIGN.md)), recorded decisions
  ([DECISIONS.md](DECISIONS.md)), security policy and threat model
  ([SECURITY.md](SECURITY.md)), and contribution guide.

