import json

import pytest
from helpers import hook, table_rows, write_config

from banksman import cli
from banksman.cli import main
from banksman.inventory import Decisions, Inventory, load_inventory, save_inventory

PRINT_ARGUMENT = "import sys; print(sys.argv[1])"

BENCH = [
    {"name": "qa_phone_api35", "facts": {"form": "phone", "api": 35}, "accounts": []},
    {"name": "Pixel_7_API_34", "facts": {"form": "phone", "api": 34}},
    {
        "name": "R5CR1234ABC",
        "facts": {"model": "Pixel 7"},
        "accounts": ["qa-1@example.test", "someone@example.com"],
    },
    {"name": "R5CR5678DEF", "accounts": ["qa-1@example.test", "qa-2@example.test"]},
    {"name": "R5CR9999XYZ", "accounts": None, "note": "cannot read the accounts"},
]


def configure(config_path, instances=BENCH, *, preselect=None, accounts=None, extra=""):
    found = json.dumps({"schema": 1, "instances": instances})
    text = f"[kinds.lab]\ndiscover = {json.dumps(hook(PRINT_ARGUMENT, found))}\n"
    if preselect is not None:
        text += f"preselect = {json.dumps(preselect)}\n"
    if accounts is not None:
        text += f"[accounts]\npreselect = {json.dumps(accounts)}\n"
    write_config(config_path, text + extra)


def answers(monkeypatch, *replies):
    monkeypatch.setattr(cli, "_is_terminal", lambda: True)
    replies = iter(replies)

    def reply(prompt):
        print(prompt)
        try:
            return next(replies)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", reply)


def unwrapped(out):
    """Return the output with each wrapped sentence of the summary on one line."""
    return out.replace("\n  ", " ")


def run_json(capsys, *arguments):
    assert main(["admin", "discover", "--json", *arguments]) == 0
    return json.loads(capsys.readouterr().out)


def test_json_shows_the_selection_and_writes_nothing(capsys, config_path, inventory_path):
    configure(config_path, preselect=["qa_*", "R5CR*"], accounts=["qa-*@example.test"])
    shown = run_json(capsys)
    assert shown["written"] is False
    assert not inventory_path.exists()
    assert {item["name"]: item["selected"] for item in shown["instances"]} == {
        "qa_phone_api35": True,
        "Pixel_7_API_34": False,
        "R5CR1234ABC": True,
        "R5CR5678DEF": True,
        "R5CR9999XYZ": True,
    }
    assert shown["instances"][0]["facts"] == {
        "form": "phone",
        "api": 35,
        "kind": "lab",
        "paid": False,
    }
    assert shown["accounts"] == [
        {"name": "qa-1@example.test", "on": ["R5CR1234ABC", "R5CR5678DEF"], "selected": True},
        {"name": "qa-2@example.test", "on": ["R5CR5678DEF"], "selected": True},
        {"name": "someone@example.com", "on": ["R5CR1234ABC"], "selected": False},
    ]
    assert shown["inventory"]["kinds"]["lab"]["refused"] == []
    assert shown["inventory"]["accounts"] == {
        "allowed": ["qa-1@example.test", "qa-2@example.test"],
        "refused": [],
    }


def test_without_patterns_nothing_is_selected(capsys, config_path):
    configure(config_path)
    shown = run_json(capsys)
    assert not any(item["selected"] for item in shown["instances"])
    # Accounts are shown only for the selected instances.
    assert shown["accounts"] == []


def test_yes_writes_the_preselection_and_refuses_nothing(capsys, config_path, inventory_path):
    configure(config_path, preselect=["qa_*"])
    assert main(["admin", "discover", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Kind lab: agents may use qa_phone_api35." in out
    assert "Accounts: agents may use none." in out
    assert out.endswith(f"Wrote {inventory_path}.\n")
    assert load_inventory() == Inventory(
        kinds={"lab": Decisions(allowed=("qa_phone_api35",))}, accounts=Decisions()
    )


def test_discover_keeps_the_tags_of_the_operator(capsys, config_path):
    save_inventory(Inventory(tags={"lab": ("qa_*",)}))
    configure(config_path, preselect=["qa_*"])
    shown = run_json(capsys, "--yes")
    assert shown["inventory"]["tags"] == {"lab": ["qa_*"]}
    assert load_inventory().tags == {"lab": ("qa_*",)}


def test_a_rescan_cannot_widen_access_to_a_refused_name(capsys, config_path):
    save_inventory(
        Inventory(
            kinds={"lab": Decisions(refused=("Pixel_7_API_34", "R5CR1234ABC"))},
            accounts=Decisions(refused=("qa-1@example.test",)),
        )
    )
    configure(config_path, preselect=["*"], accounts=["*"])
    shown = run_json(capsys, "--yes", "--all")
    assert shown["written"] is True
    inventory = load_inventory()
    assert inventory.kinds["lab"] == Decisions(
        allowed=("R5CR5678DEF", "R5CR9999XYZ", "qa_phone_api35"),
        refused=("Pixel_7_API_34", "R5CR1234ABC"),
    )
    assert inventory.accounts == Decisions(
        allowed=("qa-2@example.test",), refused=("qa-1@example.test",)
    )


def test_all_selects_names_outside_the_patterns_and_warns(capsys, config_path):
    configure(config_path, preselect=["qa_*", "R5CR*"], accounts=["qa-*@example.test"])
    assert main(["admin", "discover", "--yes", "--all"]) == 0
    out = capsys.readouterr().out
    assert (
        "Warning: Pixel_7_API_34 (kind lab) is selected, but no preselect pattern matches it."
        in out
    )
    assert (
        "Warning: the account someone@example.com is selected, but no preselect pattern matches"
        " it." in out
    )


def test_a_name_that_was_allowed_before_needs_no_pattern(capsys, config_path):
    save_inventory(Inventory(kinds={"lab": Decisions(allowed=("Pixel_7_API_34",))}))
    configure(config_path, preselect=["qa_*"])
    assert main(["admin", "discover", "--yes"]) == 0
    assert "no preselect pattern" not in capsys.readouterr().out


def test_without_patterns_nothing_is_outside_them(capsys, config_path):
    # No pattern of [accounts], as on a machine whose accounts follow no naming rule.
    configure(config_path, preselect=["*"])
    shown = run_json(capsys, "--all")
    assert shown["outside_patterns"] == []
    assert main(["admin", "discover", "--yes", "--all"]) == 0
    assert "no preselect pattern" not in capsys.readouterr().out


def test_names_outside_the_patterns_are_one_warning(capsys, config_path):
    configure(config_path, preselect=["qa_*"], accounts=["nobody@*"])
    assert main(["admin", "discover", "--yes", "--all"]) == 0
    out = unwrapped(capsys.readouterr().out)
    assert (
        "Warning: Pixel_7_API_34, R5CR1234ABC, R5CR5678DEF, R5CR9999XYZ (kind lab) are selected,"
        " but no preselect pattern matches them." in out
    )
    assert (
        "Warning: the accounts qa-1@example.test, qa-2@example.test, someone@example.com are"
        " selected, but no preselect pattern matches them." in out
    )


def test_each_kind_has_a_table_with_the_facts_that_matter(capsys, config_path):
    emulators = [
        {
            "name": f"qa_phone_api{api}",
            "facts": {"avd": f"qa_phone_api{api}", "form": "phone", "api": api, "abi": "x86_64"},
            "accounts": [],
        }
        for api in (34, 35)
    ]
    devices = [
        {
            "name": "R5CR1234ABC",
            "facts": {"serial": "R5CR1234ABC", "manufacturer": "samsung", "model": "SM-S926B"},
            "note": "slow",
        },
        {"name": "R5CR5678DEF", "facts": {"model": "Pixel 7", "abi": "arm64-v8a", "rack": 2}},
    ]
    text = ""
    for kind, found in (("lab", emulators), ("rack", devices)):
        document = json.dumps({"schema": 1, "instances": found})
        text += f"[kinds.{kind}]\ndiscover = {json.dumps(hook(PRINT_ARGUMENT, document))}\n"
    write_config(config_path, text + 'preselect = ["*"]\n')
    assert main(["admin", "discover", "--yes"]) == 0
    lab, rack = capsys.readouterr().out.split("\n\n")[:2]
    title, table = lab.split("\n", 1)
    # A fact that every instance has once, in the title; the AVD name only once, as the name.
    assert title == "Kind lab: 2 found, each with abi=x86_64"
    assert list(table_rows(table)[0]) == ["#", "NAME", "BEFORE", "FORM", "API", "ACCOUNTS"]
    assert [list(row.values()) for row in table_rows(table)] == [
        ["1", "qa_phone_api34", "new", "phone", "34", "0"],
        ["2", "qa_phone_api35", "new", "phone", "35", "0"],
    ]
    title, table = rack.split("\n", 1)
    assert title == "Kind rack: 2 found"
    rows = table_rows(table)
    assert list(rows[0]) == ["#", "NAME", "BEFORE", "MODEL", "ACCOUNTS", "DETAILS"]
    assert [list(row.values()) for row in rows] == [
        ["3", "R5CR1234ABC", "new", "samsung SM-S926B", "?", "note: slow"],
        ["4", "R5CR5678DEF", "new", "Pixel 7", "?", "abi=arm64-v8a  rack=2"],
    ]
    # JSON keeps every fact.
    facts = {item["name"]: item["facts"] for item in run_json(capsys)["instances"]}
    assert facts["qa_phone_api34"]["avd"] == "qa_phone_api34"
    assert facts["R5CR1234ABC"]["serial"] == "R5CR1234ABC"


def test_notes_about_many_names_are_one_line_each(capsys, config_path):
    shared = ["qa-1@example.test", "qa-2@example.test"]
    found = [
        {"name": "R5CR1234ABC", "accounts": shared},
        {"name": "R5CR5678DEF", "accounts": shared},
        {"name": "qa_phone_api34"},
        {"name": "qa_phone_api35"},
    ]
    configure(config_path, found, preselect=["*"], accounts=["qa-*@example.test"])
    assert main(["admin", "discover", "--yes"]) == 0
    out = unwrapped(capsys.readouterr().out)
    assert out.count("are not known") == 1
    assert (
        "The accounts on 2 instances of kind lab are not known (? in the ACCOUNTS column), so"
        " they are not offered for work that needs an account." in out
    )
    assert out.count("signed in on") == 1
    assert (
        "Warning: 2 accounts are signed in on R5CR1234ABC, R5CR5678DEF: qa-1@example.test,"
        " qa-2@example.test." in out
    )


def test_a_long_list_of_names_wraps_between_names(capsys, config_path):
    names = [f"qa_phone_api{api}" for api in range(21, 37)]
    configure(config_path, [{"name": name} for name in names], preselect=["*"])
    assert main(["admin", "discover", "--yes"]) == 0
    summary = capsys.readouterr().out.split("\n\n")[-1].splitlines()
    allowed = summary[: summary.index("Accounts: agents may use none.")]
    assert len(allowed) > 1
    assert all(len(line) <= 100 for line in allowed)
    assert all(line.startswith("  ") for line in allowed[1:])
    assert " ".join(allowed).split() == ["Kind", "lab:", "agents", "may", "use"] + [
        f"{name}," for name in names[:-1]
    ] + [f"{names[-1]}."]


def test_an_allowed_instance_with_an_account_that_agents_may_not_use_warns(
    capsys, config_path
):
    configure(config_path, preselect=["R5CR1234ABC"], accounts=["qa-*@example.test"])
    assert main(["admin", "discover", "--yes"]) == 0
    out = unwrapped(capsys.readouterr().out)
    # The warning names the device, not the account, which can be personal.
    assert (
        "Warning: agents may use R5CR1234ABC, and 1 account that agents may not use is signed in"
        " on it." in out
    )
    assert "that account is signed in" in out


def test_a_person_chooses_and_what_they_leave_out_is_refused(
    capsys, config_path, inventory_path, monkeypatch
):
    configure(config_path, preselect=["qa_*"], accounts=["qa-*@example.test"])
    # Select the two devices with readable accounts, then leave out qa-2, then confirm.
    answers(monkeypatch, "3 4", "", "2", "", "y")
    assert main(["admin", "discover"]) == 0
    out = capsys.readouterr().out
    assert "Warning: qa-1@example.test is signed in on R5CR1234ABC, R5CR5678DEF." in out
    assert out.endswith(f"Wrote {inventory_path}.\n")
    inventory = load_inventory()
    assert inventory.kinds["lab"] == Decisions(
        allowed=("R5CR1234ABC", "R5CR5678DEF", "qa_phone_api35"),
        refused=("Pixel_7_API_34", "R5CR9999XYZ"),
    )
    assert inventory.accounts == Decisions(
        allowed=("qa-1@example.test",), refused=("qa-2@example.test", "someone@example.com")
    )


def test_a_person_can_allow_a_name_that_was_refused(capsys, config_path, monkeypatch):
    save_inventory(Inventory(kinds={"lab": Decisions(refused=("Pixel_7_API_34",))}))
    configure(config_path)
    answers(monkeypatch, "2", "", "y")
    assert main(["admin", "discover"]) == 0
    assert load_inventory().kinds["lab"].allowed == ("Pixel_7_API_34",)


def test_all_and_none_switch_every_name(capsys, config_path, monkeypatch):
    configure(config_path, [{"name": "a"}, {"name": "b"}])
    answers(monkeypatch, "all", "none", "1", "", "y")
    assert main(["admin", "discover"]) == 0
    assert load_inventory().kinds["lab"] == Decisions(allowed=("a",), refused=("b",))


def test_an_answer_that_is_not_valid_is_asked_again(capsys, config_path, monkeypatch):
    configure(config_path, [{"name": "a"}])
    answers(monkeypatch, "7", "one", "1", "", "y")
    assert main(["admin", "discover"]) == 0
    assert capsys.readouterr().out.count("Type numbers from 1 to 1, all, or none.") == 2
    assert load_inventory().kinds["lab"].allowed == ("a",)


def test_without_a_yes_nothing_is_written(capsys, config_path, inventory_path, monkeypatch):
    configure(config_path, preselect=["*"])
    answers(monkeypatch, "", "", "n")
    assert main(["admin", "discover"]) == 0
    assert capsys.readouterr().out.endswith("Nothing was written.\n")
    assert not inventory_path.exists()


def test_the_end_of_the_input_writes_nothing(capsys, config_path, inventory_path, monkeypatch):
    configure(config_path, preselect=["*"])
    answers(monkeypatch)
    assert main(["admin", "discover"]) == 1
    assert capsys.readouterr().err == "banksman: no answer, so nothing was written\n"
    assert not inventory_path.exists()


def test_without_a_terminal_discover_needs_yes_or_json(capsys, config_path, monkeypatch):
    configure(config_path)
    monkeypatch.setattr(cli, "_is_terminal", lambda: False)
    assert main(["admin", "discover"]) == 1
    assert "standard input is not a terminal" in capsys.readouterr().err


def test_an_instance_with_unknown_accounts_is_not_offered_for_accounts(capsys, config_path):
    configure(config_path, preselect=["R5CR9999XYZ"])
    assert main(["admin", "discover", "--yes"]) == 0
    out = capsys.readouterr().out
    assert (
        "The accounts on R5CR9999XYZ are not known, so it is not offered for work that needs an"
        " account." in out
    )
    assert load_inventory().accounts == Decisions()


def test_an_allowed_instance_that_is_not_found_now_stays_allowed(capsys, config_path):
    save_inventory(Inventory(kinds={"lab": Decisions(allowed=("unplugged",))}))
    configure(config_path, [{"name": "a"}])
    shown = run_json(capsys, "--yes")
    assert shown["missing"] == [{"kind": "lab", "name": "unplugged"}]
    assert load_inventory().kinds["lab"].allowed == ("unplugged",)
    assert main(["admin", "discover", "--yes"]) == 0
    # Not only an unplugged device: one that is not authorized is not offered either.
    assert (
        "unplugged (kind lab) is allowed but not offered now. It stays allowed."
        in capsys.readouterr().out
    )


def test_a_name_of_another_kind_is_left_out(capsys, config_path):
    configure(config_path, [{"name": "build-0"}, {"name": "a"}], extra="[kinds.build]\ncount = 2\n")
    shown = run_json(capsys)
    assert [item["name"] for item in shown["instances"]] == ["a"]
    assert shown["notes"] == [
        {"kind": "lab", "note": "build-0 is also an instance of kind build, so it is left out"}
    ]


def test_a_failing_hook_is_a_note_and_changes_nothing(capsys, config_path):
    save_inventory(Inventory(kinds={"lab": Decisions(allowed=("a",))}))
    write_config(
        config_path,
        f"[kinds.lab]\ndiscover = {json.dumps(hook('raise SystemExit(4)'))}\n",
    )
    assert main(["admin", "discover", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "note: the discover hook failed with exit status 4" in out
    assert load_inventory().kinds["lab"] == Decisions(allowed=("a",))


def test_kind_limits_discover_to_one_kind(capsys, config_path):
    save_inventory(Inventory(kinds={"rack": Decisions(allowed=("r1",))}))
    configure(
        config_path,
        [{"name": "a"}],
        preselect=["*"],
        extra=f"[kinds.rack]\ndiscover = {json.dumps(hook('raise SystemExit(1)'))}\n",
    )
    shown = run_json(capsys, "--yes", "--kind", "lab")
    assert [item["name"] for item in shown["instances"]] == ["a"]
    assert load_inventory().kinds == {
        "lab": Decisions(allowed=("a",)),
        "rack": Decisions(allowed=("r1",)),
    }


@pytest.mark.parametrize(
    ("text", "arguments", "error"),
    [
        ("", [], "no kind gets its instances from discovery"),
        ("[kinds.build]\ncount = 2\n", ["--kind", "build"], "declares no kind build"),
    ],
)
def test_discover_needs_a_discovered_kind(capsys, config_path, text, arguments, error):
    write_config(config_path, text)
    assert main(["admin", "discover", "--yes", *arguments]) == 1
    assert error in capsys.readouterr().err


def test_text_from_a_hook_is_shown_without_control_sequences(capsys, config_path):
    configure(config_path, [{"name": "a", "note": "slow\x1b[2J", "facts": {"model": "x"}}])
    assert main(["admin", "discover", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "note: slow" in out

def test_json_shows_no_account_of_an_instance_that_is_not_selected(capsys, config_path):
    configure(config_path, preselect=["R5CR5678DEF"])
    assert main(["admin", "discover", "--json"]) == 0
    out = capsys.readouterr().out
    # R5CR1234ABC is not selected, so its accounts stay out, also its personal one.
    assert "someone@example.com" not in out
    shown = json.loads(out)
    assert all("accounts" not in item for item in shown["instances"])
    assert {item["name"]: item["accounts_known"] for item in shown["instances"]} == {
        "qa_phone_api35": True,
        "Pixel_7_API_34": False,
        "R5CR1234ABC": True,
        "R5CR5678DEF": True,
        "R5CR9999XYZ": False,
    }
    assert [item["name"] for item in shown["accounts"]] == [
        "qa-1@example.test",
        "qa-2@example.test",
    ]


def two_kinds(config_path, found, *, preselect="*"):
    text = ""
    for kind in ("lab", "rack"):
        document = json.dumps({"schema": 1, "instances": found})
        text += f"[kinds.{kind}]\ndiscover = {json.dumps(hook(PRINT_ARGUMENT, document))}\n"
        text += f"preselect = {json.dumps([preselect])}\n"
    write_config(config_path, text)


def test_a_name_that_another_kind_has_in_the_inventory_is_left_out(capsys, config_path):
    two_kinds(config_path, [{"name": "a"}])
    assert main(["admin", "discover", "--yes", "--kind", "lab"]) == 0
    capsys.readouterr()
    shown = run_json(capsys, "--yes", "--kind", "rack")
    assert shown["instances"] == []
    assert shown["notes"] == [
        {"kind": "rack", "note": "a is an instance of kind lab in the inventory, so it is left out"}
    ]
    assert load_inventory().kinds == {"lab": Decisions(allowed=("a",)), "rack": Decisions()}


def test_a_refusal_under_one_kind_holds_for_every_kind(capsys, config_path):
    save_inventory(Inventory(kinds={"lab": Decisions(refused=("a",))}))
    two_kinds(config_path, [{"name": "a"}, {"name": "b"}])
    # A person refused "a" as a lab instance. A scan of rack alone, with --all, must not
    # allow it as a rack instance.
    shown = run_json(capsys, "--yes", "--all", "--kind", "rack")
    assert [item["name"] for item in shown["instances"]] == ["b"]
    assert load_inventory().kinds == {
        "lab": Decisions(refused=("a",)),
        "rack": Decisions(allowed=("b",)),
    }

