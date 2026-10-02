# banksman design

**Status:** the project scaffold, the lease core, kinds as configuration, fencing, discovery,
holder identity, and requests by properties exist. Sections 3, 4, 5, 6, 8, and 9 are
implemented, with `banksman acquire`, `touch`, `release`, `reap`, a `status` that
lists the leases and their holders, `banksman whoami`, the commands that scripts run
(`enter`, `check`, and `leave`), `banksman admin discover`, and
`banksman admin release --force`. Joint acquire, the console, and build slots are not
implemented yet. Resolved decisions and how to change them are in
[DECISIONS.md](../DECISIONS.md); the threat model is in [SECURITY.md](../SECURITY.md).

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
| Named kind | Instances have identities: serials, emulator names, account addresses, or names that the operator lists, such as port numbers. |
| Counted kind | Instances are interchangeable slots: build slots, licence seats. |
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
- **Lease before start.** banksman starts nothing. A run takes the lease before it starts the
  instance, so an instance that is still starting always has a lease, and cannot be
  mistaken for an orphan. A lease is `booting` only while the kind's `on_acquire` hook
  resets the instance (section 5), with its own deadline, so that a caller that ends in the
  middle frees the resource soon.
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
  the lock, it ends the instance with the kind's `on_void` hook (section 5), and stops the
  registered scripts if stopping is on (section 4). Then, under the lock again, and only if
  the file still holds the same lease, it deletes the lease when every registered script has
  ended. Otherwise the lease drains until its scripts end, and later commands check them
  again. Only one reaper takes a lease back: another reaper leaves a `draining` lease alone
  while the reaper named in it runs, and finishes it only after that reaper has ended. So a
  late take-back never acts on a newer lease. A lease whose instance `acquire` resets names
  that process in the same way (section 6): when the lease becomes void during the reset, it
  drains, and it is taken back only after that process has ended. So a take-back never runs
  next to a reset.
- **Damaged lease files.** A lease file with a newer `schema` stops every command, because
  two versions of banksman must not share one state directory. A lease file that cannot be
  read keeps its resource out of use, and `status` shows it. Other resources are not
  affected.

## 4. Fencing, in both directions

Checking ownership when a script starts is not enough: one long test run can last tens of
minutes, and its lease can become void in the middle.

- **The lease id is a token.** The commands that grant a lease print its id. A script passes
  it to `enter`, `check`, and `leave`, and `touch` and `release` need it too. A
  script whose token does not name the current lease of the resource has lost it. So a
  script of an earlier lease, for example one of the same worktree, never acts on a newer
  lease.
- **The user's side.** A script registers itself with `enter` when it starts: its pid and,
  if it has one, the process group that it created for its work. `enter` also checks and
  touches the lease. A long-running script runs its work in its own process group, and runs
  `check` in a loop: `check` touches the lease and exits with status 0 while the lease is
  held, and with status 3 when the lease is lost, void, or draining. The script then stops
  its own process group. When the script no longer uses the resource, it runs `leave`.
  `leave` refuses while another process of the script still runs, so a script cannot leave
  while its work still uses the resource.
- **The reaper's side: no signals by default.** For a void lease, the reaper first marks it
  `draining`, so that no further ownership check passes. By default it sends no signal to
  any process. It waits until every registered script has stopped itself, confirms that by
  pid and start time, and then frees the resource. A script with a process group runs until
  no process is left in its group. If a script still runs at the drain deadline
  (`drain_timeout`, section 5), the lease is quarantined: it is never handed on, and it
  stays in `status` until a person runs `banksman admin release --force`, also when the
  script ends later. That command refuses while a banksman process takes the lease back or
  resets its instance, because that process could act on the instance of the next holder. The command that
  quarantines the lease names the processes that still run, on standard error. The reason:
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
  and 1 for a kind with the `android-device` preset, because physical devices are usually
  scarcer than emulators.
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

  [kinds.build]
  count = 4
  owner_grace = "0s"
  idle_timeout = "off"

  [kinds.port]
  instances = ["9101", "9102", "9103"]

  [kinds.emulator]
  preset = "android-emulator"
  preselect = ["qa_*"]
  boot_timeout = "8m"
  on_acquire = ["/usr/local/bin/reset-avd"]
  on_void = ["/usr/local/bin/stop-avd"]

  [kinds.device]
  preset = "android-device"
  ```

- **`on_void`** is a command that ends a void instance, for example one that kills an
  emulator. The reaper runs it when it takes back a void lease of the kind. Only a declared
  hook lets the reaper end an instance: banksman ships no such command, and without one the
  reaper ends nothing. If the hook exits with status 0, the instance is gone. If it fails,
  does not end in time, or cannot start, the lease is quarantined. The reaper reads the hook
  from the current configuration, not from the lease, so when the operator removes a hook,
  it stops at once, also for leases that are already void.
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
  The hook runs in its own process group. banksman kills that group after 60 seconds, and
  also when the command that runs the hook ends first, for example because its caller
  stopped it, so that a hook never outlives its reaper. A reaper that ends in the middle
  leaves the lease to the next reaper, which runs the hook again, so a hook must be safe to
  run again: for example, it exits with status 0 when the instance is already gone. When 3
  reapers have started the take-back of a lease and none has finished, for example because
  their callers stopped them, the lease is quarantined, so that a slow hook does not run in
  every command.
- **`on_acquire`** is a command that resets an instance before a run gets it. `acquire` runs
  it for each new lease of the kind, after it writes the lease and outside the lock, with the
  hook contract above. If it fails, the lease is given back (section 6). banksman ships no
  such command.
- Hooks do device-level work only. App-level work stays in the project's scripts.

## 6. Requests by properties

A run asks for what it needs, not for a resource by name.

- **Facts** come from the kind's `discover` hook or preset (section 8), and an agent cannot
  set them: `form` (phone, tablet, watch), `api`, `image`, `abi`, `manufacturer`, `model`,
  `codename`, `display_name`, `avd`, `serial`, and `running` (whether the instance runs
  now). banksman sets three attributes itself, and a hook cannot set them: `kind`; `account`,
  which is true when an allowed account is signed in on the instance now, false when its
  accounts are known and none of them is allowed, and missing when its accounts are not
  known; and `tag`.
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
                 --for "verify the tablet layout" --expect 20m --wait 15m
```

When several resources match, banksman chooses in a fixed order:

1. The resource of the caller's lease, if the caller passes the lease id with `--lease` and
   the resource still matches. The lease is touched, and it records the caller's agent
   process as its owner process.
2. A kind with a lower `rank` (section 5). So a request that does not name a kind gets an
   emulator before a physical device.
3. An instance that runs before one that does not run, which saves the time and the memory
   of a start.
4. The resource name.

`acquire` without `--lease` never grants a held resource, also not to the same worktree:
several agents can work in one worktree at the same time, so only the lease id tells their
holdings apart (section 9).

- **The grant.** `acquire` prints `KEY=value` lines: `RESOURCE`, `KIND`, `LEASE` (the lease
  id that the scripts pass back), `STATE`, and `SERIAL` when discovery knows it. Each value
  has only the characters of a resource name, so that a shell script can use it as it is: a
  serial with other characters is not printed. With `--json`, it prints the same as JSON.
- **banksman starts nothing.** Every grant is `ready`. The holder checks whether the
  instance runs, and starts it if it does not, for example with the `emulator` command. A
  fact from discovery can be wrong, for example for an emulator that is still starting, so
  `running` only orders the matches, and the holder decides.
- **The agent can choose.** `acquire` grants one resource and names it. It does not return a
  list, because two callers that choose from the same list choose the same resource. An
  agent that needs a specific resource asks for it, for example with `--where avd=qa_phone`.
- **`on_acquire`.** When the kind declares an `on_acquire` hook (section 5), the lease is
  `booting` while the hook runs, and then `ready`. If the hook fails, the lease is given back
  and `acquire` fails. A failed reset does not make the instance unsafe for the next holder,
  as a failed take-back does, so the instance is not quarantined. A lease that the caller
  keeps with `--lease` is not reset again. The hook can restart the instance, so `acquire`
  reads its facts again after the reset, and prints the serial that discovery finds then.
- **No other process acts on the instance during a reset.** The lease names the `acquire`
  that runs the hook, and a hook never outlives that process (section 5). When the lease
  becomes void or is released during the reset, it drains, and the resource is taken back
  or freed only after that process has ended. `admin release --force` refuses meanwhile.
- **Waiting.** While every matching resource is held, `acquire` fails with exit status 4.
  With `--wait`, it looks again every 5 seconds until the wait ends, and each look reads the
  configuration, the inventory, and the machine again. So the reaper of a caller that waits
  also uses the hooks of the current configuration (section 5). When no permitted resource that is present now matches, held or free,
  `acquire` fails at once, also with `--wait`, because only a held resource can become free.
  There is no queue yet: when many callers wait, a caller can get a resource before another
  caller that started to wait earlier.

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
  accounts. When adb cannot list the running emulators, `running` is left out.
  `preset = "android-device"` finds the physical devices that adb lists, with the facts
  `serial`, `manufacturer`, `model`, `codename`, `api`, `abi`, `form`, and `running`, which
  is always true, and their accounts. The facts are what the device gives: a Samsung phone
  gives the model `SM-S926B`, not "Galaxy S24+". banksman ships no table of marketing names, and it does
  not read the device name in the settings, which a person can change and which often
  holds a personal name. A device that is
  not authorized is reported, not offered. The presets use the SDK in `~/Library/Android/sdk`
  on macOS and in `~/Android/Sdk` on Linux, and the AVDs in `~/.android/avd`. The `sdk` and
  `avd_home` keys of the `[android]` table change them. The presets never read
  `$ANDROID_HOME` or `$ANDROID_AVD_HOME`, for the same reason as for the state directory.
  They only read, and they declare no `on_void`.
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

- `touch` records the caller's agent process as the owner process, so a lease survives a
  restart of the agent.
- `release --all` gives back only the leases whose owner process is the caller's agent
  process, so it acts for one agent. After a restart of the agent, it finds a lease only
  after a touch has recorded the new agent process; until then, the lease ends by its
  timeouts.

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

`watch`, `explain`, `log`, and `acquire --kind build --user-pid` do not exist yet. The rest
exists.

```
banksman acquire --where <attr><op><value> ... [--for <text>] [--expect <duration>]
                 [--wait <duration>] [--lease <id>] [--issue <id>] [--owner-pid <pid>] [--json]
banksman acquire --kind build --user-pid <pid> [--wait <duration>]
banksman enter   --resource <name> --lease <id> --pid <pid> [--pgid <pgid>]  # check, touch, register
banksman check   --resource <name> --lease <id>                    # touch; exit 0: yours, 3: lost
banksman leave   --resource <name> --lease <id> --pid <pid>
banksman touch   --resource <name> --lease <id> [--expect <duration>] [--owner-pid <pid>]
banksman release --resource <name> --lease <id>
banksman release --all [--owner-pid <pid>]                         # the leases of this agent
banksman status  [--json] [--verbose]
banksman whoami  [--json]
banksman watch
banksman explain --where <attr><op><value> ...
banksman log     [--since <time>]
banksman reap
banksman version [--json]

banksman admin discover [--kind <kind>] [--all] [--json] [--yes]
banksman admin release --force --resource <name>
```

Commands that grant a resource print `KEY=value` lines, for example `RESOURCE=`, `SERIAL=`,
and `LEASE=`, the lease id that the scripts pass back, so that a shell script can read them.
With `--json` they print JSON instead. Exit status 1 is an error, 2 a usage error, 3 a lease
that is lost, and 4 a request whose matching resources are all in use.

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
- Keep the lease id that `acquire` prints. Pass it to the scripts, and back to `acquire` with
  `--lease` to keep the same resource.
- Start the granted instance when it does not run, and handle one that already runs:
  discovery can miss an emulator that is still starting.
- Release leases before waiting for a person, so that a run that waits overnight does not
  hold a device. If a run forgets, the owner and idle checks free the device later.
- Keep app setup and cleanup in the project's scripts.

## 15. Roadmap

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

Next:

- Joint acquire and pairs.
- `status`, `watch`, `explain`, and `log`, and a queue for the callers that wait.
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
  Codex background server keeps running after its session ends. `banksman whoami` shows
  what banksman finds in each setup.
- Is the idle timeout longer than the longest silent period of a legitimate run, for
  example a cold build that also waits for a build slot?
- Some agents run their shell commands in a sandbox. On macOS, a sandboxed command cannot
  start `ps`, because `ps` is a setuid program, and banksman needs `ps` to check owner
  processes. So banksman must run outside the agent's sandbox. Which setting does each agent
  need for that?
- A raw `adb -s` call from an agent is not fenced, because only the scripts register as
  users. An `adb` wrapper on `PATH`, or an agent hook that checks the lease before a device
  command, would enforce it.
- Should a lease record the serial of an instance that its holder started, so that `status`
  and the `on_void` hook know it? That needs a command for the holder to report it, and it
  changes the lease file format.
- Only a quarantined lease is kept outside `/tmp`. A void lease whose scripts still run, and
  that no command reaps, is kept only in `/tmp`. If no banksman command runs for 3 days, a
  cleaner of temporary files can delete it before it is quarantined. Should every lease with
  registered scripts also be kept outside `/tmp`?

