# banksman design

**Status:** the project scaffold, the lease core, kinds as configuration, fencing, discovery,
holder identity, requests by properties, joint acquire with accounts, the console, the
supervised run, the serial in the lease, the optional build slots, and the optional adb guard
exist. Sections 3 to 9, 11, 12, and 15 are implemented, with
`banksman acquire`, `touch`, `release`, `reap`, `banksman whoami`, `banksman held`,
`banksman agent-guide`, the commands that scripts run (`enter`, `check`, `leave`, and `run`),
`banksman guard`, `banksman admin discover`,
`banksman admin release --force`, `banksman admin gradle-init`, and `banksman admin adb-shim`.
Of section 10, `status`, `watch`, and `log` are implemented.
`explain` and the queue of the callers that wait are not implemented yet.
Resolved decisions and how to change them are in [DECISIONS.md](../DECISIONS.md); the threat
model is in [SECURITY.md](../SECURITY.md).

## 1. Goals and non-goals

Goals:

- Parallel runs on one machine share physical devices and emulators without using the same
  one at the same time. This is the main purpose of banksman. Things that live on a device,
  such as a signed-in account, are leased with their device.
- The same leases work for other resources that the operator declares, such as host ports or
  a mutex for `sdkmanager`. These are optional: a machine that only shares devices does not
  declare them. Build slots for Gradle (section 11) are one example.
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
| Resource | One thing that can be leased: a device serial, an emulator, an account, or another resource that the operator declares, such as a host port. |
| Kind | A class of resources, declared in configuration: how to find instances, how to reset one, how to take one back. |
| Named kind | Instances have identities: serials, emulator names, account addresses, or names that the operator lists, such as port numbers. |
| Counted kind | Instances are interchangeable slots, for example a mutex with one slot, licence seats, or build slots. |
| Lease | The record that one holder may use one resource until the lease is released or void. |
| Holder | Who holds a lease: the worktree, the issue, the agent process, and a purpose. The lease id identifies one holding. |
| Inventory | The allowlist of resources that agents may use at all, and the operator's tags. |
| Fact | A property of a resource that discovery finds, such as `form` or `api`. An agent cannot set it. |
| Tag | A label that the operator gives resources in the inventory, with name patterns. |

## 3. Leases, not locks

A lock held by "whoever started it" stays taken forever when that run dies. banksman uses
leases instead.

- **Lease files.** There is one JSON lease file per resource, in a state directory that
  belongs to the current user: `/tmp/banksman-<uid>`. The path is fixed on purpose. It does
  not come from `$TMPDIR`, because the agents, sandboxes, and scheduled jobs of one user can
  each have a different `$TMPDIR`, and callers that use different directories do not
  exclude each other. `BANKSMAN_STATE_DIR` changes the path for tests; every caller must
  then use the same value.
- **Quarantines outside `/tmp`.** Cleaners of temporary files delete old files in `/tmp`,
  for example every night on macOS when a file is older than 3 days, and macOS empties
  `/tmp` at boot. But a quarantine must last until a person releases it. So a quarantined
  lease is also kept in `~/.local/state/banksman/quarantine`, where the home directory comes
  from the user database, as for the configuration file. Every command first restores a
  quarantined lease whose file in the state directory has gone. The copy is deleted only when
  the resource is free again, or when a person releases it. `BANKSMAN_QUARANTINE_DIR` changes
  the path for tests. When only `BANKSMAN_STATE_DIR` is set, the quarantines are kept next to
  that directory, so that a state directory for tests never mixes with the real quarantines.
- **One file lock.** Bookkeeping happens under one lock file in the state directory. The
  lock is held for milliseconds, never while a resource is in use. Every write replaces a
  file with a rename, so a reader never sees half a file. The kernel releases the lock when
  its holder ends, so there is no lock-stealing logic. A holder that is alive but stopped,
  for example with Ctrl-Z, keeps the lock, so a command waits at most 10 seconds and then
  fails with an error that names the holder's pid.
- **States.** `booting`, then `ready`, then `draining`, then released. A resource that
  cannot be taken back safely becomes `quarantined`, and stays out of use until a person
  runs `banksman admin release --force`.
- **Lease before start.** A run takes the lease before it starts the instance, so an instance
  that is still starting always has a lease, and cannot be mistaken for an orphan. banksman
  starts an instance only on `--start`, and only for a kind that it can start (section 6). A
  lease is `booting` only while banksman starts the instance, or the kind's `on_acquire` hook
  resets it (section 5), with its own deadline, so that a caller that ends in the middle frees
  the resource soon.
- **Void triggers.** A lease is void when any of these is true. The defaults are starting
  values, to be tuned by measurement. The operator can change each of them in the
  configuration, for all kinds or for one kind (section 5). A lease keeps the values that
  it was created with.

  | Trigger | Default | Catches |
  |---|---|---|
  | the machine restarted after the lease was written | n/a | leases that describe an earlier boot |
  | `booting` past its boot deadline | 5 min | resets that never complete |
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
  void, also a quarantined one: no process of an earlier boot still runs, so its registered
  scripts count as ended. Its take-back still runs, because an instance, such as a physical
  device, can outlive a restart of the machine. If that take-back fails again, the lease
  stays quarantined, now for the current boot. The awake-time clock starts again at every
  boot, so the boot id also keeps the deadlines of two boots apart.
- **Reaping in two steps.** Under the lock, the reaper marks a void lease `draining`, which
  fails every ownership check, and records its own pid and start time in the lease. Outside
  the lock, it ends the instance: an emulator, or a reservation of a remote device, that
  banksman started (section 8), then the kind's `on_void` hook (section 5). It also stops the registered
  scripts if stopping is on (section 4). Then, under the lock again, and only if
  the file still holds the same lease, it deletes the lease when every registered script has
  ended. Otherwise the lease drains until its scripts end, and later commands check them
  again. Only one reaper takes a lease back: another reaper leaves a `draining` lease alone
  while the reaper named in it runs, and finishes it only after that reaper has ended. So a
  late take-back never acts on a newer lease. A lease whose instance `acquire` starts or resets
  names that process in the same way (section 6): when the lease becomes void during the start
  or the reset, it drains, and it is taken back only after that process has ended. So a
  take-back never runs next to a start or a reset.
- **The serial.** A lease of an instance that runs records its serial: the address that tools
  such as adb use, for example `emulator-5554`. Only discovery (section 8), or a start that
  banksman performs (section 6), sets it, never the holder, because `banksman run` and the hooks
  act on this address, and a wrong one would make them act on the instance of another holder.
  A start of an emulator records it before the boot, because banksman chooses the console port
  itself (section 8). Every command that runs discovery records what
  it finds in the held leases of the kind: `acquire`, `run`, `status`, and `watch`. A serial
  that discovery reports is recorded. An instance that reports no serial and does not run loses
  its serial, because an emulator takes the lowest free console port, and the next emulator
  that starts can get the port of one that stopped. In every other case the lease keeps its
  serial, for example when discovery fails or does not find the instance. A booting lease keeps
  its serial too. The lease records when the discovery started, and the result of a discovery
  that started earlier does not change the serial, so a slow discovery never undoes a newer
  one. A serial belongs to one held lease at most: when a newer discovery finds it on another
  instance, also on one without a lease, the lease that had it loses it. Recording a serial is
  not a touch. `banksman run` gives its command only a serial that its own discovery confirms
  before the command starts: a discovery that fails cannot tell whether another instance has
  the serial by now.
- **The handle.** A lease of a remote device (section 8) also records the id of its
  reservation, its handle. Discovery or a start that banksman performs sets it, never the
  holder. Discovery never clears it: the reservation can outlive the connection of its device,
  and banksman uses the handle to connect the device again, or to end the reservation.
- **Damaged lease files.** A lease file with a newer lease schema stops every command,
  because two versions of banksman must not share one state directory. A lease file that
  cannot be read keeps its resource out of use, and `status` shows it. Other resources are
  not affected.

## 4. Fencing, in both directions

Checking ownership when a script starts is not enough: one long test run can last tens of
minutes, and its lease can become void in the middle.

- **The lease id is a token.** The commands that grant a lease print its id. A script passes
  it to `enter`, `check`, and `leave`, and `touch` and `release` need it too. A
  script whose token does not name the current lease of the resource has lost it. So a
  script of an earlier lease, for example one of the same worktree, never acts on a newer
  lease. Each lease has its own id, also when one `acquire` grants several leases; those
  leases form one holding (section 7).
- **The user's side.** A script registers itself with `enter` when it starts: its pid and,
  if it has one, the process group that it created for its work. `enter` also checks and
  touches the lease. A long-running script runs its work in its own process group, and runs
  `check` in a loop: `check` touches the lease and exits with status 0 while the lease is
  held, and with status 3 when the lease is lost, void, or draining. The script then stops
  its own process group. When the script no longer uses the resource, it runs `leave`.
  `leave` refuses while another process of the script still runs, so a script cannot leave
  while its work still uses the resource.
- **`banksman run` does the user's side for one command.** A shell script cannot easily create
  a process group, because macOS has no `setsid` command, and a check loop is easy to get
  wrong in each project. `run` starts a small leader program as the leader of a new process
  group, registers the leader and its group with `enter`, and only then lets the leader start
  the command in the group. While the command runs, `run` checks the lease every 10 seconds,
  or every quarter of the drain timeout or the idle timeout of the lease when that is shorter.
  When the lease is lost, it stops the group: SIGTERM, and SIGKILL to the processes that still
  run after 10 seconds. It leaves only when no process is left in the group, so it also waits
  for the processes that the command started and left behind. The leader lives until `run` is
  done with the group, so the id of the group never names another group when `run` signals
  it. When `run` ends first, for example because its caller killed it, the leader ends the
  group, so the command never runs without its check loop. `run` exits with the status of the
  command, or with status 3 when the lease was lost, also when the command failed after the
  lease was lost, because the reaper can stop the group or end the instance. `run` fences only
  the processes in the group: a daemon that the command uses, such as the Gradle daemon or the
  adb server, runs outside it. With `--where` instead of a lease id, it
  acquires a lease for the command, with itself as the owner process, and releases it after
  the command, so a counted kind with `count = 1` is a mutex for one command at a time.
  [PROJECTS.md](PROJECTS.md) has the details.
- **The reaper's side: no signals by default.** For a void lease, the reaper first marks it
  `draining`, so that no further ownership check passes. By default it sends no signal to
  any script. The only process that it signals by default is an emulator that banksman
  started itself, to end it (section 8). It waits until every registered script has stopped
  itself, confirms that by
  pid and start time, and then frees the resource. A script with a process group runs until
  no process is left in its group. If a script still runs at the drain deadline
  (`drain_timeout`, section 5), the lease is quarantined: it is never handed on, and it
  stays in `status` until a person runs `banksman admin release --force`, also when the
  script ends later. That command refuses while a banksman process takes the lease back or
  resets its instance, because that process could act on the instance of the next holder. The command that
  quarantines the lease names the processes that still run, on standard error, and the log
  records them, with whether stopping was on for the kind (section 10). The reason:
  banksman must never stop a process that the user did not expect, whatever agent the user
  runs. A blocked device costs time; a stopped process costs trust.
- **A release waits for the scripts too.** When the holder releases a lease while its
  scripts still run, the lease drains like a void lease, so that the scripts end before the
  resource is handed on. The `on_void` hook does not run: the holder gives the instance back
  as it is.
- **Stopping is opt-in.** The operator can turn stopping on for a kind with `stop = true`
  (section 5). The reaper then sends SIGTERM to the registered scripts and to their process
  groups, waits up to 10 seconds, sends SIGKILL to those that still run, and confirms their
  end by pid and start time. It never trusts an exit code alone. It proves its targets once,
  before SIGTERM, because a script that SIGTERM ends gives its work to process 1, and the
  work then no longer descends from the owner process. If the leader of a group has ended
  before SIGKILL, only the processes that were in the group get SIGKILL. A script that the
  reaper cannot stop keeps the lease draining until the drain deadline, and then the lease
  is quarantined. For a resource that can be killed, such as an emulator, the operator can
  also declare an `on_void` hook that kills it (section 5). A kill alone is not a fence: the
  next instance can get the same address, for example the same emulator serial, and a
  script of the earlier holder that still runs would then act on the new holder's instance.
  So the reaper frees the resource only after the registered scripts have also ended.
- **Never the agent or the app.** When stopping is on, the reaper signals whole process
  groups, and the kernel does not stop it from signalling the user's own programs. An agent
  can share one process group with the app that runs it, so one signal to that group would
  stop the app and every session in it. So `enter` accepts a process group only if the
  script created it for its work: the group leader and every other process in the group is
  the script or a process that the script started. A script without its own group registers
  only its pid, and the reaper then signals only that pid. `enter` also refuses to register
  the owner process of the lease or a process above it. But a script can still register a
  process that it did not start, such as its agent, when banksman does not know the owner
  process. So the reaper signals a process or a group only if the owner process still runs
  and started it: the pid, or the group leader, must be a descendant of the owner process.
  Without a known owner process, the reaper signals nothing. Right before each signal, the
  reaper checks the pid and the start time again, so it never signals a pid that the system
  gave to a new process. It never signals itself, the owner process, a process above either
  of them, or a group that holds one of them. It does not signal a group whose leader has
  ended, because the system can give that group id to a new group.
- **Reaping** runs at the start of every command, and whenever a caller runs `banksman reap`.

## 5. Kinds are configuration

The lease mechanics say nothing about what is leased, so a new kind needs configuration,
not code.

- **The configuration file** is `~/.config/banksman/config.toml`. The operator writes it,
  and banksman only reads it. The home directory comes from the user database, not from
  `$HOME`, and banksman does not use `$XDG_CONFIG_HOME`. The reason is the same as for the
  state directory: the reaper takes resources back with the hooks in this file, so every
  caller of one user must read the same file. `BANKSMAN_CONFIG` changes the path for tests;
  every caller must then use the same value. Without the file, banksman knows no kinds and
  uses the default timeouts. A key that banksman does not know is an error, so that a
  misspelled key is never ignored.
- **Kinds.** Each `[kinds.<name>]` table declares a kind. A kind with `count` is counted:
  its instances are `<name>-0`, `<name>-1`, and so on. A kind can instead list its
  instances by name with `instances`, for example port numbers; a run then gets the port
  itself as its resource. Or a kind gets its instances from discovery, with a `discover`
  hook or a `preset`, and `preselect` patterns (section 8). A kind has only one of `count`,
  `instances`, `discover`, and `preset`. An instance name must be unique across all kinds,
  because each resource has one lease file. Counted kinds need no hooks.
- **Rank.** When a request matches instances of several kinds, the kind with the lower `rank`
  is chosen first (section 6). A rank is a whole number from 0 to 1000. It is 0 by default,
  1 for a kind with the `android-device` preset, because physical devices are usually
  scarcer than emulators, and 2 for a kind with the `android-remote` preset.
- **Paid kinds.** `paid = true` marks a kind whose every use costs money, such as the remote
  devices of a cloud service. A request gets a paid resource only with `--paid`, and only
  after every resource that costs nothing (section 6). It is false by default, and true by
  default for the `android-remote` preset; the operator can set it either way. banksman knows
  no price: it marks the kind as paid, and the lease bounds how long a use lasts.
- **Timeouts.** `[defaults]` sets the timeouts of section 3 for every kind, and a kind can
  set its own. A duration is a whole number and a unit: `90s`, `20m`, or `3h`.
  `idle_timeout = "off"` turns the idle timeout off, for example for build slots. An
  `owner_grace` of `"0s"` frees a resource as soon as its owner process ends, which suits a
  holder whose end is exact, such as a build. `drain_timeout` is how long a void lease
  waits for its scripts to end before it is quarantined (section 4), 5 minutes by default.
  The check loops of the scripts must run more often than that. A lease keeps the values
  that it was created with, so a change applies to new leases only, and the void rules need
  no configuration.

  ```toml
  [defaults]
  boot_timeout = "5m"
  owner_grace = "5m"
  idle_timeout = "20m"
  hard_cap = "3h"
  drain_timeout = "5m"

  [kinds.emulator]
  preset = "android-emulator"
  preselect = ["qa_*"]
  boot_timeout = "8m"
  on_acquire = ["/usr/local/bin/reset-avd"]
  on_void = ["/usr/local/bin/stop-avd"]

  [kinds.device]
  preset = "android-device"

  # Optional kinds for resources that are not devices.
  [kinds.port]
  instances = ["9101", "9102", "9103"]

  # Optional: build slots for Gradle (section 11).
  [kinds.build]
  count = 5
  owner_grace = "0s"
  idle_timeout = "off"
  ```

- **`on_void`** is a command that ends a void instance, for example one that kills an
  emulator. The reaper runs it when it takes back a void lease of the kind. Only a declared
  hook lets the reaper end an instance: banksman ships no such command, and without one the
  reaper ends nothing, with one exception: the reaper ends an emulator, or the reservation of a
  remote device, that banksman itself started with `--start` (section 8), and then runs the
  hook. If the hook exits with status 0, the instance is gone. If it fails,
  does not end in time, or cannot start, the lease is quarantined. The reaper reads the hook
  from the current configuration, not from the lease, so when the operator removes a hook,
  it stops at once, also for leases that are already void. The serial in the lease can be old,
  so before the hook runs, the reaper asks discovery for the serial of the instance now, and
  the hook gets that one. The reaper runs at the start of every command, so a command of any
  caller, such as `check`, can then wait for that discovery: up to 60 seconds for a `discover`
  hook, or the timeouts of adb, and then the `on_void` hook. It does not hold the lock
  meanwhile, so other commands go on.
- **Signals are a separate setting.** `stop = true` in the table of a kind lets the reaper
  signal the scripts of a void lease of that kind (section 4). It is off by default, and
  `[defaults]` cannot turn it on for all kinds. Ending an instance and signalling processes
  are two separate choices, because ending an emulator that banksman leased is much less
  risky than signalling a process. Like the hook, the reaper reads `stop` from the current
  configuration, so turning it off takes effect at once. The reaper has no branch for a
  specific kind.
- **The hook contract.** A hook is a list of strings: a program and its arguments. The
  program is an absolute path, because a hook runs in the environment of whichever command
  reaps, and a program that one caller's `PATH` finds can be missing for another caller.
  banksman runs it without a shell, in the directory `/`, with no input, and with
  `BANKSMAN_RESOURCE` and `BANKSMAN_KIND` in its environment, and it discards the output.
  `BANKSMAN_SERIAL` is set when the serial of the instance is known, and `BANKSMAN_HANDLE`
  when the lease records the id of a reservation (section 3). A hook of a kind with an
  Android preset also gets `ANDROID_SERIAL`: the serial, or a value that no device has while
  the serial is not known, so that `adb` without `-s` fails instead of acting on a device that
  adb chooses. Without `BANKSMAN_SERIAL`, the instance does not run, or its serial is not
  known; a hook that ends the instance exits with status 0 when it is gone, and checks that a
  serial still names the instance before it acts on it. A hook that runs adb runs it by its
  full path too, because the adb guard can refuse an adb from the `PATH` (section 12).
  The hook runs in its own process group. banksman kills that group after 60 seconds, and
  also when the command that runs the hook ends first, for example because its caller
  stopped it, so that a hook never outlives its reaper. A reaper that ends in the middle
  leaves the lease to the next reaper, which runs the hook again, so a hook must be safe to
  run again: for example, it exits with status 0 when the instance is already gone. When 3
  reapers have started the take-back of a lease and none has finished, for example because
  their callers stopped them, the lease is quarantined, so that a slow hook does not run in
  every command.
- **`start_args`** is a list of options of the `emulator` command, for example
  `["-no-window", "-no-snapshot-save"]`, valid only with the `android-emulator` preset.
  banksman adds them after `-avd <name> -port <port>` when it starts an AVD on `--start`
  (section 8). banksman chooses the AVD and the console port itself, so `-avd`, `-port`,
  `-ports`, and `@<name>` are refused here.
- **`unmanaged`** decides what a request gets of an instance that runs, but that banksman did
  not start (section 6): `"skip"`, the default, never grants it, and `"grant"` grants it after
  the instances that banksman knows. It is valid only with the `android-emulator` and
  `android-remote` presets, whose instances banksman starts.
- **`on_acquire`** is a command that resets an instance before a run gets it. `acquire` runs
  it for each new lease of the kind, after it writes the lease and outside the lock, with the
  hook contract above. Its serial is the one that the discovery of the request found, seconds
  before. If it fails, the lease is given back (section 6). banksman ships no such command.
- Hooks do device-level work only. App-level work stays in the project's scripts.

## 6. Requests by properties

A run asks for what it needs, not for a resource by name.

- **Facts** come from the kind's `discover` hook or preset (section 8), and an agent cannot
  set them: `form` (phone, tablet, watch), `api`, `image`, `abi`, `manufacturer`, `model`,
  `codename`, `display_name`, `avd`, `serial`, `running` (whether the instance runs now), and,
  for a remote device, `handle` and `ends`. banksman sets four attributes itself, and a hook
  cannot set them: `kind`; `paid`, from the kind (section 5); `account`, which is true when an
  allowed account is signed in on the instance now, false when its accounts are known and none
  of them is allowed, and missing when its accounts are not known; and `tag`.
- **Tags** come from the operator, who assigns them in the inventory with name patterns
  (section 8). A tag gives no access.
- A request is an AND of `--where` clauses. The operators are `=`, `!=`, `~` (a glob, with `*`
  and `?`), `>=`, and `<=`. Text is compared without regard to case, because devices give,
  for example, both `samsung` and `Google`. `>=` and `<=` compare whole numbers only. For
  `tag`, `=` and `~` ask whether the resource has a matching tag, and `!=` asks whether it
  has none. A fact that a resource does not have matches no clause, with any operator, so
  an unknown value never passes for a known one.
- An attribute that is neither one of the above nor a fact that discovery finds now is an
  error, never an empty match that waits forever. So is `kind=` with a kind that the
  configuration does not declare.
- Only what the configuration declares or the inventory allows, and what is present now, can
  match. A request cannot widen access, also not when it names a resource.

Quote the clauses in a shell, because `>` and `<` redirect:

```
banksman acquire --where form=tablet --where 'api>=33' \
                 --for "verify the tablet layout" --expect 20m --wait 5m
```

When several resources match, banksman chooses in a fixed order:

1. A resource of the caller's holding, if the caller passes the lease id with `--lease`, or
   the label of the holding with `--holding` (section 9), and the resource still matches. The lease is touched, and it records the caller's agent
   process as its owner process, unless its owner process still runs and started the
   caller, as `banksman run` does for a script that it runs (section 9).
2. A resource that costs nothing before a paid one, whatever the ranks are (section 5).
3. A kind with a lower `rank` (section 5). So a request that does not name a kind gets an
   emulator before a physical device, and a physical device before a remote one.
4. An instance that runs before one that does not run, which saves the time and the memory
   of a start. Of the instances that run, one that banksman knows comes before an unmanaged
   one, which a request gets only with `unmanaged = "grant"`.
5. The resource name.

`acquire` without `--lease` or `--holding` never grants a held resource, also not to the same
worktree:
several agents can work in one worktree at the same time, so only the lease id tells their
holdings apart (section 9).

- **The grant.** `acquire` prints `KEY=value` lines: `RESOURCE`, `KIND`, `LEASE` (the lease
  id that the scripts pass back), `STATE`, `KEPT` (`true` for a lease of the caller's holding
  that `--lease` or `--holding` kept, otherwise `false`), `SERIAL` when the lease records it,
  `HANDLE` when the lease records the id of a reservation (section 3), and `ACCOUNTS` when the
  request asks for accounts (section 7). A kept lease can have another id than the
  one that the caller passed, because each lease of a holding has its own id. Each value has only the characters of a
  resource name, so that a shell script can use it as it is: a serial with other characters
  is not printed. With `--json`, it prints a list `parts` with the same values for each part
  of the request, and `paid` for each part.
- **Paid resources need an acknowledgement.** Without `--paid`, the resources of a paid kind
  (section 5) are not candidates. A request whose candidates are all paid, for example
  `--where kind=remote`, fails at once with exit status 1, and says that the resources are
  paid and need `--paid` after the user agreed to the cost. A request that waits or finds every
  match in use also says how many paid resources would match. With `--paid`, paid resources
  take part, after every resource that costs nothing. `--paid` is an acknowledgement, not a
  filter: `--where paid=true` asks for a paid resource. A lease that the caller keeps with
  `--lease` or `--holding` needs no new acknowledgement. The store checks the acknowledgement again under its
  lock, so a kept lease that ends meanwhile cannot let a new paid lease through. `status` marks
  a paid kind as `remote (paid)`, and `status --json` carries `paid` for each resource. The
  permission rules of the agents can ask the user before every `--paid` (section 15).
- **Starting an instance.** Without `--start`, every grant is `ready`. The holder checks
  whether the instance runs, and starts it if it does not, for example with the `emulator`
  command. With `--start`, banksman starts a granted instance that does not run when it can
  start its kind: an AVD of the `android-emulator` preset, or a model of the `android-remote`
  preset (section 8). The new leases of the request are `booting` during the start, under the
  boot deadline, and name the `acquire`, as during a reset. The start records the serial in
  the lease before the instance is ready: for an emulator, the serial of the console port that
  banksman chose, before the boot; for a remote device, when it is connected, and its handle
  as soon as the tool prints it. So the adb guard protects the instance from the first
  moment. When the start fails, or does not end by the
  boot deadline, every new lease of the request is given back, and `acquire` fails with the
  reason. A lease that the caller keeps with `--lease` is not started again. The starts and
  the resets of one request run one after another, so the boot deadline of each new lease
  also counts the boot timeout of every other start of the request.
- **Instances that banksman did not start.** An instance of the `android-emulator` or the
  `android-remote` preset that runs, but that banksman did not start and did not see run when
  a lease of it ended, is `unmanaged`: a person or a program outside banksman started it, for
  example an emulator in Android Studio, or a remote device that a person reserved by hand.
  Without this rule, banksman would give such an instance to the next request first, because
  it runs. So a request does not get it: it counts as a resource in use, a request that waits
  waits for it, and the request says why. `unmanaged = "grant"` for the kind (section 5) lets
  a request get it, after the instances that banksman knows. When a lease ends, by a release
  or by a take-back, banksman records the instance that runs then, so an emulator that the
  holder started under its lease is not unmanaged when it is free. banksman knows an emulator
  by its pid and start time, and a remote device by the id of its reservation (section 8).
- **The agent can choose.** `acquire` grants one resource for each part of the request and
  names it. It does not return a list, because two callers that choose from the same list
  choose the same resource. An agent that needs a specific resource asks for it, for example
  with `--where avd=qa_phone`.
- **`on_acquire`.** When the kind declares an `on_acquire` hook (section 5), the lease is
  `booting` while the hook runs, and then `ready`. If a hook fails, every new lease of the
  request is given back, and `acquire` fails. A failed reset does not make the instance
  unsafe for the next holder, as a failed take-back does, so the instance is not quarantined.
  While a request has resets, every new lease of the request is `booting` and names the
  `acquire`, so that no other process frees a lease of one part while another part is reset.
  The resets run one after another, so each boot deadline also counts 60 seconds, the longest
  time of a hook, for each reset of the request. At the end, every new lease must still be
  held; otherwise all of them are given back. A lease that the caller
  keeps with `--lease` is not reset again. The hook can restart the instance, so `acquire`
  reads its facts again after the reset, and records and prints the serial that discovery finds
  then. An instance that discovery does not find with a serial then, also because discovery
  fails, has no serial.
- **No other process acts on the instance during a start or a reset.** The lease names the
  `acquire` that starts the instance or runs the hook, and a hook never outlives that process
  (section 5). When the lease becomes void or is released meanwhile, it drains, and the
  resource is taken back or freed only after that process has ended. `admin release --force`
  refuses meanwhile.
- **Waiting.** While every matching resource is held, `acquire` fails with exit status 4. For
  a request with several parts, this is while the parts cannot all be granted (section 7).
  With `--wait`, it looks again every 5 seconds until the wait ends, and each look reads the
  configuration, the inventory, and the machine again. So the reaper of a caller that waits
  also uses the hooks of the current configuration (section 5). When it starts to wait, it
  says so once on standard error, with the resources that are in use, so that a person sees
  why a command, such as a build, does not go on. It stops waiting, and fails, when its owner
  process (section 9) ends, because nothing would use what it gets. When no permitted resource that is present now matches, held or free,
  `acquire` fails at once, also with `--wait`, because only a held resource can become free.
  There is no queue yet: when many callers wait, a caller can get a resource before another
  caller that started to wait earlier.

## 7. Several resources at once, and accounts

- **Joint acquire.** A run that needs several resources at the same time, for example a
  phone and a tablet, gets them in one call, all or nothing. Two runs that each hold one
  resource and wait for the other would deadlock. Each `--as <name>` starts a part of the
  request, and the `--where` and `--accounts` options after it belong to that part:

  ```
  banksman acquire --as phone --where form=phone --accounts 1 \
                   --as tablet --where form=tablet --for "verify the sign-in" --wait 5m
  PHONE_RESOURCE=R5CR1234ABC
  PHONE_KIND=device
  PHONE_LEASE=4f1c2b0e9d7a4c55a0f3e6b1c2d3e4f5
  PHONE_STATE=ready
  PHONE_KEPT=false
  PHONE_SERIAL=R5CR1234ABC
  PHONE_ACCOUNTS=qa@example.test
  TABLET_RESOURCE=qa_tablet
  TABLET_KIND=emulator
  TABLET_LEASE=9e8d7c6b5a4f4e3d2c1b0a9f8e7d6c5b
  TABLET_STATE=ready
  TABLET_KEPT=false
  ```

  A part name has lowercase letters, digits, and `_`, and becomes the prefix of the keys of
  its part. A request without `--as` has one part, and its keys have no prefix. A request has
  at most 8 parts.
- **The choice.** banksman chooses for all parts under the lock of the lease store. Each part
  takes its matches in the order of choice (section 6), and an earlier part chooses first.
  But a part takes a later match when a later part can get nothing else, so that a request
  that can be met is met. A resource and an account go to one part only. When the parts
  could not all be met even if every resource were free, for example two tablet parts on a
  machine with one tablet, `acquire` fails at once. When they could, but resources that they
  need are in use, it fails with exit status 4, or waits with `--wait`.
- **A time limit.** The search for a choice stops after 1 second, because other commands wait
  at most 10 seconds for the lock. A request that needs more time fails, and the agent can
  ask for fewer parts or more specific clauses. Requests of a usual size need milliseconds.
- **One holding.** Each lease has its own id, which its scripts pass back (section 4). The
  leases that one `acquire` grants form one holding. `check`, `enter`, and `touch` touch every
  held lease of the holding, so that a tablet does not time out while a long test runs on the
  phone of the same holding. The id of any lease of a holding names the holding:
  `touch --lease <id>` touches all its leases, `release --lease <id>` gives back all its
  resources, and `release --resource <name> --lease <id>` gives back one. A request with
  `--lease <id>` first keeps the leases of that holding that still match its parts. While
  the holding has a held lease, the new leases of the request join it, each with a new id,
  so that a script or a cleanup of an earlier lease of the same resource never acts on the
  new lease. A holding can also have a label, which names it instead of a lease id
  (section 9); its new leases get the label too.
- **Accounts.** Some resources are useful only together with a scarcer thing that lives on
  them, such as a device with a signed-in account. `--accounts N` asks for a resource on
  which at least N allowed accounts are signed in now, and leases N of them with it, in the
  order of their names. The grant names them in `ACCOUNTS`, separated by commas. banksman
  grants exactly the accounts that it names. It does not return the list of candidates,
  because two clients that choose from the same list choose the same account. A test that
  needs several accounts, for example to share something between two users, asks for that
  many.
- **An account is in use while a lease lists it.** The lease of the device lists the
  accounts that came with it, and no request gets an account while a lease in any state
  lists it. So two runs never use one account on two devices at the same time. The account
  stays in use while its lease drains or is quarantined, because the scripts of the holder
  can still use it then. If the account were only an attribute of the device, two runs could
  take two devices that are signed in to the same account, and then interfere with each
  other through that account. A kept lease can get more accounts on its device, and gives
  none back until it is released.
- Leasing an account only stops two runs from using the same account at the same time.
  banksman keeps no app state for an account. A run that does not ask for an account can
  still get a device whose account another run uses on another device. A run that must not
  touch an allowed account asks for `--where account=false`.
- A lease file that cannot be read keeps its own resource out of use, but its accounts are
  not known, so banksman can grant them with another device.

## 8. Discovery and the allowlist

- **A human gate that defaults to closed.** `banksman admin discover` runs the discovery of
  every kind that has a `discover` hook or a preset, shows what it found, asks which
  instances and which accounts agents may use, and writes the inventory. A machine with
  test devices on it usually also has something personal on it. Offering that by mistake
  gives an unattended agent real credentials, while leaving out a test device by mistake
  costs one more run of `discover`.
- **Pre-selection.** The `preselect` patterns of a kind, and `preselect` in the `[accounts]`
  table, name what discover selects at first. `*` matches any text, `?` matches one
  character, and case matters. banksman ships no default pattern. Anything outside the
  patterns starts unselected, and a selected name outside them is shown again as a warning.
  `--all` selects everything at first. A pattern gives no access by itself: only the
  inventory does.

  ```toml
  [kinds.device]
  preset = "android-device"

  [accounts]
  preselect = ["*@example.test"]
  ```

- **Accounts.** discover shows only the accounts of the selected instances. An account is
  allowed or refused as a whole, wherever it is signed in, because two runs that use one
  account on two devices interfere through the account (section 7). discover warns when one
  selected account is signed in on several selected instances. An instance whose accounts
  are not known is not offered for work that needs an account.
- **A refusal is a person's decision.** In the interactive mode, a name that the person sees
  and leaves unselected is refused. A refused name stays refused when discover runs again,
  also when a pattern or `--all` matches it, until a person selects it again or edits the
  inventory. With `--yes`, the selection is the pre-selection, and an unselected name gets
  no decision, so that a pattern that the operator adds later can still select it. A name
  that discover does not find keeps its decision, so a phone that is unplugged during
  discover keeps its place. A name belongs to one kind: when the inventory has it under one
  kind, allowed or refused, a scan of another kind leaves it out, so a refused device does
  not come back as an instance of another kind. So a new scan can never silently widen
  access.
- **Scriptable.** `--yes` takes the pre-selection without questions and writes the
  inventory. `--json` prints what discover found and the inventory that it would write, and
  writes it only together with `--yes`. Like the questions, it shows only the accounts of
  the selected instances; for each instance it says only whether its accounts are known. Without a terminal, discover needs one of the two.
  `--kind` limits discover to one kind; the decisions for the other kinds do not change.
- **The inventory is policy, not a cache.** The inventory is
  `~/.config/banksman/inventory.toml`, with the home directory from the user database, as
  for the configuration file. It holds only names: the allowed and the refused instances of
  each kind, and the allowed and the refused accounts. Under `[tags]`, it also holds the
  operator's tags: each tag and the name patterns of the resources that have it, for example
  `lab = ["qa_*"]`. discover keeps the tags when it writes the file. The operator can edit
  it. banksman
  refuses an inventory that another user owns or that group or others can write to.
  `acquire` reads the machine again and grants only what is both allowed and present now
  (section 6). An entry that has gone is reported, not handed out. Counted kinds and kinds
  that list their instances need no inventory: the configuration that declares them is the
  permission. `BANKSMAN_INVENTORY` changes the path for tests; when only `BANKSMAN_CONFIG`
  is set, the inventory is next to that file.
- **The `discover` hook** is a program and its arguments, run like `on_void` (section 5),
  with `BANKSMAN_KIND` in its environment and 60 seconds to end. It prints one JSON
  document:

  ```json
  {"schema": 1,
   "instances": [{"name": "R5CR1234ABC",
                  "facts": {"form": "phone", "api": 35},
                  "accounts": ["qa@example.test"],
                  "note": "a short text for the operator"}],
   "notes": ["0A1B2C3D is unauthorized, so it is not offered"]}
  ```

  `schema` is the version of this format, now 1. `name` is a resource name. A fact name
  has lowercase letters, digits, and `_`, and a fact is a text of at most 128 characters
  without control characters, a whole number, or `true` or `false`. banksman sets the
  attributes `kind`, `account`, and `tag` itself (section 6), so a hook cannot make an
  instance look like one of another kind, or like one with an allowed account. `running`
  tells whether the instance runs now.
  `accounts` lists the accounts on the instance; without it, or with `null`, they are not
  known. A value that is not valid is left out with a note, never repaired, and a note never
  shows a name that is not valid. A name can belong to only one kind.
- **Presets.** `preset = "android-emulator"` finds the AVDs, with the facts `avd`,
  `display_name` (the name that the AVD manager shows), `manufacturer`, `api`, `image`,
  `abi`, `form`, and `running`, and, for an emulator that runs, its `serial` and its
  accounts. When adb cannot list the running emulators, `running` is left out. An emulator that
  adb lists as `offline`, because the adb daemon in it has not started yet, for example while
  it boots, runs too: its console gives its AVD name, so its serial is known from the start,
  and its accounts are not known yet. An AVD also runs when a process has the arguments
  `-avd <name>`, or an emulator program has the argument `@<name>`: adb does not list an
  emulator in the first seconds of its start, or one whose console port is outside the range
  that adb scans. banksman reads each argument as the process got it, from `/proc` on Linux
  and from the kernel on macOS, so a shell whose script starts an emulator is not an emulator.
  An AVD that runs as several emulators, for example with `-read-only`, has no serial, because
  banksman cannot tell which of them a lease has; a held lease keeps its own.
  `preset = "android-device"` finds the physical devices that adb lists, with the facts
  `serial`, `manufacturer`, `model`, `codename`, `api`, `abi`, `form`, and `running`, which
  is always true, and their accounts. The facts are what the device gives: a Samsung phone
  gives the model `SM-S926B`, not "Galaxy S24+". banksman ships no table of marketing names, and it does
  not read the device name in the settings, which a person can change and which often
  holds a personal name. A device that is
  not authorized is reported, not offered. A device whose serial starts with `localhost:` is a
  port forward, such as a remote device, and gets another port on every connection, so this
  preset reports it and does not offer it; the `android-remote` preset offers it. The presets use the SDK in `~/Library/Android/sdk`
  on macOS and in `~/Android/Sdk` on Linux, and the AVDs in `~/.android/avd`. The `sdk` and
  `avd_home` keys of the `[android]` table change them. The presets never read
  `$ANDROID_HOME` or `$ANDROID_AVD_HOME`, for the same reason as for the state directory.
  They only read, except for the starts on `--start`, and they declare no `on_void`.
- **Emulators that banksman starts.** `acquire --start` (section 6) for an AVD that does not
  run reads the process list again, and refuses to start an AVD whose emulator runs already.
  It chooses a console port: an even port from 5554 to 5584, the range in which adb finds
  emulators by itself, where nothing listens on the port or on the next one, the adb port.
  Under the lock of the lease store, it records the serial `emulator-<port>` in the lease,
  the first one that no held lease records. So two starts at the same time never get the
  same port, and the serial is known before the boot. A program outside banksman, such as
  Android Studio, can still take the port first; the start then fails, because the console at
  the port gives another AVD name, and never records a wrong serial. banksman runs
  `<sdk>/emulator/emulator -avd <name> -port <port> <start_args>` in a session of its own, in
  the directory `/`, with the SDK and the AVD home of banksman in `ANDROID_HOME`,
  `ANDROID_SDK_ROOT`, and `ANDROID_AVD_HOME`, so the emulator outlives the command, finds the
  AVD that discovery found, and gets no Ctrl-C of the caller. The emulator command becomes the
  emulator process, and banksman records its pid and start time at once. The lease is
  `booting` until `sys.boot_completed` is `1` and the console at the port gives the name of the
  AVD, under the boot deadline. When the emulator ends before that, does not complete its boot
  by the boot deadline, or the start is interrupted, banksman ends the emulator and gives the
  lease back. What the emulator prints goes to `<name>.log`, which each start begins again;
  the reason of a failed start is its last line.

  **The record.** `<kind>-emulators.json` lists the emulators that banksman knows: the ones that
  it started, and the ones that ran when a lease of their AVD ended (section 6), each with its
  pid, its start time, the boot of the machine, and whether banksman started it. An entry
  whose process has ended, or that belongs to an earlier boot, goes. An allowed AVD that runs
  is managed only while the emulator of its entry is its only emulator. So an AVD that also
  runs as another emulator, for example one with `-read-only` that a person started, is
  unmanaged, and when an AVD runs as several emulators at the end of a lease, banksman records
  none of them. The record and the logs are in the
  emulator directory, `~/.local/state/banksman/emulator`, outside `/tmp`, because an emulator
  can run for days; next to the state directory when only `BANKSMAN_STATE_DIR` is set; or in
  `BANKSMAN_EMULATOR_DIR`. Without a record that banksman can read, no emulator counts as one
  that banksman knows.

  **The end.** A release leaves the emulator running, and the next request gets it without a
  start. The reaper ends the emulator of a void lease only when the record says that banksman
  started it, whichever lease it started it for: SIGTERM to the emulator process, SIGKILL when
  it still runs after 20 seconds, and the end confirmed by pid and start time. It never
  signals a process group, because an emulator can start the `netsimd` daemon in its group,
  and the other emulators use that daemon. When the emulator does not end, the lease is
  quarantined. An emulator that the holder started stays, and banksman records it. An
  `acquire --start` that is killed in the middle, for example by the time limit of an agent,
  leaves the lease `booting`, and the reaper ends the emulator after the boot deadline.
- **Remote devices.** `preset = "android-remote"` offers the physical devices of Android
  Device Streaming, a service that reserves a real device in a lab and forwards it to the
  local adb server as `localhost:<port>`. The `android` command line tool does that, and
  banksman runs it from the path in `cli` of the `[android]` table, never from the `PATH`. The
  kind is off unless the operator declares it and allows models in the inventory, and it is
  paid by default (section 5). banksman never knows a price, an account, or a project of its
  own: the project is configuration.

  ```toml
  [android]
  cli = "/home/me/.local/bin/android"    # the android command line tool; `command -v android`

  [kinds.remote]
  preset = "android-remote"
  project = "my-project"                 # the Google Cloud project, required with this preset
  preselect = ["tokay:*"]
  boot_timeout = "8m"
  hard_cap = "3h"                        # the service ends a reservation 3 h after its creation
  ```

  `project` is valid only with this preset. Every call of the tool gets `--project` and
  `--sdk` with the SDK of banksman, so that the tool and the daemon of each connection use
  the same adb as banksman, never an adb from the `PATH`, which can be the wrapper of the
  adb guard or another version that restarts the adb server. `cli_state` in `[android]` is the
  directory where the tool keeps its files about connections, by default
  `~/.android/cli/remote-devices` in the home directory from the user database.

  One instance is one model of the catalogue of the service, named `<codename>:<api>`, for
  example `tokay:34`; the tool writes `tokay/34`, but a resource name cannot have `/`. The
  service allows one reservation of a model for each account, so one instance for each model
  loses nothing. The facts are `codename`, `api`, `manufacturer` (the group of the catalogue),
  `model` (the name in the catalogue), and `form`, from the words of that name: `Tab` or
  `Tablet` gives `tablet`, `Watch` gives `watch`, and everything else is `phone`. A connected
  device gives its own facts instead, as for the `android-device` preset, except `codename`
  and `api`, which name the model. It also has `running`, `serial`, `handle` (the id of its
  reservation), and `ends` (when the reservation ends, as a UTC time, when banksman knows it).

  **No network in discovery.** `banksman admin discover --kind <kind>` runs
  `android device remote models`, saves the catalogue of models as `<kind>-catalogue.json` in
  the remote directory, shows the live reservations of the account as notes, and then works as
  for every kind. The remote directory is `~/.local/state/banksman/remote`, outside `/tmp`, as
  the log; next to the state directory when only `BANKSMAN_STATE_DIR` is set; or
  `BANKSMAN_REMOTE_DIR`. Without a catalogue, the kind has no instances, and agents see the
  command that fetches it; `status` shows how old the catalogue is. Every other command reads
  only local sources: the catalogue; the properties file `active-connections.properties` in
  `cli_state`, where the daemon of each live connection writes its reservation and its port;
  `adb devices -l`, which confirms the port; and `<kind>-sessions.json`, banksman's own record
  of the reservations that it started or adopted. A live port maps to its model through that
  record, or else through `getprop` on the device. The codename of a device of a partner lab is
  not a property of the device, so only the record maps it; otherwise the device is reported
  and not offered.

  **The start.** `acquire --start` (section 6) for a model that does not run lists the live
  reservations of the account, then runs `android device remote create <codename>/<api>
  --connect`, which reserves the device, waits until it is active, and connects it, in about
  40 seconds. As soon as the tool prints the id, banksman records it in its file and in the
  lease, so a take-back can end the reservation also when the `acquire` is killed in the
  middle. A reservation that the account already had, or that was live on this machine
  before, is adopted: the tool connects it and prints the same text, and banksman does not
  record it as one that it created, unless it created it earlier itself. banksman then extends the reservation to the hard
  deadline of the lease, records the serial, waits until adb lists the device, and marks the
  lease ready. The service caps a reservation at 3 hours after its creation without an error,
  so banksman records the end that it got. When a step after the id fails, banksman removes a
  reservation that it created, and the lease is given back.

  **The connection.** The daemon of a connection ends when adb drops it, for example after
  `adb kill-server`, `adb disconnect`, or a restart of the machine, and it does not connect
  again. When `banksman run`, or `acquire --lease`, finds a lease with a handle and no live
  port, it runs `android device remote connect <id>`, which takes about 3 seconds and gives a
  new port, and records the new serial. When the reservation has ended, `run` fails before its
  command, and the holder releases the lease.

  **The end.** A release keeps the reservation: the model is then a free instance that runs,
  until the reservation ends, and the next request gets it without a new reservation, as an
  emulator that a release leaves running. The reaper ends only what banksman started: for a
  void lease whose handle the record lists as created by banksman, it runs
  `android device remote remove <id>`. A reservation that has ended already counts as gone.
  A live connection whose reservation is not in banksman's record is unmanaged (section 6).
  When a lease of a model ends, by a release or a take-back, and the lease records the id of a
  reservation that the record does not have, banksman records it as one that it did not
  create: the holder reserved the model by hand.
  The holder can extend a reservation by hand with `BANKSMAN_HANDLE`, so the end that banksman
  recorded can be too early: only `remove` confirms that a reservation has ended, and the record
  stays 3 hours after banksman first recorded the reservation. A reservation that nothing takes
  back, for example after a restart that emptied `/tmp`, ends by itself, at the latest 3 hours
  after its creation.

  The tool prints local times such as `9:56 AM` without a date; banksman takes the next such
  time, and records no end when it cannot read one. Everything that the tool prints, and every
  value in its files, is untrusted, and checked as discovered values are.
- **Identifiers only.** For accounts, banksman stores addresses, never credentials. Reading
  the accounts on an Android device depends on `dumpsys account` output, which is not a
  stable interface. When that output has no list of accounts, the accounts are not known,
  and banksman says so. The presets keep only accounts of type `com.google`.

## 9. Holder identity

A person at the console, and other agents, must see who holds a resource and why. banksman
records the holder at acquire time from facts that every caller has, so it works the same
for any agent and for a person at a terminal.

| Field | Source |
|---|---|
| owner | the worktree: the nearest directory, from the caller's working directory up, that has a `.git` entry; outside a repository, the working directory |
| issue | `--issue`, otherwise the first match of `issue_pattern` in the branch name, then in the name of the worktree directory |
| agent | the nearest ancestor process whose program is in `agents`: the last part of its executable path, with case |
| owner process | that process's pid and start time, used for liveness |
| session | an agent-specific extra when the environment has one, never required: `CLAUDE_CODE_SESSION_ID` for `claude` |

If no known agent is in the ancestry, the owner is the worktree, and only the touch-based
expiry applies, unless the caller passes `--owner-pid`.

The owner is the readable name of a holding, not its identity: several agents can work in one
worktree at the same time, each with its own agent process. The lease id identifies a
holding, and `acquire --lease`, `touch`, and `release` take it. Liveness stays with each
agent, through its own agent process.

- `touch` records the caller's agent process as the owner process of every lease of the
  holding, so a lease survives a restart of the agent. A lease whose owner process still runs
  and started the caller keeps that owner, also for `acquire --lease`: the caller works within
  the life of that process. So a script that `banksman run --where` runs cannot move the lease
  of the run to the agent, and the lease still ends with the run.
- `release --all` gives back only the leases whose owner process is the caller's agent
  process, so it acts for one agent. After a restart of the agent, it finds a lease only
  after a touch has recorded the new agent process; until then, the lease ends by its
  timeouts. A subagent runs its commands in the agent process of its session (section 17), so
  `release --all` also gives back the leases of the other subagents of the session.

- **Named holdings.** An agent must keep the lease id between its commands. But the shell tool
  of an agent does not keep variables from one command to the next, a compaction of the
  context of the agent can drop the id, and a subagent does not see the context of its parent.
  So the caller can give its holding a label: `acquire --holding <label>` keeps the holding of
  the caller's agent process that has this label, as `--lease` keeps the holding of a lease
  id, and otherwise starts a holding with the label. `run`, `touch`, and `release` take
  `--holding` instead of `--lease`; `run --holding` needs `--resource` only when the holding
  has several resources. `banksman held` lists the held leases of the agent process, with
  their labels, lease ids, resources, and serials. The label is looked up under the lock of the
  lease store, so two requests of one agent process with the same label never start two
  holdings.
- The caller chooses the label, and banksman does not derive it. banksman cannot tell the
  subagents of one session apart: in Claude Code and in Codex, they run their commands in one
  agent process (section 17). So two subagents that give the same label share one holding,
  and so one device. A label names the task, such as `login-tests`, and the guide for agents
  says so (section 15).
- A label has 1 to 64 letters, digits, and the characters `.`, `_`, and `-`, and starts with a
  letter or a digit, so that it carries no escape sequence and can be printed as it is.
  Another agent can have chosen it, so `status --json` shows it only with `--verbose`, as the
  purpose. A label needs an owner process: without one, every caller of the user would share
  it.
- A holding that has a booting lease is not kept, by a label or by a lease id, and the
  request waits as for a resource in use. Only the `acquire` that starts or resets the instance
  gives the lease to its holder, and it can have ended before it was done, for example when the
  time limit of a tool stopped it. Then the instance can be reset only in part, so the lease
  waits for its boot deadline, and `run --holding` refuses it with exit status 4.
  `release --holding` gives it back at once.
- After a restart of the agent, a label finds its holding only after a touch with a lease id
  has recorded the new agent process, as for `release --all`. When that touch gives the new
  process two holdings with one label, banksman refuses the label and names a lease of each
  holding, instead of choosing one.

- banksman reads the branch from the `HEAD` file of the worktree, and runs no `git`
  process. A detached `HEAD` has no branch, so the directory name is the fallback.
- The `[holder]` table of the configuration sets `agents`, by default
  `["claude", "codex"]`, and `issue_pattern`, a regular expression that by default finds ids
  of letters, a hyphen, and digits, such as `abc-123` in the branch `abc-123-tablet-layout`.
  With a group in the pattern, the issue id is the text of the first group. An empty
  `agents` list turns the agent detection off.
- An issue id has 1 to 64 letters, digits, and the characters `#`, `.`, `_`, `/`, and `-`.
  A branch name is text that anybody can choose, so a match that is not a valid issue id is
  not used, and an explicit `--issue` that is not valid is an error.
- `banksman whoami` shows the holder that banksman would record for a lease taken from the
  current directory. It is the quick check that banksman finds the agent inside a given
  agent app.

The caller adds only a purpose (`--for`) and an expected hold time (`--expect`, which a
later `touch` can update). The console then shows, for example,
`codex · #123 · verify the tablet layout`; without an issue id it shows the name of the
worktree directory.

The purpose is text that one agent writes and other agents read. It is untrusted: see
SECURITY.md. Its whitespace becomes single spaces, its control characters are removed, and
it has at most 200 characters. `status --json` carries the owner, the owner process, the
agent, the issue, and the session, and the purpose only with `--verbose`.

## 10. Status, queries, and history

- `banksman status` prints one table with every resource that agents may use, free ones
  included, and every resource that has a lease file: resource, kind, serial, form and API level,
  state, since, last use, the three "free by" values, the accounts of the lease, and the
  holder. The state is `free`, `unmanaged` for an instance without a lease that runs but that
  banksman did not start (section 6), `booting`, `ready`, `draining`, `quarantined`, `absent` for an
  allowed instance that discovery does not find now, or `unreadable` for a lease file that
  cannot be read. An instance that discovery finds but that the inventory does not allow is
  never shown, also not to agents, because it can be personal. `status` runs discovery, so
  it takes as long as one look of `acquire`, and it records the serials that it finds in the
  leases (section 3). The serial of a resource without a lease is the one that discovery
  finds. Under the table, it shows why the discovery of a
  kind failed, and how many other notes the discovery has. Those notes can name an instance
  that agents may not use, such as a personal phone that is not authorized, so the commands
  for agents, `status` and `acquire`, never show their text; `banksman admin discover` does.
- `banksman watch` shows the same table again every 5 seconds, or at `--interval`. Each
  refresh reaps, reads the configuration again, and runs discovery. An error is shown in
  place of the table, and the next refresh tries again.
- `banksman status --json` carries the same data for agents and other tools: for each
  resource its kind, its state, whether it is present now, its serial, the facts `form` and
  `api`, and its lease. The purpose only with `--verbose` (section 9).
- **"Free by"** cannot be known, so it shows three labeled values:
  1. expected: the holder's `--expect`, only a hint, or unknown;
  2. at the latest: the hard cap, which the reaper enforces;
  3. if abandoned: when the lease becomes void if nothing touches it again. That is the last
     touch plus the idle timeout, or plus the owner grace when the owner process has ended,
     and never later than the hard cap.

  The table shows them as times of day. The deadlines count awake time (section 3), so a
  time of day moves later when the machine sleeps. The JSON gives each value as a UTC time
  and as seconds from now (`in`), which is negative for an expected time that has passed. A
  draining lease has no "free by" values: it is free when its scripts end, and the JSON gives
  its drain deadline instead.
- `banksman log` shows the history of acquire, release, void, reap, quarantine, and forced
  release events, with the holder. With `--since` and `--resource`, it answers "who held this
  device at 3 a.m.?". A release and a void record how long the lease was held and the longest
  time between two touches, both in awake time. They are the data for tuning the hard cap and
  the idle timeout. A quarantine records its problem, or the scripts that still ran at the
  drain deadline and whether stopping was on for the kind, so that an operator can see what
  the reaper would have stopped before turning `stop = true` on (section 4).
- **The log file** is `~/.local/state/banksman/log.jsonl`, with the home directory from the
  user database. It is outside `/tmp`, as the quarantines are, because the history must
  outlive restarts and cleaners of temporary files. It has one JSON object for each event,
  each with the output schema number (section 14), and banksman adds to it only under the
  lock of the lease store. At 10 MB, banksman starts a new file and keeps the earlier one as
  `log.jsonl.1`. When the log cannot be written, the command still does its work and prints
  a warning: the change to the lease is already written. `log` leaves out a line that is not
  a valid event, and says how many it left out. It also reads the lines of an earlier output
  schema whose events it knows, so an upgrade does not hide the history. `BANKSMAN_LOG`
  changes the path for tests; when only `BANKSMAN_STATE_DIR` is set, the log is next to that
  directory.
- Not implemented yet: `banksman explain --where ...` answers the question "can I get one
  now?". For each matching resource it says free, held by whom and until when, not
  permitted, not present, or quarantined, and it gives the caller's place in the queue. An
  agent can then decide to wait or to do something else. The queue of the callers that wait
  comes with it, and `status` then also shows the callers that wait for each resource.

## 11. Optional: build slots for Gradle

This section is an example of a counted kind for a resource that is not a device. Build slots
are not part of the device setup, and a project that uses banksman for devices and emulators
does not need them. Set them up only when the operator wants to limit parallel Gradle builds
on the machine.

Sessions are cheap; builds and emulators are what use up memory. Build slots are a counted
kind in the same pool, and a Gradle init script makes every Gradle build of the user hold one
while it runs. So a build that an agent starts directly is covered too, not only the
project's own build script.

- **Setup.** The setup has two files. `banksman admin gradle-init` prints the init script,
  and `banksman admin gradle-init --build-slot` prints the build-slot part, which the init
  script applies and which takes and gives back the slot. The operator saves both files, and
  declares the kind `build` in the configuration:

  ```sh
  banksman admin gradle-init > ~/.gradle/init.d/banksman-build-slot.gradle
  mkdir -p ~/.gradle/banksman
  banksman admin gradle-init --build-slot > ~/.gradle/banksman/build-slot.gradle
  ```

  ```toml
  [kinds.build]
  count = 5
  owner_grace = "0s"
  idle_timeout = "off"
  ```

  banksman does not write the files itself, because `~/.gradle` belongs to Gradle and to the
  operator. After an upgrade of banksman, the operator saves both files again. The build-slot
  part is outside `init.d`, because Gradle applies every file in `init.d`. When the build-slot
  part is missing, every build fails with a message that gives the command that saves it.
  Without `banksman` on the `PATH` of the build, the build-slot part does nothing. It looks
  only in absolute directories of the `PATH`, so a file in a project cannot stand in for
  `banksman`.
- **Gradle versions.** The init script applies to every build of the user, also in projects
  on an old Gradle that have nothing to do with banksman, so it must work on every version.
  It applies the build-slot part only on Gradle 7.4 and later. On an older Gradle, the build
  runs as it does without banksman, and the init script logs one line at the info level, so
  the console of a normal build does not change. Gradle compiles a whole script before it
  runs any of it, and the build-slot part uses APIs that Gradle 6.2 added, so the init script
  uses only APIs that old versions also have. Gradle 6.5 to 7.3 also refuse to read a value
  source or a Gradle property while they configure a build, unless the script calls
  `forUseAtConfigurationTime()`, which Gradle 9 removed.
- **The owner process is the process that runs the build:** the Gradle daemon, or the client
  with `--no-daemon`. The daemon does not know the process of its client, so the client
  cannot be the owner. A daemon runs one build at a time, so its slot is the slot of the
  build that it runs. With `owner_grace = "0s"`, the slot is free as soon as that process
  ends, for example when the daemon crashes, and with `idle_timeout = "off"` no touch loop is
  needed. At the hard cap, the slot is free again, also when the build still runs: a hung
  build must not hold a slot forever, and banksman does not stop it. Until such a build ends,
  one more build than the slots can run. The holder is the worktree of the directory where the
  build started, and the purpose is `gradle` and the requested tasks.
- **When the build starts**, the build-slot part runs
  `banksman release --all --owner-pid <pid>`, and then
  `banksman acquire --where kind=build --owner-pid <pid> --wait 30m`. It does this in a
  Gradle `ValueSource`: an init script, and a script that it applies, do not run when Gradle
  reuses the configuration cache, but Gradle runs their value sources again before it reuses
  an entry, before any task. The value never changes, so it never makes an entry stale. A
  change of the build-slot part, for example after an upgrade, does. When an entry is stale,
  Gradle runs the value source twice in one build, and the two runs share no memory, so the
  build-slot part keeps no lease id: it first gives back any slot that its process still
  holds.
- **When the build ends**, also when it fails, a Gradle build service of the build-slot part
  runs `banksman release --all --owner-pid <pid>`. A release that fails does not fail the
  build: the next build of the same daemon gives the slot back, and the end of the daemon
  frees it. The same is true for a build that runs no task while Gradle reuses the
  configuration cache, for example with `--dry-run`: Gradle creates the build service then
  only after a task, so the slot stays held until the next build of the daemon, the end of
  the daemon, or the hard cap. Gradle's only hook at the end of a build without tasks,
  `FlowAction`, is incubating and needs Gradle 8.1, and a change in Gradle would then fail
  every build of the user.
- **Waiting.** A build waits for a free slot for 30 minutes, or for the Gradle property
  `banksman.buildSlotWait`, such as `45m`, for example in `~/.gradle/gradle.properties`. While
  it waits, its console shows the line that `acquire` prints when it starts to wait
  (section 6), and `banksman status` shows who holds the slots. When the wait ends, the build
  fails with a message that says that this is a limit of the machine, not a failure of the
  build. A caller should treat it as an infrastructure problem. Any other error of banksman,
  such as a configuration without the kind `build`, fails the build too, because a build that
  ignores the slots uses the memory that they protect.
- **Limits.** Gradle cannot interrupt a build that waits in a value source. When its client
  stops, for example with Ctrl-C, the daemon stops itself after 10 seconds, the next build
  starts a new daemon, and the `acquire` that waited ends with its owner process. A slot
  bounds the builds that run, not the daemons that wait for the next build: a daemon keeps
  its memory until it ends, by default after 3 hours without a build. Gradle reuses an idle
  daemon before it starts a new one, so the number of daemons tends towards the number of
  slots. So `count` comes from the peak memory of one build, with its daemon, its worker
  processes, and the Kotlin daemon. Every Gradle build of the user takes a slot, also a build
  or a sync in an IDE. A build that runs another Gradle build in another daemon, and waits for
  it, needs two slots.

## 12. Optional: the adb guard

Fencing covers the scripts that register with a lease (section 4). A device command that does
not register is not fenced: an agent that types `adb -s <serial> install ...`, a script of a
project that does not use banksman yet, or a skill that the project has not changed. The adb
guard refuses such a command when another holding leases the device. Like the build slots, it
is a setup of the operator, and a project needs nothing for it.

- **Setup.** `banksman admin adb-shim` prints a small `sh` wrapper. The operator saves it as
  `adb` in a directory of its own, and puts that directory first on the `PATH` of the agents,
  before the Android SDK:

  ```sh
  mkdir -p ~/.local/share/banksman/bin
  banksman admin adb-shim > ~/.local/share/banksman/bin/adb
  chmod +x ~/.local/share/banksman/bin/adb
  ```

  For every call, the wrapper runs `banksman guard adb --fallback-adb <saved adb> -- <arguments>`
  as a child process that only decides: with exit status 0, it prints the path of the adb to
  run, and with exit status 3, it refuses the call. The wrapper then runs that adb in its own
  place, with the pid, the arguments, the terminal, and the environment of the call, and with
  `BANKSMAN_GUARD=1` added. The adb server that a call starts keeps that variable, and nothing
  reads it. The adb is the adb of the Android SDK of the configuration (section 8). When the
  configuration names no `sdk`, or cannot be read, it is the adb that the wrapper saved when
  banksman printed it, because the default SDK can be missing, or have an adb of another version,
  which would restart the adb server. It is never an adb from the `PATH`. When the adb of the SDK
  is a wrapper itself, that wrapper sees `BANKSMAN_GUARD` and stops, so the two never run each
  other without end. The wrapper uses only a `banksman` in an absolute directory of the
  `PATH`, so a file in a project cannot stand in for it. banksman does not install the wrapper
  itself: the operator chooses where it goes. After an upgrade of banksman, or a change of `sdk`
  in `[android]`, the operator saves it again. A wrapper on the `PATH` is better than a hook of
  one agent that runs before each shell command: it also sees the adb calls inside scripts, and
  it works the same for every agent.
- **Which device.** The guard reads the command line as adb reads it. The device comes from
  `-t`, then `-s`, then `-d` or `-e`, then `ANDROID_SERIAL`, and then it is the only device that
  adb knows. `-s` also takes the other names that adb takes, such as `model:Pixel_8`, a USB
  address, or a host without its port. For these names, for `-t`, `-d`, and `-e`, and for a
  command that names no device, the guard asks `adb devices -l`. When adb would find no single
  device, the call runs, and adb refuses it itself. `adb emu` has rules of its own, and the
  guard follows them: it talks to the console of the emulator whose port is in the serial of
  `-s` or `ANDROID_SERIAL`, and without one, to the only emulator, also when physical devices
  are connected; it ignores `-t`, `-d`, and `-e`. A command that acts on no device, such as
  `adb devices`, `adb connect`, or `adb forward --list`, always runs.
- **The lease of the device.** A held lease that records the serial; a lease in another state
  that records it, while no held lease records it, because discovery keeps the serial up to
  date in held leases only (section 3); and a lease in any state whose resource is the serial,
  as for a physical device. A lease file that cannot be read keeps its device out of use.
- **Who may use the device.** The call runs when one of these is true:
  - No agent process (section 9) is above the caller, for example for a person in a terminal.
  - The device has no lease, and the guard is not strict.
  - The lease is held, and its holding is the caller's: `BANKSMAN_LEASE` names a lease of the
    holding, as `banksman run` sets it, or the owner process of a lease of the holding is above
    the caller.
  - The banksman process that the lease names is above the caller: the `acquire` that resets
    the instance, or the reaper that takes it back. Their hooks act on the device, and a reaper
    can run inside a command of another agent (section 5).

  Otherwise the guard exits with status 3, the status of `check` for a lost lease, and runs no
  adb. On standard error, it names the device, the state of its lease, and the holder as
  `status` shows it. The purpose in it is untrusted text, as in `status`. When the lease belongs
  to the caller's holding but is no longer held, for example because it drains, the guard says
  that the lease is lost.
- **Starts.** The start of an emulator records its serial before the boot. The serial of a
  remote device, `localhost:<port>`, is a serial like any other, and the start records it in
  the lease before the lease is ready. So the guard protects an instance that banksman starts
  from the first moment. `adb kill-server` and `adb disconnect
  localhost:<port>` end the connection of a remote device, so the guard refuses them for the
  devices of other holders as for every device, and `banksman run` connects the caller's own
  device again (section 8).
- **Strict mode.** With `strict = true` in the `[guard]` table of the configuration, the guard
  also refuses an agent's device command on a device that no lease of its holding has: a free
  device, or a device that the inventory does not allow, such as a personal phone.

  ```toml
  [guard]
  strict = true
  ```

- **Commands for every device.** `adb kill-server` stops the adb server, which disconnects
  every device; `adb reconnect offline` resets the connection of every offline device, such as
  an emulator that boots; `adb disconnect` without an address ends every connection over
  TCP/IP; and `adb forward --remove-all` removes the port forwards of every device, also with
  `-s`. The run that breaks is then not the run that gave the command. So the guard refuses
  these commands for an agent while another holding has a held lease that records a serial,
  that is, while an instance of another holder runs. `adb reconnect` and
  `adb reconnect device` act on one device, like other device commands.
- **Speed.** The guard runs before every adb call, and a UI test can make many. It reads the
  lease files without the lock: every write replaces a whole file with a rename, so each file
  that it reads is whole. It also reads the copies of the quarantines, so a quarantine counts
  also after a restart emptied the state directory. It does not reap, and it runs no discovery.
  It reads the process list only when the lease id in `BANKSMAN_LEASE` does not decide, and it
  asks `adb devices` only when the command line does not name the device by a serial that a
  lease has. On the Mac where it was measured, the wrapper and the guard together add about
  35 ms to a call that needs no process list, and about 70 ms to one that needs it.
- **When the guard fails.** When banksman cannot decide, for example because of an error in the
  configuration, a lease file of a newer banksman, or a process list that the sandbox of an
  agent hides, the call runs, and banksman says on standard error that the call is not checked.
  When banksman cannot start, for example because its Python broke after an upgrade, or ends in
  any other way, the wrapper runs the adb that it saved, and says the same. That is why the
  wrapper runs adb itself: a wrapper that replaced itself with banksman could not fall back. A
  broken guard that blocks every device is worse than a short time without protection. Without
  `banksman` on the `PATH`, the wrapper runs the saved adb, and says the same. Ctrl-C during the
  check ends the call.
- **Limits.** The guard is a guardrail, not a security boundary. These do not pass through it: a
  call through the full path of adb, such as the adb of Android Studio; a tool that talks to the
  adb server itself, such as the connected tests of the Android Gradle plugin, for which the
  `ANDROID_SERIAL` of `banksman run` names the leased device (section 4); and the emulator
  console. A second connection to an emulator, with `adb connect` to its adb port, has another
  serial, and the guard does not know that it is the same device. A process that no agent
  process is above passes as a person's, for example a task that a Gradle daemon runs after an
  earlier build started the daemon. The guard never acquires a lease itself: that would make it
  a second way to get a device, and hide that two holders want the same one.

## 13. Command line (sketch)

`explain` does not exist yet. The rest exists.

```
banksman acquire [--as <part>] [--where <attr><op><value> ...] [--accounts <n>] ...
                 [--for <text>] [--expect <duration>] [--wait <duration>]
                 [--lease <id> | --holding <label>] [--paid] [--start] [--issue <id>]
                 [--owner-pid <pid>] [--json]
banksman enter   --resource <name> --lease <id> --pid <pid> [--pgid <pgid>]  # check, touch, register
banksman check   --resource <name> --lease <id>                    # touch; exit 0: yours, 3: lost
banksman leave   --resource <name> --lease <id> --pid <pid>
banksman run     --lease <id> --resource <name> -- <command> ...   # in a group of its own, fenced
banksman run     --holding <label> [--resource <name>] [--owner-pid <pid>] -- <command> ...
banksman run     --where <attr><op><value> ... [--accounts <n>] [--for <text>] [--expect <duration>]
                 [--wait <duration>] [--paid] [--start] [--issue <id>] [--owner-pid <pid>]
                 -- <command> ...
banksman touch   (--lease <id> | --holding <label>) [--resource <name>] [--expect <duration>]
                 [--owner-pid <pid>]
banksman release (--lease <id> | --holding <label>) [--resource <name>]
                 [--owner-pid <pid>]                               # the holding, or one resource
banksman release --all [--owner-pid <pid>]                         # the leases of this agent
banksman held    [--owner-pid <pid>] [--json]                      # the holdings of this agent
banksman status  [--json] [--verbose]
banksman whoami  [--json]
banksman agent-guide                                               # the guide for agents
banksman watch   [--interval <duration>]
banksman explain --where <attr><op><value> ...
banksman log     [--since <time>] [--resource <name>] [--json] [--verbose]
banksman reap
banksman version [--json]
banksman guard adb [--fallback-adb <path>] -- <adb argument> ...  # what the adb wrapper runs

banksman admin discover [--kind <kind>] [--all] [--json] [--yes]
banksman admin release --force --resource <name>
banksman admin gradle-init [--build-slot]                          # optional build slots
banksman admin adb-shim                                            # optional adb guard
```

Commands that grant a resource print `KEY=value` lines, for example `RESOURCE=`, `SERIAL=`,
`HANDLE=`, `ACCOUNTS=`, and `LEASE=`, the lease id that the scripts pass back, so that a shell
script can read them.
With `--json` they print JSON instead. Exit status 1 is an error, 2 a usage error, 3 a lease
that is lost, also a label that names no held holding of the agent process, and 4 a request
whose matching resources are all in use. `run` exits with the
status of its command, unless banksman fails before the command starts or the lease is lost.

In the command of `banksman run`, each argument that is exactly `{serial}` becomes the serial
of the lease, and each argument that is exactly `{resource}` becomes the resource: the shell of
the caller cannot expand a variable that banksman sets only for the command. `run` fails before
the command starts when the command has `{serial}` and the serial is not known. The command
gets `BANKSMAN_LEASE`, `BANKSMAN_RESOURCE`, `BANKSMAN_KIND`, `BANKSMAN_SERIAL` and
`BANKSMAN_HANDLE` when they are known, and `ANDROID_SERIAL` for a kind with an Android preset,
so that a script can, for example, extend the reservation of a remote device by hand.

Every command that only an operator may run is under `banksman admin`, so that an agent's
permission rules can refuse all of them with one pattern, including ones added later.

## 14. The contract

- Lease files and every JSON document carry a `schema` number. There are two numbers, and
  `banksman version` shows both.
- The lease schema is the version of the lease file format. It changes on every change of
  the format, also on a new field, because a banksman that does not know a field would drop
  it when it writes the file again. So a lease file with a newer lease schema stops every
  command (section 3).
- The output schema is the version of the JSON that the commands print and of the lines of
  the log. A new field does not change it, so a reader ignores the fields that it does not
  know. It changes when a field is removed or renamed, or changes its type or its meaning. A
  reader refuses an output schema that it does not know, instead of guessing. The test suite
  records the shape of every JSON document, so a change of the output cannot pass without a
  decision about the number.
- The document of a `discover` hook has a schema of its own (section 8), and so have the
  catalogue and the record of reservations of the `android-remote` preset, and the record of
  the emulators of the `android-emulator` preset.
- The surfaces treated as public API are the CLI (commands, flags, exit codes, `KEY=value`
  output), the JSON output, the lease file format, the log file format, the configuration
  files, and the hook contract.
- There is no library API. Every client, a project's scripts and other tools alike, calls
  the command. So a machine has exactly one lease implementation, and two versions of the
  lease logic never write the same lease files.

## 15. Using banksman from a project

[PROJECTS.md](PROJECTS.md) has the helper, the stub for tests, and the rules for agents that
this section names.

- Every script goes through one small helper that returns at once when `banksman` is not
  on `PATH`, so the script behaves exactly as before on a machine without it. The project
  copies the helper into its repository, because its scripts cannot depend on a file that
  banksman installs. Test the scripts both ways: with a stub `banksman` on `PATH`, and
  without one.
- Run the work that uses a resource with `banksman run`, so that it stops when its lease is
  lost (section 4).
- Pass the leased serial to every tool, for example `adb -s <serial>`. A bare `adb` call
  acts on whatever device adb chooses. In the command of `banksman run`, the argument
  `{serial}` is the serial, and for a kind with an Android preset, `ANDROID_SERIAL` names the
  leased device, also for the connected tests of the Android Gradle plugin.
- Keep the lease id that `acquire` prints. Pass it to the scripts, and back to `acquire` with
  `--lease` to keep the same resource. An agent gives its holding a label with `--holding`
  instead, and names the holding with the label in later commands (section 9).
- Give the agents the guide that `banksman agent-guide` prints. It is in the format of a
  skill, and agents may run the command, so the text always matches the installed banksman.
  The instructions of the project then only add what is its own, such as the scripts that take
  the serial.
- Keep every wait shorter than the time limit of the shell tool of the agent. Claude Code
  stops a command of its Bash tool after 10 minutes at most, so a longer `--wait` is stopped
  before it ends. A long run of tests under `banksman run` runs in the background of the tool.
- Let banksman start an emulator with `--start`, or start the granted instance when it does
  not run, and handle one that already runs. Start it before `banksman run`, not inside it:
  `run` waits for every process that its command leaves in its group. With `--start`,
  `acquire` lasts until the boot completes, so `--wait` and the `boot_timeout` of the kind
  together must be shorter than the time limit of the shell tool, or `acquire` runs in the
  background of the tool. An `acquire` that the tool stops leaves the lease booting until the
  boot deadline, and the emulator is then ended and started again for the next request.
- Release leases before waiting for a person, so that a run that waits overnight does not
  hold a device. If a run forgets, the owner and idle checks free the device later.
- Before `--paid`, tell the user that the device costs money, and get a yes. Let the
  permission rules of the agents ask the user before every `banksman acquire ... --paid` and
  `banksman run ... --paid`.
- Keep app setup and cleanup in the project's scripts.
- Refuse `banksman admin` in the permission rules of the agents. Without such a rule, an agent
  can widen what agents may use, or take a resource from another holder. The rule is a
  guardrail, not a security boundary.
- When the operator installs the adb guard (section 12), `adb` exits with status 3 when an
  agent acts on the device of another holder. An agent that uses the devices of its own leases
  never sees it.

An example configuration for a typical Android project, in `~/.config/banksman/config.toml`:

```toml
# Emulators: discover selects at first only the AVDs whose names start with e2e_, so a
# personal AVD on the same machine is never offered by mistake.
[kinds.emulator]
preset = "android-emulator"
preselect = ["e2e_*"]
boot_timeout = "4m"
start_args = ["-no-window", "-no-snapshot-save"]

# Physical devices: no pattern, so a person selects each device in banksman admin discover.
[kinds.device]
preset = "android-device"

# Accounts: only the test accounts are selected at first.
[accounts]
preselect = ["*@example.test"]

# Host ports, for example for a server that a test run starts: each run gets its own.
[kinds.port]
instances = ["9101", "9102", "9103"]

# A mutex: one AVD creation or sdkmanager call at a time. The lease of banksman run --where
# ends with the run, so a run that is killed frees the mutex at the next command.
[kinds.sdk]
count = 1
owner_grace = "0s"
```

The emulators and the devices are the main part. The ports and the mutex are optional.
Build slots for Gradle (section 11) are not in this configuration: they are a separate,
optional setup of the operator for every Gradle build on the machine, and the project does
not need them.

After `banksman admin discover`, the inventory allows the selected AVDs, devices, and
accounts. A test run then gets an emulator and a port in one call, and the scripts use the
mutex with `banksman run`:

```sh
banksman acquire --as phone --where kind=emulator --where form=phone --start \
                 --as port --where kind=port --for "UI tests" --wait 5m
banksman run --where kind=sdk --wait 5m --for "create an AVD" -- avdmanager create avd ...
```

## 16. Roadmap

Done:

- Project scaffold: package, CLI with `version` and `status`, tests, CI.
- The lease core: lease files, the file lock, states, void triggers, reaping.
- Kinds as configuration: the configuration file, counted kinds, kinds that list their
  instances, timeouts for each kind, and the `on_void` hook.
- Fencing in both directions and confirmed termination, with quarantines that last until a
  person releases them.
- Discovery and the allowlist: `admin discover`, the `discover` hook, presets for Android
  emulators and Android devices, and the inventory.
- Holder identity for any agent, and `whoami`.
- Requests by properties: `acquire` with facts, tags, and an order of choice; `touch` and
  `release`; and the `on_acquire` hook.
- Joint acquire: several resources in one call, one holding for each call, and accounts
  leased with their device.
- The console: `status` with every resource and when it can be free, `watch`, and the `log`.
- Optional build slots: a Gradle init script that holds a slot while each build runs, also
  when Gradle reuses the configuration cache.
- The supervised run: `banksman run`, and the helper, the stub for tests, and the rules for
  the scripts and agents of a project.
- The serial in the lease, from discovery only, for `status`, the hooks, and `banksman run`,
  with `ANDROID_SERIAL` and the placeholders `{serial}` and `{resource}`.
- The optional adb guard: an adb wrapper that refuses an agent's device command on the device
  of another holding, and commands that disconnect every device.
- Remote devices of Android Device Streaming: the `android-remote` preset with a catalogue
  that `admin discover` fetches, paid kinds and `--paid`, and `acquire --start`, which
  reserves, connects, and extends a remote device, with the reservation id in the lease, a
  new connection in `run`, and a take-back that ends only what banksman created.
- Named holdings: `--holding <label>` for `acquire`, `run`, `touch`, and `release`, and
  `held`; and the guide for agents, which `banksman agent-guide` prints in the format of a
  skill.
- Emulators that banksman starts: `acquire --start` for the `android-emulator` preset, on a
  console port that banksman chooses before the boot, a take-back that ends only the
  emulators that banksman started, and unmanaged instances, which a request does not get
  unless the kind says so.

Next:

- `explain`, and a queue for the callers that wait.
- Keeping a lease while an agent waits for a person, and getting the same resource back
  after a lease ends.
- Ending a free emulator that banksman started after a set time without a lease, because each
  emulator uses several GB of memory. The reaper must then hold a lease of its own while it
  ends the emulator, so that no request gets it meanwhile.

## 17. Open questions

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
  Codex background server keeps running after its session ends. `banksman whoami` shows
  what banksman finds in each setup.
- Can banksman tell the subagents of one session apart? Checked with Claude Code 2.1 and with
  the Codex command-line tool 0.159: no. A subagent runs its commands in the agent process of
  its parent, and `banksman whoami` shows the same agent process for both. Claude Code gives a
  subagent the same `CLAUDE_CODE_SESSION_ID` as its parent and no value of its own. Codex gives
  it the same `CODEX_SESSION_ID` and its own `CODEX_THREAD_ID`. So the label of a holding is
  what tells subagents apart (section 9). Should banksman scope labels by `CODEX_THREAD_ID` for
  Codex, although Claude Code has no such value?
- Is the idle timeout longer than the longest silent period of a legitimate run, for
  example a cold build that also waits for a build slot? The log records the longest time
  between two touches of each lease that its holder released (section 10).
- Some agents run their shell commands in a sandbox. On macOS, a sandboxed command cannot
  start `ps`, because `ps` is a setuid program, and banksman needs `ps` to check owner
  processes. So banksman must run outside the agent's sandbox. Which setting does each agent
  need for that?
- The adb guard reads the process list with `ps` when the lease id does not decide, and on
  macOS that alone takes about 20 ms. Is the time that the guard adds a problem for a test that
  calls adb many times? A direct read of the process table, with `sysctl`, would be faster.
- Only a quarantined lease is kept outside `/tmp`. A void lease whose scripts still run, and
  that no command reaps, is kept only in `/tmp`. If no banksman command runs for 3 days, a
  cleaner of temporary files can delete it before it is quarantined. Should every lease with
  registered scripts also be kept outside `/tmp`?

