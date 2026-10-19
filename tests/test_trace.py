"""The trace: one line for every command while the operator turns it on, with what the command
saw and decided."""

import json
import os
import signal
import stat
import time

import pytest
import shapes
from helpers import age_lease, hook, work_in, write_config

from banksman import cli, hooks, trace
from banksman.trace import TRACE_SCHEMA
from banksman.cli import main
from banksman.config import ConfigError, load_config
from banksman.inventory import Decisions, Inventory, save_inventory
from banksman.lease import Holder
from banksman.store import Store
from banksman.system import Machine

TRACE_ON = "[log]\ntrace = true\n"
NO_AGENTS = "[holder]\nagents = []\n"
PRINT_ARGUMENT = "import sys; print('looking', file=sys.stderr); print(sys.argv[1])"
BENCH = [
    {"name": "qa_tablet", "facts": {"form": "tablet", "running": False}},
    {"name": "qa_phone", "facts": {"form": "phone", "running": True, "serial": "emulator-5554"}},
    {"name": "Personal_AVD", "facts": {"form": "phone", "running": True}},
]
OTHER = Holder("/work/tree-b", issue="#123", agent="codex", purpose="verify the tablet layout")


def configure(config_path, text=""):
    write_config(config_path, f"{NO_AGENTS}{TRACE_ON}{text}")


def bench(config_path, text=""):
    found = json.dumps({"schema": 1, "instances": BENCH})
    configure(
        config_path,
        f"[kinds.emulator]\ndiscover = {json.dumps(hook(PRINT_ARGUMENT, found))}\n{text}",
    )
    save_inventory(
        Inventory(kinds={"emulator": Decisions(allowed=("qa_tablet", "qa_phone", "qa_gone"))})
    )


def traced(path):
    """Return the lines of the trace, each checked against its recorded shape."""
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    for line in lines:
        shapes.check_trace_line(line)
    return lines


def steps(line, name):
    return [step for step in line["steps"] if step["step"] == name]


def hold(resource, kind, holder=OTHER):
    return Store(cli.default_state_dir(), Machine()).acquire(resource, kind, holder)


def test_without_the_trace_a_command_writes_nothing(config_path, trace_path, capsys):
    assert main(["version"]) == 0
    write_config(config_path, "[log]\ntrace = false\n[kinds.build]\ncount = 1\n")
    assert main(["acquire", "--where", "kind=build"]) == 0
    assert not trace_path.exists()


def test_a_command_adds_one_line_with_its_caller_status_and_duration(
    config_path, trace_path, capsys, tmp_path, monkeypatch
):
    configure(config_path)
    work_in(monkeypatch, tmp_path / "tree-a")
    assert main(["version", "--json"]) == 0
    (line,) = traced(trace_path)
    assert line["schema"] == TRACE_SCHEMA
    assert (line["command"], line["status"], line["error"]) == (["version", "--json"], 0, None)
    assert (line["pid"], line["continues"]) == (os.getpid(), None)
    assert line["caller"]["owner"] == str((tmp_path / "tree-a").resolve())
    assert line["caller"]["agent"] is None
    assert (line["stderr"], line["steps"]) == ([], [])
    assert line["seconds"] >= 0
    # Only the user may read it: it holds full adb command lines and other agents' purposes.
    assert stat.S_IMODE(trace_path.stat().st_mode) == 0o600


def test_a_failed_command_records_its_status_and_standard_error(config_path, trace_path, capsys):
    configure(config_path)
    assert main(["release"]) == 1
    with pytest.raises(SystemExit):
        main(["acquire", "--no-such-option"])
    printed = capsys.readouterr().err
    failed, usage = traced(trace_path)
    assert failed["status"] == 1
    assert failed["stderr"] == [
        "banksman: release needs --lease, the id of the lease, --holding, its label, or --all"
    ]
    assert usage["status"] == 2
    assert usage["stderr"][-1].endswith("unrecognized arguments: --no-such-option")
    # The command still prints what it prints.
    assert "release needs --lease" in printed


def test_a_request_records_discovery_the_search_and_why_it_skipped_a_resource(
    config_path, trace_path, capsys
):
    bench(config_path)
    held = hold("qa_phone", "emulator")
    assert main(["acquire", "--where", "form=phone"]) == cli.EXIT_BUSY
    (line,) = traced(trace_path)
    assert line["status"] == cli.EXIT_BUSY
    assert any("every matching resource is in use: qa_phone" in each for each in line["stderr"])
    (reap,) = steps(line, "reap")
    assert reap["leases"] == [
        {"resource": "qa_phone", "lease_id": held.lease_id, "state": "ready", "void": None}
    ]
    (discovery,) = steps(line, "discovery")
    assert (discovery["kind"], discovery["failure"]) == ("emulator", None)
    assert discovery["instances"][1] == {
        "name": "qa_phone",
        "running": True,
        "serial": "emulator-5554",
        "input_age": None,
        "unmanaged": False,
        "started_by": None,
    }
    (search,) = steps(line, "search")
    assert search["parts"] == [
        {
            "part": None,
            "where": ["form=phone"],
            "accounts": 0,
            "matches": ["qa_phone"],
            "no_match": [{"resource": "qa_tablet", "clause": "form=phone"}],
        }
    ]
    assert search["left_out"] == [
        {"resource": "Personal_AVD", "kind": "emulator", "reason": "not_permitted"}
    ]
    assert search["absent"] == [{"resource": "qa_gone", "kind": "emulator"}]
    (grant,) = steps(line, "grant")
    assert (grant["outcome"], grant["granted"]) == ("busy", [])
    assert grant["parts"] == [
        [
            {
                "resource": "qa_phone",
                "kept": False,
                "skipped": "in_use",
                "lease_id": held.lease_id,
                "state": "ready",
            }
        ]
    ]
    (discover,) = steps(line, "hook")
    # What the hook says on standard error, not the document that it prints.
    assert (discover["hook"], discover["status"], discover["output"]) == (
        "discover",
        0,
        ["looking"],
    )


def test_a_grant_records_the_resources_that_need_paid(config_path, trace_path, capsys):
    configure(config_path, "[kinds.build]\ncount = 1\n[kinds.cloud]\ncount = 1\npaid = true\n")
    hold("build-0", "build")
    assert main(["acquire"]) == cli.EXIT_BUSY
    assert main(["acquire", "--paid"]) == 0
    busy, granted = traced(trace_path)
    (grant,) = steps(busy, "grant")
    assert grant["outcome"] == "busy"
    assert [(option["resource"], option["skipped"]) for option in grant["parts"][0]] == [
        ("build-0", "in_use"),
        ("cloud-0", "paid"),
    ]
    (grant,) = steps(granted, "grant")
    assert (grant["outcome"], grant["granted"]) == ("granted", ["cloud-0"])


def test_the_reaper_records_its_void_checks(config_path, trace_path, state_dir, capsys):
    configure(config_path, "[kinds.build]\ncount = 2\n")
    idle = hold("build-0", "build")
    fresh = hold("build-1", "build")
    age_lease(state_dir, "build-0", 2 * 60 * 60)
    assert main(["status"]) == 0
    (line,) = traced(trace_path)
    reap = steps(line, "reap")[0]
    assert reap["leases"] == [
        {"resource": "build-0", "lease_id": idle.lease_id, "state": "ready", "void": "idle"},
        {"resource": "build-1", "lease_id": fresh.lease_id, "state": "ready", "void": None},
    ]


def test_a_hook_run_records_its_status_and_the_end_of_its_output(
    config_path, trace_path, capsys
):
    code = (
        "import sys; print('resetting', flush=True); print('no device', file=sys.stderr);"
        " sys.exit(5)"
    )
    configure(config_path, f"[kinds.build]\ncount = 1\non_acquire = {json.dumps(hook(code))}\n")
    assert main(["acquire", "--where", "kind=build"]) == 1
    (line,) = traced(trace_path)
    (run,) = steps(line, "hook")
    assert run["hook"] == "on_acquire"
    assert (run["kind"], run["resource"], run["status"]) == ("build", "build-0", 5)
    assert run["failure"] == "failed with exit status 5"
    assert run["output"] == ["resetting", "no device"]
    assert run["command"] == hook(code)


def test_a_hook_that_exits_with_127_itself_failed_also_in_the_trace(
    config_path, trace_path, capsys
):
    # The supervisor says why a hook could not start on a channel of its own, so what the hook
    # says is never taken for that reason.
    code = "import sys; print('sh: tool: not found', file=sys.stderr); sys.exit(127)"
    configure(config_path, f"[kinds.build]\ncount = 1\non_acquire = {json.dumps(hook(code))}\n")
    assert main(["acquire", "--where", "kind=build"]) == 1
    assert "on_acquire hook failed with exit status 127" in capsys.readouterr().err
    (line,) = traced(trace_path)
    (run,) = steps(line, "hook")
    assert (run["failure"], run["output"]) == (
        "failed with exit status 127",
        ["sh: tool: not found"],
    )


def test_a_hook_that_cannot_start_says_why_in_the_trace(config_path, trace_path, tmp_path, capsys):
    missing = str(tmp_path / "no-such-hook")
    configure(config_path, f"[kinds.build]\ncount = 1\non_acquire = [{json.dumps(missing)}]\n")
    assert main(["acquire", "--where", "kind=build"]) == 1
    (line,) = traced(trace_path)
    (run,) = steps(line, "hook")
    assert run["failure"] == f"could not start {missing}: No such file or directory"
    assert (run["status"], run["output"]) == (127, [])


# A program that a hook starts and leaves running: it writes to its standard output and error
# for a second, after the hook has ended, and then says what they are, and in which group it ran.
WRITER = "\n".join(
    [
        "import os, stat, sys, time",
        "time.sleep(0.5)",
        "start = time.monotonic()",
        "while time.monotonic() - start < 1:",
        "    os.write(1, b'x' * 65536)",
        "    os.write(2, b'y' * 65536)",
        "kinds = [stat.S_ISFIFO(os.fstat(fd).st_mode) for fd in (1, 2)]",
        "open(sys.argv[1], 'w').write(f'{os.getpgrp()} {kinds}')",
    ]
)
LEAVES_A_WRITER = "\n".join(
    [
        "import subprocess, sys",
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])",
        "print('reset done')",
    ]
)


def test_a_program_that_a_traced_hook_leaves_running_neither_fills_a_file_nor_ends(
    config_path, trace_path, tmp_path, capsys
):
    # Such a program, for example an emulator that the hook starts, can write for hours. Its
    # output goes to a pipe that is read and discarded, never to a file, and the program never
    # gets SIGPIPE, which would end it.
    report = tmp_path / "report"
    command = hook(LEAVES_A_WRITER, WRITER, str(report))
    configure(config_path, f"[kinds.build]\ncount = 1\non_acquire = {json.dumps(command)}\n")
    started = time.monotonic()
    assert main(["acquire", "--where", "kind=build"]) == 0
    assert time.monotonic() - started < 5
    (line,) = traced(trace_path)
    (run,) = steps(line, "hook")
    assert run["output"] == ["reset done"]
    _wait_for(lambda: report.exists() and report.read_text())
    group, kinds = report.read_text().split(" ", 1)
    assert kinds == "[True, True]"
    # The process that read the pipe ends with the program.
    _wait_for(lambda: all(each.pgid != int(group) for each in Machine().process_table().values()))


def test_a_traced_hook_that_says_more_than_a_pipe_holds_keeps_its_last_lines(
    config_path, trace_path, capsys
):
    code = "for number in range(20000): print(f'line {number}')"
    configure(config_path, f"[kinds.build]\ncount = 1\non_acquire = {json.dumps(hook(code))}\n")
    assert main(["acquire", "--where", "kind=build"]) == 0
    (line,) = traced(trace_path)
    (run,) = steps(line, "hook")
    assert run["output"][-1] == "line 19999"
    assert len(run["output"]) == trace.OUTPUT_LINES


def test_a_traced_hook_that_is_killed_at_its_timeout_keeps_what_it_said(trace_path):
    lease = Store(cli.default_state_dir(), Machine()).acquire(
        "build-0", "build", Holder("/work/tree-a")
    )
    trace.start(["reap"], trace_path)
    code = "import time; print('waiting for the device', flush=True); time.sleep(60)"
    failure = hooks.run(hook(code), lease, timeout=2, hook="on_void")
    trace.finish(0)
    assert failure == "did not end within 2 s"
    (line,) = [json.loads(each) for each in trace_path.read_text().splitlines()]
    (run,) = steps(line, "hook")
    assert run["output"] == ["waiting for the device"]


def _wait_for(condition, seconds=20.0):
    give_up = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < give_up, "the condition did not come true in time"
        time.sleep(0.1)


def test_a_hook_without_the_trace_shows_nothing(config_path, trace_path, capfd):
    code = "import sys; print('hook output'); print('hook output', file=sys.stderr)"
    write_config(config_path, f"[kinds.build]\ncount = 1\non_acquire = {json.dumps(hook(code))}\n")
    assert main(["acquire", "--where", "kind=build"]) == 0
    assert "hook output" not in "".join(capfd.readouterr())
    assert not trace_path.exists()


def test_the_texts_of_a_line_have_no_control_characters(config_path, trace_path, capsys):
    configure(config_path, "[kinds.build]\ncount = 1\n")
    purpose = "\x1b]0;title\x07fix the \x1b[31mlogin\x1b[0m"
    assert main(["acquire", "--where", "kind=build", "--for", purpose]) == 0
    assert main(["release", "--lease", "\x1b[2Jnot-a-lease"]) != 0
    raw = trace_path.read_text()
    assert "\\u001b" not in raw and "\\u0007" not in raw
    granted, refused = traced(trace_path)
    assert granted["command"][-1] == "fix the login"
    assert all("\x1b" not in line for line in refused["stderr"])


def test_a_trace_that_cannot_be_written_changes_nothing(config_path, trace_path, capsys):
    configure(config_path, "[kinds.build]\ncount = 1\n")
    trace_path.mkdir(parents=True)
    assert main(["acquire", "--where", "kind=build"]) == 0
    err = capsys.readouterr().err
    assert err.count("this command is not in the trace") == 1
    assert err.startswith("banksman: warning: this command is not in the trace: cannot open")


def test_a_trace_that_others_can_write_is_not_used(config_path, trace_path, capsys):
    configure(config_path)
    trace_path.parent.mkdir(parents=True)
    trace_path.write_text("")
    trace_path.chmod(0o666)
    assert main(["version"]) == 0
    assert "group or others can write to" in capsys.readouterr().err
    assert trace_path.read_text() == ""


def test_a_trace_that_others_can_read_becomes_private(config_path, trace_path, capsys):
    configure(config_path)
    trace_path.parent.mkdir(parents=True)
    trace_path.write_text("")
    trace_path.chmod(0o644)
    assert main(["version"]) == 0
    assert stat.S_IMODE(trace_path.stat().st_mode) == 0o600
    assert len(traced(trace_path)) == 1


def test_the_trace_starts_again_at_its_size_limit(trace_path):
    line = {"schema": TRACE_SCHEMA, "text": "x" * 100}
    for _ in range(5):
        trace.append(trace_path, line, max_bytes=300)
    earlier = trace.earlier_path(trace_path)
    assert len(earlier.read_text().splitlines()) == 2
    assert len(trace_path.read_text().splitlines()) == 1


def test_a_long_command_keeps_its_first_and_its_last_steps(trace_path):
    trace.start(["watch"], trace_path)
    for number in range(trace.MAX_STEPS + 150):
        trace.step("look", number=number)
    trace.finish(0)
    (line,) = [json.loads(each) for each in trace_path.read_text().splitlines()]
    numbers = [step["number"] for step in line["steps"]]
    half = trace.MAX_STEPS // 2
    assert numbers == [*range(half), *range(half + 150, trace.MAX_STEPS + 150)]
    assert line["steps_left_out"] == 150


def test_a_command_that_ends_by_a_signal_records_the_status_that_the_shell_sees(
    config_path, trace_path, monkeypatch
):
    configure(config_path)
    trace.start(["run", "--", "./tests"], trace_path)
    monkeypatch.setattr(cli.signal, "signal", lambda signum, handler: None)
    monkeypatch.setattr(cli.os, "kill", lambda pid, signum: None)
    cli._end_by(signal.SIGTERM, None)
    (line,) = [json.loads(each) for each in trace_path.read_text().splitlines()]
    assert line["status"] == 128 + signal.SIGTERM
    assert not trace.active()


def test_the_doctor_writes_no_trace(config_path, trace_path, capsys):
    configure(config_path)
    main(["admin", "doctor", "--json"])
    assert not trace_path.exists()


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("[log]\ntrace = 'yes'\n", "log.trace: must be true or false"),
        ("[log]\nlevel = 1\n", "log.level: unknown key"),
        ("log = true\n", "log: must be a table"),
    ],
)
def test_the_log_table_of_the_configuration_is_checked(config_path, text, problem):
    write_config(config_path, text)
    with pytest.raises(ConfigError, match=problem):
        load_config()
