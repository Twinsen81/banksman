"""The helper that a project copies, docs/examples/banksman.sh: with banksman, without it, and
with the stub from docs/examples/banksman-stub, as the project's own tests use it."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import SRC, write_config

from banksman.lease import Holder
from banksman.store import Store
from banksman.system import Machine

EXAMPLES = Path(__file__).resolve().parents[1] / "docs" / "examples"
HELPER = EXAMPLES / "banksman.sh"
STUB = EXAMPLES / "banksman-stub"
# A directory list without banksman, also on a machine where banksman is installed.
SYSTEM_PATH = "/usr/bin:/bin"
# The helper must work in every POSIX shell; dash allows little more than POSIX.
SHELLS = [shell for shell in ("/bin/sh", "/bin/dash") if Path(shell).exists()]
# A script as a project writes one: it leases a resource, runs its work under the lease, and
# gives the lease back.
SCRIPT = """
. "$HELPER"
banksman_acquire --where kind=sdk --for "a test" || exit
echo "resource=${BANKSMAN_RESOURCE:-} serial=${BANKSMAN_SERIAL:-}"
banksman_run sh -c 'echo "in: ${BANKSMAN_RESOURCE:-none}"; exit 5'
status=$?
banksman_release || exit
exit "$status"
"""


@pytest.fixture(params=SHELLS)
def project_script(tmp_path, request):
    script = tmp_path / "script.sh"
    script.write_text(SCRIPT)
    return request.param, script


def run_script(project_script, path, **variables):
    shell, script = project_script
    return subprocess.run(
        [shell, str(script)],
        env={**os.environ, "PATH": path, "HELPER": str(HELPER), **variables},
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def with_banksman(tmp_path, config_path):
    write_config(
        config_path,
        "[holder]\nagents = []\n"
        "[kinds.sdk]\ncount = 1\n[kinds.phone]\ncount = 1\n[kinds.tablet]\ncount = 1\n",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "banksman"
    wrapper.write_text(f'#!/bin/sh\nPYTHONPATH="{SRC}" exec "{sys.executable}" -m banksman "$@"\n')
    wrapper.chmod(0o755)
    return f"{bin_dir}:{SYSTEM_PATH}"


def test_with_banksman_the_script_runs_under_a_lease_and_gives_it_back(
    project_script, with_banksman, state_dir
):
    result = run_script(project_script, with_banksman)
    assert result.returncode == 5, result.stderr
    assert result.stdout == "resource=sdk-0 serial=\nin: sdk-0\n"
    assert result.stderr == "Released sdk-0: it is free.\n"
    assert not (state_dir / "sdk-0.json").exists()


def test_a_lease_that_the_caller_gives_stays_with_the_caller(
    project_script, with_banksman, state_dir
):
    lease = Store(state_dir, Machine()).acquire("sdk-0", "sdk", Holder("/work/tree-a"))
    result = run_script(project_script, with_banksman, BANKSMAN_LEASE=lease.lease_id)
    assert result.returncode == 5, result.stderr
    assert result.stdout == "resource=sdk-0 serial=\nin: sdk-0\n"
    assert (state_dir / "sdk-0.json").exists()


def test_a_lease_that_acquire_keeps_stays_with_the_caller(tmp_path, with_banksman, state_dir):
    # The caller holds a phone and a tablet, and gives the id of the phone to a script that
    # needs the tablet. acquire keeps the tablet of the holding, which has an id of its own.
    banksman = with_banksman.split(":")[0] + "/banksman"
    grant = subprocess.run(
        [banksman, "acquire", "--as", "phone", "--where", "kind=phone"]
        + ["--as", "tablet", "--where", "kind=tablet"],
        capture_output=True,
        text=True,
        check=True,
    )
    values = dict(line.split("=", 1) for line in grant.stdout.splitlines())
    script = tmp_path / "tablet.sh"
    script.write_text(
        '. "$HELPER"\nbanksman_acquire --where kind=tablet || exit\nbanksman_release\n'
    )
    result = run_script(("/bin/sh", script), with_banksman, BANKSMAN_LEASE=values["PHONE_LEASE"])
    assert result.returncode == 0, result.stderr
    assert (state_dir / "tablet-0.json").exists()


def test_a_script_inside_banksman_run_leaves_the_lease_with_the_run(
    tmp_path, with_banksman, state_dir
):
    script = tmp_path / "inside.sh"
    script.write_text(
        '. "$HELPER"\n'
        'banksman_acquire --where kind=sdk --owner-pid "$$" || exit\n'
        'sed -n \'s/.*"owner_pid": \\([0-9]*\\).*/owner \\1/p\' "$BANKSMAN_STATE_DIR/sdk-0.json"\n'
        # The parent of the script is the leader of its group, and banksman run started that.
        'echo "run $(ps -o ppid= -p "$PPID" | tr -d " ")"\n'
    )
    banksman = with_banksman.split(":")[0] + "/banksman"
    result = subprocess.run(
        [banksman, "run", "--where", "kind=sdk", "--", "/bin/sh", str(script)],
        env={**os.environ, "PATH": with_banksman, "HELPER": str(HELPER)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    owner, run = result.stdout.split()[1::2]
    assert owner == run
    assert not (state_dir / "sdk-0.json").exists()


def test_without_banksman_the_script_works_as_before(project_script):
    result = run_script(project_script, SYSTEM_PATH)
    assert (result.returncode, result.stdout, result.stderr) == (
        5,
        "resource= serial=\nin: none\n",
        "",
    )


@pytest.fixture
def with_stub(tmp_path):
    bin_dir = tmp_path / "stub"
    bin_dir.mkdir()
    shutil.copy(STUB, bin_dir / "banksman")
    return f"{bin_dir}:{SYSTEM_PATH}", tmp_path / "calls"


def test_the_stub_records_the_calls_of_the_script(project_script, with_stub):
    path, calls = with_stub
    result = run_script(project_script, path, BANKSMAN_STUB_LOG=str(calls))
    assert result.returncode == 5, result.stderr
    assert result.stdout == "resource=stub-0 serial=stub-serial\nin: stub-0\n"
    assert calls.read_text().splitlines() == [
        "acquire --where kind=sdk --for a test",
        "run --lease stub-lease --resource stub-0 -- sh -c echo"
        ' "in: ${BANKSMAN_RESOURCE:-none}"; exit 5',
        "release --resource stub-0 --lease stub-lease",
    ]


def test_the_stub_runs_a_command_as_banksman_run_runs_it_for_a_device(with_stub):
    path, _ = with_stub
    shown = 'printf "%s|" "$BANKSMAN_SERIAL" "$ANDROID_SERIAL" "$@"'
    for shell in SHELLS:
        result = subprocess.run(
            [shell, "-c", 'banksman run --lease stub-lease --resource stub-0 -- "$@"', "sh",
             "sh", "-c", shown, "sh", "{serial}", "{resource}", "{serial}x", "a b"],
            env={**os.environ, "PATH": path},
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "stub-serial|stub-serial|stub-serial|stub-0|{serial}x|a b|"


def test_the_stub_can_play_a_busy_machine(project_script, with_stub):
    path, calls = with_stub
    result = run_script(
        project_script, path, BANKSMAN_STUB_LOG=str(calls), BANKSMAN_STUB_STATUS="4"
    )
    assert (result.returncode, result.stdout) == (4, "")
    assert calls.read_text().splitlines() == ["acquire --where kind=sdk --for a test"]

