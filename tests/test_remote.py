"""The android-remote preset: the catalogue, discovery, the paid gate, and the lifecycle of a
reservation, all against a fake android command line tool and a fake adb."""

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from helpers import SRC, FakeAndroidCli, FakeSdk, edit_lease, hook, table_rows, write_config

from banksman import remote
from banksman.cli import main
from banksman.config import Android, Kind
from banksman.inventory import Decisions, Inventory, load_inventory, save_inventory
from banksman.lease import Holder
from banksman.store import Choice, Need, Store, default_state_dir
from banksman.system import Machine

PROJECT = FakeAndroidCli.PROJECT
# Invented reservation ids: real ones never go into the repository.
FIRST = "fakeid0000001"
SECOND = "fakeid0000002"
MODELS = """\
   MODEL                             CODENAME/API

├─ Google [37]
│  ├─ Pixel 9                        tokay/36                         tokay/35          tokay/34
│  ├─ Pixel Fold (Camera-enabled)    felix_camera/33
│  └─ Pixel Watch                    r11/30
│
├─ OPPO [1]
│  └─ A79 5G                         OP573DL1/34
│
├─ Samsung [118]
│  ├─ Galaxy S23                     dm1q-SM-S911U/36                 dm1qcsx/36 dm1q/35
│  └─ Galaxy Tab S9                  gts9wifi/34
│
├─ Tecno [1]
│  └─ Pova 6                         transsion_lab_tecno-lj7/34
│
└─ Xiaomi [1]
   └─ Xiaomi 14                      houji/35
"""
LIST = f"""\
Reservation    State   Expires   Manufacturer  Model    Codename  API Level
{SECOND}  ACTIVE  9:59 AM   OPPO          A79 5G   OP573DL1  34
{FIRST}  ACTIVE  12:41 PM  Google        Pixel 9  tokay     34
"""
GETPROP = """\
[ro.product.device]: [tokay]
[ro.product.manufacturer]: [Google]
[ro.product.model]: [Pixel 9]
[ro.build.version.sdk]: [34]
[ro.build.characteristics]: [nosdcard]
"""
OPPO_GETPROP = """\
[ro.product.device]: [OP573DL1]
[ro.product.manufacturer]: [OPPO]
[ro.product.model]: [CPH2557]
[ro.build.version.sdk]: [34]
"""
TECNO_GETPROP = """\
[ro.product.device]: [TECNO-LJ7]
[ro.product.manufacturer]: [TECNO]
[ro.build.version.sdk]: [34]
"""
EMULATORS = [
    {"name": "qa_phone", "facts": {"form": "phone", "api": 35, "running": True}},
]
PRINT_ARGUMENT = "import sys; print(sys.argv[1])"
# The command writes its serial argument and the handle in its environment to a file.
SHOW = (
    "import os, sys;"
    " open(sys.argv[2], 'w').write(sys.argv[1] + ' ' + os.environ['BANKSMAN_HANDLE'])"
)


@pytest.fixture
def sdk(tmp_path):
    return FakeSdk(tmp_path)


@pytest.fixture
def cli(tmp_path):
    return FakeAndroidCli(tmp_path)


def configure(config_path, sdk, cli, *, remote_keys="", emulators=False):
    text = sdk.config() + cli.config() + "[holder]\nagents = []\n"
    text += f'[kinds.remote]\npreset = "android-remote"\nproject = "{PROJECT}"\n{remote_keys}'
    allowed = {"remote": Decisions(allowed=("tokay:34", "tokay:35", "OP573DL1:34", "r11:30"))}
    if emulators:
        found = json.dumps({"schema": 1, "instances": EMULATORS})
        text += f"[kinds.emulator]\ndiscover = {json.dumps(hook(PRINT_ARGUMENT, found))}\n"
        allowed["emulator"] = Decisions(allowed=("qa_phone",))
    write_config(config_path, text)
    save_inventory(Inventory(kinds=allowed))


def kind(**changes):
    return Kind("remote", preset="android-remote", project=PROJECT, paid=True, **changes)


def save_catalogue(fetched=None):
    remote.save_catalogue(kind(), remote.parse_models(MODELS), fetched or time.time())


def adb_lists(sdk, ports, getprop=None):
    """Answer as adb does while these remote devices are connected."""
    devices = "".join(f"localhost:{port}\tdevice\n" for port in ports)
    long = "".join(f"localhost:{port}        device transport_id:{port}\n" for port in ports)
    answers = {
        "devices": f"List of devices attached\n{devices}",
        "devices -l": f"List of devices attached\n{long}",
    }
    for port in ports:
        answers[f"-s localhost:{port} shell getprop"] = (getprop or {}).get(port, GETPROP)
        answers[f"-s localhost:{port} shell dumpsys account"] = "  Accounts: 0\n"
    sdk.answer(answers)


def clock_text(minutes):
    return (datetime.now() + timedelta(minutes=minutes)).strftime("%I:%M %p")


def create_answer(cli, handle=FIRST, port=49522, pause=0.0, status=0):
    return {
        "chunks": [
            f"New reservation created with id {handle}. Reservation ends at {clock_text(15)}\n",
            "Waiting for reservation to activate...\nReservation is ACTIVE\n"
            "Starting connection in background...\n"
            f"Logs will be written to /home/me/.android/cli/remote-devices/daemon.log\n"
            f"Device connected on port {port}\nConnection process started in background.\n",
        ],
        "pause": pause,
        "append": {
            str(cli.connections_file): (
                f"projects/{PROJECT}/deviceSessions/session-{handle}={port}\n"
            )
        },
        "status": status,
    }


def lifecycle(cli, *, listed="", create=None, extend=None):
    """Answer a start: no reservation yet, a create that connects, and an extend."""
    cli.connections_file.write_text("#Fri Oct 09 09:44:08 CEST 2026\n")
    answers = {
        "device remote list": {"chunks": [listed]}
        if listed
        else {"chunks": ["No reservations found.\n"], "status": 1},
        "device remote create tokay/34 --connect": create or create_answer(cli),
        f"device remote extend {FIRST} --duration=*": extend
        or {"chunks": [f"Reservation extended. New expire time: {clock_text(60)}\n"]},
        f"device remote remove {FIRST}": {"chunks": ["Reservation cancelled successfully.\n"]},
    }
    cli.answer(answers)
    return answers


def acquire(capsys, *arguments):
    status = main(["acquire", *arguments])
    captured = capsys.readouterr()
    values = dict(line.split("=", 1) for line in captured.out.splitlines() if "=" in line)
    return status, values, captured.err


def leases():
    snapshot = Store(default_state_dir(), Machine()).snapshot()
    return {lease.resource: lease for lease in snapshot.leases}


def sessions():
    return remote.load_sessions(kind())


# The parsers.


def test_the_models_tree_gives_one_model_for_each_codename_and_api():
    models = {model.name: model for model in remote.parse_models(MODELS)}
    assert list(models)[:5] == ["tokay:36", "tokay:35", "tokay:34", "felix_camera:33", "r11:30"]
    assert models["tokay:34"] == remote.Model("tokay", 34, "Google", "Pixel 9")
    # A name with parentheses, the last group, and a line whose last columns have one space.
    assert models["felix_camera:33"].model == "Pixel Fold (Camera-enabled)"
    assert models["houji:35"] == remote.Model("houji", 35, "Xiaomi", "Xiaomi 14")
    assert {models[name].model for name in ("dm1q-SM-S911U:36", "dm1qcsx:36", "dm1q:35")} == {
        "Galaxy S23"
    }
    assert len(models) == 12


def test_the_models_tree_can_have_other_tree_characters():
    text = (
        "MODEL  CODENAME/API\n|- Google [2]\n|  |- Pixel 9 tokay/34\n"
        "|  `- Pixel Tablet  tangorpro/34\n"
    )
    models = remote.parse_models(text)
    assert [(model.name, model.manufacturer, model.model) for model in models] == [
        ("tokay:34", "Google", "Pixel 9"),
        ("tangorpro:34", "Google", "Pixel Tablet"),
    ]


@pytest.mark.parametrize(
    "model,form",
    [
        ("Pixel 9", "phone"),
        ("Galaxy Tab S9", "tablet"),
        ("Pixel Tablet", "tablet"),
        ("Pixel Watch", "watch"),
        ("Table Top", "phone"),
    ],
)
def test_the_form_comes_from_the_words_of_the_model_name(model, form):
    assert remote.form_of(model) == form


def test_the_reservations_of_the_list():
    found = remote.parse_reservations(LIST)
    assert [(each.handle, each.state, each.model, each.codename, each.api) for each in found] == [
        (SECOND, "ACTIVE", "A79 5G", "OP573DL1", "34"),
        (FIRST, "ACTIVE", "Pixel 9", "tokay", "34"),
    ]


def test_the_live_connections_of_the_project():
    text = (
        "#Fri Oct 09 09:44:08 CEST 2026\n"
        f"projects/{PROJECT}/deviceSessions/session-{FIRST}=49920\n"
        f"projects/other/deviceSessions/session-{SECOND}=50066\n"
        "projects/example.com\\:lab/deviceSessions/session-abc=50070\n"
        "garbage\n"
    )
    assert remote.parse_connections(text, PROJECT) == {FIRST: 49920}
    assert remote.parse_connections(text, "example.com:lab") == {"abc": 50070}
    assert remote.parse_connections("", PROJECT) == {}


def at(hour, minute):
    return datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)


@pytest.mark.parametrize(
    "text,now,expected",
    [
        ("9:56 AM", at(9, 0), at(9, 56)),
        ("12:41 PM", at(9, 0), at(12, 41)),
        ("1:05 PM", at(12, 50), at(13, 5)),
        # A time that has passed today is tomorrow.
        ("9:56 AM", at(10, 30), at(9, 56) + timedelta(days=1)),
        # Within a minute, it is the time that just passed.
        ("9:56 AM", at(9, 56) + timedelta(seconds=40), at(9, 56)),
        # The tool puts a narrow no-break space before AM, or "?" in a locale without Unicode.
        ("9:56\u202fAM", at(9, 0), at(9, 56)),
        ("4:35?PM", at(16, 0), at(16, 35)),
        ("21:07", at(20, 0), at(21, 7)),
    ],
)
def test_a_time_of_day_is_its_next_occurrence(text, now, expected):
    assert remote.time_of_day(text, now.timestamp()) == expected.timestamp()


@pytest.mark.parametrize("text", ["", "soon", "25:00", "9:56 XM"])
def test_a_time_that_does_not_parse_is_not_guessed(text):
    assert remote.time_of_day(text, time.time()) is None


# The tool.


def test_the_tool_gets_the_project_and_the_sdk(cli, sdk, tmp_path):
    settings = Android(sdk=sdk.sdk, cli=cli.path, cli_state=cli.state)
    cli.answer({"device remote models": {"chunks": [MODELS]}})
    assert remote.renew_catalogue(kind(), settings) == 12
    assert cli.full_calls() == [
        f"device remote models --project={PROJECT} --sdk={sdk.sdk}",
    ]
    models, fetched = remote.load_catalogue(kind())
    assert models[2] == remote.Model("tokay", 34, "Google", "Pixel 9")
    assert time.time() - fetched < 60


def test_the_errors_of_the_service_come_from_standard_error(cli, sdk):
    settings = Android(sdk=sdk.sdk, cli=cli.path, cli_state=cli.state)
    cli.answer(
        {
            "device remote list": {
                "err": "Error: PERMISSION_DENIED: the project is not ready\n",
                "status": 1,
            }
        }
    )
    with pytest.raises(remote.RemoteError, match="list failed: Error: PERMISSION_DENIED"):
        remote.reservations(kind(), settings)


def test_no_reservation_is_an_empty_list(cli, sdk):
    settings = Android(sdk=sdk.sdk, cli=cli.path, cli_state=cli.state)
    cli.answer({"device remote list": {"chunks": ["No reservations found.\n"], "status": 1}})
    assert remote.reservations(kind(), settings) == []


def test_a_tool_that_does_not_exist_names_the_setting(tmp_path):
    settings = Android(cli=tmp_path / "missing" / "android")
    with pytest.raises(remote.RemoteError, match="set cli in \\[android\\]"):
        remote.reservations(kind(), settings)


def test_a_tool_that_hangs_is_stopped_and_its_output_is_kept(cli):
    cli.answer({"x": {"chunks": ["first\n", "second\n"], "pause": 30}})
    seen = []
    started = time.monotonic()
    answer = remote.run_cli([str(cli.path), "x"], 1.0, seen.append)
    assert time.monotonic() - started < 10
    assert answer == remote.Answer(None, "first\n")
    assert seen and seen[-1] == "first\n"


# Discovery.


def test_without_a_catalogue_agents_see_the_command_that_fetches_it(
    capsys, config_path, sdk, cli
):
    configure(config_path, sdk, cli)
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert (
        "note: kind remote: kind remote has no catalogue of models yet: run banksman admin"
        " discover --kind remote"
    ) in out
    assert cli.calls() == []


def test_the_allowed_models_are_free_and_paid_without_a_call_of_the_tool(
    capsys, config_path, sdk, cli
):
    configure(config_path, sdk, cli)
    save_catalogue(time.time() - 3 * 24 * 60 * 60)
    adb_lists(sdk, [])
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    rows = {row["RESOURCE"]: row for row in table_rows(out.split("\nnote:")[0])}
    assert sorted(rows) == ["OP573DL1:34", "r11:30", "tokay:34", "tokay:35"]
    assert (rows["tokay:34"]["KIND"], rows["tokay:34"]["STATE"]) == ("remote (paid)", "free")
    assert rows["r11:30"]["FORM"] == "watch"
    assert "the catalogue of models was fetched 3 days ago" in out
    assert cli.calls() == []


def test_a_connected_model_has_its_serial_handle_and_end(capsys, config_path, sdk, cli):
    configure(config_path, sdk, cli)
    save_catalogue()
    ends = time.time() + 600
    with remote.changing_sessions(kind()) as records:
        records[FIRST] = remote.Session(FIRST, "tokay:34", True, time.time(), 49920, ends)
    cli.connected({FIRST: 49920})
    adb_lists(sdk, [49920])
    assert main(["status", "--json"]) == 0
    shown = {each["resource"]: each for each in json.loads(capsys.readouterr().out)["resources"]}
    phone = shown["tokay:34"]
    assert (phone["state"], phone["paid"], phone["serial"], phone["handle"]) == (
        "free",
        True,
        "localhost:49920",
        FIRST,
    )
    assert phone["ends"]["at"] == remote.utc_text(ends)
    assert 500 < phone["ends"]["in"] <= 600
    assert shown["tokay:35"]["serial"] is None


def test_a_connection_that_banksman_did_not_make_is_mapped_by_the_device(sdk, cli, config_path):
    configure(config_path, sdk, cli)
    save_catalogue()
    cli.connected({SECOND: 50066})
    adb_lists(sdk, [50066], {50066: OPPO_GETPROP})
    document, _ = remote.discover(kind(), Android(sdk=sdk.sdk, cli=cli.path, cli_state=cli.state))
    oppo = next(each for each in document["instances"] if each["name"] == "OP573DL1:34")
    # The device gives its own model name; the catalogue's name only identifies the model.
    assert oppo["facts"] == {
        "codename": "OP573DL1",
        "api": 34,
        "manufacturer": "OPPO",
        "model": "CPH2557",
        "form": "phone",
        "running": True,
        "serial": "localhost:50066",
        "handle": SECOND,
    }
    assert oppo["accounts"] == []


def test_a_device_that_nothing_maps_is_not_offered(sdk, cli, config_path):
    configure(config_path, sdk, cli)
    save_catalogue()
    cli.connected({SECOND: 50070})
    adb_lists(sdk, [50070], {50070: TECNO_GETPROP})
    settings = Android(sdk=sdk.sdk, cli=cli.path, cli_state=cli.state)
    document, _ = remote.discover(kind(), settings)
    assert not any(each["facts"].get("running") for each in document["instances"])
    assert document["notes"] == [
        f"the remote device on localhost:50070 (reservation {SECOND}) is not a model of the"
        " catalogue, so it is not offered"
    ]


def test_a_connection_that_adb_does_not_list_is_not_running(sdk, cli, config_path):
    configure(config_path, sdk, cli)
    save_catalogue()
    cli.connected({FIRST: 49920})
    adb_lists(sdk, [])
    settings = Android(sdk=sdk.sdk, cli=cli.path, cli_state=cli.state)
    document, _ = remote.discover(kind(), settings)
    phone = next(each for each in document["instances"] if each["name"] == "tokay:34")
    assert phone["facts"]["running"] is False
    assert "serial" not in phone["facts"]


def test_admin_discover_renews_the_catalogue_and_writes_the_inventory(
    capsys, config_path, sdk, cli, inventory_path
):
    configure(config_path, sdk, cli, remote_keys='preselect = ["tokay:*"]\n')
    inventory_path.unlink()
    adb_lists(sdk, [])
    cli.answer(
        {
            "device remote models": {"chunks": [MODELS]},
            "device remote list": {"chunks": [LIST]},
        }
    )
    assert main(["admin", "discover", "--kind", "remote", "--json", "--yes"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["written"] is True
    assert load_inventory().kinds["remote"].allowed == ("tokay:34", "tokay:35", "tokay:36")
    notes = [each["note"] for each in shown["notes"]]
    assert notes[0] == "the catalogue has 12 models now"
    assert notes[1] == (
        f"the account has the reservation {SECOND} of A79 5G (OP573DL1/34): ACTIVE, until 9:59 AM"
    )
    assert cli.calls() == ["device remote models", "device remote list"]
    assert len(remote.load_catalogue(kind())[0]) == 12


def test_admin_discover_keeps_the_catalogue_when_the_tool_fails(
    capsys, config_path, sdk, cli
):
    configure(config_path, sdk, cli)
    save_catalogue()
    adb_lists(sdk, [])
    assert main(["admin", "discover", "--kind", "remote", "--json"]) == 0
    notes = [each["note"] for each in json.loads(capsys.readouterr().out)["notes"]]
    assert notes[0].startswith("cannot renew the catalogue of models, so the earlier one is used")
    assert len(remote.load_catalogue(kind())[0]) == 12


# The paid gate.


def test_a_request_for_only_paid_resources_fails_at_once_without_paid(
    capsys, config_path, sdk, cli
):
    configure(config_path, sdk, cli)
    save_catalogue()
    adb_lists(sdk, [])
    status, values, err = acquire(capsys, "--where", "kind=remote", "--wait", "5m")
    assert (status, values) == (1, {})
    assert "costs money: 4 of kind remote" in err
    assert "only when the user agrees, run the command again with --paid" in err
    assert leases() == {}


def test_a_request_without_paid_never_gets_a_paid_resource(capsys, config_path, sdk, cli):
    configure(config_path, sdk, cli, emulators=True)
    save_catalogue()
    adb_lists(sdk, [])
    status, values, err = acquire(capsys)
    assert (status, values["RESOURCE"], err) == (0, "qa_phone", "")
    # While the emulator is in use, the request is busy; it says that paid ones would match.
    status, values, err = acquire(capsys)
    assert (status, values) == (4, {})
    assert "paid resources match too: 4 of kind remote" in err
    assert "every matching resource is in use: qa_phone" in err


def test_with_paid_a_paid_resource_comes_after_the_free_ones(capsys, config_path, sdk, cli):
    configure(config_path, sdk, cli, emulators=True, remote_keys="rank = 0\n")
    save_catalogue()
    adb_lists(sdk, [])
    status, values, _ = acquire(capsys, "--paid")
    assert (status, values["RESOURCE"]) == (0, "qa_phone")
    status, values, _ = acquire(capsys, "--paid", "--json")
    assert status == 0
    status, values, _ = acquire(capsys, "--paid")
    assert (status, values["KIND"]) == (0, "remote")


def test_a_kept_lease_needs_no_new_acknowledgement(capsys, config_path, sdk, cli):
    configure(config_path, sdk, cli)
    save_catalogue()
    adb_lists(sdk, [])
    status, values, _ = acquire(capsys, "--where", "kind=remote", "--paid")
    assert status == 0
    lease = values["LEASE"]
    status, kept, _ = acquire(capsys, "--where", "kind=remote", "--lease", lease)
    assert (status, kept["KEPT"], kept["RESOURCE"]) == (0, "true", values["RESOURCE"])


def test_the_store_grants_a_paid_choice_only_with_the_acknowledgement(state_dir, system):
    store = Store(state_dir, system)
    need = Need((Choice("tokay:34", "remote", paid=True),))
    holder = Holder("/work/tree-a")
    assert store.grant([need], holder) is None
    (granted,) = store.grant([need], holder, paid=True)
    assert granted.lease.resource == "tokay:34"


def test_a_grant_carries_paid_in_json(capsys, config_path, sdk, cli):
    configure(config_path, sdk, cli)
    save_catalogue()
    adb_lists(sdk, [])
    assert main(["acquire", "--where", "kind=remote", "--paid", "--json"]) == 0
    (part,) = json.loads(capsys.readouterr().out)["parts"]
    assert (part["paid"], part["handle"], part["state"]) == (True, None, "ready")


# The start.


def test_start_reserves_connects_and_extends_the_model(capsys, config_path, sdk, cli):
    configure(config_path, sdk, cli, remote_keys='hard_cap = "1h"\n')
    save_catalogue()
    adb_lists(sdk, [49522])
    lifecycle(cli)
    status, values, err = acquire(
        capsys, "--where", "kind=remote", "--where", "codename=tokay", "--paid", "--start"
    )
    assert status == 0, err
    assert values["SERIAL"] == "localhost:49522"
    assert values["HANDLE"] == FIRST
    assert (values["RESOURCE"], values["STATE"]) == ("tokay:34", "ready")
    lease = leases()["tokay:34"]
    assert (lease.serial, lease.handle, lease.boot_deadline) == ("localhost:49522", FIRST, None)
    calls = cli.calls()
    assert calls[:2] == ["device remote list", "device remote create tokay/34 --connect"]
    # From the end of the reservation, in 15 minutes, to the hard cap of the lease, in 1 hour.
    assert calls[2].startswith(f"device remote extend {FIRST} --duration=")
    assert 44 <= int(calls[2].rsplit("=", 1)[1]) <= 46
    assert len(calls) == 3
    record = sessions()[FIRST]
    assert (record.resource, record.port, record.created) == ("tokay:34", 49522, True)
    assert record.ends is not None and record.ends > time.time() + 50 * 60


def test_start_adopts_a_reservation_that_the_account_already_has(capsys, config_path, sdk, cli):
    configure(config_path, sdk, cli)
    save_catalogue()
    adb_lists(sdk, [49522])
    lifecycle(cli, listed=LIST)
    status, values, err = acquire(capsys, "--where", "codename=tokay", "--paid", "--start")
    assert (status, values["HANDLE"]) == (0, FIRST), err
    assert sessions()[FIRST].created is False


def test_a_failure_after_the_reservation_removes_it_and_gives_the_lease_back(
    capsys, config_path, sdk, cli
):
    configure(config_path, sdk, cli)
    save_catalogue()
    adb_lists(sdk, [49522])
    lifecycle(cli, extend={"err": "Error: INTERNAL: try again\n", "status": 1})
    status, values, err = acquire(capsys, "--where", "codename=tokay", "--paid", "--start")
    assert (status, values) == (1, {})
    assert (
        "cannot start tokay:34: android device remote extend failed: Error: INTERNAL: try again;"
        " the lease is given back"
    ) in err
    assert cli.calls()[-1] == f"device remote remove {FIRST}"
    assert leases() == {}
    assert sessions() == {}


def test_a_failed_create_says_why(capsys, config_path, sdk, cli):
    configure(config_path, sdk, cli)
    save_catalogue()
    adb_lists(sdk, [])
    lifecycle(
        cli,
        create={
            "err": 'Error: INVALID_ARGUMENT: Model "tokay/34" is not available.\n',
            "status": 1,
        },
    )
    status, _, err = acquire(capsys, "--where", "codename=tokay", "--paid", "--start")
    assert status == 1
    assert 'create failed: Error: INVALID_ARGUMENT: Model "tokay/34" is not available.' in err
    assert "remove" not in " ".join(cli.calls())


def test_a_start_that_passes_the_boot_deadline_is_stopped_and_removed(
    capsys, config_path, sdk, cli
):
    configure(config_path, sdk, cli, remote_keys='boot_timeout = "3s"\n')
    save_catalogue()
    adb_lists(sdk, [49522])
    lifecycle(cli, create=create_answer(cli, pause=30))
    started = time.monotonic()
    status, _, err = acquire(capsys, "--where", "codename=tokay", "--paid", "--start")
    assert time.monotonic() - started < 20
    assert status == 1
    assert "did not end by the boot deadline of tokay:34" in err
    assert cli.calls()[-1] == f"device remote remove {FIRST}"
    assert leases() == {}


def test_a_start_that_is_killed_leaves_the_reservation_to_the_reaper(
    capsys, config_path, sdk, cli, state_dir
):
    configure(config_path, sdk, cli)
    save_catalogue()
    adb_lists(sdk, [49522])
    lifecycle(cli, create=create_answer(cli, pause=5))
    arguments = ["acquire", "--where", "codename=tokay", "--paid", "--start"]
    process = subprocess.Popen(
        [sys.executable, "-m", "banksman", *arguments],
        env={**os.environ, "PYTHONPATH": SRC},
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    path = state_dir / "tokay:34.json"
    give_up = time.monotonic() + 30
    while time.monotonic() < give_up:
        if path.exists() and json.loads(path.read_text()).get("handle") == FIRST:
            break
        time.sleep(0.05)
    else:
        process.kill()
        pytest.fail("the start did not record the handle")
    os.killpg(process.pid, signal.SIGKILL)
    process.wait()
    edit_lease(state_dir, "tokay:34", lambda data: data["awake"].update(boot_deadline=0))
    assert main(["reap"]) == 0
    assert f"device remote remove {FIRST}" in cli.calls()
    assert leases() == {}


# Reconnect, take-back, and release.


def start_one(capsys, config_path, sdk, cli, *, listed=""):
    configure(config_path, sdk, cli)
    save_catalogue()
    adb_lists(sdk, [49522])
    answers = lifecycle(cli, listed=listed)
    status, values, err = acquire(capsys, "--where", "codename=tokay", "--paid", "--start")
    assert status == 0, err
    return values, answers


def test_run_connects_a_dropped_device_again(capsys, config_path, sdk, cli, tmp_path):
    values, answers = start_one(capsys, config_path, sdk, cli)
    # adb kill-server ended the daemon: its line is gone, and adb lists another port now.
    cli.connected({})
    adb_lists(sdk, [49920])
    answers[f"device remote connect {FIRST}"] = {
        "chunks": [
            "Starting connection in background...\nDevice connected on port 49920\n"
            "Connection process started in background.\n"
        ],
        "append": {
            str(cli.connections_file): (
                f"projects/{PROJECT}/deviceSessions/session-{FIRST}=49920\n"
            )
        },
    }
    cli.answer(answers)
    command = ["run", "--lease", values["LEASE"], "--resource", "tokay:34", "--"]
    shown = tmp_path / "shown"
    assert main([*command, sys.executable, "-c", SHOW, "{serial}", str(shown)]) == 0
    assert shown.read_text().split() == ["localhost:49920", FIRST]
    assert cli.calls()[-1] == f"device remote connect {FIRST}"
    assert leases()["tokay:34"].serial == "localhost:49920"
    assert sessions()[FIRST].port == 49920


def test_run_fails_before_the_command_when_the_reservation_has_ended(
    capsys, config_path, sdk, cli
):
    values, answers = start_one(capsys, config_path, sdk, cli)
    cli.connected({})
    answers[f"device remote connect {FIRST}"] = {
        "chunks": [f"Invalid reservation ID {FIRST}.\nThere are no active reservations.\n"],
        "status": 1,
    }
    cli.answer(answers)
    command = ["run", "--lease", values["LEASE"], "--resource", "tokay:34", "--"]
    status = main([*command, sys.executable, "-c", "raise SystemExit(7)"])
    err = capsys.readouterr().err
    assert status == 1
    assert (
        f"the reservation {FIRST} of tokay:34 has ended, so its device is gone (Invalid"
        f" reservation ID {FIRST}.). Release the lease"
    ) in err
    assert leases()["tokay:34"].handle == FIRST


def test_acquire_with_lease_connects_a_kept_device_again(capsys, config_path, sdk, cli):
    values, answers = start_one(capsys, config_path, sdk, cli)
    cli.connected({})
    adb_lists(sdk, [49920])
    answers[f"device remote connect {FIRST}"] = {
        "chunks": ["Device connected on port 49920\n"],
        "append": {
            str(cli.connections_file): (
                f"projects/{PROJECT}/deviceSessions/session-{FIRST}=49920\n"
            )
        },
    }
    cli.answer(answers)
    status, kept, err = acquire(capsys, "--where", "codename=tokay", "--lease", values["LEASE"])
    assert status == 0, err
    assert (kept["KEPT"], kept["SERIAL"], kept["HANDLE"]) == ("true", "localhost:49920", FIRST)


def test_a_kept_device_that_cannot_be_connected_gives_the_new_leases_back(
    capsys, config_path, sdk, cli
):
    configure(config_path, sdk, cli, remote_keys="[kinds.build]\ncount = 1\n")
    save_catalogue()
    adb_lists(sdk, [49522])
    answers = lifecycle(cli)
    status, values, err = acquire(capsys, "--where", "codename=tokay", "--paid", "--start")
    assert status == 0, err
    cli.connected({})
    answers[f"device remote connect {FIRST}"] = {"err": "Error: UNAVAILABLE\n", "status": 1}
    cli.answer(answers)
    status, _, err = acquire(
        capsys,
        "--lease",
        values["LEASE"],
        "--as",
        "phone",
        "--where",
        "codename=tokay",
        "--as",
        "slot",
        "--where",
        "kind=build",
    )
    assert status == 1
    assert "cannot connect tokay:34 again" in err
    assert "the new lease is given back" in err
    assert sorted(leases()) == ["tokay:34"]


def test_a_later_start_keeps_a_reservation_that_banksman_created(
    capsys, config_path, sdk, cli, state_dir
):
    values, _ = start_one(capsys, config_path, sdk, cli)
    assert main(["release", "--lease", values["LEASE"]]) == 0
    # The connection ended, so the model does not run; the account still has the reservation.
    lifecycle(cli, listed=LIST)
    status, again, err = acquire(capsys, "--where", "codename=tokay", "--paid", "--start")
    assert (status, again["HANDLE"]) == (0, FIRST), err
    assert sessions()[FIRST].created is True
    edit_lease(state_dir, "tokay:34", lambda data: data["awake"].update(hard_deadline=0))
    assert main(["reap"]) == 0
    assert cli.calls()[-1] == f"device remote remove {FIRST}"


def test_a_take_back_removes_a_reservation_whose_recorded_end_has_passed(
    capsys, config_path, sdk, cli, state_dir
):
    start_one(capsys, config_path, sdk, cli)
    # The holder extended the reservation by hand, so the end that banksman recorded is old.
    with remote.changing_sessions(kind()) as records:
        records[FIRST] = replace(records[FIRST], ends=time.time() - 600)
    assert FIRST in sessions()
    edit_lease(state_dir, "tokay:34", lambda data: data["awake"].update(hard_deadline=0))
    assert main(["reap"]) == 0
    assert cli.calls()[-1] == f"device remote remove {FIRST}"
    assert sessions() == {}


def test_a_record_goes_only_after_the_longest_life_of_a_reservation():
    now = time.time()
    with remote.changing_sessions(kind()) as records:
        records[FIRST] = remote.Session(FIRST, "tokay:34", True, now - 60, ends=now - 30)
        records[SECOND] = remote.Session(SECOND, "houji:35", True, now - 3 * 60 * 60 - 120)
    assert list(sessions()) == [FIRST]


def test_a_void_lease_ends_the_reservation_that_banksman_created(
    capsys, config_path, sdk, cli, state_dir
):
    start_one(capsys, config_path, sdk, cli)
    edit_lease(state_dir, "tokay:34", lambda data: data["awake"].update(hard_deadline=0))
    assert main(["reap"]) == 0
    assert cli.calls()[-1] == f"device remote remove {FIRST}"
    assert leases() == {}
    assert sessions() == {}


def test_a_reservation_that_has_ended_already_counts_as_taken_back(
    capsys, config_path, sdk, cli, state_dir
):
    _, answers = start_one(capsys, config_path, sdk, cli)
    answers[f"device remote remove {FIRST}"] = {
        "chunks": [f"Reservation {FIRST} has already ended.\nActive reservations are:\n  x\n"],
        "status": 1,
    }
    cli.answer(answers)
    edit_lease(state_dir, "tokay:34", lambda data: data["awake"].update(hard_deadline=0))
    assert main(["reap"]) == 0
    assert leases() == {}


def test_a_take_back_that_fails_quarantines_the_model(capsys, config_path, sdk, cli, state_dir):
    _, answers = start_one(capsys, config_path, sdk, cli)
    answers[f"device remote remove {FIRST}"] = {"err": "Error: UNAVAILABLE\n", "status": 1}
    cli.answer(answers)
    edit_lease(state_dir, "tokay:34", lambda data: data["awake"].update(hard_deadline=0))
    assert main(["reap"]) == 0
    assert "cannot take back tokay:34: android device remote remove failed" in (
        capsys.readouterr().err
    )
    assert leases()["tokay:34"].state == "quarantined"


def test_a_void_lease_leaves_a_reservation_that_banksman_did_not_create(
    capsys, config_path, sdk, cli, state_dir
):
    start_one(capsys, config_path, sdk, cli, listed=LIST)
    edit_lease(state_dir, "tokay:34", lambda data: data["awake"].update(hard_deadline=0))
    assert main(["reap"]) == 0
    assert not any("remove" in call for call in cli.calls())
    assert leases() == {}


def test_a_release_keeps_the_reservation_for_the_next_request(capsys, config_path, sdk, cli):
    values, _ = start_one(capsys, config_path, sdk, cli)
    assert main(["release", "--lease", values["LEASE"]]) == 0
    capsys.readouterr()
    assert not any("remove" in call for call in cli.calls())
    # The model runs and is free now, so the next request gets it without a start.
    status, again, _ = acquire(capsys, "--where", "codename=tokay", "--paid", "--start")
    assert (status, again["RESOURCE"], again["SERIAL"], again["HANDLE"]) == (
        0,
        "tokay:34",
        "localhost:49522",
        FIRST,
    )
    assert cli.calls().count("device remote create tokay/34 --connect") == 1


# Devices that banksman did not start.


def test_a_connection_that_banksman_did_not_make_is_unmanaged_and_in_use(
    capsys, config_path, sdk, cli
):
    configure(config_path, sdk, cli)
    save_catalogue()
    # A person reserved and connected the model by hand.
    cli.connected({SECOND: 50066})
    adb_lists(sdk, [50066], {50066: OPPO_GETPROP})
    assert main(["status", "--json"]) == 0
    shown = {each["resource"]: each for each in json.loads(capsys.readouterr().out)["resources"]}
    assert (shown["OP573DL1:34"]["state"], shown["OP573DL1:34"]["handle"]) == ("unmanaged", SECOND)
    assert shown["tokay:34"]["state"] == "free"
    status, _, err = acquire(capsys, "--where", "codename=OP573DL1", "--paid", "--start")
    assert status == 4
    assert "OP573DL1:34 runs, but banksman did not start it" in err
    assert not any("create" in call for call in cli.calls())


def test_with_grant_a_connection_that_banksman_did_not_make_is_granted(
    capsys, config_path, sdk, cli
):
    configure(config_path, sdk, cli, remote_keys='unmanaged = "grant"\n')
    save_catalogue()
    cli.connected({SECOND: 50066})
    adb_lists(sdk, [50066], {50066: OPPO_GETPROP})
    status, values, err = acquire(capsys, "--where", "codename=OP573DL1", "--paid")
    assert (status, values["SERIAL"], values["HANDLE"]) == (0, "localhost:50066", SECOND), err


def test_a_release_records_the_reservation_that_the_holder_made(capsys, config_path, sdk, cli):
    configure(config_path, sdk, cli)
    save_catalogue()
    adb_lists(sdk, [])
    status, values, err = acquire(capsys, "--where", "codename=OP573DL1", "--paid")
    assert (status, values["STATE"]) == (0, "ready"), err
    # The holder reserves the model by hand, and banksman sees the connection while it holds it.
    cli.connected({SECOND: 50066})
    adb_lists(sdk, [50066], {50066: OPPO_GETPROP})
    assert main(["status"]) == 0
    assert leases()["OP573DL1:34"].handle == SECOND
    assert main(["release", "--lease", values["LEASE"]]) == 0
    capsys.readouterr()
    assert sessions()[SECOND].created is False
    status, again, err = acquire(capsys, "--where", "codename=OP573DL1", "--paid")
    assert (status, again["HANDLE"]) == (0, SECOND), err


def test_a_void_lease_records_a_reservation_that_banksman_has_no_record_of(
    capsys, config_path, sdk, cli, state_dir
):
    configure(config_path, sdk, cli)
    save_catalogue()
    adb_lists(sdk, [])
    status, values, err = acquire(capsys, "--where", "codename=OP573DL1", "--paid")
    assert status == 0, err
    cli.connected({SECOND: 50066})
    adb_lists(sdk, [50066], {50066: OPPO_GETPROP})
    assert main(["status"]) == 0
    edit_lease(state_dir, "OP573DL1:34", lambda data: data["awake"].update(hard_deadline=0))
    assert main(["reap"]) == 0
    assert not any("remove" in call for call in cli.calls())
    assert sessions()[SECOND].created is False


def test_start_and_paid_need_where_in_run(capsys, config_path, sdk, cli):
    configure(config_path, sdk, cli)
    for flag in ("--paid", "--start"):
        status = main(["run", "--lease", "x", "--resource", "tokay:34", flag, "--", "true"])
        assert status == 1
        assert f"run {flag} needs --where" in capsys.readouterr().err
