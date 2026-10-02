from dataclasses import replace

import pytest

from banksman import SCHEMA_VERSION
from banksman.errors import BanksmanError
from banksman.lease import (
    BOOT_TIMEOUT,
    BOOTING,
    DRAINING,
    HARD_CAP,
    IDLE,
    OWNER_GONE,
    READY,
    REBOOTED,
    Lease,
    LeaseFormatError,
    User,
    check_names,
    void_reason,
)

START = 1000.0
LEASE = Lease(
    lease_id="lease-1",
    resource="phone-1",
    kind="emulator",
    state=READY,
    owner="/work/tree-a",
    owner_pid=None,
    owner_started=None,
    boot_id="boot-1",
    acquired_at=1_790_000_000.0,
    touched_at=1_790_000_000.0,
    touched=START,
    boot_deadline=None,
    hard_deadline=START + 3 * 60 * 60,
    idle_timeout=20 * 60,
    owner_grace=5 * 60,
    drain_timeout=5 * 60,
)
OWNED = replace(LEASE, owner_pid=42, owner_started="start-42")


def reason(lease, after, running=None, boot_id="boot-1"):
    return void_reason(lease, boot_id=boot_id, now=START + after, running=running or {})


def test_a_fresh_lease_is_valid():
    assert reason(LEASE, after=0) is None


def test_a_lease_from_an_earlier_boot_is_void():
    assert reason(LEASE, after=0, boot_id="boot-2") == REBOOTED


def test_a_booting_lease_is_void_at_its_boot_deadline():
    booting = replace(LEASE, state=BOOTING, boot_deadline=START + 300)
    assert reason(booting, after=299) is None
    assert reason(booting, after=300) == BOOT_TIMEOUT


def test_the_hard_cap_voids_a_lease_that_is_still_touched():
    touched_recently = replace(LEASE, touched=START + 3 * 60 * 60 - 1)
    assert reason(touched_recently, after=3 * 60 * 60 - 1) is None
    assert reason(touched_recently, after=3 * 60 * 60) == HARD_CAP


def test_a_dead_owner_voids_the_lease_after_the_grace_period():
    assert reason(OWNED, after=299) is None
    assert reason(OWNED, after=300) == OWNER_GONE


def test_a_running_owner_keeps_a_quiet_lease_until_the_idle_timeout():
    running = {42: "start-42"}
    assert reason(OWNED, after=20 * 60 - 1, running=running) is None
    assert reason(OWNED, after=20 * 60, running=running) == IDLE


def test_a_reused_pid_does_not_keep_the_lease():
    assert reason(OWNED, after=300, running={42: "start-of-another-process"}) == OWNER_GONE


def test_without_an_owner_process_only_touches_count():
    assert reason(LEASE, after=20 * 60 - 1) is None
    assert reason(LEASE, after=20 * 60) == IDLE


def test_a_lease_without_an_idle_timeout_lives_until_its_hard_cap():
    no_idle = replace(LEASE, idle_timeout=None)
    assert reason(no_idle, after=3 * 60 * 60 - 1) is None
    assert reason(no_idle, after=3 * 60 * 60) == HARD_CAP


def test_json_round_trip():
    draining = replace(
        LEASE,
        state=DRAINING,
        void_reason=IDLE,
        drain_deadline=START + 300,
        ended=True,
        reaper_pid=7,
        reaper_started="s",
    )
    booting = replace(LEASE, state=BOOTING, boot_deadline=START + 300)
    users = replace(
        LEASE,
        users=(User(pid=9, started="start-9"), User(10, "start-10", pgid=11, leader_started="s")),
    )
    for lease in (LEASE, OWNED, booting, draining, users):
        assert Lease.from_json(lease.to_json()) == lease


def test_the_lease_file_carries_the_schema():
    assert LEASE.to_json()["schema"] == SCHEMA_VERSION


def _without_awake(data):
    del data["awake"]


def _user(**changes):
    return {"pid": 9, "started": "s", "pgid": None, "leader_started": None, **changes}


@pytest.mark.parametrize(
    "change",
    [
        lambda data: data.update(schema=SCHEMA_VERSION + 1),
        lambda data: data.update(state="lost"),
        lambda data: data.update(state=["ready"]),
        lambda data: data.update(resource="../escape"),
        lambda data: data.update(owner="owner\x1b[2J"),
        lambda data: data.update(owner_pid=True),
        lambda data: data.update(owner_pid=42),
        lambda data: data.update(void_reason=["idle"]),
        lambda data: data["awake"].update(touched="soon"),
        lambda data: data["awake"].update(hard_deadline=float("nan")),
        lambda data: data["awake"].update(touched=float("-inf")),
        lambda data: data.update(acquired_at=float("inf")),
        lambda data: data.update(touched_at=1e300),
        lambda data: data.update(idle_timeout=float("inf")),
        lambda data: data.update(reaper_pid=7),
        lambda data: data.update(state=BOOTING),
        lambda data: data.update(state=DRAINING),
        lambda data: data.update(state=DRAINING, void_reason=IDLE),
        lambda data: data.update(ended=None),
        lambda data: data.update(drain_timeout="5m"),
        lambda data: data.update(users=None),
        lambda data: data.update(users=[42]),
        lambda data: data.update(users=[_user(pid=0)]),
        lambda data: data.update(users=[_user(started="")]),
        lambda data: data.update(users=[_user(pgid=9)]),
        lambda data: data.update(users=[_user(leader_started="s")]),
        _without_awake,
    ],
)
def test_a_lease_file_that_is_not_valid_is_refused(change):
    data = LEASE.to_json()
    change(data)
    with pytest.raises(LeaseFormatError):
        Lease.from_json(data)


@pytest.mark.parametrize(
    "resource", ["emulator-5554", "R5CR1234ABC", "192.168.1.5:5555", "qa+1@example.test", "build-0"]
)
def test_common_resource_names_are_valid(resource):
    check_names(resource, "device", "/work/tree-a")


@pytest.mark.parametrize(
    ("resource", "kind", "owner"),
    [
        ("", "device", "/work/tree-a"),
        (".hidden", "device", "/work/tree-a"),
        ("a/b", "device", "/work/tree-a"),
        ("a b", "device", "/work/tree-a"),
        ("x" * 129, "device", "/work/tree-a"),
        ("phone-1", "Device", "/work/tree-a"),
        ("phone-1", "device", ""),
        ("phone-1", "device", "/work/tree\x1b[31m"),
    ],
)
def test_names_that_are_not_valid_are_refused(resource, kind, owner):
    with pytest.raises(BanksmanError):
        check_names(resource, kind, owner)

