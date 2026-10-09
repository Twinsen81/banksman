"""The console: status, watch, and log."""

import argparse
import json
import os
import time
from datetime import datetime

import pytest
from helpers import drain, edit_lease, hook, table_rows, write_config

from banksman import OUTPUT_SCHEMA, cli, console
from banksman.cli import main
from banksman.history import Event, Process
from banksman.inventory import Decisions, Inventory, save_inventory
from banksman.lease import Holder
from banksman.store import Store
from banksman.system import Machine

PRINT_ARGUMENT = "import sys; print(sys.argv[1])"
READ = "import sys; print(open(sys.argv[1]).read())"
OWNER = "/work/tree-a"
BENCH = [
    {"name": "qa_phone", "facts": {"form": "phone", "api": 35, "running": True}},
    {"name": "qa_tablet", "facts": {"form": "tablet", "api": 33}},
    {"name": "Personal_AVD", "facts": {"form": "phone", "api": 35}},
    {"name": "Stranger_AVD", "facts": {"form": "phone", "api": 34}},
]


def configure(config_path):
    found = json.dumps({"schema": 1, "instances": BENCH})
    write_config(
        config_path,
        "[holder]\nagents = []\n"
        f"[kinds.emulator]\ndiscover = {json.dumps(hook(PRINT_ARGUMENT, found))}\n"
        "[kinds.build]\ncount = 1\n",
    )
    save_inventory(
        Inventory(
            kinds={
                "emulator": Decisions(
                    allowed=("qa_phone", "qa_tablet", "qa_gone"), refused=("Personal_AVD",)
                )
            }
        )
    )


def status_json(capsys, *arguments):
    assert main(["status", "--json", *arguments]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["schema"] == OUTPUT_SCHEMA
    return {each["resource"]: each for each in shown["resources"]}


def acquire(capsys, *arguments):
    assert main(["acquire", *arguments]) == 0
    values = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    return values["LEASE"]


def test_status_shows_every_permitted_resource_also_when_it_is_free(capsys, config_path):
    configure(config_path)
    assert main(["status"]) == 0
    rows = {row["RESOURCE"]: row for row in table_rows(capsys.readouterr().out)}
    # A refused instance, and one that nobody allowed, can be personal, so status never shows
    # them, also not to agents.
    assert sorted(rows) == ["build-0", "qa_gone", "qa_phone", "qa_tablet"]
    assert [rows["qa_phone"][key] for key in ("KIND", "FORM", "API", "STATE", "HOLDER")] == [
        "emulator",
        "phone",
        "35",
        "free",
        "-",
    ]
    assert (rows["qa_gone"]["STATE"], rows["qa_gone"]["FORM"]) == ("absent", "-")
    assert rows["build-0"]["STATE"] == "free"

    shown = status_json(capsys)
    assert shown["qa_tablet"] == {
        "resource": "qa_tablet",
        "kind": "emulator",
        "state": "free",
        "present": True,
        "serial": None,
        "facts": {"form": "tablet", "api": 33},
        "lease": None,
    }
    assert (shown["qa_gone"]["state"], shown["qa_gone"]["present"]) == ("absent", False)


def test_status_shows_when_a_lease_can_be_free(capsys, config_path, tmp_path, monkeypatch):
    configure(config_path)
    (tmp_path / "tree-a").mkdir()
    monkeypatch.chdir(tmp_path / "tree-a")
    acquire(capsys, "--where", "form=phone", "--expect", "20m", "--for", "verify the layout")
    lease = status_json(capsys)["qa_phone"]["lease"]
    ends = lease["free_by"]
    # Seconds of awake time from now; the commands take a moment.
    assert 20 * 60 - 5 <= ends["expected"]["in"] <= 20 * 60
    assert 3 * 60 * 60 - 5 <= ends["latest"]["in"] <= 3 * 60 * 60
    assert 20 * 60 - 5 <= ends["abandoned"]["in"] <= 20 * 60
    assert all(ends[key]["at"].endswith("Z") for key in ends)
    assert lease["drain_deadline"] is None

    before = time.time()
    assert main(["status"]) == 0
    after = time.time()
    row = {row["RESOURCE"]: row for row in table_rows(capsys.readouterr().out)}["qa_phone"]
    assert row["STATE"] == "ready"
    assert row["HOLDER"] == "tree-a · verify the layout"
    # The hard cap ends up to 5 seconds before 3 hours after status; a new minute can start
    # between the commands, so either minute is correct.
    latest = {
        time.strftime("%H:%M", time.localtime(moment + 3 * 60 * 60))
        for moment in (before - 5, after)
    }
    assert row["LATEST"][-5:] in latest


def test_status_shows_the_drain_deadline_of_a_draining_lease(capsys, state_dir):
    Store(state_dir, Machine()).acquire("phone-1", "device", Holder(OWNER))
    # The instance is already ended, and the drain deadline is 10 minutes away.
    edit_lease(
        state_dir,
        "phone-1",
        lambda data: (
            drain(data, deadline=data["awake"]["touched"] + 600),
            data.update(ended=True),
        ),
    )
    # A script that still runs: the test process itself.
    me = {"pid": os.getpid(), "started": Machine().running([os.getpid()])[os.getpid()]}
    user = {**me, "pgid": None, "leader_started": None}
    edit_lease(state_dir, "phone-1", lambda data: data.update(users=[user]))
    lease = status_json(capsys)["phone-1"]["lease"]
    assert (lease["state"], lease["free_by"]) == ("draining", None)
    assert 590 <= lease["drain_deadline"]["in"] <= 600


def test_status_records_the_serials_that_its_discovery_finds(capsys, config_path, tmp_path):
    found = tmp_path / "found.json"

    def instances(*listed):
        for each in listed:
            each["facts"]["avd"] = each["name"]
        found.write_text(json.dumps({"schema": 1, "instances": list(listed)}))

    instances(
        {"name": "qa_phone", "facts": {"running": False}},
        {"name": "qa_tablet", "facts": {"running": False}},
    )
    write_config(
        config_path,
        "[holder]\nagents = []\n"
        f"[kinds.emulator]\ndiscover = {json.dumps(hook(READ, str(found)))}\n",
    )
    save_inventory(Inventory(kinds={"emulator": Decisions(allowed=("qa_phone", "qa_tablet"))}))
    acquire(capsys, "--where", "avd=qa_phone")
    acquire(capsys, "--where", "avd=qa_tablet")
    # The holder starts the emulator. The status of any caller records its serial.
    instances(
        {"name": "qa_phone", "facts": {"running": True, "serial": "emulator-5554"}},
        {"name": "qa_tablet", "facts": {"running": True, "serial": "emulator-5556"}},
    )
    assert main(["status"]) == 0
    rows = {row["RESOURCE"]: row for row in table_rows(capsys.readouterr().out)}
    assert (rows["qa_phone"]["SERIAL"], rows["qa_tablet"]["SERIAL"]) == (
        "emulator-5554",
        "emulator-5556",
    )
    # The phone stops, and the tablet is not found: only the phone loses its serial.
    instances({"name": "qa_phone", "facts": {"running": False}})
    shown = status_json(capsys)
    assert (shown["qa_phone"]["serial"], shown["qa_phone"]["lease"]["serial"]) == (None, None)
    assert shown["qa_tablet"]["lease"]["serial"] == "emulator-5556"
    # A discovery that fails keeps every serial.
    found.write_text("not JSON")
    assert status_json(capsys)["qa_tablet"]["serial"] == "emulator-5556"


def test_status_shows_the_serial_of_a_free_instance(capsys, config_path, tmp_path):
    found = tmp_path / "found.json"
    running = {"name": "qa_phone", "facts": {"running": True, "serial": "emulator-5554"}}
    found.write_text(json.dumps({"schema": 1, "instances": [running]}))
    discover = json.dumps(hook(READ, str(found)))
    write_config(config_path, f"[kinds.emulator]\ndiscover = {discover}\n")
    save_inventory(Inventory(kinds={"emulator": Decisions(allowed=("qa_phone",))}))
    shown = status_json(capsys)["qa_phone"]
    assert (shown["state"], shown["serial"], shown["lease"]) == ("free", "emulator-5554", None)


def test_status_shows_the_notes_of_discovery(capsys, config_path):
    write_config(
        config_path,
        f"[kinds.emulator]\ndiscover = {json.dumps(hook('raise SystemExit(3)'))}\n",
    )
    assert main(["status"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == (
        "note: kind emulator: the discover hook failed with exit status 3"
    )
    assert main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["notes"] == [
        "kind emulator: the discover hook failed with exit status 3"
    ]


def test_status_does_not_show_a_note_that_can_name_a_personal_device(capsys, config_path):
    found = {
        "schema": 1,
        "instances": [{"name": "qa_phone", "facts": {"form": "phone"}}],
        "notes": ["R5CR_PERSONAL is unauthorized, so it is not offered"],
    }
    write_config(
        config_path,
        f"[kinds.device]\ndiscover = {json.dumps(hook(PRINT_ARGUMENT, json.dumps(found)))}\n",
    )
    save_inventory(Inventory(kinds={"device": Decisions(allowed=("qa_phone",))}))
    expected = "kind device: discovery has 1 other note; banksman admin discover shows it"
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "R5CR_PERSONAL" not in out
    assert out.splitlines()[-1] == f"note: {expected}"
    assert main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["notes"] == [expected]


def test_watch_shows_the_table_until_it_is_stopped(capsys, config_path, monkeypatch):
    configure(config_path)
    pauses = []

    def pause(seconds):
        pauses.append(seconds)
        if len(pauses) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_pause", pause)
    assert main(["watch", "--interval", "10s"]) == 0
    out = capsys.readouterr().out
    assert pauses == [10, 10]
    assert out.count("RESOURCE") == 2
    assert "shown every 10s. Press Ctrl-C to stop." in out
    # Standard output is not a terminal here, so the screen is not cleared.
    assert "\x1b" not in out


def test_watch_needs_a_pause_between_refreshes(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["watch", "--interval", "0s"])
    assert exc.value.code == 2
    assert "at least 1s" in capsys.readouterr().err


def test_watch_shows_an_error_and_tries_again(capsys, config_path, monkeypatch):
    write_config(config_path, "[kinds.build]\ncuont = 4\n")
    pauses = []

    def pause(seconds):
        pauses.append(seconds)
        if len(pauses) == 1:
            write_config(config_path, "[kinds.build]\ncount = 1\n")
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_pause", pause)
    assert main(["watch"]) == 0
    out = capsys.readouterr().out
    assert "banksman: " in out and "unknown key" in out
    assert "build-0" in out
    assert pauses == [cli.WATCH_SECONDS, cli.WATCH_SECONDS]


def test_log_shows_who_held_a_resource(capsys, config_path, tmp_path, monkeypatch):
    configure(config_path)
    monkeypatch.chdir(tmp_path)
    lease = acquire(capsys, "--where", "form=phone", "--for", "verify the layout", "--issue", "#7")
    acquire(capsys, "--where", "kind=build")
    assert main(["release", "--lease", lease]) == 0
    capsys.readouterr()

    assert main(["log", "--resource", "qa_phone"]) == 0
    rows = table_rows(capsys.readouterr().out)
    assert [(row["EVENT"], row["RESOURCE"], row["KIND"]) for row in rows] == [
        ("acquire", "qa_phone", "emulator"),
        ("release", "qa_phone", "emulator"),
    ]
    assert rows[0]["HOLDER"] == "#7 · verify the layout"
    assert rows[1]["DETAILS"].startswith("held ")

    assert main(["log", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert (shown["schema"], shown["skipped"]) == (OUTPUT_SCHEMA, 0)
    events = shown["events"]
    assert [(event["event"], event["resource"]) for event in events] == [
        ("acquire", "qa_phone"),
        ("acquire", "build-0"),
        ("release", "qa_phone"),
    ]
    assert events[0]["lease_id"] == lease
    assert events[0]["at"].endswith("Z")
    # The purpose is untrusted text from another agent, as in status.
    assert "purpose" not in events[0]
    assert main(["log", "--json", "--verbose"]) == 0
    assert json.loads(capsys.readouterr().out)["events"][0]["purpose"] == "verify the layout"


def test_log_since_a_time(capsys, config_path):
    configure(config_path)
    acquire(capsys, "--where", "kind=build")
    assert main(["log", "--since", "1h", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["events"]) == 1
    tomorrow = datetime.fromtimestamp(time.time() + 24 * 60 * 60).strftime("%Y-%m-%d")
    assert main(["log", "--since", tomorrow]) == 0
    assert capsys.readouterr().out == "No events.\n"


def test_since_takes_a_duration_or_a_local_date_and_time():
    assert abs(cli._since("3h") - (time.time() - 3 * 60 * 60)) < 5
    assert cli._since("2026-10-02T03:00") == datetime(2026, 10, 2, 3, 0).timestamp()
    assert cli._since("2026-10-02T03:00Z") == datetime.fromisoformat(
        "2026-10-02T03:00+00:00"
    ).timestamp()
    with pytest.raises(argparse.ArgumentTypeError, match="neither a duration"):
        cli._since("yesterday")


def test_log_tells_about_lines_that_are_left_out(capsys, log_path):
    log_path.parent.mkdir(parents=True, mode=0o700)
    log_path.write_text("not json\n")
    log_path.chmod(0o600)
    assert main(["log"]) == 0
    assert capsys.readouterr().out == (
        "No events.\nnote: 1 lines of the log are not valid events, so they are left out\n"
    )


def test_a_log_that_cannot_be_written_does_not_fail_the_command(
    capsys, config_path, tmp_path, monkeypatch
):
    configure(config_path)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    monkeypatch.setenv("BANKSMAN_LOG", str(blocker / "log.jsonl"))
    assert main(["acquire", "--where", "kind=build"]) == 0
    captured = capsys.readouterr()
    assert "LEASE=" in captured.out
    assert captured.err.startswith("banksman: warning: an event is not in the log:")


def test_the_log_text_shows_why_a_lease_was_void(capsys):
    event = Event(1_790_000_000.0, "void", "phone-1", kind="device", owner=OWNER, reason="idle")
    (row,) = console.log_rows([event])
    assert row[1:5] == [
        "void",
        "phone-1",
        "device",
        "nothing touched the lease for the idle timeout",
    ]


def test_the_log_text_says_why_a_release_drains():
    scripts = Event(1_790_000_000.0, "release", "phone-1", drained=True, running=(Process(7, 8),))
    reset = Event(1_790_000_000.0, "release", "phone-1", drained=True)
    assert [console.log_rows([event])[0][4] for event in (scripts, reset)] == [
        "it drains while its scripts run: pid 7 in group 8",
        "it drains while another banksman process resets its instance",
    ]


def test_a_span_is_short():
    assert [console.span(seconds) for seconds in (0, 45, 26 * 60, 80 * 60)] == [
        "0s",
        "45s",
        "26m",
        "1h 20m",
    ]
