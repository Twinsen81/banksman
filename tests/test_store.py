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
from dataclasses import replace
from pathlib import Path

import pytest
from helpers import SRC, age_lease, drain, edit_lease, reap_in_child

from banksman import LEASE_SCHEMA
from banksman.errors import BanksmanError
from banksman.fencing import Refused
from banksman.history import ACQUIRE, QUARANTINE, REAP
from banksman.history import RELEASE as RELEASE_EVENT
from banksman.lease import (
    ACQUIRE_GONE,
    BOOT_TIMEOUT,
    BOOTING,
    DRAINING,
    FREE_TIMEOUT,
    HARD_CAP,
    IDLE,
    OWNER_GONE,
    QUARANTINED,
    READY,
    REBOOTED,
    RELEASE,
    RELEASED_END,
    Holder,
    SerialSeen,
    Timeouts,
)
from banksman.store import (
    RELEASED,
    Busy,
    Choice,
    LastUse,
    LockTimeout,
    Need,
    NotHeld,
    Outdated,
    Store,
    Snapshot,
    StoreError,
    Unreadable,
    Unused,
    default_ended_path,
    default_quarantine_dir,
    default_state_dir,
    peek,
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


def grant_one(store, choices, holder, **options):
    """Grant a request with one part, and return what that part got."""
    granted = store.grant([Need(tuple(choices))], holder, **options)
    return None if granted is None else granted[0]


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
    assert data["schema"] == LEASE_SCHEMA
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


def test_ready_ends_the_boot(store, system):
    reserved = store.reserve("emu-1", "emulator", Holder(OWNER))
    system.advance(60)
    lease = store.ready("emu-1", reserved.lease_id)
    assert (lease.state, lease.boot_deadline, lease.touched) == (READY, None, system.now)
    assert store.ready("emu-1", reserved.lease_id) == lease


def test_ready_needs_the_id_of_the_current_lease(store):
    # A script of an earlier boot of the same worktree must not mark a newer lease ready.
    earlier = store.reserve("emu-1", "emulator", Holder(OWNER))
    store.release("emu-1", earlier.lease_id)
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


def test_a_held_resource_is_busy_also_for_the_same_owner(store):
    # Several agents can work in one worktree, so the owner does not tell their holdings apart.
    store.acquire("phone-1", "device", Holder(OWNER))
    with pytest.raises(Busy, match="held by"):
        store.acquire("phone-1", "device", Holder(OWNER))


def test_only_the_lease_id_can_use_the_lease(store):
    store.reserve("emu-1", "emulator", Holder(OWNER))
    for call in (store.touch, store.release):
        with pytest.raises(NotHeld, match="another lease"):
            call("emu-1", "an-earlier-lease")


def test_touch_keeps_the_lease_valid(store, system):
    lease = store.acquire("phone-1", "device", Holder(OWNER))
    for _ in range(3):
        system.advance(15 * 60)
        store.touch("phone-1", lease.lease_id)
    assert reaped(store) == []


def test_touch_records_the_new_process_of_a_restarted_owner(store, system):
    system.processes = {100: "start-100", 200: "start-200"}
    first = store.acquire("phone-1", "device", Holder(OWNER, owner_pid=100))
    del system.processes[100]
    lease = store.touch("phone-1", first.lease_id, owner_pid=200)
    assert (lease.owner_pid, lease.owner_started) == (200, "start-200")
    system.advance(10 * 60)
    assert reaped(store) == []


def test_a_lease_keeps_an_owner_process_that_started_the_caller(store, system):
    # banksman run (100) took the lease for its command, and a script that it runs keeps the
    # lease; the agent of the script (200) runs longer than banksman run.
    system.processes = {100: "start-100", 200: "start-200"}
    system.parents[100] = 200
    system.parents[os.getpid()] = 100
    first = store.acquire("phone-1", "device", Holder(OWNER, owner_pid=100))
    lease = store.touch("phone-1", first.lease_id, owner_pid=200)
    assert (lease.owner_pid, lease.owner_started) == (100, "start-100")
    kept = grant_one(
        store, [Choice("phone-1", "device")], Holder(OWNER, owner_pid=200), keep=first.lease_id
    )
    assert (kept.kept, kept.lease.owner_pid) == (True, 100)
    # When that owner has ended, the caller's agent becomes the owner.
    system.end(100)
    lease = store.touch("phone-1", first.lease_id, owner_pid=200)
    assert lease.owner_pid == 200


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
    lease_id = store.acquire("phone-1", "device", Holder(OWNER)).lease_id
    system.advance(60)
    assert store.touch("phone-1", lease_id).expected is None
    assert store.touch("phone-1", lease_id, expect=5 * 60).expected == system.now + 5 * 60


def test_grant_takes_the_first_free_choice(store):
    store.acquire("emu-1", "emulator", Holder(OTHER))
    choices = [
        Choice("emu-1", "emulator"),
        Choice("emu-2", "emulator"),
        Choice("phone-1", "device"),
    ]
    granted = grant_one(store, choices, Holder(OWNER))
    assert (granted.lease.resource, granted.lease.owner, granted.kept) == ("emu-2", OWNER, False)
    assert grant_one(store, choices, Holder(OWNER)).lease.resource == "phone-1"
    assert grant_one(store, choices, Holder(OWNER)) is None


def test_grant_gives_each_choice_its_timeouts(store, system):
    timeouts = Timeouts(boot_timeout=8 * 60, hard_cap=60 * 60)
    choices = [Choice("emu-1", "emulator", timeouts)]
    lease = grant_one(store, choices, Holder(OWNER), expect=60).lease
    assert (lease.state, lease.boot_deadline, lease.reaper_pid) == (READY, None, None)
    assert (lease.hard_deadline, lease.expected) == (system.now + 60 * 60, system.now + 60)


def test_a_lease_whose_instance_is_reset_names_the_process_that_resets_it(store, system):
    choice = Choice("emu-1", "emulator", Timeouts(boot_timeout=8 * 60), reset=True)
    lease = grant_one(store, [choice], Holder(OWNER)).lease
    assert (lease.state, lease.boot_deadline) == (BOOTING, system.now + 8 * 60)
    assert (lease.reaper_pid, lease.reaper_started) == (os.getpid(), "start-self")
    ready = store.ready("emu-1", lease.lease_id)
    assert (ready.state, ready.boot_deadline, ready.reaper_pid, ready.reaper_started) == (
        READY,
        None,
        None,
        None,
    )


def reset_by_another_process(store, system, state_dir):
    """Grant emu-1 with a reset that process 4242 runs, as another acquire would."""
    system.spawn(4242)
    lease = grant_one(store, [Choice("emu-1", "emulator", reset=True)], Holder(OWNER)).lease
    edit_lease(
        state_dir, "emu-1", lambda data: data.update(reaper_pid=4242, reaper_started="start-4242")
    )
    return lease


def test_a_void_lease_is_taken_back_only_after_its_reset_has_ended(state_dir, system):
    taken = []
    store = Store(state_dir, system, take_back=lambda lease: taken.append(lease) or True)
    lease = reset_by_another_process(store, system, state_dir)
    system.advance(5 * 60)
    # The reset can still act on the instance, so no take-back runs next to it.
    assert reaped(store) == []
    assert lease_file(state_dir, "emu-1")["state"] == DRAINING
    with pytest.raises(NotHeld, match="draining"):
        store.ready("emu-1", lease.lease_id)
    with pytest.raises(Busy):
        store.acquire("emu-1", "emulator", Holder(OTHER))
    assert reaped(store) == []
    system.end(4242)
    assert reaped(store) == [("emu-1", BOOT_TIMEOUT, RELEASED)]
    assert len(taken) == 1


def test_a_booting_lease_stays_valid_while_the_process_that_resets_it_runs(
    store, system, state_dir
):
    system.spawn(100)
    system.spawn(4242)
    holder = Holder(OWNER, owner_pid=100)
    lease = grant_one(store, [Choice("emu-1", "emulator", reset=True)], holder, label="tests").lease
    edit_lease(
        state_dir, "emu-1", lambda data: data.update(reaper_pid=4242, reaper_started="start-4242")
    )
    # Every lookup reads the start time of that process, not only of the owner process.
    assert store.check("emu-1", lease.lease_id).state == BOOTING
    assert list(store.holding(lease.lease_id)) == ["emu-1"]
    assert list(store.labelled(100, "tests")) == ["emu-1"]
    assert [each.resource for each in store.held_by(100)] == ["emu-1"]
    assert reaped(store) == []


def test_a_booting_lease_is_taken_back_as_soon_as_the_process_that_resets_it_has_ended(
    state_dir, system
):
    taken = []
    store = Store(state_dir, system, take_back=lambda lease: taken.append(lease) or True)
    lease = reset_by_another_process(store, system, state_dir)
    system.end(4242)
    # Nothing marks the lease ready any more, so the next command does not wait for the boot
    # deadline.
    with pytest.raises(NotHeld, match="started or reset the instance ended before it was ready"):
        store.check("emu-1", lease.lease_id)
    assert store.holding(lease.lease_id) == {}
    assert reaped(store) == [("emu-1", ACQUIRE_GONE, RELEASED)]
    assert [each.void_reason for each in taken] == [ACQUIRE_GONE]
    assert resources(store) == []


def test_a_pid_that_names_a_new_process_does_not_keep_a_booting_lease(store, system, state_dir):
    reset_by_another_process(store, system, state_dir)
    system.end(4242)
    system.processes[4242] = "start-of-another-process"
    assert reaped(store) == [("emu-1", ACQUIRE_GONE, RELEASED)]


def test_a_release_during_a_reset_by_another_process_waits_for_the_reset(state_dir, system):
    taken = []
    store = Store(state_dir, system, take_back=lambda lease: taken.append(lease) or True)
    lease = reset_by_another_process(store, system, state_dir)
    drained = store.release("emu-1", lease.lease_id)
    assert (drained.state, drained.void_reason, drained.reaper_pid) == (DRAINING, RELEASE, 4242)
    with pytest.raises(Busy):
        store.acquire("emu-1", "emulator", Holder(OTHER))
    system.end(4242)
    assert reaped(store) == [("emu-1", RELEASE, RELEASED)]
    assert taken == []


def test_release_all_during_a_reset_by_another_process_waits_for_the_reset(
    store, system, state_dir
):
    system.spawn(100)
    system.spawn(4242)
    choice = Choice("emu-1", "emulator", reset=True)
    grant_one(store, [choice], Holder(OWNER, owner_pid=100))
    edit_lease(
        state_dir, "emu-1", lambda data: data.update(reaper_pid=4242, reaper_started="start-4242")
    )
    ((_, drained),) = store.release_all(100)
    assert (drained.state, drained.reaper_pid) == (DRAINING, 4242)


def test_the_process_that_resets_an_instance_gives_its_lease_back_at_once(store, state_dir):
    lease = grant_one(store, [Choice("emu-1", "emulator", reset=True)], Holder(OWNER)).lease
    assert store.release("emu-1", lease.lease_id) is None
    assert not (state_dir / "emu-1.json").exists()


def test_a_reset_that_ended_does_not_hold_a_released_lease(store, system, state_dir):
    lease = reset_by_another_process(store, system, state_dir)
    system.end(4242)
    assert store.release("emu-1", lease.lease_id) is None


def test_force_release_waits_for_a_reset_that_runs(store, system, state_dir):
    reset_by_another_process(store, system, state_dir)
    with pytest.raises(BanksmanError, match="process 4242 takes emu-1 back or resets it now"):
        store.force_release("emu-1")


def test_grant_keeps_the_lease_whose_id_the_caller_passes(store, system):
    system.processes = {100: "start-100", 200: "start-200"}
    choices = [Choice("emu-1", "emulator"), Choice("emu-2", "emulator")]
    grant_one(store, choices, Holder(OTHER))
    held = grant_one(store, choices, Holder(OWNER, owner_pid=100)).lease
    assert held.resource == "emu-2"
    system.advance(60)
    # A restarted agent keeps its lease, and the lease records its new process.
    kept = grant_one(store, choices, Holder(OWNER, owner_pid=200), keep=held.lease_id, expect=60)
    assert kept.kept
    assert (kept.lease.lease_id, kept.lease.touched) == (held.lease_id, system.now)
    assert (kept.lease.owner_pid, kept.lease.owner_started) == (200, "start-200")
    assert kept.lease.expected == system.now + 60


def test_grant_does_not_keep_a_lease_that_is_lost_or_that_does_not_match(store, system):
    held = grant_one(store, [Choice("emu-1", "emulator")], Holder(OWNER)).lease
    # The request no longer matches the resource of the lease: the caller gets another one.
    other = grant_one(store, [Choice("emu-2", "emulator")], Holder(OWNER), keep=held.lease_id)
    assert (other.lease.resource, other.kept) == ("emu-2", False)
    system.advance(20 * 60)
    choices = [Choice("emu-1", "emulator"), Choice("emu-3", "emulator")]
    again = grant_one(store, choices, Holder(OWNER), keep=held.lease_id)
    assert (again.lease.resource, again.kept) == ("emu-3", False)


def test_release_all_gives_back_the_leases_of_one_agent_process(store, system):
    system.processes = {100: "start-100", 200: "start-200"}
    store.acquire("phone-1", "device", Holder(OWNER, owner_pid=100))
    store.acquire("emu-1", "emulator", Holder(OWNER, owner_pid=100))
    store.acquire("emu-2", "emulator", Holder(OWNER, owner_pid=200))
    store.acquire("emu-3", "emulator", Holder(OWNER))
    released = store.release_all(100)
    assert sorted((lease.resource, after) for lease, after in released) == [
        ("emu-1", None),
        ("phone-1", None),
    ]
    assert [lease.resource for lease in store.snapshot().leases] == ["emu-2", "emu-3"]


def test_release_all_does_not_take_the_lease_of_an_earlier_process_with_the_same_pid(
    store, system
):
    system.processes = {100: "start-100"}
    store.acquire("phone-1", "device", Holder(OWNER, owner_pid=100))
    system.processes = {100: "start-100-again"}
    assert store.release_all(100) == []


def test_release_all_drains_a_lease_whose_scripts_still_run(store, system):
    entered(store, system)
    ((lease, drained),) = store.release_all(101)
    assert lease.resource == "phone-1"
    assert (drained.state, drained.void_reason) == (DRAINING, RELEASE)


def test_a_holder_that_is_not_valid_gets_no_lease(store, state_dir):
    with pytest.raises(BanksmanError, match="purpose"):
        store.acquire("phone-1", "device", Holder(OWNER, purpose="verify\x1b[2J"))
    assert not (state_dir / "phone-1.json").exists()


def test_the_owner_process_must_run(store):
    with pytest.raises(BanksmanError, match="not running"):
        store.acquire("phone-1", "device", Holder(OWNER, owner_pid=4242))


def test_a_touch_does_not_revive_a_void_lease(store, system):
    lease = store.acquire("phone-1", "device", Holder(OWNER))
    system.advance(20 * 60)
    with pytest.raises(NotHeld, match="void"):
        store.touch("phone-1", lease.lease_id)
    with pytest.raises(Busy):
        store.acquire("phone-1", "device", Holder(OWNER))


def test_release_frees_the_resource(store, state_dir):
    lease = store.acquire("phone-1", "device", Holder(OWNER))
    assert store.release("phone-1", lease.lease_id) is None
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
    lease = store.acquire("phone-1", "device", Holder(OWNER))
    for _ in range(17):
        system.advance(10 * 60)
        store.touch("phone-1", lease.lease_id)
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
            Store(state_dir, system).touch("phone-1", lease.lease_id)
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
    edit_lease(state_dir, "phone-1", lambda data: data.update(schema=LEASE_SCHEMA + 1))
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



# Joint grants: several parts at once, all or nothing, and the accounts on a resource.

QA = "qa@example.test"
QB = "qb@example.test"


def phone(name, *accounts):
    return Choice(name, "device", accounts=accounts)


def phone_and_tablet():
    return [Need((Choice("phone-1", "device"),)), Need((Choice("tab-1", "device"),))]


def test_a_joint_grant_gives_every_part_or_nothing(store):
    store.acquire("tab-1", "device", Holder(OTHER))
    assert store.grant(phone_and_tablet(), Holder(OWNER)) is None
    # The free part is not held while the other part is in use.
    assert resources(store) == ["tab-1"]


def test_the_leases_of_a_joint_grant_form_one_holding(store):
    phone_1, tablet = store.grant(phone_and_tablet(), Holder(OWNER))
    assert (phone_1.lease.resource, tablet.lease.resource) == ("phone-1", "tab-1")
    # Each lease has its own token, so a script of one lease never acts on another.
    assert phone_1.lease.lease_id != tablet.lease.lease_id
    assert phone_1.lease.holding == tablet.lease.holding
    other = grant_one(store, [Choice("emu-1", "emulator")], Holder(OWNER))
    assert other.lease.holding != phone_1.lease.holding


def test_parts_that_match_the_same_resources_get_different_ones(store):
    choices = (Choice("phone-1", "device"), Choice("phone-2", "device"))
    first, second = store.grant([Need(choices), Need(choices[:1])], Holder(OWNER))
    assert (first.lease.resource, second.lease.resource) == ("phone-2", "phone-1")


def test_a_part_gets_accounts_on_its_resource_and_its_lease_lists_them(store, state_dir):
    (granted,) = store.grant([Need((phone("phone-1", QA, QB),), accounts=1)], Holder(OWNER))
    assert (granted.lease.resource, granted.accounts) == ("phone-1", (QA,))
    assert lease_file(state_dir, "phone-1")["accounts"] == [QA]


def test_an_account_is_in_use_while_any_lease_lists_it(store, system):
    # The same account is signed in on two phones, so two runs must not use it at the same time.
    need = Need((phone("phone-2", QA),), accounts=1)
    lease = store.grant([Need((phone("phone-1", QA),), accounts=1)], Holder(OTHER))[0].lease
    assert store.grant([need], Holder(OWNER)) is None
    # The scripts of a lease that drains can still use the account.
    system.spawn(4242)
    store.enter("phone-1", lease.lease_id, 4242)
    assert store.release("phone-1", lease.lease_id).state == DRAINING
    assert store.grant([need], Holder(OWNER)) is None
    system.end(4242)
    assert reaped(store) == [("phone-1", RELEASE, RELEASED)]
    (granted,) = store.grant([need], Holder(OWNER))
    assert (granted.lease.resource, granted.accounts) == ("phone-2", (QA,))


def test_a_part_waits_until_enough_accounts_on_its_resource_are_free(store):
    store.grant([Need((phone("phone-1", QA),), accounts=1)], Holder(OTHER))
    need = Need((phone("phone-2", QA, QB),), accounts=2)
    assert store.grant([need], Holder(OWNER)) is None
    (granted,) = store.grant([replace(need, accounts=1)], Holder(OWNER))
    assert granted.accounts == (QB,)


def test_a_kept_lease_can_get_more_accounts_on_its_resource(store):
    choices = (phone("phone-1", QA, QB),)
    (first,) = store.grant([Need(choices)], Holder(OWNER))
    assert first.lease.accounts == ()
    keep = first.lease.lease_id
    (kept,) = store.grant([Need(choices, accounts=1)], Holder(OWNER), keep=keep)
    assert (kept.kept, kept.accounts, kept.lease.accounts) == (True, (QA,), (QA,))
    (kept,) = store.grant([Need(choices, accounts=2)], Holder(OWNER), keep=keep)
    assert (kept.accounts, kept.lease.accounts) == ((QA, QB), (QA, QB))
    # Asking for fewer accounts gives none of them back.
    (kept,) = store.grant([Need(choices, accounts=1)], Holder(OWNER), keep=keep)
    assert (kept.accounts, kept.lease.accounts) == ((QA,), (QA, QB))


def test_new_leases_of_a_request_that_keeps_a_lease_join_its_holding(store):
    first = grant_one(store, [Choice("phone-1", "device")], Holder(OWNER))
    keep = first.lease.lease_id
    phone_1, tablet = store.grant(phone_and_tablet(), Holder(OWNER), keep=keep)
    assert (phone_1.kept, tablet.kept) == (True, False)
    assert (phone_1.lease.lease_id, tablet.lease.holding) == (keep, first.lease.holding)


def test_a_resource_that_joins_its_holding_again_gets_a_new_token(store):
    phone_1, tablet = store.grant(phone_and_tablet(), Holder(OWNER))
    old = tablet.lease.lease_id
    assert store.release("tab-1", old) is None
    need = phone_and_tablet()[1]
    (again,) = store.grant([need], Holder(OWNER), keep=phone_1.lease.lease_id)
    assert (again.lease.holding, again.kept) == (phone_1.lease.holding, False)
    assert again.lease.lease_id != old
    # A script or a cleanup of the earlier tablet lease does not act on the new one.
    for call in (store.check, store.touch, store.release):
        with pytest.raises(NotHeld, match="another lease"):
            call("tab-1", old)
    assert resources(store) == ["phone-1", "tab-1"]


def test_an_id_that_names_no_held_lease_starts_no_holding(store):
    (granted,) = store.grant(phone_and_tablet()[:1], Holder(OWNER), keep="$(reboot)")
    assert granted.lease.lease_id != "$(reboot)"


def test_check_touches_every_lease_of_the_holding(store, system):
    phone_1, _ = store.grant(phone_and_tablet(), Holder(OWNER))
    grant_one(store, [Choice("emu-1", "emulator")], Holder(OWNER))
    system.advance(15 * 60)
    store.check("phone-1", phone_1.lease.lease_id)
    system.advance(10 * 60)
    # The tablet was touched with the phone. The lease of another holding was not.
    assert reaped(store) == [("emu-1", IDLE, RELEASED)]
    assert resources(store) == ["phone-1", "tab-1"]


def test_a_touch_of_the_holding_does_not_revive_a_void_lease(store, state_dir):
    phone_1, _ = store.grant(phone_and_tablet(), Holder(OWNER))
    age_lease(state_dir, "tab-1", 21 * 60)
    store.touch("phone-1", phone_1.lease.lease_id)
    assert reaped(store) == [("tab-1", IDLE, RELEASED)]


def test_touch_and_release_by_the_id_of_a_holding(store, system):
    phone_1, _ = store.grant(phone_and_tablet(), Holder(OWNER))
    lease_id = phone_1.lease.lease_id
    system.advance(60)
    touched = store.touch_holding(lease_id, expect=5 * 60)
    assert [(lease.resource, lease.touched, lease.expected) for lease in touched] == [
        ("phone-1", system.now, system.now + 5 * 60),
        ("tab-1", system.now, system.now + 5 * 60),
    ]
    released = store.release_holding(lease_id)
    assert [(lease.resource, drained) for lease, drained in released] == [
        ("phone-1", None),
        ("tab-1", None),
    ]
    assert resources(store) == []
    with pytest.raises(NotHeld, match="has no held lease"):
        store.release_holding(lease_id)
    with pytest.raises(NotHeld, match="has no held lease"):
        store.touch_holding(lease_id)


def test_the_id_of_any_lease_of_a_holding_names_the_holding(store):
    _, tablet = store.grant(phone_and_tablet(), Holder(OWNER))
    released = store.release_holding(tablet.lease.lease_id)
    assert sorted(lease.resource for lease, _ in released) == ["phone-1", "tab-1"]


def test_while_a_request_is_reset_every_new_lease_is_booting_and_names_its_acquire(
    store, system
):
    needs = [
        Need((Choice("emu-1", "emulator", Timeouts(boot_timeout=4 * 60), reset=True),)),
        Need((Choice("build-0", "build"),)),
        Need((Choice("emu-2", "emulator", reset=True),)),
    ]
    granted = store.grant(needs, Holder(OWNER), reset_time=60)
    # The resets run one after another, so each deadline counts the time of both resets.
    assert [
        (grant.lease.state, grant.lease.boot_deadline - system.now, grant.lease.reaper_pid)
        for grant in granted
    ] == [
        (BOOTING, 4 * 60 + 2 * 60, os.getpid()),
        (BOOTING, 5 * 60 + 2 * 60, os.getpid()),
        (BOOTING, 5 * 60 + 2 * 60, os.getpid()),
    ]
    # Without a reset, the leases are ready at once.
    (alone,) = store.grant([Need((Choice("build-1", "build"),))], Holder(OWNER), reset_time=60)
    assert (alone.lease.state, alone.lease.reaper_pid) == (READY, None)


def test_a_lease_of_a_request_that_is_reset_is_not_handed_on_while_its_acquire_runs(
    state_dir,
):
    store = Store(state_dir, Machine())
    needs = [
        Need((Choice("emu-1", "emulator", reset=True),)),
        Need((Choice("build-0", "build"),)),
    ]
    _, build = store.grant(needs, Holder(OWNER))
    age_lease(state_dir, "build-0", 21 * 60)
    # Another command reaps while this acquire still resets emu-1: the idle lease drains, but
    # it is not freed while the acquire that it names runs.
    assert [item["outcome"] for item in reap_in_child(state_dir)] == []
    assert lease_file(state_dir, "build-0")["state"] == DRAINING
    assert grant_one(store, [Choice("build-0", "build")], Holder(OTHER)) is None
    with pytest.raises(NotHeld, match="draining"):
        store.ready("build-0", build.lease.lease_id)


JOINT_GRANT = """
import sys, time
from pathlib import Path
from banksman.lease import Holder
from banksman.store import Choice, Need, Store
from banksman.system import Machine
state_dir, go, owner = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
accounts = ("qa@example.test", "qb@example.test")
phones = tuple(Choice(f"phone-{index}", "device", accounts=accounts) for index in range(4))
while not go.exists():
    time.sleep(0.001)
granted = Store(state_dir, Machine()).grant([Need(phones, 1), Need(phones, 1)], Holder(owner))
if granted is None:
    print("busy")
else:
    print(" ".join(f"{part.lease.resource}:{part.accounts[0]}" for part in granted))
"""


def test_runs_that_ask_at_the_same_time_never_share_a_resource_or_an_account(
    store, state_dir, tmp_path
):
    # Four phones, all signed in to the same two accounts. Each run needs two phones with an
    # account each, so only one run can get them.
    store.snapshot()
    go = tmp_path / "go"
    env = {**os.environ, "PYTHONPATH": SRC}
    children = [
        subprocess.Popen(
            [sys.executable, "-c", JOINT_GRANT, str(state_dir), str(go), f"/work/tree-{index}"],
            stdout=subprocess.PIPE,
            text=True,
            env=env,
        )
        for index in range(8)
    ]
    go.touch()
    results = sorted(child.communicate(timeout=60)[0].strip() for child in children)
    assert results[:7] == ["busy"] * 7
    assert results[7].split() == ["phone-0:qa@example.test", "phone-1:qb@example.test"]


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
    lease = entered(store, system)
    drained = store.release("phone-1", lease.lease_id)
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
    lease = entered(store, system)
    end_the_script(system)
    assert store.release("phone-1", lease.lease_id) is None
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


# A release that ends the instance.


def test_a_release_with_end_takes_the_instance_back_at_once(state_dir, system):
    seen = []

    def take_back(lease):
        seen.append(lease)
        # A request during the end waits, as for an instance in use, and a forced release waits
        # for the end.
        other = Store(state_dir, system)
        assert grant_one(other, [Choice("emu-1", "emulator")], Holder(OTHER)) is None
        with pytest.raises(BanksmanError, match=f"process {os.getpid()} takes emu-1 back"):
            other.force_release("emu-1")
        return True

    events = []
    store = Store(state_dir, system, take_back=take_back, record=events.append)
    lease = store.acquire("emu-1", "emulator", Holder(OWNER))
    system.advance(60)
    assert store.release("emu-1", lease.lease_id, end=True) is None
    (taken,) = seen
    assert (taken.lease_id, taken.state, taken.void_reason) == (
        lease.lease_id,
        DRAINING,
        RELEASED_END,
    )
    assert (taken.reaper_pid, taken.attempts) == (os.getpid(), 1)
    assert resources(store) == []
    assert [(each.event, each.reason, each.end, each.drained) for each in events] == [
        (ACQUIRE, None, None, None),
        (RELEASE_EVENT, None, True, False),
        (REAP, RELEASED_END, None, None),
    ]
    assert events[1].held == 60


def test_a_release_without_end_says_so_in_the_log(state_dir, system):
    events = []
    store = Store(state_dir, system, record=events.append)
    lease = store.acquire("emu-1", "emulator", Holder(OWNER))
    store.release("emu-1", lease.lease_id)
    assert [(each.event, each.end) for each in events] == [(ACQUIRE, None), (RELEASE_EVENT, False)]


def test_a_release_with_end_waits_for_the_scripts_before_the_instance_ends(state_dir, system):
    seen = []
    store = Store(state_dir, system, take_back=lambda lease: seen.append(lease) or True)
    lease = entered(store, system)
    drained = store.release("phone-1", lease.lease_id, end=True)
    assert (drained.state, drained.void_reason) == (DRAINING, RELEASED_END)
    assert ([user.pid for user in drained.users], drained.reaper_pid, drained.attempts) == (
        [201],
        None,
        0,
    )
    with pytest.raises(Busy, match="draining"):
        store.acquire("phone-1", "device", Holder(OTHER))
    # The scripts can still use the instance, so it does not end yet.
    assert store.reap() == []
    assert lease_file(state_dir, "phone-1")["state"] == DRAINING
    assert (seen, system.signals) == ([], [])
    end_the_script(system)
    assert reaped(store) == [("phone-1", RELEASED_END, RELEASED)]
    assert [(each.void_reason, each.users, each.attempts) for each in seen] == [
        (RELEASED_END, (), 1)
    ]


def test_a_release_with_end_whose_scripts_still_run_at_the_drain_deadline_is_quarantined(
    state_dir, system
):
    store = Store(state_dir, system, take_back=lambda lease: pytest.fail("took back"))
    lease = entered(store, system)
    store.release("phone-1", lease.lease_id, end=True)
    system.advance(5 * 60)
    (item,) = store.reap()
    assert (item.outcome, item.lease.void_reason) == (QUARANTINED, RELEASED_END)
    assert [user.pid for user in item.running] == [201]


def test_with_stopping_on_a_release_with_end_stops_the_scripts_before_the_end(state_dir, system):
    seen = []
    store = stopping(
        state_dir, system, take_back=lambda lease: seen.append(list(system.signals)) or True
    )
    lease = entered(store, system)
    assert store.release("phone-1", lease.lease_id, end=True) is None
    assert seen == [[("pid", 201, signal.SIGTERM), ("group", 300, signal.SIGTERM)]]
    assert resources(store) == []


def test_a_release_with_end_whose_scripts_outlive_the_stop_waits_for_them(state_dir, system):
    seen = []
    store = stopping(state_dir, system, take_back=lambda lease: seen.append(lease) or True)
    lease = entered(store, system)
    system.ignores[201] = {signal.SIGTERM, signal.SIGKILL}
    drained = store.release("phone-1", lease.lease_id, end=True)
    assert (drained.state, [user.pid for user in drained.users]) == (DRAINING, [201])
    assert (drained.reaper_pid, drained.attempts, seen) == (None, 0, [])
    signals = len(system.signals)
    assert store.reap() == []
    # The later reapers only wait: the release stopped the scripts once.
    assert len(system.signals) == signals
    system.end(201)
    assert reaped(store) == [("phone-1", RELEASED_END, RELEASED)]
    assert len(seen) == 1


def test_a_release_with_end_whose_instance_does_not_end_is_quarantined(state_dir, system):
    events = []
    store = Store(state_dir, system, take_back=lambda lease: False, record=events.append)
    lease = store.acquire("emu-1", "emulator", Holder(OWNER))
    quarantined = store.release("emu-1", lease.lease_id, end=True)
    assert (quarantined.state, quarantined.void_reason) == (QUARANTINED, RELEASED_END)
    assert lease_file(state_dir, "emu-1")["state"] == QUARANTINED
    assert (events[-1].event, events[-1].problem) == (
        QUARANTINE,
        "its instance could not be taken back",
    )
    with pytest.raises(Busy, match="quarantined"):
        store.acquire("emu-1", "emulator", Holder(OTHER))


def test_a_release_with_end_that_ends_in_the_middle_is_finished_by_the_next_reaper(
    state_dir, system
):
    attempts = []

    def take_back(lease):
        attempts.append(lease.attempts)
        if len(attempts) == 1:
            raise KeyboardInterrupt
        return True

    store = Store(state_dir, system, take_back=take_back)
    lease = store.acquire("emu-1", "emulator", Holder(OWNER))
    with pytest.raises(KeyboardInterrupt):
        store.release("emu-1", lease.lease_id, end=True)
    assert lease_file(state_dir, "emu-1")["state"] == DRAINING
    # The releasing command has ended, so another one takes its claim over.
    edit_lease(state_dir, "emu-1", lambda data: data.update(reaper_pid=4242))
    assert reaped(store) == [("emu-1", RELEASED_END, RELEASED)]
    assert attempts == [1, 2]


def test_a_release_with_end_during_a_reset_by_another_process_waits_for_the_reset(
    state_dir, system
):
    seen = []
    store = Store(state_dir, system, take_back=lambda lease: seen.append(lease) or True)
    lease = reset_by_another_process(store, system, state_dir)
    drained = store.release("emu-1", lease.lease_id, end=True)
    assert (drained.state, drained.void_reason, drained.reaper_pid) == (
        DRAINING,
        RELEASED_END,
        4242,
    )
    assert reaped(store) == []
    system.end(4242)
    assert reaped(store) == [("emu-1", RELEASED_END, RELEASED)]
    assert len(seen) == 1


def test_a_release_with_end_needs_the_current_lease(state_dir, system):
    store = Store(state_dir, system, take_back=lambda lease: pytest.fail("took back"))
    earlier = store.acquire("emu-1", "emulator", Holder(OWNER))
    store.release("emu-1", earlier.lease_id)
    store.acquire("emu-1", "emulator", Holder(OTHER))
    with pytest.raises(NotHeld, match="another lease"):
        store.release("emu-1", earlier.lease_id, end=True)
    with pytest.raises(NotHeld):
        store.release_holding(earlier.lease_id, end={"emu-1"})
    assert [lease.owner for lease in store.snapshot().leases] == [OTHER]


def test_a_holding_release_ends_only_the_instances_that_it_names(state_dir, system):
    seen = []
    store = Store(state_dir, system, take_back=lambda lease: seen.append(lease.resource) or True)
    system.spawn(100)
    needs = [Need((Choice("emu-1", "emulator"),)), Need((Choice("phone-1", "device"),))]
    granted = store.grant(needs, Holder(OWNER, owner_pid=100))
    released = store.release_holding(granted[0].lease.lease_id, end={"emu-1"})
    assert sorted((lease.resource, after) for lease, after in released) == [
        ("emu-1", None),
        ("phone-1", None),
    ]
    assert seen == ["emu-1"]
    store.acquire("emu-2", "emulator", Holder(OWNER, owner_pid=100))
    store.acquire("emu-3", "emulator", Holder(OWNER, owner_pid=100))
    assert len(store.release_all(100, end={"emu-3"})) == 2
    assert seen == ["emu-1", "emu-3"]
    assert resources(store) == []


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
        with pytest.raises(BanksmanError, match=f"process {os.getpid()} takes phone-1 back"):
            Store(state_dir, system).force_release("phone-1")
        return True

    store = Store(state_dir, system, take_back=take_back)
    store.acquire("phone-1", "device", Holder(OWNER))
    system.advance(20 * 60)
    assert reaped(store) == [("phone-1", IDLE, RELEASED)]


# Leases that end after the discovery of a request started.


def ended_while_discovering(store, system, resource="phone-1"):
    """Hold a resource, start a discovery, and end the lease before the grant. Return the awake
    time at which that discovery started."""
    lease = store.acquire(resource, "device", Holder(OTHER))
    since = system.clock()
    system.advance(1)
    store.release(resource, lease.lease_id)
    return since


def test_a_resource_whose_lease_ended_after_the_discovery_started_is_not_granted(store, system):
    since = ended_while_discovering(store, system)
    with pytest.raises(Outdated, match="a lease on phone-1 ended after the discovery"):
        grant_one(store, [Choice("phone-1", "device")], Holder(OWNER), since=since)
    assert resources(store) == []
    # A discovery that started after the end saw the instance as it is now.
    system.advance(1)
    granted = grant_one(store, [Choice("phone-1", "device")], Holder(OWNER), since=system.clock())
    assert granted.lease.resource == "phone-1"


def test_another_free_choice_is_granted_instead_of_one_whose_lease_ended(store, system):
    since = ended_while_discovering(store, system)
    choices = [Choice("phone-1", "device"), Choice("phone-2", "device")]
    assert grant_one(store, choices, Holder(OWNER), since=since).lease.resource == "phone-2"


def test_a_request_that_the_ended_resource_would_not_meet_is_busy(store, system):
    since = ended_while_discovering(store, system)
    store.acquire("tab-1", "device", Holder(OTHER))
    assert store.grant(phone_and_tablet(), Holder(OWNER), since=since) is None


def test_a_request_without_a_discovery_time_gets_the_resource(store, system):
    ended_while_discovering(store, system)
    assert grant_one(store, [Choice("phone-1", "device")], Holder(OWNER)).lease.resource == "phone-1"


def released(store, system, lease):
    store.release(lease.resource, lease.lease_id)


def taken_back(store, system, lease):
    system.advance(20 * 60)
    assert reaped(store) == [("phone-1", IDLE, RELEASED)]


def forced(store, system, lease):
    store.force_release(lease.resource)


@pytest.mark.parametrize("end", [released, taken_back, forced])
def test_every_end_of_a_lease_is_recorded(state_dir, system, end):
    store = Store(state_dir, system)
    lease = store.acquire("phone-1", "device", Holder(OTHER))
    since = system.clock()
    end(store, system, lease)
    with pytest.raises(Outdated):
        grant_one(store, [Choice("phone-1", "device")], Holder(OWNER), since=since)
    # The scan does not read the record as a lease file.
    assert store.snapshot() == Snapshot([], [])


def test_the_end_of_a_take_back_is_recorded_when_its_lease_is_deleted(state_dir, system):
    discovered = []

    def take_back(lease):
        # A discovery that starts while the instance is taken back can still see it.
        discovered.append(system.clock())
        system.advance(5)
        return True

    store = Store(state_dir, system, take_back=take_back)
    store.acquire("phone-1", "device", Holder(OTHER))
    system.advance(20 * 60)
    assert reaped(store) == [("phone-1", IDLE, RELEASED)]
    with pytest.raises(Outdated):
        grant_one(store, [Choice("phone-1", "device")], Holder(OWNER), since=discovered[0])


def test_a_restart_keeps_the_ends_of_the_earlier_boot_out_of_the_check(
    state_dir, system, ended_path
):
    store = Store(state_dir, system)
    since = ended_while_discovering(store, system)
    system.boot = "boot-2"
    granted = grant_one(store, [Choice("phone-1", "device")], Holder(OWNER), since=since)
    assert granted.lease.resource == "phone-1"
    store.release("phone-1", granted.lease.lease_id)
    record = json.loads(ended_path.read_text())
    assert record["schema"] == 2
    assert record["resources"]["phone-1"]["boot_id"] == "boot-2"


@pytest.mark.parametrize(
    "text",
    [
        "{",
        json.dumps({"schema": 3, "resources": {}}),
        json.dumps({"schema": 1, "boot_id": "boot-1", "ended": {"phone-1": 10_000}}),
        json.dumps({"schema": 2, "resources": ["phone-1"]}),
    ],
)
def test_a_record_of_ends_that_cannot_be_read_counts_as_empty_and_is_replaced(
    store, system, ended_path, text
):
    since = ended_while_discovering(store, system)
    ended_path.write_text(text)
    granted = grant_one(store, [Choice("phone-1", "device")], Holder(OWNER), since=since)
    assert granted.lease.resource == "phone-1"
    store.release("phone-1", granted.lease.lease_id)
    with pytest.raises(Outdated):
        grant_one(store, [Choice("phone-1", "device")], Holder(OWNER), since=since)


def test_a_record_of_ends_that_others_can_write_to_is_not_used_and_is_replaced(
    store, system, ended_path
):
    since = ended_while_discovering(store, system)
    ended_path.chmod(0o666)
    granted = grant_one(store, [Choice("phone-1", "device")], Holder(OWNER), since=since)
    store.release("phone-1", granted.lease.lease_id)
    assert stat.S_IMODE(ended_path.stat().st_mode) == 0o600
    with pytest.raises(Outdated):
        grant_one(store, [Choice("phone-1", "device")], Holder(OWNER), since=since)


def test_an_entry_that_is_not_valid_is_dropped(store, system, ended_path):
    since = ended_while_discovering(store, system)
    record = json.loads(ended_path.read_text())
    entry = record["resources"]["phone-1"]
    record["resources"].update(
        {
            "../x": entry,
            "phone-2": {**entry, "at": True},
            "phone-3": {**entry, "last_use": {**entry["last_use"], "how": "lost"}},
            "phone-4": {**entry, "last_use": {**entry["last_use"], "owner": "a\x1bb"}},
            "phone-5": {**entry, "last_use": {**entry["last_use"], "reason": ["idle"]}},
        }
    )
    ended_path.write_text(json.dumps(record))
    with pytest.raises(Outdated):
        grant_one(store, [Choice("phone-1", "device")], Holder(OWNER), since=since)
    store.release("phone-9", store.acquire("phone-9", "device", Holder(OTHER)).lease_id)
    record = json.loads(ended_path.read_text())
    assert sorted(record["resources"]) == ["phone-1", "phone-9"]


def test_a_record_of_ends_that_cannot_be_written_does_not_stop_the_release(
    state_dir, system, ended_path
):
    warnings = []
    store = Store(state_dir, system, warn=warnings.append)
    lease = store.acquire("phone-1", "device", Holder(OWNER))
    store.release("phone-1", lease.lease_id)
    lease = store.acquire("phone-1", "device", Holder(OTHER))
    ended_path.parent.chmod(0o777)
    assert store.release("phone-1", lease.lease_id) is None
    assert resources(store) == []
    assert warnings == [
        f"the end of the lease on phone-1 is not recorded: group or others can write to"
        f" {ended_path.parent}"
    ]
    # The record of the release by OWNER is gone too, so OWNER does not count as the last holder.
    assert not ended_path.exists()
    assert grant_one(store, [Choice("phone-1", "device")], Holder(OWNER)).previous is None


def test_the_record_of_ends_is_kept_outside_the_state_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("BANKSMAN_ENDED")
    assert default_ended_path() == tmp_path / "state-ended.json"
    monkeypatch.delenv("BANKSMAN_STATE_DIR")
    home = pwd.getpwuid(os.geteuid()).pw_dir
    assert default_ended_path() == Path(home) / ".local" / "state" / "banksman" / "ended.json"


# The last lease of each resource that a holder could use.


def last_use(store, resource="phone-1"):
    end = store.ends().get(resource)
    return None if end is None else end.last_use


def test_a_release_records_who_used_the_resource_last(store, system):
    holder = Holder(OWNER, owner_pid=os.getpid(), issue="#12")
    lease = grant_one(store, [Choice("phone-1", "device")], holder, label="login").lease
    store.release("phone-1", lease.lease_id)
    assert last_use(store) == LastUse(
        OWNER, "boot-1", system.wall, "release", label="login", issue="#12"
    )
    assert last_use(store).left_as_is(OWNER, "boot-1", None)
    assert not last_use(store).left_as_is(OTHER, "boot-1", None)
    assert not last_use(store).left_as_is(OWNER, "boot-2", None)
    assert not last_use(store).left_as_is(OWNER, "boot-1", "reservation-2")


def test_a_release_while_scripts_run_is_a_release(state_dir, system):
    store = Store(state_dir, system)
    lease = store.acquire("phone-1", "device", Holder(OWNER))
    system.spawn(4242)
    store.enter("phone-1", lease.lease_id, 4242)
    assert store.release("phone-1", lease.lease_id).state == DRAINING
    system.end(4242)
    assert reaped(store) == [("phone-1", RELEASE, RELEASED)]
    assert last_use(store).how == "release"
    assert last_use(store).left_as_is(OWNER, "boot-1", None)


@pytest.mark.parametrize("on_void", [False, True])
def test_a_release_with_end_is_a_release_that_ran_the_hook_of_the_kind(state_dir, system, on_void):
    store = Store(state_dir, system, hooked=lambda lease: on_void)
    lease = store.acquire("phone-1", "device", Holder(OWNER))
    store.release("phone-1", lease.lease_id, end=True)
    use = last_use(store)
    assert (use.how, use.reason, use.hook) == ("release", None, on_void)
    # Without a hook, the disk of the instance keeps what the holder left on it.
    assert use.left_as_is(OWNER, "boot-1", None) is not on_void


def test_a_forced_release_of_a_release_with_end_ran_no_hook(state_dir, system):
    store = Store(state_dir, system, hooked=lambda lease: True)
    lease = entered(store, system)
    store.release("phone-1", lease.lease_id, end=True)
    store.force_release("phone-1")
    assert (last_use(store).how, last_use(store).hook) == ("force_release", False)


@pytest.mark.parametrize("on_void", [False, True])
def test_a_void_lease_records_its_reason_and_whether_a_hook_ran(state_dir, system, on_void):
    store = Store(state_dir, system, hooked=lambda lease: on_void)
    store.acquire("phone-1", "device", Holder(OWNER))
    system.advance(20 * 60)
    assert reaped(store) == [("phone-1", IDLE, RELEASED)]
    assert (last_use(store).how, last_use(store).reason, last_use(store).hook) == (
        "void",
        IDLE,
        on_void,
    )
    assert not last_use(store).left_as_is(OWNER, "boot-1", None)


def test_a_forced_release_records_the_holder_and_an_unreadable_file_records_nobody(
    store, state_dir
):
    store.acquire("phone-1", "device", Holder(OWNER))
    store.force_release("phone-1")
    assert (last_use(store).owner, last_use(store).how) == (OWNER, "force_release")
    (state_dir / "phone-1.json").write_text("not json")
    store.force_release("phone-1")
    assert "phone-1" in store.ends()
    assert last_use(store) is None


def test_the_record_survives_a_restart(store, system):
    lease = store.acquire("phone-1", "device", Holder(OWNER))
    store.release("phone-1", lease.lease_id)
    system.boot = "boot-2"
    assert last_use(store).owner == OWNER
    assert not last_use(store).left_as_is(OWNER, "boot-2", None)


def test_a_lease_that_another_worktree_used_in_between_replaces_the_record(store):
    for owner in (OWNER, OTHER):
        lease = store.acquire("phone-1", "device", Holder(owner))
        store.release("phone-1", lease.lease_id)
    assert last_use(store).owner == OTHER
    assert not last_use(store).left_as_is(OWNER, "boot-1", None)


@pytest.mark.parametrize("on_void", [False, True])
def test_the_reapers_end_of_a_free_instance_keeps_the_holder_before_it(
    state_dir, system, on_void
):
    store = Store(state_dir, system, hooked=lambda lease: on_void)
    lease = store.acquire("qa_phone", "emulator", Holder(OWNER))
    store.release("qa_phone", lease.lease_id)
    store = Store(state_dir, system, hooked=lambda lease: on_void, unused=ending("qa_phone"))
    assert reaped(store) == [("qa_phone", FREE_TIMEOUT, RELEASED)]
    use = last_use(store, "qa_phone")
    assert (use.owner, use.how, use.hook) == (OWNER, "release", on_void)
    assert use.left_as_is(OWNER, "boot-1", None) is not on_void


def test_after_a_restart_the_reapers_quarantined_lease_keeps_the_holder_before_it(
    state_dir, system
):
    store = Store(state_dir, system)
    lease = store.acquire("qa_phone", "emulator", Holder(OWNER))
    store.release("qa_phone", lease.lease_id)
    failing = Store(state_dir, system, take_back=lambda lease: False, unused=ending("qa_phone"))
    assert reaped(failing) == [("qa_phone", FREE_TIMEOUT, QUARANTINED)]
    # The take-back after the restart gives the reaper's lease the reason rebooted.
    system.boot = "boot-2"
    assert reaped(store) == [("qa_phone", REBOOTED, RELEASED)]
    use = last_use(store, "qa_phone")
    assert (use.owner, use.how, use.boot_id) == (OWNER, "release", "boot-1")


def test_a_lease_that_was_never_ready_keeps_the_holder_before_it(store, system):
    lease = store.acquire("emu-1", "emulator", Holder(OTHER))
    store.release("emu-1", lease.lease_id)
    # A start of the instance for another worktree fails, and its lease is given back.
    booting = store.reserve("emu-1", "emulator", Holder(OWNER))
    store.release("emu-1", booting.lease_id)
    assert last_use(store, "emu-1").owner == OTHER
    # A start that stops in the middle is taken back as a void lease.
    booting = store.reserve("emu-1", "emulator", Holder(OWNER))
    system.advance(6 * 60)
    assert reaped(store) == [("emu-1", BOOT_TIMEOUT, RELEASED)]
    assert (last_use(store, "emu-1").owner, last_use(store, "emu-1").how) == (OTHER, "release")
    assert store.ends()["emu-1"].at == system.clock()


def test_a_forced_release_of_a_lease_that_was_never_ready_counts_against_the_holder_before_it(
    store,
):
    lease = store.acquire("emu-1", "emulator", Holder(OWNER))
    store.release("emu-1", lease.lease_id)
    store.reserve("emu-1", "emulator", Holder(OTHER))
    store.force_release("emu-1")
    assert (last_use(store, "emu-1").owner, last_use(store, "emu-1").how) == (
        OWNER,
        "force_release",
    )


def test_a_grant_comes_with_the_last_use_of_its_resource(store):
    lease = store.acquire("phone-1", "device", Holder(OWNER))
    store.release("phone-1", lease.lease_id)
    granted = grant_one(store, [Choice("phone-1", "device")], Holder(OTHER))
    assert granted.previous.owner == OWNER
    choices = [Choice("phone-1", "device")]
    kept = grant_one(store, choices, Holder(OTHER), keep=granted.lease.lease_id)
    assert (kept.kept, kept.previous) == (True, None)
    assert grant_one(store, [Choice("phone-2", "device")], Holder(OWNER)).previous is None


# Free instances that the reaper ends.


def ending(*resources):
    """Return a search for free instances that finds these resources, and record what it got."""
    calls = []

    def unused(leased, now):
        calls.append((leased, now))
        return [Unused(resource, "emulator", f"serial-of-{resource}") for resource in resources]

    unused.calls = calls
    return unused


def test_the_reaper_ends_a_free_instance_under_a_lease_of_its_own(state_dir, system):
    seen = []

    def take_back(lease):
        seen.append(lease)
        # A request during the end waits, as for an instance in use.
        other = Store(state_dir, system)
        assert grant_one(other, [Choice("qa_phone", "emulator")], Holder(OTHER)) is None
        with pytest.raises(BanksmanError, match=f"process {os.getpid()} takes qa_phone back"):
            other.force_release("qa_phone")
        return True

    events = []
    store = Store(
        state_dir, system, take_back=take_back, unused=ending("qa_phone"), record=events.append
    )
    assert reaped(store) == [("qa_phone", FREE_TIMEOUT, RELEASED)]
    (lease,) = seen
    assert (lease.state, lease.owner, lease.owner_pid, lease.serial) == (
        DRAINING,
        "banksman",
        None,
        "serial-of-qa_phone",
    )
    assert (lease.reaper_pid, lease.attempts) == (os.getpid(), 1)
    assert resources(store) == []
    assert [(event.event, event.resource, event.reason) for event in events] == [
        (REAP, "qa_phone", FREE_TIMEOUT)
    ]


def test_the_search_for_free_instances_gets_every_resource_with_a_lease_file(state_dir, system):
    unused = ending()
    store = Store(state_dir, system, unused=unused)
    store.acquire("phone-1", "device", Holder(OWNER))
    (state_dir / "phone-2.json").write_text("not json")
    assert reaped(store) == []
    assert unused.calls == [(frozenset({"phone-1", "phone-2"}), system.clock())]


def test_the_reaper_never_ends_an_instance_that_has_a_lease(state_dir, system):
    def take_back(lease):
        pytest.fail("the reaper took the lease back")

    store = Store(state_dir, system, take_back=take_back, unused=ending("phone-1"))
    lease = store.acquire("phone-1", "device", Holder(OWNER))
    assert reaped(store) == []
    assert store.snapshot().leases == [lease]


def test_a_free_instance_that_does_not_end_is_quarantined(state_dir, system):
    store = Store(state_dir, system, take_back=lambda lease: False, unused=ending("qa_phone"))
    assert reaped(store) == [("qa_phone", FREE_TIMEOUT, QUARANTINED)]
    assert lease_file(state_dir, "qa_phone")["state"] == QUARANTINED
    with pytest.raises(Busy, match="quarantined"):
        store.acquire("qa_phone", "emulator", Holder(OTHER))


def test_the_end_of_a_free_instance_that_a_reaper_left_is_finished(state_dir, system):
    attempts = []

    def take_back(lease):
        attempts.append(lease.attempts)
        if len(attempts) == 1:
            raise KeyboardInterrupt
        return True

    store = Store(state_dir, system, take_back=take_back, unused=ending("qa_phone"))
    with pytest.raises(KeyboardInterrupt):
        store.reap()
    # The reaper has ended. The next one takes its lease over, and writes no lease of its own.
    edit_lease(state_dir, "qa_phone", lambda data: data.update(reaper_pid=4242))
    assert reaped(store) == [("qa_phone", FREE_TIMEOUT, RELEASED)]
    assert attempts == [1, 2]


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


def test_without_a_quarantine_no_quarantine_directory_is_made(store, system, quarantine_dir):
    store.acquire("phone-1", "device", Holder(OWNER))
    system.advance(20 * 60)
    store.reap()
    assert not quarantine_dir.exists()


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
        lambda: store.touch("../phone-1", "lease-1"),
        lambda: store.release("../phone-1", "lease-1"),
        lambda: grant_one(store, [Choice("../phone-1", "device")], Holder(OWNER)),
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


# The serial of an instance: only discovery sets it.


def seen(resource, serial, at, kind="emulator"):
    return SerialSeen(resource, kind, serial, at)


def serials(store):
    return {lease.resource: (lease.serial, lease.serial_seen) for lease in store.snapshot().leases}


def test_a_new_lease_gets_the_serial_that_the_discovery_of_its_request_found(store, system):
    choices = [Choice("emu-1", "emulator"), Choice("emu-2", "emulator")]
    found = [seen("emu-1", "emulator-5554", 5.0), seen("emu-2", None, 5.0)]
    first = grant_one(store, choices, Holder(OWNER), seen=found).lease
    second = grant_one(store, choices, Holder(OWNER), seen=found).lease
    assert (first.resource, first.serial, first.serial_seen) == ("emu-1", "emulator-5554", 5.0)
    assert (second.resource, second.serial) == ("emu-2", None)
    # A result that it does not run is recorded too, so that an older result cannot set one.
    assert serials(store) == {"emu-1": ("emulator-5554", 5.0), "emu-2": (None, 5.0)}


def test_a_kept_lease_gets_the_serial_that_discovery_finds_now(store, system):
    choices = [Choice("emu-1", "emulator")]
    held = grant_one(store, choices, Holder(OWNER)).lease
    assert held.serial is None
    # The holder started the instance after the grant.
    found = [seen("emu-1", "emulator-5556", 9.0)]
    kept = grant_one(store, choices, Holder(OWNER), keep=held.lease_id, seen=found)
    assert (kept.kept, kept.lease.serial) == (True, "emulator-5556")


def test_observe_sets_and_clears_the_serial_and_is_not_a_touch(store, system):
    lease = store.acquire("emu-1", "emulator", Holder(OWNER))
    system.advance(60)
    now = store.observe([seen("emu-1", "emulator-5554", system.now)])
    assert now["emu-1"].serial == "emulator-5554"
    assert now["emu-1"].touched == lease.touched
    # The instance stopped: a discovery that started later finds that it does not run.
    store.observe([seen("emu-1", None, system.now + 1)])
    assert serials(store) == {"emu-1": (None, system.now + 1)}


def test_discovery_sets_a_handle_and_never_clears_it(store, system):
    store.acquire("tokay:34", "remote", Holder(OWNER))
    found = SerialSeen("tokay:34", "remote", "localhost:49920", 5.0, "fakeid0000001")
    store.observe([found])
    # The connection ended, and the reservation can still be live.
    store.observe([SerialSeen("tokay:34", "remote", None, 6.0)])
    (lease,) = store.snapshot().leases
    assert (lease.serial, lease.handle) == (None, "fakeid0000001")


def test_a_start_records_the_handle_at_once_and_the_serial_later(store, system):
    (granted,) = store.grant(
        [Need((Choice("tokay:34", "remote", start=True),))], Holder(OWNER)
    )
    lease = granted.lease
    assert (lease.state, lease.reaper_pid) == ("booting", os.getpid())
    early = store.started("tokay:34", lease.lease_id, handle="fakeid0000001")
    assert (early.handle, early.serial) == ("fakeid0000001", None)
    started = store.started(
        "tokay:34", lease.lease_id, handle="fakeid0000001", serial="localhost:49522"
    )
    assert (started.serial, started.serial_seen) == ("localhost:49522", system.now)


def test_a_start_records_the_handle_of_a_lease_that_became_void(store, system, state_dir):
    (granted,) = store.grant(
        [Need((Choice("tokay:34", "remote", start=True),))], Holder(OWNER)
    )
    edit_lease(state_dir, "tokay:34", lambda data: drain(data, "boot_timeout"))
    lease = store.started("tokay:34", granted.lease.lease_id, handle="fakeid0000001")
    assert (lease.state, lease.handle) == ("draining", "fakeid0000001")


def test_the_boot_deadline_counts_the_other_starts_of_the_request(store, system):
    timeouts = Timeouts(boot_timeout=100)
    granted = store.grant(
        [
            Need((Choice("tokay:34", "remote", timeouts, start=True),)),
            Need((Choice("houji:35", "remote", timeouts, start=True),)),
            Need((Choice("emu-1", "emulator", timeouts),)),
        ],
        Holder(OWNER),
    )
    deadlines = [each.lease.boot_deadline - system.now for each in granted]
    assert deadlines == [200, 200, 300]


def test_a_result_that_says_nothing_keeps_the_serial(store, system):
    store.acquire("emu-1", "emulator", Holder(OWNER))
    store.observe([seen("emu-1", "emulator-5554", 5.0)])
    # A failed discovery, an absent instance, or one without the running fact gives no result.
    store.observe([])
    assert serials(store) == {"emu-1": ("emulator-5554", 5.0)}


def test_a_discovery_that_started_earlier_does_not_undo_a_newer_one(store, system):
    store.acquire("emu-1", "emulator", Holder(OWNER))
    # A slow discovery started before the holder started the emulator, and ends after a run
    # recorded the serial.
    store.observe([seen("emu-1", "emulator-5554", 20.0)])
    store.observe([seen("emu-1", None, 10.0)])
    assert serials(store) == {"emu-1": ("emulator-5554", 20.0)}


def test_two_emulators_that_exchange_their_ports_keep_both_serials(store, system):
    store.acquire("emu-1", "emulator", Holder(OWNER))
    store.acquire("emu-2", "emulator", Holder(OTHER))
    store.observe([seen("emu-1", "emulator-5554", 5.0), seen("emu-2", "emulator-5556", 5.0)])
    # Both restarted, and each got the port of the other. One discovery finds both.
    store.observe([seen("emu-1", "emulator-5556", 9.0), seen("emu-2", "emulator-5554", 9.0)])
    assert serials(store) == {"emu-1": ("emulator-5556", 9.0), "emu-2": ("emulator-5554", 9.0)}


def test_a_serial_on_an_instance_without_a_lease_leaves_every_lease(store, system):
    store.acquire("lab-1", "lab", Holder(OWNER))
    store.observe([seen("lab-1", "emulator-5554", 5.0, kind="lab")])
    # lab-1 stopped, and a discover hook that lists only running instances does not list it.
    # An instance that nobody holds started on its port.
    store.observe([seen("lab-2", "emulator-5554", 8.0, kind="lab")])
    assert serials(store) == {"lab-1": (None, 8.0)}
    # A result that is older than the serial of the lease does not take it.
    store.observe([seen("lab-1", "emulator-5556", 9.0, kind="lab")])
    store.observe([seen("lab-2", "emulator-5556", 7.0, kind="lab")])
    assert serials(store) == {"lab-1": ("emulator-5556", 9.0)}


def test_a_serial_belongs_to_one_held_lease_also_across_kinds(store, system):
    store.acquire("emu-1", "emulator", Holder(OWNER))
    store.acquire("lab-1", "lab", Holder(OTHER))
    store.observe([seen("emu-1", "emulator-5554", 5.0)])
    # The emulator stopped, and another instance has its port now.
    store.observe([seen("lab-1", "emulator-5554", 8.0, kind="lab")])
    assert serials(store) == {"emu-1": (None, 8.0), "lab-1": ("emulator-5554", 8.0)}
    # A result that is older than the claim of the other lease is out of date.
    store.observe([seen("emu-1", "emulator-5554", 7.0)])
    assert serials(store) == {"emu-1": (None, 8.0), "lab-1": ("emulator-5554", 8.0)}


def test_a_new_lease_takes_its_serial_from_an_older_claim(store, system):
    store.acquire("emu-1", "emulator", Holder(OWNER))
    store.observe([seen("emu-1", "emulator-5554", 5.0)])
    found = [seen("emu-2", "emulator-5554", 9.0)]
    granted = grant_one(store, [Choice("emu-2", "emulator")], Holder(OTHER), seen=found)
    assert granted.lease.serial == "emulator-5554"
    assert serials(store)["emu-1"] == (None, 9.0)


def test_a_result_applies_to_the_lease_of_its_resource_and_kind_only(store, system):
    store.acquire("emu-1", "emulator", Holder(OWNER))
    store.observe([seen("emu-1", "emulator-5554", 5.0, kind="lab"), seen("emu-2", "x", 5.0)])
    assert serials(store) == {"emu-1": (None, None)}


def test_a_booting_lease_keeps_its_serial(store, system):
    lease = store.reserve("emu-1", "emulator", Holder(OWNER))
    store.observe([seen("emu-1", "emulator-5554", 5.0)])
    store.observe([seen("emu-1", None, 6.0)])
    assert serials(store) == {"emu-1": ("emulator-5554", 5.0)}
    assert lease.state == BOOTING


def test_observe_leaves_leases_that_are_not_held(store, system, state_dir):
    store.acquire("emu-1", "emulator", Holder(OWNER))
    edit_lease(state_dir, "emu-1", drain)
    store.observe([seen("emu-1", "emulator-5554", 5.0)])
    assert lease_file(state_dir, "emu-1")["serial"] is None


# Reading the leases without the lock, for the adb guard.


def test_peek_reads_the_leases_while_another_process_holds_the_lock(
    store, state_dir, quarantine_dir
):
    store.acquire("phone-1", "device", Holder(OWNER))
    fd = os.open(state_dir / ".lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert [lease.resource for lease in peek(state_dir, quarantine_dir).leases] == ["phone-1"]
    finally:
        os.close(fd)


def test_peek_changes_nothing(state_dir, quarantine_dir, system):
    store = failing(state_dir, system)
    quarantine(store, system)
    (state_dir / "phone-1.json").unlink()
    (state_dir / ".lock").unlink()
    (state_dir / ".tmp-left").write_text("")
    # A quarantine whose file a cleaner deleted still counts, but only the next command that takes
    # the lock restores it.
    assert [lease.state for lease in peek(state_dir, quarantine_dir).leases] == [QUARANTINED]
    assert sorted(os.listdir(state_dir)) == [".tmp-left"]


def test_peek_prefers_the_file_in_the_state_directory(state_dir, quarantine_dir, system):
    store = failing(state_dir, system)
    quarantine(store, system)
    edit_lease(state_dir, "phone-1", lambda data: data.update(owner=OTHER))
    (lease,) = peek(state_dir, quarantine_dir).leases
    assert lease.owner == OTHER


def test_peek_without_a_state_directory_finds_no_leases(state_dir, quarantine_dir):
    assert peek(state_dir, quarantine_dir) == Snapshot([], [])
    assert not state_dir.exists()


def test_peek_finds_a_quarantine_after_a_restart_emptied_the_state_directory(
    state_dir, quarantine_dir, system
):
    quarantine(failing(state_dir, system), system)
    shutil.rmtree(state_dir)
    assert [lease.state for lease in peek(state_dir, quarantine_dir).leases] == [QUARANTINED]
    assert not state_dir.exists()


def test_peek_refuses_a_state_directory_that_others_can_write_to(store, state_dir, quarantine_dir):
    store.acquire("phone-1", "device", Holder(OWNER))
    state_dir.chmod(0o777)
    with pytest.raises(StoreError, match="can write"):
        peek(state_dir, quarantine_dir)


def test_peek_shows_a_lease_file_that_cannot_be_read(store, state_dir, quarantine_dir):
    store.acquire("phone-1", "device", Holder(OWNER))
    (state_dir / "phone-2.json").write_text("{")
    snapshot = peek(state_dir, quarantine_dir)
    assert [lease.resource for lease in snapshot.leases] == ["phone-1"]
    assert snapshot.unreadable == [Unreadable("phone-2", "the file is not valid JSON")]
