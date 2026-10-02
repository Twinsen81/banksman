import fcntl
import json
import os
import pwd
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from helpers import SRC, age_lease, drain, edit_lease, reap_in_child

from banksman import SCHEMA_VERSION
from banksman.errors import BanksmanError
from banksman.fencing import Refused
from banksman.lease import (
    BOOT_TIMEOUT,
    BOOTING,
    DRAINING,
    HARD_CAP,
    IDLE,
    OWNER_GONE,
    QUARANTINED,
    READY,
    REBOOTED,
    RELEASE,
    Holder,
    Timeouts,
)
from banksman.store import (
    RELEASED,
    Busy,
    LockTimeout,
    NotHeld,
    Store,
    StoreError,
    Unreadable,
    default_quarantine_dir,
    default_state_dir,
)
from banksman.system import Machine

OWNER = "/work/tree-a"
OTHER = "/work/tree-b"


def lease_file(state_dir, resource):
    return json.loads((state_dir / f"{resource}.json").read_text())


def reaped(store):
    return [(item.lease.resource, item.lease.void_reason, item.outcome) for item in store.reap()]


def resources(store):
    return [lease.resource for lease in store.snapshot().leases]


def test_the_state_directory_does_not_depend_on_tmpdir(monkeypatch):
    monkeypatch.delenv("BANKSMAN_STATE_DIR")
    monkeypatch.setenv("TMPDIR", "/somewhere/else")
    assert str(default_state_dir()) == f"/tmp/banksman-{os.geteuid()}"


def test_the_state_directory_is_private(store, state_dir):
    store.snapshot()
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700


def test_a_state_directory_that_others_can_write_to_is_refused(store, state_dir):
    state_dir.mkdir()
    state_dir.chmod(0o777)
    with pytest.raises(StoreError, match="can write"):
        store.snapshot()


def test_a_state_directory_of_another_user_is_refused(store, state_dir, monkeypatch):
    state_dir.mkdir(mode=0o700)
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(StoreError, match="another user"):
        store.snapshot()


def test_a_symbolic_link_as_state_directory_is_refused(store, state_dir, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    state_dir.symlink_to(target)
    with pytest.raises(StoreError, match="not a directory"):
        store.snapshot()


def test_a_lease_file_that_others_can_write_to_is_refused(store, state_dir):
    store.acquire("phone-1", "device", Holder(OWNER))
    (state_dir / "phone-1.json").chmod(0o666)
    with pytest.raises(StoreError, match="can write"):
        store.snapshot()


def test_reserve_writes_a_booting_lease_before_the_start(store, system, state_dir):
    lease = store.reserve("emu-1", "emulator", Holder(OWNER))
    assert lease.state == BOOTING
    data = lease_file(state_dir, "emu-1")
    assert data["schema"] == SCHEMA_VERSION
    assert data["state"] == BOOTING
    assert data["awake"]["boot_deadline"] == system.now + 5 * 60
    assert stat.S_IMODE((state_dir / "emu-1.json").stat().st_mode) == 0o600


def test_a_lease_keeps_the_timeouts_that_it_was_created_with(store, system, state_dir):
    timeouts = Timeouts(boot_timeout=8 * 60, owner_grace=0, idle_timeout=None, hard_cap=60 * 60)
    store.reserve("emu-1", "emulator", Holder(OWNER), timeouts=timeouts)
    data = lease_file(state_dir, "emu-1")
    assert data["awake"]["boot_deadline"] == system.now + 8 * 60
    assert data["awake"]["hard_deadline"] == system.now + 60 * 60
    assert (data["idle_timeout"], data["owner_grace"]) == (None, 0)
    # The same owner gets its lease again with the values that it had.
    assert store.reserve("emu-1", "emulator", Holder(OWNER)).idle_timeout is None


def test_ready_ends_the_boot(store, system):
    reserved = store.reserve("emu-1", "emulator", Holder(OWNER))
    system.advance(60)
    lease = store.ready("emu-1", reserved.lease_id)
    assert (lease.state, lease.boot_deadline, lease.touched) == (READY, None, system.now)
    assert store.ready("emu-1", reserved.lease_id) == lease


def test_ready_needs_the_id_of_the_current_lease(store):
    # A script of an earlier boot of the same worktree must not mark a newer lease ready.
    earlier = store.reserve("emu-1", "emulator", Holder(OWNER))
    store.release("emu-1", OWNER)
    store.reserve("emu-1", "emulator", Holder(OWNER))
    with pytest.raises(NotHeld, match="another lease"):
        store.ready("emu-1", earlier.lease_id)


def test_acquire_writes_a_ready_lease(store):
    assert store.acquire("phone-1", "device", Holder(OWNER)).state == READY


def test_a_resource_has_one_holder(store):
    store.acquire("phone-1", "device", Holder(OWNER))
    with pytest.raises(Busy, match="held by"):
        store.acquire("phone-1", "device", Holder(OTHER))
    with pytest.raises(Busy):
        store.reserve("phone-1", "device", Holder(OTHER))


def test_the_same_owner_gets_its_own_lease_again(store, system):
    first = store.acquire("phone-1", "device", Holder(OWNER))
    system.advance(60)
    again = store.acquire("phone-1", "device", Holder(OWNER))
    assert again.lease_id == first.lease_id
    assert again.touched == system.now


def test_only_the_owner_can_use_the_lease(store):
    store.reserve("emu-1", "emulator", Holder(OWNER))
    for call in (store.touch, store.release):
        with pytest.raises(NotHeld, match="another owner|not held"):
            call("emu-1", OTHER)


def test_touch_keeps_the_lease_valid(store, system):
    store.acquire("phone-1", "device", Holder(OWNER))
    for _ in range(3):
        system.advance(15 * 60)
        store.touch("phone-1", OWNER)
    assert reaped(store) == []


def test_touch_records_the_new_process_of_a_restarted_owner(store, system):
    system.processes = {100: "start-100", 200: "start-200"}
    store.acquire("phone-1", "device", Holder(OWNER, owner_pid=100))
    del system.processes[100]
    lease = store.touch("phone-1", OWNER, owner_pid=200)
    assert (lease.owner_pid, lease.owner_started) == (200, "start-200")
    system.advance(10 * 60)
    assert reaped(store) == []


def test_a_lease_records_its_holder_and_the_expected_hold_time(store, system, state_dir):
    system.processes = {100: "start-100"}
    holder = Holder(
        OWNER,
        owner_pid=100,
        issue="#123",
        agent="claude",
        session="s-1",
        purpose="verify the tablet layout",
    )
    lease = store.acquire("phone-1", "device", holder, expect=20 * 60)
    assert (lease.issue, lease.agent, lease.session, lease.purpose) == (
        "#123",
        "claude",
        "s-1",
        "verify the tablet layout",
    )
    assert lease.expected == system.now + 20 * 60
    assert lease_file(state_dir, "phone-1")["purpose"] == "verify the tablet layout"
    assert store.snapshot().leases == [lease]


def test_a_touch_can_change_the_expected_hold_time(store, system):
    store.acquire("phone-1", "device", Holder(OWNER))
    system.advance(60)
    assert store.touch("phone-1", OWNER).expected is None
    assert store.touch("phone-1", OWNER, expect=5 * 60).expected == system.now + 5 * 60


def test_a_holder_that_is_not_valid_gets_no_lease(store, state_dir):
    with pytest.raises(BanksmanError, match="purpose"):
        store.acquire("phone-1", "device", Holder(OWNER, purpose="verify\x1b[2J"))
    assert not (state_dir / "phone-1.json").exists()


def test_the_owner_process_must_run(store):
    with pytest.raises(BanksmanError, match="not running"):
        store.acquire("phone-1", "device", Holder(OWNER, owner_pid=4242))


def test_a_touch_does_not_revive_a_void_lease(store, system):
    store.acquire("phone-1", "device", Holder(OWNER))
    system.advance(20 * 60)
    with pytest.raises(NotHeld, match="void"):
        store.touch("phone-1", OWNER)
    with pytest.raises(Busy):
        store.acquire("phone-1", "device", Holder(OWNER))


def test_release_frees_the_resource(store, state_dir):
    store.acquire("phone-1", "device", Holder(OWNER))
    store.release("phone-1", OWNER)
    assert not (state_dir / "phone-1.json").exists()
    assert store.acquire("phone-1", "device", Holder(OTHER)).owner == OTHER


def test_names_that_are_not_valid_are_refused(store):
    with pytest.raises(BanksmanError, match="resource name"):
        store.acquire("../phone-1", "device", Holder(OWNER))


def test_reap_keeps_valid_leases(store, system):
    store.reserve("emu-1", "emulator", Holder(OWNER))
    store.acquire("phone-1", "device", Holder(OTHER))
    system.advance(5 * 60 - 1)
    assert reaped(store) == []
    assert resources(store) == ["emu-1", "phone-1"]


def test_a_boot_that_does_not_end_is_reaped(store, system):
    store.reserve("emu-1", "emulator", Holder(OWNER))
    system.advance(5 * 60)
    assert reaped(store) == [("emu-1", BOOT_TIMEOUT, RELEASED)]
    assert resources(store) == []


def test_a_dead_owner_frees_its_lease_after_the_grace_period(store, system):
    system.processes = {100: "start-100"}
    store.acquire("phone-1", "device", Holder(OWNER, owner_pid=100))
    system.advance(4 * 60)
    del system.processes[100]
    assert reaped(store) == []
    system.advance(60)
    assert reaped(store) == [("phone-1", OWNER_GONE, RELEASED)]


def test_an_idle_lease_is_reaped(store, system):
    system.processes = {100: "start-100"}
    store.acquire("phone-1", "device", Holder(OWNER, owner_pid=100))
    system.advance(20 * 60)
    assert reaped(store) == [("phone-1", IDLE, RELEASED)]


def test_the_hard_cap_is_enforced_although_the_holder_touches(store, system):
    store.acquire("phone-1", "device", Holder(OWNER))
    for _ in range(17):
        system.advance(10 * 60)
        store.touch("phone-1", OWNER)
    assert reaped(store) == []
    system.advance(10 * 60)
    assert reaped(store) == [("phone-1", HARD_CAP, RELEASED)]


def test_a_restart_voids_every_lease(store, system):
    store.acquire("phone-1", "device", Holder(OWNER))
    system.boot = "boot-2"
    assert reaped(store) == [("phone-1", REBOOTED, RELEASED)]


def test_a_reaped_resource_can_be_leased_again(store, system):
    store.acquire("phone-1", "device", Holder(OWNER))
    system.advance(20 * 60)
    store.reap()
    assert store.acquire("phone-1", "device", Holder(OTHER)).owner == OTHER


def test_a_resource_that_cannot_be_taken_back_is_quarantined(state_dir, system):
    freed = False
    store = Store(state_dir, system, take_back=lambda lease: freed)
    store.acquire("phone-1", "device", Holder(OWNER))
    system.advance(20 * 60)
    assert reaped(store) == [("phone-1", IDLE, QUARANTINED)]
    system.advance(24 * 60 * 60)
    assert reaped(store) == []
    assert lease_file(state_dir, "phone-1")["state"] == QUARANTINED
    assert lease_file(state_dir, "phone-1")["reaper_pid"] is None
    with pytest.raises(Busy, match="quarantined"):
        store.acquire("phone-1", "device", Holder(OTHER))
    # After a restart, no process of the earlier boot can still use the resource.
    freed = True
    system.boot = "boot-2"
    assert reaped(store) == [("phone-1", REBOOTED, RELEASED)]


def test_a_quarantine_that_a_restart_did_not_clear_stays(state_dir, system):
    attempts = []

    def take_back(lease):
        attempts.append(lease.void_reason)
        return False

    store = Store(state_dir, system, take_back=take_back)
    store.acquire("phone-1", "device", Holder(OWNER))
    system.advance(20 * 60)
    assert reaped(store) == [("phone-1", IDLE, QUARANTINED)]
    system.boot = "boot-2"
    # A restart retries the take-back once. If it fails again, the quarantine belongs to the
    # new boot, so the next commands do not run the take-back again.
    assert reaped(store) == [("phone-1", REBOOTED, QUARANTINED)]
    assert reaped(store) == []
    assert attempts == [IDLE, REBOOTED]
    assert lease_file(state_dir, "phone-1")["boot_id"] == "boot-2"
    with pytest.raises(Busy, match="quarantined"):
        store.acquire("phone-1", "device", Holder(OTHER))


def test_the_owner_check_fails_while_the_lease_drains(state_dir, system):
    checks = []

    def take_back(lease):
        with pytest.raises(NotHeld, match="draining"):
            Store(state_dir, system).touch("phone-1", OWNER)
        checks.append(lease.state)
        return True

    store = Store(state_dir, system, take_back=take_back)
    store.acquire("phone-1", "device", Holder(OWNER))
    system.advance(20 * 60)
    assert reaped(store) == [("phone-1", IDLE, RELEASED)]
    assert checks == [DRAINING]


def test_a_draining_lease_left_by_a_dead_reaper_is_finished(store, system, state_dir):
    store.acquire("phone-1", "device", Holder(OWNER))
    edit_lease(state_dir, "phone-1", drain)
    assert reaped(store) == [("phone-1", IDLE, RELEASED)]


def taken_back_by(pid):
    def change(data):
        drain(data)
        data.update(reaper_pid=pid, reaper_started=f"start-{pid}")

    return change


def test_a_lease_that_a_running_reaper_takes_back_is_left_to_it(store, system, state_dir):
    store.acquire("phone-1", "device", Holder(OWNER))
    system.processes = {4242: "start-4242"}
    edit_lease(state_dir, "phone-1", taken_back_by(4242))
    assert reaped(store) == []
    assert lease_file(state_dir, "phone-1")["reaper_pid"] == 4242


def test_the_lease_of_a_reaper_that_ended_is_taken_over(store, system, state_dir):
    store.acquire("phone-1", "device", Holder(OWNER))
    edit_lease(state_dir, "phone-1", taken_back_by(4242))
    assert reaped(store) == [("phone-1", IDLE, RELEASED)]


def test_a_reaper_finishes_its_own_lease_after_a_failed_take_back(state_dir, system):
    attempts = []

    def take_back(lease):
        attempts.append(lease.lease_id)
        if len(attempts) == 1:
            raise OSError("the device did not answer")
        return True

    store = Store(state_dir, system, take_back=take_back)
    store.acquire("phone-1", "device", Holder(OWNER))
    system.advance(20 * 60)
    with pytest.raises(OSError):
        store.reap()
    assert reaped(store) == [("phone-1", IDLE, RELEASED)]
    assert len(attempts) == 2


def test_a_reaper_leaves_a_lease_that_another_reaper_took_over(state_dir, system):
    def take_back(lease):
        system.processes = {4242: "start-4242"}
        edit_lease(state_dir, "phone-1", taken_back_by(4242))
        return True

    store = Store(state_dir, system, take_back=take_back)
    store.acquire("phone-1", "device", Holder(OWNER))
    system.advance(20 * 60)
    assert reaped(store) == []
    assert lease_file(state_dir, "phone-1")["reaper_pid"] == 4242


def test_only_one_reaper_takes_a_lease_back(state_dir):
    # Two reapers in two processes: the second one runs while the first takes the lease back.
    machine = Machine()
    Store(state_dir, machine).acquire("phone-1", "device", Holder(OWNER))
    age_lease(state_dir, "phone-1", 21 * 60)
    second = []

    def take_back(lease):
        second.append(reap_in_child(state_dir))
        return True

    first = Store(state_dir, machine, take_back=take_back).reap()
    assert second == [[]]
    assert [(item.lease.resource, item.outcome) for item in first] == [("phone-1", RELEASED)]


def test_the_claim_of_a_reaper_from_an_earlier_boot_is_taken_over(store, system, state_dir):
    store.acquire("phone-1", "device", Holder(OWNER))
    # The pid of that reaper now names a process of this boot.
    system.processes = {4242: "start-4242"}

    def claimed_before_a_restart(data):
        taken_back_by(4242)(data)
        data["boot_id"] = "boot-0"

    edit_lease(state_dir, "phone-1", claimed_before_a_restart)
    assert reaped(store) == [("phone-1", IDLE, RELEASED)]


@pytest.mark.parametrize("state", [READY, QUARANTINED])
def test_only_one_reaper_takes_back_a_lease_from_an_earlier_boot(state_dir, state):
    machine = Machine()
    Store(state_dir, machine).acquire("phone-1", "device", Holder(OWNER))

    def from_an_earlier_boot(data):
        data["boot_id"] = "an-earlier-boot"
        if state == QUARANTINED:
            data.update(state=QUARANTINED, void_reason=IDLE)

    edit_lease(state_dir, "phone-1", from_an_earlier_boot)
    second = []

    def take_back(lease):
        second.append(reap_in_child(state_dir))
        return True

    first = Store(state_dir, machine, take_back=take_back).reap()
    assert second == [[]]
    assert [(item.lease.resource, item.lease.void_reason, item.outcome) for item in first] == [
        ("phone-1", REBOOTED, RELEASED)
    ]


def test_the_reaper_does_not_delete_a_newer_lease(state_dir, system):
    def take_back(lease):
        # Meanwhile another reaper finished this lease, another owner leased the resource,
        # and a third reaper is taking that newer lease back.
        (state_dir / "phone-1.json").unlink()
        Store(state_dir, system).acquire("phone-1", "device", Holder(OTHER))
        edit_lease(state_dir, "phone-1", drain)
        return True

    store = Store(state_dir, system, take_back=take_back)
    store.acquire("phone-1", "device", Holder(OWNER))
    system.advance(20 * 60)
    assert reaped(store) == []
    assert lease_file(state_dir, "phone-1")["owner"] == OTHER


def test_an_unreadable_lease_file_keeps_only_its_resource_out_of_use(store, state_dir):
    store.acquire("phone-2", "device", Holder(OWNER))
    (state_dir / "phone-1.json").write_text("{not json")
    snapshot = store.snapshot()
    assert snapshot.unreadable == [Unreadable("phone-1", "the file is not valid JSON")]
    assert [lease.resource for lease in snapshot.leases] == ["phone-2"]
    assert reaped(store) == []
    with pytest.raises(Busy, match="cannot be read"):
        store.acquire("phone-1", "device", Holder(OTHER))


def test_a_lease_file_from_a_newer_banksman_stops_every_command(store, state_dir):
    store.acquire("phone-1", "device", Holder(OWNER))
    edit_lease(state_dir, "phone-1", lambda data: data.update(schema=SCHEMA_VERSION + 1))
    for call in (store.snapshot, store.reap):
        with pytest.raises(StoreError, match="schema"):
            call()


def test_temporary_files_of_an_interrupted_write_are_removed(store, state_dir):
    store.snapshot()
    (state_dir / ".tmp-left-over").write_text("{")
    store.snapshot()
    assert not (state_dir / ".tmp-left-over").exists()


LOCK_HOLDER = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
os.ftruncate(fd, 0)
os.write(fd, f"{os.getpid()}\\n".encode())
print("locked", flush=True)
time.sleep(60)
"""


def test_a_stopped_lock_holder_makes_commands_fail_instead_of_wait(store, state_dir, system):
    store.snapshot()
    holder = subprocess.Popen(
        [sys.executable, "-c", LOCK_HOLDER, str(state_dir / ".lock")],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        started = time.monotonic()
        with pytest.raises(LockTimeout, match=f"pid {holder.pid}"):
            Store(state_dir, system, lock_wait=0.2).snapshot()
        assert time.monotonic() - started < 5
    finally:
        holder.kill()
        holder.wait()
        holder.stdout.close()
    # The kernel frees the lock of a process that ended.
    assert store.snapshot().leases == []


TRY_LOCK = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("blocked")
else:
    print("locked")
"""


def test_the_lock_is_taken_on_a_lock_file_that_was_created_again(store, state_dir, monkeypatch):
    store.snapshot()
    lock_file = state_dir / ".lock"
    real_flock = fcntl.flock
    replaced = []

    def flock_while_a_cleaner_runs(fd, operation):
        if not replaced and operation & fcntl.LOCK_EX:
            # A cleaner of temporary files deletes the lock file, and another banksman
            # process creates it again.
            lock_file.unlink()
            lock_file.touch(mode=0o600)
            replaced.append(True)
        return real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", flock_while_a_cleaner_runs)
    with store._lock():
        result = subprocess.run(
            [sys.executable, "-c", TRY_LOCK, str(lock_file)],
            capture_output=True,
            text=True,
            check=True,
        )
    assert replaced
    assert result.stdout.strip() == "blocked"


GRANT = """
import sys, time
from pathlib import Path
from banksman.lease import Holder
from banksman.store import Busy, Store
from banksman.system import Machine
state_dir, go, owner = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
while not go.exists():
    time.sleep(0.001)
try:
    Store(state_dir, Machine()).acquire("phone-1", "device", Holder(owner))
except Busy:
    print("busy")
else:
    print("granted")
"""


def test_only_one_of_many_processes_gets_the_resource(store, state_dir, tmp_path):
    store.snapshot()
    go = tmp_path / "go"
    env = {**os.environ, "PYTHONPATH": SRC}
    children = [
        subprocess.Popen(
            [sys.executable, "-c", GRANT, str(state_dir), str(go), f"/work/tree-{index}"],
            stdout=subprocess.PIPE,
            text=True,
            env=env,
        )
        for index in range(8)
    ]
    go.touch()
    results = sorted(child.communicate(timeout=60)[0].strip() for child in children)
    assert results == ["busy"] * 7 + ["granted"]



# Fencing: the scripts that use a resource, and what the reaper does with them.

# The processes of an agent app (parent, process group): the app, the agent, a command that
# the agent runs in its own group, a script, and the group that the script created for its work.
AGENT_APP = {
    100: (1, 100),
    101: (100, 100),
    200: (101, 200),
    201: (200, 200),
    300: (201, 300),
    301: (300, 300),
}


def entered(store, system, owner_pid=101):
    """Lease phone-1 to the agent, and let script 201 enter it with its work group 300."""
    for pid, (ppid, pgid) in AGENT_APP.items():
        system.spawn(pid, parent=ppid, group=pgid)
    lease = store.acquire("phone-1", "device", Holder(OWNER, owner_pid=owner_pid))
    store.enter("phone-1", lease.lease_id, 201, 300)
    return lease


def end_the_script(system):
    system.end(201, 300, 301)


def users(store):
    (lease,) = store.snapshot().leases
    return [user.pid for user in lease.users]


def test_a_script_enters_the_lease_that_it_names(store, system, state_dir):
    lease = entered(store, system)
    assert lease_file(state_dir, "phone-1")["users"] == [
        {"pid": 201, "started": "start-201", "pgid": 300, "leader_started": "start-300"}
    ]
    system.advance(60)
    assert store.enter("phone-1", lease.lease_id, 201, 300).touched == system.now
    assert users(store) == [201]


def test_a_script_of_another_lease_has_lost_it(store, system):
    lease = entered(store, system)
    with pytest.raises(NotHeld, match="another lease"):
        store.check("phone-1", "an-earlier-lease")
    with pytest.raises(NotHeld, match="another lease"):
        store.enter("phone-1", "an-earlier-lease", 201)
    with pytest.raises(NotHeld, match="another lease"):
        store.leave("phone-1", "an-earlier-lease", 201)
    with pytest.raises(NotHeld, match="no lease"):
        store.check("phone-2", lease.lease_id)


def test_check_touches_the_lease_while_it_is_held(store, system):
    lease = entered(store, system)
    for _ in range(3):
        system.advance(15 * 60)
        assert store.check("phone-1", lease.lease_id).touched == system.now
    assert reaped(store) == []


def test_check_fails_once_the_lease_is_void_or_draining(store, system):
    lease = entered(store, system)
    system.advance(20 * 60)
    with pytest.raises(NotHeld, match="void"):
        store.check("phone-1", lease.lease_id)
    store.reap()
    with pytest.raises(NotHeld, match="draining"):
        store.check("phone-1", lease.lease_id)
    with pytest.raises(NotHeld, match="draining"):
        store.enter("phone-1", lease.lease_id, 201)


def test_enter_refuses_a_group_that_the_script_did_not_create(store, system):
    lease = entered(store, system)
    with pytest.raises(Refused, match="process group 200 has process 200 in it"):
        store.enter("phone-1", lease.lease_id, 201, 200)
    assert users(store) == [201]


def test_enter_forgets_the_scripts_that_have_ended(store, system):
    lease = entered(store, system)
    system.spawn(202, parent=200, group=200)
    end_the_script(system)
    store.enter("phone-1", lease.lease_id, 202)
    assert users(store) == [202]


def test_a_script_leaves_after_its_work_has_ended(store, system):
    lease = entered(store, system)
    # The script runs banksman itself.
    system.parents[os.getpid()] = 201
    with pytest.raises(BanksmanError, match="process 300 of the script still runs"):
        store.leave("phone-1", lease.lease_id, 201)
    system.end(300, 301)
    store.leave("phone-1", lease.lease_id, 201)
    assert users(store) == []


def test_a_second_enter_keeps_the_group_that_still_runs(store, system):
    lease = entered(store, system)
    store.enter("phone-1", lease.lease_id, 201)
    # The agent ends the command of the script, but the work group of the script still runs.
    system.end(200, 201)
    system.advance(20 * 60)
    assert [item.outcome for item in store.reap()] == [DRAINING]
    system.end(300, 301)
    assert reaped(store) == [("phone-1", IDLE, RELEASED)]


def test_leave_of_a_script_that_did_not_enter_changes_nothing(store, system):
    lease = entered(store, system)
    store.leave("phone-1", lease.lease_id, 4242)
    assert users(store) == [201]


def test_a_void_lease_waits_for_its_scripts_and_sends_no_signal(store, system):
    entered(store, system)
    system.advance(20 * 60)
    (item,) = store.reap()
    assert (item.outcome, [user.pid for user in item.running]) == (DRAINING, [201])
    assert store.reap() == []
    end_the_script(system)
    assert reaped(store) == [("phone-1", IDLE, RELEASED)]
    assert system.signals == []


def test_scripts_that_still_run_at_the_drain_deadline_quarantine_the_lease(
    store, system, state_dir
):
    entered(store, system)
    system.advance(20 * 60)
    store.reap()
    system.advance(5 * 60 - 1)
    assert store.reap() == []
    system.advance(1)
    (item,) = store.reap()
    assert (item.outcome, item.lease.state) == (QUARANTINED, QUARANTINED)
    assert [user.pid for user in item.running] == [201]
    assert system.signals == []
    with pytest.raises(Busy, match="quarantined"):
        store.acquire("phone-1", "device", Holder(OTHER))
    # The quarantine stays when the scripts end later: a person decides.
    end_the_script(system)
    assert store.reap() == []
    assert lease_file(state_dir, "phone-1")["state"] == QUARANTINED


def test_the_drain_timeout_is_kept_in_the_lease(state_dir, system):
    store = Store(state_dir, system)
    for pid, (ppid, pgid) in AGENT_APP.items():
        system.spawn(pid, parent=ppid, group=pgid)
    lease = store.acquire("phone-1", "device", Holder(OWNER), timeouts=Timeouts(drain_timeout=60))
    store.enter("phone-1", lease.lease_id, 201)
    system.advance(20 * 60)
    store.reap()
    system.advance(60)
    assert reaped(store) == [("phone-1", IDLE, QUARANTINED)]


def test_the_instance_is_ended_once_and_before_its_scripts_end(state_dir, system):
    ended = []

    def take_back(lease):
        ended.append([user.pid for user in lease.users])
        return True

    store = Store(state_dir, system, take_back=take_back)
    entered(store, system)
    system.advance(20 * 60)
    store.reap()
    store.reap()
    end_the_script(system)
    assert reaped(store) == [("phone-1", IDLE, RELEASED)]
    assert ended == [[201]]


def test_a_release_while_scripts_run_drains_without_ending_the_instance(state_dir, system):
    ended = []
    store = Store(state_dir, system, take_back=lambda lease: ended.append(lease) or True)
    entered(store, system)
    drained = store.release("phone-1", OWNER)
    assert (drained.state, drained.void_reason) == (DRAINING, RELEASE)
    with pytest.raises(Busy, match="draining"):
        store.acquire("phone-1", "device", Holder(OTHER))
    (item,) = store.reap()
    assert item.outcome == DRAINING
    end_the_script(system)
    assert reaped(store) == [("phone-1", RELEASE, RELEASED)]
    assert ended == []


def test_a_release_after_the_scripts_have_ended_frees_the_resource_at_once(
    store, system, state_dir
):
    entered(store, system)
    end_the_script(system)
    assert store.release("phone-1", OWNER) is None
    assert not (state_dir / "phone-1.json").exists()


def stopping(state_dir, system, **options):
    return Store(state_dir, system, stopping=lambda lease: True, **options)


def test_with_stopping_on_the_reaper_stops_the_scripts_of_the_owner(state_dir, system):
    store = stopping(state_dir, system)
    entered(store, system)
    system.advance(20 * 60)
    assert reaped(store) == [("phone-1", IDLE, RELEASED)]
    assert system.signals == [("pid", 201, signal.SIGTERM), ("group", 300, signal.SIGTERM)]


def test_with_stopping_on_nothing_is_signalled_without_an_owner_process(state_dir, system):
    store = stopping(state_dir, system)
    entered(store, system, owner_pid=None)
    system.advance(20 * 60)
    assert [item.outcome for item in store.reap()] == [DRAINING]
    assert system.signals == []


def test_with_stopping_on_a_script_that_left_meanwhile_is_not_stopped(state_dir, system):
    def take_back(lease):
        # While the reaper ends the instance, script 202 leaves the lease. It keeps running.
        system.parents[os.getpid()] = 202
        Store(state_dir, system).leave("phone-1", lease.lease_id, 202)
        del system.parents[os.getpid()]
        return True

    store = stopping(state_dir, system, take_back=take_back)
    lease = entered(store, system)
    system.spawn(202, parent=200, group=200)
    store.enter("phone-1", lease.lease_id, 202)
    system.advance(20 * 60)
    assert reaped(store) == [("phone-1", IDLE, RELEASED)]
    assert [target for _, target, _ in system.signals] == [201, 300]
    assert 202 in system.processes


def test_a_take_back_that_never_finishes_is_quarantined(state_dir, system):
    # Every reaper ends in the middle of the take-back, for example because its caller stops
    # it after a timeout.
    attempts = []

    def take_back(lease):
        attempts.append(lease.attempts)
        raise KeyboardInterrupt

    store = Store(state_dir, system, take_back=take_back)
    store.acquire("phone-1", "device", Holder(OWNER))
    system.advance(20 * 60)
    for _ in range(3):
        with pytest.raises(KeyboardInterrupt):
            store.reap()
        edit_lease(state_dir, "phone-1", lambda data: data.update(reaper_pid=4242))
    (item,) = store.reap()
    assert (item.outcome, item.problem) == (QUARANTINED, "3 take-backs started and none finished")
    assert attempts == [1, 2, 3]


def test_force_release_waits_for_a_take_back_that_runs(state_dir, system):
    def take_back(lease):
        with pytest.raises(BanksmanError, match=f"process {os.getpid()} takes phone-1 back now"):
            Store(state_dir, system).force_release("phone-1")
        return True

    store = Store(state_dir, system, take_back=take_back)
    store.acquire("phone-1", "device", Holder(OWNER))
    system.advance(20 * 60)
    assert reaped(store) == [("phone-1", IDLE, RELEASED)]


def test_a_restart_forgets_the_scripts_of_the_earlier_boot(state_dir, system):
    store = stopping(state_dir, system)
    entered(store, system)
    # The pids of the earlier boot can name new processes now.
    system.boot = "boot-2"
    assert reaped(store) == [("phone-1", REBOOTED, RELEASED)]
    assert system.signals == []


def test_a_draining_lease_of_an_earlier_boot_is_finished_on_this_boot(store, system):
    entered(store, system)
    system.advance(20 * 60)
    store.reap()
    system.boot = "boot-2"
    assert reaped(store) == [("phone-1", IDLE, RELEASED)]


# Quarantines last until a person releases them, also when /tmp is cleaned or emptied.


def failing(state_dir, system, results=()):
    results = list(results)
    return Store(state_dir, system, take_back=lambda lease: results.pop(0) if results else False)


def quarantine(store, system):
    store.acquire("phone-1", "device", Holder(OWNER))
    system.advance(20 * 60)
    assert reaped(store) == [("phone-1", IDLE, QUARANTINED)]


def test_a_quarantine_is_also_kept_outside_the_state_directory(state_dir, quarantine_dir, system):
    quarantine(failing(state_dir, system), system)
    record = quarantine_dir / "phone-1.json"
    assert record.read_text() == (state_dir / "phone-1.json").read_text()
    assert stat.S_IMODE(quarantine_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(record.stat().st_mode) == 0o600


def test_without_a_quarantine_nothing_is_written_outside_the_state_directory(
    store, system, tmp_path
):
    store.acquire("phone-1", "device", Holder(OWNER))
    system.advance(20 * 60)
    store.reap()
    assert not (tmp_path / "home").exists()


def test_a_quarantine_that_a_cleaner_of_temporary_files_deleted_comes_back(state_dir, system):
    store = failing(state_dir, system)
    quarantine(store, system)
    (state_dir / "phone-1.json").unlink()
    with pytest.raises(Busy, match="quarantined"):
        store.acquire("phone-1", "device", Holder(OTHER))
    assert [lease.state for lease in store.snapshot().leases] == [QUARANTINED]


def test_a_quarantine_that_a_cleaner_deletes_during_a_command_is_not_handed_on(
    state_dir, system, monkeypatch
):
    store = failing(state_dir, system)
    quarantine(store, system)
    # The cleaner deletes the file after the command has restored the missing ones.
    monkeypatch.setattr(store, "_restore_quarantines", lambda: None)
    (state_dir / "phone-1.json").unlink()
    with pytest.raises(Busy, match="quarantined"):
        store.acquire("phone-1", "device", Holder(OTHER))


def test_a_quarantine_survives_restarts_that_empty_tmp(state_dir, quarantine_dir, system):
    store = failing(state_dir, system, results=[False, False, True])
    quarantine(store, system)
    shutil.rmtree(state_dir)
    system.boot = "boot-2"
    # The take-back runs again, because an instance can outlive a restart. It fails again.
    assert reaped(store) == [("phone-1", REBOOTED, QUARANTINED)]
    assert json.loads((quarantine_dir / "phone-1.json").read_text())["boot_id"] == "boot-2"
    shutil.rmtree(state_dir)
    system.boot = "boot-3"
    assert reaped(store) == [("phone-1", REBOOTED, RELEASED)]
    assert not (quarantine_dir / "phone-1.json").exists()
    assert store.acquire("phone-1", "device", Holder(OTHER)).owner == OTHER


def test_a_quarantine_that_cannot_be_kept_outside_tmp_keeps_the_lease_draining(
    state_dir, quarantine_dir, system
):
    quarantine_dir.parent.mkdir(parents=True, mode=0o500)
    try:
        store = failing(state_dir, system)
        store.acquire("phone-1", "device", Holder(OWNER))
        system.advance(20 * 60)
        with pytest.raises(StoreError, match="outside the sandbox"):
            store.reap()
        assert lease_file(state_dir, "phone-1")["state"] == DRAINING
        with pytest.raises(Busy, match="draining"):
            store.acquire("phone-1", "device", Holder(OTHER))
    finally:
        quarantine_dir.parent.chmod(0o700)


def test_force_release_removes_a_quarantine_and_its_record(state_dir, quarantine_dir, system):
    store = failing(state_dir, system)
    quarantine(store, system)
    removed, running = store.force_release("phone-1")
    assert (removed.state, running) == (QUARANTINED, ())
    assert not (state_dir / "phone-1.json").exists()
    assert not (quarantine_dir / "phone-1.json").exists()
    assert store.acquire("phone-1", "device", Holder(OTHER)).owner == OTHER


def test_force_release_takes_a_held_lease_and_names_the_scripts_that_still_run(store, system):
    entered(store, system)
    removed, running = store.force_release("phone-1")
    assert removed.state == READY
    assert [user.pid for user in running] == [201]
    assert system.signals == []
    assert store.snapshot().leases == []


def test_force_release_removes_a_lease_file_that_cannot_be_read(store, state_dir):
    store.snapshot()
    (state_dir / "phone-1.json").write_text("{not json")
    removed, _ = store.force_release("phone-1")
    assert isinstance(removed, Unreadable)
    assert store.snapshot().unreadable == []


def test_force_release_of_a_resource_without_a_lease_is_an_error(store):
    with pytest.raises(BanksmanError, match="no lease"):
        store.force_release("phone-1")


def test_a_resource_name_that_is_not_valid_is_refused_by_every_call(store):
    calls = [
        lambda: store.force_release("../phone-1"),
        lambda: store.check("../phone-1", "lease-1"),
        lambda: store.touch("../phone-1", OWNER),
        lambda: store.release("../phone-1", OWNER),
    ]
    for call in calls:
        with pytest.raises(BanksmanError, match="resource name"):
            call()


def test_a_quarantine_directory_that_others_can_write_to_is_refused(
    state_dir, quarantine_dir, system
):
    quarantine_dir.mkdir(parents=True)
    quarantine_dir.chmod(0o777)
    with pytest.raises(StoreError, match="can write"):
        Store(state_dir, system).snapshot()


def test_a_symbolic_link_as_quarantine_directory_is_refused(
    state_dir, quarantine_dir, system, tmp_path
):
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    quarantine_dir.parent.mkdir(parents=True)
    quarantine_dir.symlink_to(target)
    with pytest.raises(StoreError, match="not a directory"):
        Store(state_dir, system).snapshot()


def test_a_state_directory_for_tests_keeps_its_own_quarantines(monkeypatch, tmp_path):
    monkeypatch.delenv("BANKSMAN_QUARANTINE_DIR")
    monkeypatch.setenv("BANKSMAN_STATE_DIR", str(tmp_path / "state"))
    assert default_quarantine_dir() == tmp_path / "state-quarantine"


def test_the_quarantine_directory_does_not_depend_on_the_environment(monkeypatch):
    monkeypatch.delenv("BANKSMAN_QUARANTINE_DIR")
    monkeypatch.delenv("BANKSMAN_STATE_DIR")
    monkeypatch.setenv("HOME", "/somewhere/else")
    monkeypatch.setenv("XDG_STATE_HOME", "/somewhere/else/state")
    home = pwd.getpwuid(os.geteuid()).pw_dir
    assert default_quarantine_dir() == Path(home) / ".local" / "state" / "banksman" / "quarantine"
