import fcntl
import json
import os
import stat
import subprocess
import sys
import time

import pytest
from helpers import SRC, edit_lease

from banksman import SCHEMA_VERSION
from banksman.errors import BanksmanError
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
)
from banksman.store import (
    RELEASED,
    Busy,
    LockTimeout,
    NotHeld,
    Store,
    StoreError,
    Unreadable,
    default_state_dir,
)

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
    store.acquire("phone-1", "device", OWNER)
    (state_dir / "phone-1.json").chmod(0o666)
    with pytest.raises(StoreError, match="can write"):
        store.snapshot()


def test_reserve_writes_a_booting_lease_before_the_start(store, system, state_dir):
    lease = store.reserve("emu-1", "emulator", OWNER)
    assert lease.state == BOOTING
    data = lease_file(state_dir, "emu-1")
    assert data["schema"] == SCHEMA_VERSION
    assert data["state"] == BOOTING
    assert data["awake"]["boot_deadline"] == system.now + 5 * 60
    assert stat.S_IMODE((state_dir / "emu-1.json").stat().st_mode) == 0o600


def test_ready_ends_the_boot(store, system):
    store.reserve("emu-1", "emulator", OWNER)
    system.advance(60)
    lease = store.ready("emu-1", OWNER)
    assert (lease.state, lease.boot_deadline, lease.touched) == (READY, None, system.now)
    assert store.ready("emu-1", OWNER) == lease


def test_acquire_writes_a_ready_lease(store):
    assert store.acquire("phone-1", "device", OWNER).state == READY


def test_a_resource_has_one_holder(store):
    store.acquire("phone-1", "device", OWNER)
    with pytest.raises(Busy, match="held by"):
        store.acquire("phone-1", "device", OTHER)
    with pytest.raises(Busy):
        store.reserve("phone-1", "device", OTHER)


def test_the_same_owner_gets_its_own_lease_again(store, system):
    first = store.acquire("phone-1", "device", OWNER)
    system.advance(60)
    again = store.acquire("phone-1", "device", OWNER)
    assert again.lease_id == first.lease_id
    assert again.touched == system.now


def test_only_the_owner_can_use_the_lease(store):
    store.reserve("emu-1", "emulator", OWNER)
    for call in (store.touch, store.ready, store.release):
        with pytest.raises(NotHeld, match="another owner|not held"):
            call("emu-1", OTHER)


def test_touch_keeps_the_lease_valid(store, system):
    store.acquire("phone-1", "device", OWNER)
    for _ in range(3):
        system.advance(15 * 60)
        store.touch("phone-1", OWNER)
    assert reaped(store) == []


def test_touch_records_the_new_process_of_a_restarted_owner(store, system):
    system.processes = {100: "start-100", 200: "start-200"}
    store.acquire("phone-1", "device", OWNER, owner_pid=100)
    del system.processes[100]
    lease = store.touch("phone-1", OWNER, owner_pid=200)
    assert (lease.owner_pid, lease.owner_started) == (200, "start-200")
    system.advance(10 * 60)
    assert reaped(store) == []


def test_the_owner_process_must_run(store):
    with pytest.raises(BanksmanError, match="not running"):
        store.acquire("phone-1", "device", OWNER, owner_pid=4242)


def test_a_touch_does_not_revive_a_void_lease(store, system):
    store.acquire("phone-1", "device", OWNER)
    system.advance(20 * 60)
    with pytest.raises(NotHeld, match="void"):
        store.touch("phone-1", OWNER)
    with pytest.raises(Busy):
        store.acquire("phone-1", "device", OWNER)


def test_release_frees_the_resource(store, state_dir):
    store.acquire("phone-1", "device", OWNER)
    store.release("phone-1", OWNER)
    assert not (state_dir / "phone-1.json").exists()
    assert store.acquire("phone-1", "device", OTHER).owner == OTHER


def test_names_that_are_not_valid_are_refused(store):
    with pytest.raises(BanksmanError, match="resource name"):
        store.acquire("../phone-1", "device", OWNER)


def test_reap_keeps_valid_leases(store, system):
    store.reserve("emu-1", "emulator", OWNER)
    store.acquire("phone-1", "device", OTHER)
    system.advance(5 * 60 - 1)
    assert reaped(store) == []
    assert resources(store) == ["emu-1", "phone-1"]


def test_a_boot_that_does_not_end_is_reaped(store, system):
    store.reserve("emu-1", "emulator", OWNER)
    system.advance(5 * 60)
    assert reaped(store) == [("emu-1", BOOT_TIMEOUT, RELEASED)]
    assert resources(store) == []


def test_a_dead_owner_frees_its_lease_after_the_grace_period(store, system):
    system.processes = {100: "start-100"}
    store.acquire("phone-1", "device", OWNER, owner_pid=100)
    system.advance(4 * 60)
    del system.processes[100]
    assert reaped(store) == []
    system.advance(60)
    assert reaped(store) == [("phone-1", OWNER_GONE, RELEASED)]


def test_an_idle_lease_is_reaped(store, system):
    system.processes = {100: "start-100"}
    store.acquire("phone-1", "device", OWNER, owner_pid=100)
    system.advance(20 * 60)
    assert reaped(store) == [("phone-1", IDLE, RELEASED)]


def test_the_hard_cap_is_enforced_although_the_holder_touches(store, system):
    store.acquire("phone-1", "device", OWNER)
    for _ in range(17):
        system.advance(10 * 60)
        store.touch("phone-1", OWNER)
    assert reaped(store) == []
    system.advance(10 * 60)
    assert reaped(store) == [("phone-1", HARD_CAP, RELEASED)]


def test_a_restart_voids_every_lease(store, system):
    store.acquire("phone-1", "device", OWNER)
    system.boot = "boot-2"
    assert reaped(store) == [("phone-1", REBOOTED, RELEASED)]


def test_a_reaped_resource_can_be_leased_again(store, system):
    store.acquire("phone-1", "device", OWNER)
    system.advance(20 * 60)
    store.reap()
    assert store.acquire("phone-1", "device", OTHER).owner == OTHER


def test_a_resource_that_cannot_be_taken_back_is_quarantined(state_dir, system):
    freed = False
    store = Store(state_dir, system, take_back=lambda lease: freed)
    store.acquire("phone-1", "device", OWNER)
    system.advance(20 * 60)
    assert reaped(store) == [("phone-1", IDLE, QUARANTINED)]
    system.advance(24 * 60 * 60)
    assert reaped(store) == []
    assert lease_file(state_dir, "phone-1")["state"] == QUARANTINED
    with pytest.raises(Busy, match="quarantined"):
        store.acquire("phone-1", "device", OTHER)
    # After a restart, no process of the earlier boot can still use the resource.
    freed = True
    system.boot = "boot-2"
    assert reaped(store) == [("phone-1", REBOOTED, RELEASED)]


def test_the_owner_check_fails_while_the_lease_drains(state_dir, system):
    checks = []

    def take_back(lease):
        with pytest.raises(NotHeld, match="draining"):
            Store(state_dir, system).touch("phone-1", OWNER)
        checks.append(lease.state)
        return True

    store = Store(state_dir, system, take_back=take_back)
    store.acquire("phone-1", "device", OWNER)
    system.advance(20 * 60)
    assert reaped(store) == [("phone-1", IDLE, RELEASED)]
    assert checks == [DRAINING]


def test_a_draining_lease_left_by_a_dead_reaper_is_finished(store, system, state_dir):
    store.acquire("phone-1", "device", OWNER)
    edit_lease(state_dir, "phone-1", lambda data: data.update(state=DRAINING, void_reason=IDLE))
    assert reaped(store) == [("phone-1", IDLE, RELEASED)]


def test_the_reaper_does_not_delete_a_newer_lease(state_dir, system):
    def take_back(lease):
        # Meanwhile another reaper finished this lease, another owner leased the resource,
        # and a third reaper is taking that newer lease back.
        (state_dir / "phone-1.json").unlink()
        Store(state_dir, system).acquire("phone-1", "device", OTHER)
        edit_lease(state_dir, "phone-1", lambda data: data.update(state=DRAINING, void_reason=IDLE))
        return True

    store = Store(state_dir, system, take_back=take_back)
    store.acquire("phone-1", "device", OWNER)
    system.advance(20 * 60)
    assert reaped(store) == []
    assert lease_file(state_dir, "phone-1")["owner"] == OTHER


def test_an_unreadable_lease_file_keeps_only_its_resource_out_of_use(store, state_dir):
    store.acquire("phone-2", "device", OWNER)
    (state_dir / "phone-1.json").write_text("{not json")
    snapshot = store.snapshot()
    assert snapshot.unreadable == [Unreadable("phone-1", "the file is not valid JSON")]
    assert [lease.resource for lease in snapshot.leases] == ["phone-2"]
    assert reaped(store) == []
    with pytest.raises(Busy, match="cannot be read"):
        store.acquire("phone-1", "device", OTHER)


def test_a_lease_file_from_a_newer_banksman_stops_every_command(store, state_dir):
    store.acquire("phone-1", "device", OWNER)
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
from banksman.store import Busy, Store
from banksman.system import Machine
state_dir, go, owner = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
while not go.exists():
    time.sleep(0.001)
try:
    Store(state_dir, Machine()).acquire("phone-1", "device", owner)
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

