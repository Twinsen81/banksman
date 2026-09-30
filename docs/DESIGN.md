# banksman design

**Status:** the project scaffold and the lease core exist. Section 3 is implemented, with
`banksman reap` and a `status` that lists the leases. The rest is not implemented yet: no
command can take a lease yet, and the reaper only deletes void leases, because the
take-back step of sections 4 and 5 does not exist yet. Resolved decisions and how to change
them are in [DECISIONS.md](../DECISIONS.md); the threat model is in
[SECURITY.md](../SECURITY.md).

## 1. Goals and non-goals

Goals:

- Parallel runs on one machine share scarce resources without using the same one at the
  same time. The resources are physical devices, emulators, memory for heavy builds, and
  things that live on a device, such as a signed-in account.
- A dead, hung, or forgotten run can never hold a resource forever. If a script of a void
  lease does not stop, the resource waits for a person (section 4).
- A person and an agent can both see what exists, who holds it, why, and when it will be
  free.
- It works the same for any coding agent, and for scripts that a person runs.
- It is optional. A project's scripts behave exactly as before on a machine without it.

Non-goals:

- Knowing any app. banksman does not install, clear, or reset anything for an app, and it
  knows no package names, app servers, test accounts, or app ports. What a run does on a
  device is the run's own business, in the project's own scripts.
- Coordinating more than one machine.
- A daemon or a server. Every command does its own bookkeeping.
- Storing credentials.

## 2. Concepts

| Term | Meaning |
|---|---|
| Resource | One thing that can be leased: a device serial, an emulator, a build slot, an account. |
| Kind | A class of resources, declared in configuration: how to find instances, how to reset one, how to take one back. |
| Named kind | Instances have identities: serials, emulator names, account addresses. |
| Counted kind | Instances are interchangeable slots: build slots, ports, licence seats. |
| Lease | The record that one holder may use one resource until the lease is released or void. |
| Holder | Who holds a lease: the worktree, the issue, the agent process, and a purpose. |
| Inventory | The allowlist of resources that agents may use at all. |

## 3. Leases, not locks

A lock held by "whoever started it" stays taken forever when that run dies. banksman uses
leases instead.

- **Lease files.** There is one JSON lease file per resource, in a state directory that
  belongs to the current user: `/tmp/banksman-<uid>`. The path is fixed on purpose. It does
  not come from `$TMPDIR`, because the agents, sandboxes, and scheduled jobs of one user can
  each have a different `$TMPDIR`, and callers that use different directories do not
  exclude each other. `BANKSMAN_STATE_DIR` changes the path for tests; every caller must
  then use the same value.
- **One file lock.** Bookkeeping happens under one lock file in the state directory. The
  lock is held for milliseconds, never while a resource is in use. Every write replaces a
  file with a rename, so a reader never sees half a file. The kernel releases the lock when
  its holder ends, so there is no lock-stealing logic. A holder that is alive but stopped,
  for example with Ctrl-Z, keeps the lock, so a command waits at most 10 seconds and then
  fails with an error that names the holder's pid.
- **States.** `booting`, then `ready`, then `draining`, then released. A resource that
  cannot be taken back safely becomes `quarantined`, and stays out of use until a person
  releases it.
- **Reserve before start.** A lease is written in the `booting` state, with its own
  deadline, before the resource is started. A resource that is still starting therefore
  always has a lease, and cannot be mistaken for an orphan.
- **Void triggers.** A lease is void when any of these is true. The defaults are starting
  values, to be tuned by measurement. The operator can change each of them in the
  configuration, for all kinds or for one kind. A lease keeps the values that it was
  created with.

  | Trigger | Default | Catches |
  |---|---|---|
  | the machine restarted after the lease was written | n/a | leases that describe an earlier boot |
  | `booting` past its boot deadline | 5 min | boots that never complete |
  | held longer than the hard cap | 3 h | runs that are alive but loop |
  | owner process ended, and no touch for a grace period | 5 min | crashed or killed runs |
  | no touch for the idle timeout | 20 min | hung runs, runs that forgot to release |

- **Time is awake time.** Deadlines and timeouts count only the time while the machine is
  awake: they use the monotonic clock, which stops while the machine sleeps. So a laptop
  that sleeps for an hour does not find every lease void when it wakes, and a change of the
  wall clock does not move a deadline. The cost: a deadline shown as a time of day moves
  later when the machine sleeps.
- **Owner liveness** is a pid together with that process's start time, so a pid that the
  system gives to a new process does not keep a lease. A zombie process counts as ended.
- **Owner death alone is not proof that a resource is idle.** Some agents keep a session's
  background commands running across a restart of the session process. Those commands keep
  touching the lease, and a touch can record the new owner process. So a restarted run
  keeps its resource if its lease is touched within the grace period; until that touch, the
  grace period is its idle timeout. A run that is really gone frees its resource within
  minutes.
- **Renewal needs no agent cooperation.** A project's device scripts touch the lease on
  every call. A long-running script keeps a touch loop alive for its own lifetime.
- **Reboots.** Lease state must not outlive a reboot, because the emulators it describes do
  not. A lease records the machine's boot session id, and a lease from an earlier boot is
  void, also a quarantined one: no process of an earlier boot still runs. The awake-time
  clock starts again at every boot, so this check also keeps the deadlines of two boots
  apart.
- **Reaping in two steps.** Under the lock, the reaper marks a void lease `draining`, which
  fails every ownership check, and records its own pid and start time in the lease. Outside
  the lock, it takes the resource back (section 4). Then, under the lock again, it deletes
  the lease, but only if the file still holds the same lease. Only one reaper takes a lease
  back: another reaper leaves a `draining` lease alone while the reaper named in it runs,
  and finishes it only after that reaper has ended. So a late take-back never acts on a
  newer lease.
- **Damaged lease files.** A lease file with a newer `schema` stops every command, because
  two versions of banksman must not share one state directory. A lease file that cannot be
  read keeps its resource out of use, and `status` shows it. Other resources are not
  affected.

## 4. Fencing, in both directions

Checking ownership when a script starts is not enough: one long test run can last tens of
minutes, and its lease can become void in the middle.

- **The user's side.** A script registers itself in the lease when it starts (pid, process
  group, start time) and leaves when it exits. A long-running script runs its work in its
  own process group, and its touch loop also checks ownership. When the lease is lost or
  draining, the loop kills its own process group.
- **The reaper's side: no signals by default.** For a void lease, the reaper first marks it
  `draining`, so that no further ownership check passes. By default it sends no signal to
  any process. It waits until every registered script has stopped itself, confirms that by
  pid and start time, and then frees the resource. If a script still runs after a
  deadline, the lease is quarantined: it is never handed on, and it stays in `status` until
  a person runs `banksman admin release --force`. While stopping is off, `banksman log`
  records which processes the reaper would have stopped. The reason: banksman must never
  stop a process that the user did not expect, whatever agent the user runs. A blocked
  device costs time; a stopped process costs trust.
- **Stopping is opt-in.** The operator can turn stopping on for a kind in the
  configuration. The reaper then sends SIGTERM to every registered process group or pid,
  waits, sends SIGKILL, and confirms death by pid and start time. It never trusts an exit
  code alone. For a resource that can be killed, such as an emulator, the operator can also
  let the kind's `on_void` hook kill it; the kill is then the fence.
- **Never the agent or the app.** When stopping is on, the reaper signals whole process
  groups, and the kernel does not stop it from signalling the user's own programs. An agent
  can share one process group with the app that runs it, so one signal to that group would
  stop the app and every session in it. So `enter` accepts a process group only if the script created it for its
  work: the group leader is the script or a process that the script started. A script
  without its own group registers only its pid, and the reaper then signals only that pid.
  `enter` also refuses a group that contains the owner agent process or any process above
  it. Right before each signal, the reaper checks the pid and the start time again, so it
  never signals a pid that the system gave to a new process.
- **Reaping** runs at the start of every command, and whenever a caller runs `banksman reap`.

## 5. Kinds are configuration

The lease mechanics say nothing about what is leased, so a new kind needs configuration,
not code.

- A kind declares whether it is named or counted, how to `discover` its instances, what to
  run `on_acquire` to reset one, and what to run `on_void` to take one back.
- Whether the reaper may stop the scripts of a void instance, or kill the instance, is a
  setting of the kind. It is off unless the operator turns it on, also in the presets. The
  reaper has no branch for a specific kind.
- A kind can set its own timeouts, for example no idle timeout for build slots.
- Counted kinds need no hooks.
- Hooks do device-level work only. App-level work stays in the project's scripts.
- Presets can ship for common kinds: Android emulators, Android devices, and build slots.

## 6. Requests by properties

A run asks for what it needs, not for a resource by name.

- **Facts** come from the kind's `discover` hook, and an agent cannot set them: `kind`,
  `form` (phone, tablet, watch), `api`, `image`, `abi`, `model`, `avd`, `serial`, and
  `google_account` (whether a Google account is signed in).
- **Tags** come from the operator, who assigns them in the machine's inventory with pattern
  rules.
- A request is an AND of `--where` clauses. The operators are `=`, `!=`, `~` (glob), `>=`
  and `<=`. An unknown attribute is an error, never an empty match that waits forever.

```
banksman acquire --where form=tablet --where api>=33 \
                 --for "verify the tablet layout" --expect 20m --wait 15m
```

When several resources match, banksman chooses in a fixed order:

1. A matching resource that this owner already holds.
2. A free emulator that has already booted.
3. A cold boot of a permitted emulator.

It prefers emulators to physical devices unless the request asks for `kind=physical`.

## 7. Several resources at once, and pairs

- **Joint acquire.** A run that needs several resources at the same time, for example a
  phone and a tablet, gets them in one call, under the same lock, all or nothing. Two runs
  that each hold one resource and wait for the other would deadlock.
- **Pairs.** Some resources are useful only together with a scarcer thing that lives on
  them, such as a device with a signed-in account. The account is leased in its own right,
  and a request for "a device with an account" is one joint acquire. If the account were
  only an attribute of the device, two runs could take two devices that are signed in to the
  same account, and then interfere with each other through that account.
- banksman grants exactly one pair member and names it. It does not return the list of
  candidates, because two clients that choose from the same list choose the same one.
- The account lease only stops two runs from using the same account at the same time.
  banksman keeps no app state for an account.

## 8. Discovery and the allowlist

- **A human gate that defaults to closed.** `banksman admin discover` lists what is
  attached and running, asks which instances agents may use, and writes the inventory. A
  machine with test devices on it usually also has something personal on it. Offering that
  by mistake gives an unattended agent real credentials, while leaving out a test device by
  mistake costs one more run of `discover`.
- The operator can pre-select by name patterns in the machine's configuration. banksman
  ships no default pattern. Anything outside the patterns starts unselected, and a selected
  entry outside them is shown again as a warning.
- **The inventory is policy, not a cache.** What is on a device changes, so `acquire` reads
  the machine again and grants only what is both permitted and present now. An entry that
  has gone is reported, not handed out. Running `discover` again merges: an entry that was
  refused stays refused unless the operator enables it.
- **Identifiers only.** For accounts, banksman stores addresses, never credentials. Reading
  the accounts on an Android device depends on `dumpsys` output, which is not a stable
  interface. A parse failure means "no accounts found", and banksman says so.

## 9. Holder identity

A person at the console, and other agents, must see who holds a resource and why. banksman
records the holder at acquire time from facts that every caller has, so it works the same
for any agent and for a person at a terminal.

| Field | Source |
|---|---|
| owner | the worktree: the caller's git toplevel |
| issue | `--issue`, otherwise the branch name or the worktree directory name, through a configurable pattern |
| agent | the nearest ancestor process whose program is in a configured list of agent programs (`claude`, `codex`, and so on) |
| owner process | that process's pid and start time, used for liveness |
| session | an agent-specific extra when the environment has one, never required |

If no known agent is in the ancestry, the owner is the worktree, and only the touch-based
expiry applies, unless the caller passes `--owner-pid`.

The caller adds only a purpose (`--for`) and an expected hold time (`--expect`, which a
later `touch` can update). The console then shows, for example,
`codex · #123 · verify the tablet layout`.

The purpose is text that one agent writes and other agents read. It is untrusted: see
SECURITY.md.

## 10. Status, queries, and history

- `banksman status` prints one table with every resource, free ones included: resource,
  kind, form and API level, state, holder, since, last use, free by, and the callers that
  wait. `banksman watch` refreshes the same table.
- `banksman status --json` carries the same data for agents and other tools.
- `banksman explain --where ...` answers the question "can I get one now?". For each
  matching resource it says free, held by whom and until when, not permitted, not present,
  or quarantined, and it gives the caller's place in the queue. An agent can then decide to
  wait or to do something else.
- **"Free by"** cannot be known, so it shows three labeled values:
  1. expected: the holder's `--expect`, only a hint, or unknown;
  2. at the latest: the hard cap, which the reaper enforces;
  3. if abandoned: the last touch plus the idle timeout.
- `banksman log` keeps an append-only history of acquire, release, void, reap, and
  quarantine events, with the holder. It answers "who held this device at 3 a.m.?", and it
  gives the data for tuning the idle timeout and the hard cap.

## 11. Build slots

Sessions are cheap; builds and emulators are what use up memory. Build slots are a counted
kind in the same pool.

- A Gradle init script on the machine acquires a slot when a build starts and releases it
  when the build ends. So a build that an agent starts directly is covered too, not only
  the project's own build script.
- Liveness is exact: the slot is valid while the Gradle client process is alive, capped by
  a hard limit. No touch loop is needed.
- A build waits for a free slot for a bounded time. On timeout it fails with a clear
  message, which a caller should treat as an infrastructure problem, not a code failure.

## 12. Command line (sketch)

Only `version`, `status`, and `reap` exist today. The rest is the intended shape.

```
banksman acquire --where <attr><op><value> ... [--for <text>] [--expect <duration>] [--wait <duration>]
banksman acquire --kind build --user-pid <pid> [--wait <duration>]
banksman reserve --kind emulator --where ... [--wait <duration>]   # lease in state booting
banksman ready   --resource <name> --serial <serial>               # booting -> ready
banksman enter   --resource <name> --pid <pid> --pgid <pgid>       # check, touch, register a user
banksman leave   --resource <name> --pid <pid>
banksman touch   --resource <name> [--expect <duration>]
banksman check   --resource <name>                                  # exit 0: still mine, 3: lost
banksman release [--resource <name> | --all]
banksman status  [--json] [--verbose]
banksman watch
banksman explain --where <attr><op><value> ...
banksman log     [--since <time>]
banksman reap
banksman version [--json]

banksman admin discover [--kind <kind>] [--all] [--json] [--yes]
banksman admin release --force --resource <name>
```

Commands that grant a resource print `KEY=value` lines, for example `RESOURCE=` and
`SERIAL=`, so that a shell script can read them. With `--json` they print JSON instead.

Every command that only an operator may run is under `banksman admin`, so that an agent's
permission rules can refuse all of them with one pattern, including ones added later.

## 13. The contract

- Lease files and every JSON document carry a `schema` number. A caller refuses a schema
  it does not know, instead of guessing.
- The surfaces treated as public API are the CLI (commands, flags, exit codes, `KEY=value`
  output), the JSON output, the lease file format, the configuration files, and the hook
  contract.
- There is no library API. Every client, a project's scripts and other tools alike, calls
  the command. So a machine has exactly one lease implementation, and two versions of the
  lease logic never write the same lease files.

## 14. Using banksman from a project

- Every script goes through one small helper that returns at once when `banksman` is not
  on `PATH`, so the script behaves exactly as before on a machine without it. Test the
  scripts both ways.
- Pass the leased serial to every tool, for example `adb -s <serial>`. A bare `adb` call
  acts on whatever device adb chooses.
- Release leases before waiting for a person, so that a run that waits overnight does not
  hold a device. If a run forgets, the owner and idle checks free the device later.
- Keep app setup and cleanup in the project's scripts.

## 15. Roadmap

Done:

- Project scaffold: package, CLI with `version` and `status`, tests, CI.
- The lease core: lease files, the file lock, states, void triggers, reaping.

Next:

- Kinds as configuration, with presets for Android emulators, Android devices, and build
  slots.
- Fencing in both directions and confirmed termination.
- Discovery and the allowlist.
- Requests by properties, and joint acquire.
- Holder identity for any agent.
- `status`, `watch`, `explain`, and `log`.
- Build slots through a Gradle init script.

## 16. Open questions

- Does each agent run its shell commands in their own process group? That decides how a
  script isolates the group that the reaper signals. Checked for Claude Code and Codex, both
  as command-line tools and in their Mac apps: each shell command has its own process
  group. In two of these setups, the agent shares one group with the app that runs it,
  which is why the reaper never signals such a group (section 4). In the background-server
  mode of the Codex command-line tool, the server runs the commands, and each command still
  has its own group.
- Does each agent run its commands as descendants of its own process, also inside a
  sandbox, and also for background sessions? Holder identity depends on it. Checked for the
  same setups: the parent of each command is the agent process. Still open: background
  sessions, whether one agent process serves several sessions of an app, and whether the
  Codex background server keeps running after its session ends.
- Is the idle timeout longer than the longest silent period of a legitimate run, for
  example a cold build that also waits for a build slot?
- Some agents run their shell commands in a sandbox. On macOS, a sandboxed command cannot
  start `ps`, because `ps` is a setuid program, and banksman needs `ps` to check owner
  processes. So banksman must run outside the agent's sandbox. Which setting does each agent
  need for that?
- A raw `adb -s` call from an agent is not fenced, because only the scripts register as
  users. An `adb` wrapper on `PATH`, or an agent hook that checks the lease before a device
  command, would enforce it.

