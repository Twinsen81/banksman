import json
import os
import signal
import subprocess
import sys
from contextlib import suppress

import pytest
from helpers import SRC, age_lease, edit_lease, hook, reap_in_child, write_config

from banksman import SCHEMA_VERSION, __version__
from banksman.cli import main
from banksman.lease import Holder
from banksman.store import Store
from banksman.system import Machine

OWNER = "/work/tree-a"


def test_version_json(capsys):
    assert main(["version", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"schema": SCHEMA_VERSION, "version": __version__}


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"banksman {__version__} (schema {SCHEMA_VERSION})"


def test_status_json_is_an_empty_pool(capsys):
    assert main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "schema": SCHEMA_VERSION,
        "leases": [],
        "unreadable": [],
    }


def test_status_table(capsys):
    assert main(["status"]) == 0
    assert capsys.readouterr().out.strip() == "No leases."


def test_status_lists_the_leases(capsys, state_dir):
    Store(state_dir, Machine()).acquire("phone-1", "device", Holder(OWNER))
    assert main(["status", "--json"]) == 0
    (lease,) = json.loads(capsys.readouterr().out)["leases"]
    assert {key: lease[key] for key in ("resource", "kind", "state", "owner", "owner_pid")} == {
        "resource": "phone-1",
        "kind": "device",
        "state": "ready",
        "owner": OWNER,
        "owner_pid": None,
    }
    assert lease["acquired_at"].endswith("Z")

    assert main(["status"]) == 0
    header, row = capsys.readouterr().out.splitlines()
    assert header.split() == ["RESOURCE", "KIND", "STATE", "HOLDER", "SINCE", "ACCOUNTS"]
    assert row.split()[:4] == ["phone-1", "device", "ready", "tree-a"]


def test_status_shows_the_holder_and_keeps_the_purpose_out_of_default_json(capsys, state_dir):
    holder = Holder(
        OWNER,
        issue="#123",
        agent="codex",
        session="s-1",
        purpose="verify the tablet layout",
    )
    Store(state_dir, Machine()).acquire("phone-1", "device", holder)
    assert main(["status"]) == 0
    row = capsys.readouterr().out.splitlines()[1]
    assert "codex · #123 · verify the tablet layout" in row

    assert main(["status", "--json"]) == 0
    (lease,) = json.loads(capsys.readouterr().out)["leases"]
    assert {key: lease[key] for key in ("agent", "issue", "session")} == {
        "agent": "codex",
        "issue": "#123",
        "session": "s-1",
    }
    assert "purpose" not in lease

    assert main(["status", "--json", "--verbose"]) == 0
    (lease,) = json.loads(capsys.readouterr().out)["leases"]
    assert lease["purpose"] == "verify the tablet layout"


def test_whoami_shows_the_holder(capsys, tmp_path, monkeypatch):
    tree = tmp_path / "abc-12-tablet"
    tree.mkdir()
    monkeypatch.chdir(tree)
    assert main(["whoami", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert (shown["schema"], shown["owner"], shown["issue"]) == (
        SCHEMA_VERSION,
        str(tree.resolve()),
        "abc-12",
    )
    assert main(["whoami"]) == 0
    rows = dict(line.split(None, 1) for line in capsys.readouterr().out.splitlines()[1:])
    assert (rows["owner"], rows["issue"]) == (str(tree.resolve()), "abc-12")


def test_whoami_uses_the_agents_of_the_configuration(capsys, config_path, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_config(config_path, "[holder]\nagents = []\n")
    assert main(["whoami", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert (shown["agent"], shown["owner_pid"], shown["session"]) == (None, None, None)


def test_status_reaps_first(capsys, state_dir):
    Store(state_dir, Machine()).acquire("phone-1", "device", Holder(OWNER))
    age_lease(state_dir, "phone-1", 21 * 60)
    assert main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["leases"] == []


def test_status_removes_terminal_control_sequences(capsys, state_dir):
    Store(state_dir, Machine()).snapshot()
    (state_dir / "phone\x1b[2J-1.json").write_text("{")
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "phone-1" in out


def test_reap_reports_what_it_took_back(capsys, state_dir):
    Store(state_dir, Machine()).acquire("phone-1", "device", Holder(OWNER))
    age_lease(state_dir, "phone-1", 21 * 60)
    assert main(["reap", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "schema": SCHEMA_VERSION,
        "reaped": [
            {
                "resource": "phone-1",
                "kind": "device",
                "owner": OWNER,
                "void_reason": "idle",
                "outcome": "released",
                "running": [],
            }
        ],
    }


def test_status_survives_a_lease_file_with_infinity(capsys, state_dir):
    Store(state_dir, Machine()).acquire("phone-1", "device", Holder(OWNER))
    edit_lease(state_dir, "phone-1", lambda data: data.update(acquired_at=float("inf")))
    assert main(["status"]) == 0
    assert "unreadable lease file" in capsys.readouterr().out


def test_reap_with_nothing_to_do(capsys):
    assert main(["reap"]) == 0
    assert capsys.readouterr().out.strip() == "Nothing to reap."


def test_an_unsafe_state_directory_is_an_error(capsys, state_dir):
    state_dir.mkdir()
    state_dir.chmod(0o777)
    assert main(["status"]) == 1
    assert capsys.readouterr().err.startswith("banksman: group or others can write to")


def test_a_killed_owner_frees_its_lease(state_dir):
    # End to end: a real owner process, the real process list, the real lock, and the
    # command in its own process.
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        Store(state_dir, Machine()).acquire("phone-1", "device", Holder(OWNER, owner_pid=owner.pid))
        # The last touch was 6 minutes ago: past the grace period, not yet idle.
        age_lease(state_dir, "phone-1", 6 * 60)
        assert reap_in_child(state_dir) == []
        owner.kill()
        owner.wait()
        (reaped,) = reap_in_child(state_dir)
        assert (reaped["resource"], reaped["void_reason"], reaped["outcome"]) == (
            "phone-1",
            "owner_gone",
            "released",
        )
        assert not (state_dir / "phone-1.json").exists()
    finally:
        owner.kill()
        owner.wait()


def test_reap_runs_the_on_void_hook_of_the_kind(capsys, state_dir, config_path, tmp_path):
    out = tmp_path / "out"
    code = "import os, sys; open(sys.argv[1], 'w').write(os.environ['BANKSMAN_RESOURCE'])"
    write_config(config_path, f"[kinds.emulator]\non_void = {json.dumps(hook(code, str(out)))}\n")
    Store(state_dir, Machine()).acquire("emu-1", "emulator", Holder(OWNER))
    age_lease(state_dir, "emu-1", 21 * 60)
    assert main(["reap", "--json"]) == 0
    (reaped,) = json.loads(capsys.readouterr().out)["reaped"]
    assert (reaped["resource"], reaped["outcome"]) == ("emu-1", "released")
    assert out.read_text() == "emu-1"


def test_a_failing_on_void_hook_quarantines_the_lease(capsys, state_dir, config_path):
    write_config(
        config_path, f"[kinds.emulator]\non_void = {json.dumps(hook('raise SystemExit(3)'))}\n"
    )
    Store(state_dir, Machine()).acquire("emu-1", "emulator", Holder(OWNER))
    age_lease(state_dir, "emu-1", 21 * 60)
    assert main(["status"]) == 0
    captured = capsys.readouterr()
    assert captured.err == (
        "banksman: cannot take back emu-1: the on_void hook failed with exit status 3\n"
    )
    assert captured.out.splitlines()[1].split()[:3] == ["emu-1", "emulator", "quarantined"]


def test_a_configuration_error_stops_the_commands_that_reap(capsys, config_path):
    write_config(config_path, "[kinds.build]\ncuont = 4\n")
    for command in (["status"], ["reap"]):
        assert main(command) == 1
        assert capsys.readouterr().err.startswith(f"banksman: {config_path}: kinds.build.cuont: ")
    assert main(["version"]) == 0


def test_no_command_prints_help(capsys):
    assert main([]) == 2
    assert "usage: banksman" in capsys.readouterr().out


def test_abbreviated_flags_are_refused():
    with pytest.raises(SystemExit) as exc:
        main(["status", "--js"])
    assert exc.value.code == 2


@pytest.mark.parametrize("pid", ["0", "-1", "pid", "1.5"])
def test_a_process_id_that_is_not_valid_is_refused(pid):
    with pytest.raises(SystemExit) as exc:
        main(["enter", "--resource", "phone-1", "--lease", "lease-1", "--pid", pid])
    assert exc.value.code == 2


def test_check_exits_3_when_the_lease_is_lost(capsys, state_dir):
    lease = Store(state_dir, Machine()).acquire("phone-1", "device", Holder(OWNER))
    assert main(["check", "--resource", "phone-1", "--lease", lease.lease_id]) == 0
    assert main(["check", "--resource", "phone-1", "--lease", "an-earlier-lease"]) == 3
    assert capsys.readouterr().err == (
        "banksman: the lease is lost: phone-1 has another lease now\n"
    )
    age_lease(state_dir, "phone-1", 21 * 60)
    assert main(["check", "--resource", "phone-1", "--lease", lease.lease_id]) == 3


def test_a_script_enters_and_leaves_a_lease(capsys, state_dir):
    lease = Store(state_dir, Machine()).acquire("phone-1", "device", Holder(OWNER))
    script = subprocess.Popen([sys.executable, "-c", SLEEP], start_new_session=True)
    try:
        arguments = ["--resource", "phone-1", "--lease", lease.lease_id, "--pid", str(script.pid)]
        assert main(["enter", *arguments, "--pgid", str(script.pid)]) == 0
        assert main(["status", "--json"]) == 0
        (shown,) = json.loads(capsys.readouterr().out)["leases"]
        assert shown["users"] == [{"pid": script.pid, "pgid": script.pid}]
        # The script still runs, and it is not the process that runs banksman.
        assert main(["leave", *arguments]) == 1
        assert "of the script still runs" in capsys.readouterr().err
        script.kill()
        script.wait()
        assert main(["leave", *arguments]) == 0
        assert main(["status", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["leases"][0]["users"] == []
    finally:
        script.kill()
        script.wait()


SLEEP = "import time; time.sleep(60)"

# A process that starts a second one in its own process group, as an agent starts a script.
FAKE_AGENT = f"""
import subprocess, sys
script = subprocess.Popen([sys.executable, "-c", {SLEEP!r}])
print(script.pid, flush=True)
script.wait()
"""


def test_enter_refuses_a_group_that_holds_a_fake_agent(capsys, state_dir):
    lease = Store(state_dir, Machine()).acquire("phone-1", "device", Holder(OWNER))
    agent = _start(FAKE_AGENT)
    try:
        script = int(agent.stdout.readline())
        arguments = ["--resource", "phone-1", "--lease", lease.lease_id, "--pid", str(script)]
        assert main(["enter", *arguments, "--pgid", str(agent.pid)]) == 1
        assert f"process group {agent.pid} has process {agent.pid} in it" in capsys.readouterr().err
    finally:
        # The agent is this test's child, not yet waited for, so its group id is still its own.
        os.killpg(agent.pid, signal.SIGKILL)
        agent.wait()
        agent.stdout.close()


# A script as a project writes one: it runs its work in a process group of its own, enters the
# lease, and stops its work as soon as a check says that the lease is lost.
SCRIPT_WITH_A_CHECK_LOOP = f"""
import os, signal, subprocess, sys, time
resource, lease = sys.argv[1], sys.argv[2]
banksman = [sys.executable, "-m", "banksman"]
work = subprocess.Popen([sys.executable, "-c", {SLEEP!r}], process_group=0)
subprocess.run(
    [*banksman, "enter", "--resource", resource, "--lease", lease, "--pid", str(os.getpid()),
     "--pgid", str(work.pid)],
    check=True,
)
print(work.pid, flush=True)
check = [*banksman, "check", "--resource", resource, "--lease", lease]
while subprocess.run(check, stderr=subprocess.DEVNULL).returncode == 0:
    time.sleep(0.05)
os.killpg(work.pid, signal.SIGKILL)
work.wait()
"""

# A script without a check loop never sees that its lease is lost.
SCRIPT_WITHOUT_A_CHECK_LOOP = f"""
import subprocess, sys, time
work = subprocess.Popen([sys.executable, "-c", {SLEEP!r}], process_group=0)
print(work.pid, flush=True)
time.sleep(60)
"""


def test_a_script_whose_lease_is_revoked_ends_by_its_own_check_loop(state_dir):
    holder = Holder(OWNER, owner_pid=os.getpid())
    lease = Store(state_dir, Machine()).acquire("phone-1", "device", holder)
    script = _start(SCRIPT_WITH_A_CHECK_LOOP, "phone-1", lease.lease_id)
    work = None
    try:
        work = _work(script)
        _revoke(state_dir)
        # The script ended by itself: banksman sent it no signal.
        assert script.wait(timeout=60) == 0
        (reaped,) = reap_in_child(state_dir)
        assert (reaped["void_reason"], reaped["outcome"]) == ("hard_cap", "released")
    finally:
        _clean_up(script, work)


def test_by_default_a_script_that_does_not_stop_quarantines_its_lease(capsys, state_dir):
    holder = Holder(OWNER, owner_pid=os.getpid())
    lease = Store(state_dir, Machine()).acquire("phone-1", "device", holder)
    script = _start(SCRIPT_WITHOUT_A_CHECK_LOOP)
    work = None
    try:
        work = _work(script)
        arguments = ["--resource", "phone-1", "--lease", lease.lease_id, "--pid", str(script.pid)]
        assert main(["enter", *arguments, "--pgid", str(work[0])]) == 0
        _revoke(state_dir)
        assert main(["reap", "--json"]) == 0
        (reaped,) = json.loads(capsys.readouterr().out)["reaped"]
        assert (reaped["outcome"], reaped["running"]) == (
            "draining",
            [{"pid": script.pid, "pgid": work[0]}],
        )
        with Store(state_dir, Machine())._lock():
            edit_lease(state_dir, "phone-1", lambda data: data["awake"].update(drain_deadline=0))
        assert main(["status"]) == 0
        assert capsys.readouterr().err == (
            f"banksman: quarantined phone-1: its scripts still run after the drain timeout:"
            f" pid {script.pid} in group {work[0]}. Stopping is off for kind device, so banksman"
            " sent them no signal. When they have ended, run: banksman admin release --force"
            " --resource phone-1\n"
        )
        assert script.poll() is None
    finally:
        _clean_up(script, work)


def test_with_stopping_on_the_reaper_stops_a_script_that_does_not_check(
    capsys, state_dir, config_path
):
    write_config(config_path, "[kinds.device]\nstop = true\n")
    # This test process is the owner, so the reaper may signal the processes that it started.
    holder = Holder(OWNER, owner_pid=os.getpid())
    lease = Store(state_dir, Machine()).acquire("phone-1", "device", holder)
    script = _start(SCRIPT_WITHOUT_A_CHECK_LOOP)
    work = None
    try:
        work = _work(script)
        arguments = ["--resource", "phone-1", "--lease", lease.lease_id, "--pid", str(script.pid)]
        assert main(["enter", *arguments, "--pgid", str(work[0])]) == 0
        _revoke(state_dir)
        assert main(["reap", "--json"]) == 0
        (reaped,) = json.loads(capsys.readouterr().out)["reaped"]
        assert (reaped["void_reason"], reaped["outcome"]) == ("hard_cap", "released")
        assert script.wait(timeout=30) == -signal.SIGTERM
        assert Machine().running([work[0]]).get(work[0]) != work[1]
    finally:
        _clean_up(script, work)


def test_admin_release_needs_force():
    for command in (["admin", "release", "--resource", "phone-1"], ["admin"]):
        with pytest.raises(SystemExit) as exc:
            main(command)
        assert exc.value.code == 2


def test_admin_release_clears_a_quarantine(capsys, state_dir, quarantine_dir, config_path):
    write_config(
        config_path, f"[kinds.emulator]\non_void = {json.dumps(hook('raise SystemExit(3)'))}\n"
    )
    Store(state_dir, Machine()).acquire("emu-1", "emulator", Holder(OWNER))
    age_lease(state_dir, "emu-1", 21 * 60)
    assert main(["reap"]) == 0
    assert (quarantine_dir / "emu-1.json").exists()
    capsys.readouterr()
    assert main(["admin", "release", "--force", "--resource", "emu-1"]) == 0
    assert capsys.readouterr().out == "Released emu-1: its lease was quarantined.\n"
    assert not (quarantine_dir / "emu-1.json").exists()
    assert main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["leases"] == []


def _start(code, *arguments):
    return subprocess.Popen(
        [sys.executable, "-c", code, *arguments],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": SRC},
        start_new_session=True,
    )


def _work(script):
    """Return the pid and the start time of the work group that a script reports."""
    pid = int(script.stdout.readline())
    return pid, Machine().running([pid])[pid]


def _revoke(state_dir):
    """Let the lease reach its hard cap now, although its scripts keep touching it."""
    with Store(state_dir, Machine())._lock():
        edit_lease(state_dir, "phone-1", lambda data: data["awake"].update(hard_deadline=0))


def _clean_up(script, work):
    # Only processes that this test started. A script that is not yet waited for keeps its
    # group id; the work group is signalled only while its leader is the same process.
    if script.returncode is None:
        with suppress(ProcessLookupError):
            os.killpg(script.pid, signal.SIGKILL)
    script.wait()
    script.stdout.close()
    if work is not None and Machine().running([work[0]]).get(work[0]) == work[1]:
        with suppress(ProcessLookupError):
            os.killpg(work[0], signal.SIGKILL)
