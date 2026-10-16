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
- Holder identity for any agent. A lease records its owner (the worktree, found from the
  `.git` entry without running `git`), the issue id (from the branch name or the worktree
  directory name, through `issue_pattern`), the agent (the nearest ancestor process whose
  program is in `agents`, `claude` and `codex` by default), the agent's process as the owner
  process, a session id when the agent gives one, a purpose, and an expected hold time.
  The `[holder]` table of the configuration sets `agents` and `issue_pattern`. `status`
  shows the holder as, for example, `codex · #123 · verify the tablet layout`, and
  `status --json` carries the purpose only with `--verbose`. `banksman whoami` shows the
  holder that banksman would record. Lease files carry the holder, so their schema is now 3.
- Discovery and the allowlist. `banksman admin discover` runs the `discover` hook or the
  preset of each discovered kind, asks which instances and accounts agents may use, and
  writes `~/.config/banksman/inventory.toml`. It selects at first only what the `preselect`
  patterns match, refuses what a person leaves unselected, and never allows a refused name
  again on its own, also not with `--all` or as an instance of another kind. `--yes` and `--json` make it scriptable. It warns
  about an account that is signed in on several selected instances, and an instance whose
  accounts are not known is not offered for work that needs an account. The presets
  `android-emulator` and `android-device` find AVDs, running emulators, and physical
  devices with their facts (such as `manufacturer`, `model`, `codename`, and the
  `display_name` of an AVD) and Google accounts; they only read, and they find the SDK in a
  fixed place or in `[android]`, never through `$ANDROID_HOME`.
- Requests by properties. `banksman acquire --where <clause>` grants a permitted resource
  that is present now and matches every clause, with the operators `=`, `!=`, `~`, `>=`, and
  `<=`, and prints `RESOURCE`, `KIND`, `LEASE`, `STATE`, and `SERIAL` as `KEY=value` lines,
  or JSON with `--json`. Facts come from discovery, and the presets now also give `running`.
  banksman sets `kind`, `account` (whether an allowed account is signed in), and `tag`
  itself, and the operator assigns tags under `[tags]` in the inventory. An unknown
  attribute or kind is an error. banksman chooses the caller's lease that it keeps with
  `--lease`, then the kind with the lowest `rank` (1 for the `android-device` preset, 0 for
  other kinds), then an instance that runs before one that does not run. banksman starts
  nothing: every grant is `ready`, and the holder starts an instance that does not run.
  `--wait` waits while every matching resource is held, and exit status 4 means that they
  are all in use.
- The `on_acquire` hook of a kind resets an instance before a run gets it. When it fails,
  the lease is given back. The lease names the process that runs the hook, and no other
  command takes the resource back or hands it on until that process has ended. After the
  reset, `acquire` reads the facts of the instance again, so that it prints the current
  serial.
- `banksman touch` and `banksman release` take the lease id. `touch` records the agent
  process as the owner process, and `release --all` gives back the leases of the caller's
  agent process. A held resource is never granted again without its lease id, also not to
  the same worktree, because several agents can work in one worktree.
- Joint acquire. `banksman acquire --as <part> ...` grants several resources in one call, all
  or nothing, and prints the keys of each part with the part name as a prefix, for example
  `PHONE_RESOURCE`. A request whose parts could not all be met even with every resource
  free fails at once. Each lease has its own id, which is printed for each part, for example
  `PHONE_LEASE`, and the leases of one `acquire` form one holding: `check`, `enter`, and
  `touch` touch all of them, and `touch --lease` and `release --lease` without `--resource`
  act on all of them, with the id of any of them. The JSON of `acquire` has a list `parts`.
  While a request resets instances, all its new leases are booting and name the `acquire`,
  so that no other process hands one on before `acquire` returns, and each boot deadline
  counts the time of the resets before it.
- Accounts leased with their device. `acquire --accounts N` grants a resource on which N
  allowed accounts are signed in now, together with those accounts, and prints them as
  `ACCOUNTS`. No request gets an account while a lease in any state lists it, so two runs
  never use one account on two devices at the same time. `status` shows the accounts of each
  lease. Lease files carry the accounts and the holding, so their schema is now 4.
- The console. `banksman status` shows every resource that agents may use, free ones
  included, with its form and API level, its state, since when it is held, its last use, when
  it can be free (expected, at the latest, and if abandoned), its accounts, and its holder. It
  never shows a discovered instance that the inventory does not allow, and it shows the notes
  of discovery only as a count, because they can name such an instance; `acquire` does the
  same. `status --json` carries the same data in a new shape, a list `resources` and `notes`.
  `banksman watch` shows the table again every few seconds.
- `banksman log` shows the history of acquire, release, void, reap, quarantine, and forced
  release events, kept in `~/.local/state/banksman/log.jsonl`, with `--since` and
  `--resource`. A release and a void record how long the lease was held and the longest time
  between two touches; a quarantine records the scripts that still ran and whether stopping
  was on for the kind. Lease files carry the time of the grant and the longest time between
  two touches, so their schema is now 5.
- Optional build slots for Gradle, an example of a counted kind that is not a device. A
  project that leases devices does not need them. `banksman admin gradle-init` prints a
  Gradle init script that the operator saves in `~/.gradle/init.d/`, and `banksman admin gradle-init --build-slot` prints
  the build-slot part that it applies, which the operator saves in `~/.gradle/banksman/`.
  Every Gradle build of the user then holds a slot of the counted kind `build` while it runs,
  also when Gradle reuses the configuration cache, and gives it back when it ends. The owner
  process of the slot is the process that runs the build, so the slot is free as soon as a
  daemon ends. A build waits for a slot for 30 minutes, or for the Gradle property
  `banksman.buildSlotWait`, and then fails with a message that says that this is a limit of
  the machine. Without `banksman` on the `PATH`, the build-slot part does nothing. On a Gradle
  older than 7.4, a build runs as it does without banksman.
- `acquire --wait` says on standard error when it starts to wait, and stops waiting when its
  owner process ends.
- `banksman run --lease <id> --resource <name> -- <command>` runs a command under a lease, in
  a process group of its own that is registered with the lease before the command starts. It
  checks the lease while the command runs, stops the group when the lease is lost (SIGTERM,
  then SIGKILL), and leaves the lease only after the group has ended. It exits with the
  status of the command, or with 3 when the lease was lost, also when the command failed after
  that. When it ends first, the command ends too. When its input is the terminal, the command
  gets the terminal, so it can read input, and Ctrl-C and Ctrl-Z work as for any command. With `--where` instead of `--lease`, it leases a resource for
  the command and gives it back after the command, so a counted kind with `count = 1` is a
  mutex for one command at a time. The command gets `BANKSMAN_RESOURCE`, `BANKSMAN_KIND`, and
  `BANKSMAN_LEASE` in its environment, and `BANKSMAN_SERIAL` and `BANKSMAN_ACCOUNTS` when the
  grant has them. Neither the command nor a hook inherits these variables from the
  environment of banksman.
- `acquire` prints `KEPT` for each part: `true` for a lease of the caller's holding that
  `--lease` kept, which can have another id than the one that the caller passed. A lease whose
  owner process still runs and started the caller keeps that owner when the caller keeps or
  touches it, so a script inside `banksman run --where` cannot move the lease of the run to
  the agent.
- The serial in the lease. A lease records the serial of its instance, for example
  `emulator-5554`, from discovery only: at the grant, after an `on_acquire` reset, and
  whenever `acquire`, `run`, `status`, or `watch` runs discovery. An instance that stops loses
  its serial, a slow discovery never undoes a newer one, and one serial belongs to one held
  lease at most. `status` shows the serial in a column and in its JSON. Hooks get
  `BANKSMAN_SERIAL`; the `on_void` hook gets the serial that discovery finds at the take-back.
  `banksman run` asks discovery for the serial before its command starts, also with `--lease`,
  and gives its command only a serial that discovery confirms then, in `BANKSMAN_SERIAL`. For a kind with an Android preset, `run` and the hooks
  also set `ANDROID_SERIAL`, or a value that no device has while the serial is not known. `run`
  replaces each argument that is exactly `{serial}` or `{resource}`, and fails before the
  command starts when it needs a serial that is not known. The `android-emulator` preset finds
  the serial of an emulator that still boots. Lease files carry the serial, so their schema is
  now 6: lease files of an earlier banksman cannot be read after the upgrade, and
  `banksman admin release --force --resource <name>` frees their resources.
- The optional adb guard. `banksman admin adb-shim` prints an `sh` wrapper that the operator
  saves as `adb` in a directory first on the `PATH` of the agents. For every call, it runs
  `banksman guard adb`, which only decides: it refuses an agent's device command with exit
  status 3 when another holding leases the device, or prints the adb of the SDK of the
  configuration, which the wrapper then runs in its own place. A call passes for a person, for a device without a lease, for the holding of the lease
  id in `BANKSMAN_LEASE` or of an owner process above the caller, and for the hooks of the
  `acquire` or the reaper that the lease names. The guard finds the device as adb does, from
  `-t`, `-s`, `-d`, `-e`, `ANDROID_SERIAL`, or the only device, and for `adb emu` by the
  console port or the only emulator. It also refuses `adb kill-server`, `adb reconnect offline`,
  `adb disconnect` without an address, and `adb forward --remove-all` while an instance of
  another holder runs. `strict = true` in the new `[guard]` table also refuses a
  device that no lease of the caller has. The guard reads the lease files without the lock,
  reads the process list and runs `adb devices` only when it must, and lets the call run with
  a warning when it cannot decide. When the configuration names no `sdk` or cannot be read, the
  guard names the adb that the wrapper saved, and when banksman cannot start or fails, the
  wrapper runs that adb itself. The `banksman` command now starts the guard without loading
  the rest of the command line.
- Two schema numbers. The lease schema is the version of the lease file format, and it
  changes on every change of the format, as before. The output schema is the version of the
  JSON that the commands print and of the lines of the log. A new field does not change it,
  and a reader ignores the fields that it does not know. It changes when a field is removed
  or renamed, or changes its type or its meaning. Both are 6 now, so a reader of schema 6
  sees no change. `banksman version` shows both numbers, and `version --json` has the new
  field `lease_schema`. `banksman log` also reads the lines of schema 5 again, so the history
  from before the upgrade to schema 6 is back. The test suite records the shape of every JSON
  document, so a change of the output fails the tests until its shape is recorded.
- Remote devices of Android Device Streaming, with the new preset `android-remote`. An
  instance is a model of the catalogue of the service, such as `tokay:34`. `banksman admin
  discover` fetches the catalogue with the `android` command line tool, whose path is the new
  key `cli` in `[android]`, and the new kind key `project` names the Google Cloud project.
  Every other command reads only local files and adb, never the network. The
  `android-device` preset no longer offers a device whose serial starts with `localhost:`.
- Paid kinds. The new kind key `paid` marks a kind whose every use costs money; it is true by
  default for `android-remote`. `acquire` and `run --where` offer a paid resource only with
  the new flag `--paid`, and after every resource that costs nothing. A request whose matches
  are all paid fails at once and says why. `status` shows `remote (paid)`, and
  [docs/PROJECTS.md](docs/PROJECTS.md) has rules that ask the user before every `--paid`.
- `acquire --start` and `run --where --start`: banksman reserves and connects a remote device
  that does not run, extends the reservation to the hard cap of the lease, and grants it when
  adb lists it. The lease is `booting` meanwhile, under the boot deadline. The lease records
  the id of the reservation as soon as the tool prints it; `acquire` prints it as `HANDLE`,
  and `run` and the hooks get it in `BANKSMAN_HANDLE`. `run` and `acquire --lease` connect a
  dropped remote device again. A release keeps the reservation for the next request, and the
  reaper ends a reservation of a void lease only when banksman created it. Lease files carry
  the id, so the lease schema is now 7: lease files of an earlier banksman cannot be read
  after the upgrade, and `banksman admin release --force --resource <name>` frees their
  resources. The output schema stays 6: `status --json` and the grant only get the new fields
  `paid`, `handle`, and `ends`.
- Named holdings. `acquire --holding <label>` keeps the holding of the caller's agent process
  that has this label, as `--lease` does, also a paid one without a new `--paid`; without such
  a holding, it starts one with the label. The caller chooses the label, so an agent that lost
  its lease id, for example after a compaction of its context, gets the same device again
  with the same command. `run`, `touch`, and `release` take `--holding` instead of `--lease`,
  and `run --holding` needs `--resource` only when the holding has several resources. A
  holding that has a booting lease, for example of an `acquire` that a time limit stopped
  during its reset, is not kept, by a label or by a lease id: the request waits as for a
  resource in use, `run --holding` refuses it, and `release` gives it back. The new
  command `banksman held` lists the held leases of the agent process with their labels, lease
  ids, resources, and serials. `status --json --verbose` shows the label. Lease files carry the
  label, so the lease schema is now 8: lease files of an earlier banksman cannot be read after
  the upgrade, and `banksman admin release --force --resource <name>` frees their resources.
  The output schema stays 6.
- `banksman agent-guide` prints a guide for agents in the format of a skill: how to get a
  device with a label, run work on it, and give it back, the exit statuses, the time limits of
  the shell tools of agents, and the rules for paid devices and for `banksman admin`. Agents
  may run it, so the text always matches the installed banksman, and the operator can save it
  as a skill. The examples in the documentation now wait 5 minutes, less than the longest time
  for which the Bash tool of Claude Code waits for a command.
- The guide for agents describes the time limits of the shell tools as they were measured.
  The Bash tool of Claude Code moves a command that passes its timeout to the background,
  where it continues, and stops it after 10 more minutes; a stop signals every process below
  the command. The shell tool of Codex returns after about 10 seconds while the command
  continues, so the agent reads the output again until the command ends. The guide also says
  that `banksman held` shows a holding whose `acquire` continues in the background, and how to
  give a long command a background time limit. A test parses every `banksman` command that
  the guide shows, so the guide cannot show an option that banksman does not take.
- `acquire --start` and `run --where --start` start emulators too. For a granted AVD of the
  `android-emulator` preset that does not run, banksman chooses a free console port from 5554
  to 5584 and records its serial in the booting lease before the start, under the lock of the
  lease store, so two starts at the same time never get the same serial. It runs
  `emulator -avd <name> -port <port>` with the options of the new kind key `start_args` in a
  session of its own, and grants the emulator when its boot has completed, under the boot
  deadline. A start that fails, or that does not complete in time, ends the emulator and gives
  the lease back. An AVD also counts as running when the process list has its emulator, also
  in the first seconds of a start that adb does not see yet; on macOS, banksman reads the
  arguments of each process from the kernel, so a shell whose script starts an emulator does
  not count. An AVD that runs as several emulators, for example with `-read-only`, has no
  serial. A release leaves the emulator
  running; the reaper ends the emulator of a void lease only when banksman started it, by
  SIGTERM and then SIGKILL to that process only. banksman records the emulators that it
  knows in `~/.local/state/banksman/emulator`, and the output of each start in a log there.
- Unmanaged instances. An emulator or a remote device that runs, but that banksman did not
  start and did not see run when a lease of it ended, has the new state `unmanaged` in
  `status`. A request does not get it, and waits for it as for a resource in use, unless the
  new kind key `unmanaged` is `"grant"`; then it comes after the instances that banksman
  knows. An AVD whose emulator banksman knows is unmanaged too while it also runs as another
  emulator. When a lease ends, banksman records the instance that runs then, so an emulator
  that a holder started under its lease is free afterwards, not unmanaged. The lease schema and
  the output schema do not change; `status --json` has the new state value `unmanaged`.
- The free timeout. With the new kind key `free_timeout`, for example `"30m"`, valid only for
  the `android-emulator` preset and off by default, the reaper ends a free emulator that
  banksman started when no lease of its AVD has existed for that time, counted in awake time.
  Under the lock of the lease store, it first writes a `draining` lease of its own for the AVD,
  with the owner `banksman` and the new void reason `free_timeout`, then ends the emulator and
  runs the `on_void` hook as for a take-back, and deletes the lease. So a request during the end
  waits, and never gets an emulator that ends. When the end finishes after the discovery of a
  request and before its grant, the request looks again, so it never gets the serial of an
  emulator that has ended. An emulator that banksman did not start
  is never ended. The log records the end as a `reap` event with the reason `free_timeout`. The
  reaper runs only at the start of a command, so run `banksman reap` at a fixed interval to end
  free emulators also while no agent works. The record of the emulators keeps since when each
  AVD has no lease, so its schema is now 2; banksman also reads schema 1. The lease schema is
  now 9 for the new void reason, and banksman also reads lease files of schema 8, so the upgrade
  keeps the leases. The output schema stays 6: `void_reason` and the `reason` of the log have a
  new value.
- A start or a reset whose `acquire` is stopped ends at once. A `booting` lease is void as
  soon as the `acquire` that it names has ended, by pid and start time, with the new void
  reason `acquire_gone`, and the next command takes it back: it ends the emulator or the
  reservation that banksman started, and runs the `on_void` hook. Before, the lease waited for
  its boot deadline, and a request with the same label waited for it too. This covers SIGTERM
  and SIGKILL, and a shell tool that signals every process below the command, the emulator
  too. The lease schema is now 10 for the new void reason, and banksman also reads lease files
  of schemas 8 and 9. The output schema stays 6.
- A request does not get an instance whose lease ended after its discovery started. A look of
  `acquire` decides with the facts of its discovery: whether the instance runs, and its serial,
  its handle, and its accounts. A lease that ended meanwhile can have changed the instance: the
  reaper removed the reservation of a remote device, the failed start of another request ended
  its emulator, or the holder stopped its emulator before the release. Every end of a lease now
  records its awake time in `.ended.json` in the state directory, and a resource whose lease
  ended after the discovery of the request started counts as in use for that look. When only
  such resources keep the request from being met, `acquire` looks again at once, also without
  `--wait`; a second such look in a row counts as busy. This replaces the check of the
  emulators that banksman knows, which covered only emulators. The record has a schema of its
  own; the lease schema and the output schema do not change.
- A warn mode for the adb guard, and a record of what the guard refuses. With
  `strict = "warn"` in `[guard]`, the guard lets an agent's device command on a device that no
  lease of its holding has run, says in one line on standard error that strict mode would
  refuse it, and records it in the log. Each refusal, in every mode, is in the log too, as a
  new `guard` event with the new fields `action` (`refused` or `warned`), `command` (only the
  words of the adb command, never its arguments), and `serial`; its `reason` says why, and its
  holder is the caller. A refused command for every device has one event for each running
  device of another holder. `banksman log` has the new options `--event`, which shows only the
  events of one kind, and `--count`, which groups the events by event, device, worktree, and
  agent: `banksman log --event guard --count` lists the flows that still use devices without
  banksman. The guard takes the lock of the lease store only to add an event, and waits for it
  for half a second at most. The strict refusal and the refusal for the device of another
  holder now end with "Run banksman agent-guide and follow it". The output schema stays 6, and
  an earlier banksman leaves the guard events out of its `log`.
- Affinity: a request gets the free instance that its worktree used last first, among the
  instances of the same rank, also before one that runs, so that banksman starts the stopped AVD
  of the worktree instead of granting the running AVD of another worktree. An unmanaged
  instance never counts as the worktree's. `affinity = false` turns it off for a kind. The grant
  prints the new line `PREVIOUS`, and its JSON the new field `previous`: `self` when the last
  lease of the instance that a holder used was of the same worktree, ended with a release on
  this boot, and banksman knows of nothing that changed the instance since, otherwise `other`.
  A take-back hook, a void lease, a forced release, a restart, a new reservation of a remote
  device, and an `on_acquire` hook all make it `other`; a kept lease is `self`. The helper of
  a project sets `BANKSMAN_PREVIOUS`, and the stub prints `PREVIOUS`. The record of the ends of
  leases moved from `.ended.json` in the state directory to
  `~/.local/state/banksman/ended.json`, so that it outlives a restart, and it now keeps the
  last lease that a holder used for each resource; its schema is now 2, and a record of
  schema 1 counts as empty. A record that cannot be written no longer stops the end of a
  lease: the command warns, and deletes the record, so that it does not name an earlier holder
  as the last one. `BANKSMAN_ENDED` changes its path for tests. The lease schema and
  the output schema do not change.
- `release --end`, with `--lease`, `--holding`, `--resource`, or `--all`, gives the lease back
  and ends its instance when banksman started it: the emulator that banksman started, or the
  reservation of a remote device that banksman created. A release otherwise leaves the
  emulator running until the free timeout of its kind, and keeps the reservation, which costs
  money, until its own end. For an instance that banksman did not start, or a reservation that
  it adopted, the release says that it leaves the instance as it is, and releases anyway. While
  another `acquire` still starts or resets the instance, the release decides only when that
  `acquire` has ended, so that it also ends a reservation whose id the start did not know yet.
  The end is a take-back: the lease drains with the new void reason `released_end` and the
  releasing command as its reaper, so that no request gets the instance and the adb guard
  refuses it meanwhile; the command then ends the instance, runs the `on_void` hook of the
  kind, and deletes the lease. While scripts of the lease still run, the lease drains first,
  with `stop = true` the release stops them first, and the instance ends only after them, at a
  later command. An instance that does not end quarantines the lease. The log records a
  `release` event with the new field `end`, then a `reap` with the reason `released_end`. A
  release with an end counts as a release for `PREVIOUS`, unless an `on_void` hook ran. The
  lease schema is now 11 for the new void reason, and banksman also reads lease files of
  schemas 8, 9, and 10. The output schema stays 6.
- `banksman admin doctor` checks an install and changes nothing: the Python version, the
  banksman on the `PATH`, the configuration, the inventory and which allowed instances
  discovery finds now, the adb and the emulator of the Android SDK with `adb version` and
  whether adb can list the devices, the adb that comes first on the `PATH`, the directories of
  banksman, and the quarantined leases. It
  compares the saved adb wrapper, the two Gradle files, and the guide for agents saved as a
  skill with what this banksman prints, and gives the command that saves a file again. Each
  check is `ok`, `warn`, `fail`, or `skip`, the exit status is 1 when a check fails, and
  `--json` prints the checks.
- `run --holding` without `--resource` runs the command under every held lease of the
  holding, also the lease of a port or an account in it, and `--resource` can be repeated to
  choose some of the leases. The run registers its group with each lease, checks each one, and
  stops the command with status 3 when any of them is lost, so that a test runner that shards
  a run over several devices is fenced by the lease of each device. Before, `run --holding`
  needed one `--resource` for a holding of several resources. The command gets
  `BANKSMAN_RESOURCES` and `BANKSMAN_SERIALS`, and the placeholders `{resources}` and
  `{serials}` become one argument each: comma-separated lists in the order of the resource
  names, or of the `--resource` options. The lists of serials have only the serials that are
  known, and `{serials}` is an error while a device of the run has no known serial. Under
  several leases, the variables of one lease are not set, `{serial}` and `{resource}` are an
  error, `BANKSMAN_LEASE` is the id of one lease of the holding, and `ANDROID_SERIAL` names no
  device when the run has several devices, or names the only device of the run. Under one
  lease, nothing changes, and the lists have one value. The stub for tests sets the lists and
  replaces the new placeholders. The lease schema and the output schema do not change.
- A device call of the holder through the adb guard now touches its lease. When the guard lets
  a device command run because a held lease of the caller's holding has the device, through
  `BANKSMAN_LEASE` or through an owner process above the caller, it touches that lease and the
  other held leases of its holding, as `touch` does but without recording an owner process,
  when the last touch of one of these leases is more than a minute old, or more than a quarter
  of its idle timeout when that is shorter. So an agent that uses its device with direct adb calls keeps its lease, and an
  emulator that banksman started no longer ends under the agent after the idle timeout. A call
  within that time takes no lock. A touch that is due waits for the lock for half a second at
  most, never touches a lease that is void or that changed since the guard read it, and when
  it fails, the call runs as before and the guard says nothing. A refused or warned call, a
  call of a person, and a command for every device touch nothing. For a holder behind the
  guard, `longest_quiet` now counts the time without a banksman call and without a device
  call. The lease schema and the output schema do not change.
- The grant prints the new line `STARTED`, and its JSON the new field `started`: `true` for a
  part whose instance this `acquire` started with `--start`, an emulator that it started or
  the reservation of a remote device that it created, and `false` for an instance that ran
  already, a reservation that the start adopted, a kept lease, and a request without
  `--start`. A script can report whether its run started the instance, and end only what it
  started with `release --end`. The helper of a project sets `BANKSMAN_STARTED`, and the stub
  prints `STARTED=false`, or the value of `BANKSMAN_STUB_STARTED`. The guide for agents names
  the key. The lease schema and the output schema do not change.
- `banksman held` now also shows the adb calls of the agent process from the last 15 minutes
  that the adb guard refused or warned about, so that an agent finds a refusal that a script
  hid, for example a script that drops the standard error of adb. It groups them as
  `log --count` does: how many, the time of the last one, the resource, the details as `log`
  shows them, and, for the device of another holder, that holder as `status` shows it, or that
  its lease has ended. It finds them by the pid of the agent process and, for an event with a
  session, by the session too, so that a pid that the system gave to a new agent process does
  not show the calls of the earlier one; with `--owner-pid`, the pid alone counts. It reads
  only the last megabyte of the log. `held --json` has the groups in the new field `guard`, with
  the fields of `log --count --json`, or `null` when the log cannot be read. A guard event now
  records the lease id of the lease of the device. When an agent process has such calls,
  `status` shows a note that points to `held`. The guide for agents tells an agent to run
  `banksman held` first when a device command or a script fails for a reason that it does not
  explain. The lease schema and the output schema do not change.
- A command that has left the process tree of its agent, for example a test runner that a
  script started with `nohup` or in a session of its own, now finds its agent process through
  the pid variable of the agent, `CLAUDE_PID` for `claude`, when no known agent is above it.
  banksman takes the process that the variable names when it runs, its program is that agent
  and is in `agents`, and it started no later than the caller and each of its ancestors below
  the nearest ancestor that they share, so that a pid that the system gave to another agent
  process after the agent ended does not count; the walk up the process tree still comes first.
  Acquire, `touch`, `release --all`, `held`, `whoami`, `status`, and the adb guard use the same
  rule, so the guard refuses such a command on the device of another holding, and lets it use and
  touch the devices of its agent. A script that has left the process tree of `banksman run
  --where` keeps the run as the owner process of its lease when its `BANKSMAN_LEASE` names a
  lease of the holding, so the lease still ends with the run. The lease schema and the output
  schema do not change.
- An emulator that an agent started outside banksman, for example in a script that starts the
  emulator before it asks for a lease, now goes to that agent. Discovery reads the environment
  of the only emulator of an AVD that runs and that banksman does not know, and finds the agent
  process that started it as for a caller: the nearest agent above it, otherwise the agent
  process that its pid variable names. A request whose agent process is that one gets the
  emulator as one that banksman knows, also through `banksman run --where`; for every other
  request it stays unmanaged. The grant prints `STARTED=false` and `PREVIOUS=other`, and the end
  of the lease records the emulator, as for an emulator that the holder started under a lease,
  so from then on every request can get it. banksman still ends no emulator that it did not
  start, also when its agent has ended. `status` shows the agent of an unmanaged emulator in the
  HOLDER column, also when that agent has ended, and `status --json` has it in the new field
  `started_by` of each resource. The guide for agents tells an agent to acquire such an
  emulator of its own and not to start a second AVD, and to prefer `--start`. The lease
  schema and the output schema do not change.
- A start that continues after its `acquire`. `acquire --start --no-wait` prints the grant
  as soon as it has the resources, with `STATE=booting`, and a banksman process of its own
  continues the start and the `on_acquire` hook. `acquire` forks twice, so that this process
  leaves the process tree and the session of the caller before the shell tool of an agent can
  stop it. Under the lock of the lease store, it takes the booting leases over, in place of the
  `acquire`, only while they are valid and name that `acquire`. The leases are void as soon as
  it has ended, so a start that dies frees the instance at the next command. A start of an
  emulator ends early, and ends the emulator, when its lease is no longer booting, so a release
  of the holding stops it; this also applies to a start in a waiting `acquire`. `--no-wait`
  needs `--start`, and a request whose new booting leases would join a holding that has other
  leases is refused. The grant prints `STARTED=true` for a part whose instance the request starts. The new
  command `banksman wait --holding <label>`, or `--lease <id>`, exits with 0 when every lease of
  the holding is ready, and then touches the holding and prints its leases as `held` does, with
  4 while a lease boots, and with 3 when the holding is lost; `--wait` looks again every second.
  `wait --json` prints the leases with the fields of `held --json`. `run --holding` names `wait`
  when it refuses a booting lease. The guide for agents tells an agent to use `--no-wait` with
  `--start`. The lease schema and the output schema do not change.
- An agent's `adb devices` through the adb guard now says which devices are its own. For
  `adb devices` and `adb devices -l` of a caller with an agent process, the guard prints one
  line on standard error before the wrapper runs adb: the devices that the held leases of the
  caller's holdings have, by serial, or by resource while the serial is not known, after the
  label of each holding; or that no device is the caller's, and that `banksman acquire` gets
  one. A lease that is void by the rules of the reaper, for example after its idle timeout, but
  not reaped yet, is not named. Standard output stays the output of adb. The line names no free device, no other
  holder, and no purpose, adds no event to the log, and takes no lock. A person's call, a call
  with `BANKSMAN_LEASE` such as a command of `banksman run`, and `adb track-devices` get
  nothing, and when the guard cannot make the line, it says nothing. The new key
  `devices_note = false` in `[guard]` turns the line off. Outside `banksman run`, `adb devices`
  through the guard now reads the lease files and the process list, which adds about 30 ms to
  the call. The guide for agents names the line. The lease schema and the output schema do not
  change.
- [docs/PROJECTS.md](docs/PROJECTS.md): how a project uses banksman. A POSIX `sh` helper that
  a project copies into its repository and that does nothing without banksman, a stub of
  `banksman` for the tests of the project's scripts, deny rules for Claude Code and Codex
  that refuse `banksman admin`, the guide for agents, and an example of the test accounts of
  a server as a kind. Section 15 of the design has an example configuration for an Android
  project.
- Design document ([docs/DESIGN.md](docs/DESIGN.md)), recorded decisions
  ([DECISIONS.md](DECISIONS.md)), security policy and threat model
  ([SECURITY.md](SECURITY.md)), and contribution guide.

