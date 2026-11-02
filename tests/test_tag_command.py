"""Tags of the operator: admin tag, admin untag, and the note of discover about devices without
a tag."""

import json

import pytest
from helpers import hook, write_config

from banksman import android
from banksman.cli import main
from banksman.inventory import (
    TAG_RULE,
    Decisions,
    Inventory,
    InventoryError,
    add_tags,
    load_inventory,
    remove_tags,
    save_inventory,
)

PRINT_ARGUMENT = "import sys; print(sys.argv[1])"
ALLOWED = Inventory(
    kinds={"device": Decisions(allowed=("R5CR1234ABC", "R5CR5678DEF"), refused=("R5CR0000PER",))}
)


def tag(capsys, *arguments, status=0):
    assert main(["admin", *arguments]) == status
    return capsys.readouterr()


def test_tag_adds_each_pattern_to_its_tag_and_keeps_the_rest(capsys, config_path, inventory_path):
    write_config(config_path, "")
    save_inventory(ALLOWED)
    out = tag(capsys, "tag", "s24plus=R5CR1234ABC", "lab=R5CR*", "lab=R5CR5678DEF").out
    assert out.splitlines() == [
        "Tag s24plus: R5CR1234ABC",
        "Tag lab: R5CR*, R5CR5678DEF",
        f"Wrote {inventory_path}.",
    ]
    inventory = load_inventory()
    assert inventory.tags == {"s24plus": ("R5CR1234ABC",), "lab": ("R5CR*", "R5CR5678DEF")}
    assert inventory.kinds == ALLOWED.kinds
    assert inventory.tags_of("R5CR1234ABC") == ("lab", "s24plus")


def test_a_tag_that_is_there_already_writes_nothing(capsys, config_path, inventory_path):
    write_config(config_path, "")
    save_inventory(Inventory(kinds=ALLOWED.kinds, tags={"s24plus": ("R5CR1234ABC",)}))
    before = inventory_path.stat()
    out = tag(capsys, "tag", "s24plus=R5CR1234ABC").out
    assert out.splitlines()[-1] == "The inventory has these tags already. Nothing was written."
    # A write replaces the file.
    assert inventory_path.stat().st_ino == before.st_ino


def test_a_pattern_that_matches_no_permitted_resource_is_a_warning(capsys, config_path):
    # A refused name, and a typing error in a serial number, match nothing that agents may use.
    # The instances that the configuration declares are permitted without the inventory.
    write_config(config_path, '[kinds.port]\ninstances = ["9101", "9102"]\n')
    save_inventory(ALLOWED)
    out = tag(capsys, "tag", "s24plus=R5CR1234ABX", "personal=R5CR0000PER", "ports=91*").out
    assert [line for line in out.splitlines() if line.startswith("Warning")] == [
        "Warning: the pattern R5CR1234ABX of the tag s24plus matches no resource that agents"
        " may use.",
        "Warning: the pattern R5CR0000PER of the tag personal matches no resource that agents"
        " may use.",
    ]
    assert set(load_inventory().tags) == {"s24plus", "personal", "ports"}


@pytest.mark.parametrize(
    ("argument", "problem"),
    [
        ("S24=R5CR1234ABC", TAG_RULE),
        ("s24plus", "write the tag and the pattern as TAG=PATTERN"),
        ("s24plus=", "a name pattern has 1 to 256 characters"),
        ("s24plus=R5CR\x1b[31m", "a name pattern has 1 to 256 characters"),
    ],
)
def test_a_tag_or_a_pattern_that_is_not_valid_is_refused(
    capsys, config_path, inventory_path, argument, problem
):
    write_config(config_path, "")
    save_inventory(ALLOWED)
    before = inventory_path.read_text()
    with pytest.raises(SystemExit) as exited:
        main(["admin", "tag", argument])
    assert exited.value.code == 2
    assert problem in capsys.readouterr().err
    assert inventory_path.read_text() == before


def test_untag_removes_a_tag_or_one_pattern_of_it(capsys, inventory_path):
    tags = {"lab": ("R5CR*", "qa_*"), "s24plus": ("R5CR1234ABC",)}
    save_inventory(Inventory(kinds=ALLOWED.kinds, tags=tags))
    out = tag(capsys, "untag", "lab=qa_*", "s24plus").out
    assert out.splitlines() == [
        "Tag lab: R5CR*",
        "Tag s24plus: removed",
        f"Wrote {inventory_path}.",
    ]
    assert load_inventory().tags == {"lab": ("R5CR*",)}
    # A tag without patterns is gone.
    tag(capsys, "untag", "lab=R5CR*")
    assert load_inventory().tags == {}
    assert load_inventory().kinds == ALLOWED.kinds


@pytest.mark.parametrize(
    ("argument", "error"),
    [("s26", "the inventory has no tag s26"), ("lab=qa_*", "the tag lab has no pattern 'qa_*'")],
)
def test_untag_of_a_tag_that_is_not_there_writes_nothing(capsys, inventory_path, argument, error):
    save_inventory(Inventory(tags={"lab": ("R5CR*",), "s24plus": ("R5CR1234ABC",)}))
    before = inventory_path.read_text()
    # The whole command fails, also the removal that would work.
    assert tag(capsys, "untag", "s24plus", argument, status=1).err == f"banksman: {error}\n"
    assert inventory_path.read_text() == before


def test_the_functions_check_the_tags_as_the_file_does():
    assert add_tags(Inventory(), [("lab", "qa_*"), ("lab", "qa_*")]).tags == {"lab": ("qa_*",)}
    with pytest.raises(InventoryError, match="tags.Lab: a tag starts with a lowercase letter"):
        add_tags(Inventory(), [("Lab", "qa_*")])
    with pytest.raises(InventoryError, match="must be a list of 1 to 100 name patterns"):
        add_tags(Inventory(), [("lab", f"qa_{number}") for number in range(101)])
    assert remove_tags(Inventory(tags={"lab": ("qa_*",)}), [("lab", None)]) == Inventory()


def test_a_request_asks_for_a_tag_without_regard_to_case(capsys, config_path):
    found = json.dumps(
        {
            "schema": 1,
            "instances": [
                {"name": "R5CR1234ABC", "facts": {"manufacturer": "samsung", "model": "SM-S926B"}},
                {"name": "R5CR5678DEF", "facts": {"manufacturer": "Google", "model": "Pixel 7"}},
            ],
        }
    )
    write_config(
        config_path,
        "[holder]\nagents = []\n"
        f"[kinds.device]\ndiscover = {json.dumps(hook(PRINT_ARGUMENT, found))}\n",
    )
    save_inventory(ALLOWED)
    tag(capsys, "tag", "s24plus=R5CR1234ABC")
    assert main(["acquire", "--where", "tag=S24PLUS"]) == 0
    assert "RESOURCE=R5CR1234ABC" in capsys.readouterr().out.splitlines()


def devices(monkeypatch, *names):
    instances = [{"name": name, "facts": {"model": "SM-S926B"}, "accounts": []} for name in names]
    monkeypatch.setattr(
        android, "devices", lambda settings, accounts: {"schema": 1, "instances": instances}
    )


def summary(out):
    """Return the summary of discover, with each wrapped sentence on one line."""
    return out.split("\n\n")[-1].replace("\n  ", " ")


def test_discover_names_the_devices_without_a_tag(capsys, config_path, monkeypatch):
    write_config(config_path, '[kinds.device]\npreset = "android-device"\npreselect = ["*"]\n')
    devices(monkeypatch, "R5CR1234ABC", "R5CR5678DEF", "R5CR9999XYZ")
    save_inventory(Inventory(tags={"s24plus": ("R5CR1234ABC",)}))
    assert main(["admin", "discover", "--yes"]) == 0
    assert (
        "R5CR5678DEF, R5CR9999XYZ (kind device) have no tag. Give each device a tag with the name"
        " that people use for it, so that an agent can ask for it with --where tag=<tag>:"
        " banksman admin tag <tag>=R5CR5678DEF"
    ) in summary(capsys.readouterr().out)
    tag(capsys, "tag", "s24=R5CR5678DEF")
    assert main(["admin", "discover", "--yes"]) == 0
    assert "R5CR9999XYZ (kind device) has no tag." in summary(capsys.readouterr().out)
    tag(capsys, "tag", "s24ultra=R5CR9999XYZ")
    assert main(["admin", "discover", "--yes"]) == 0
    assert "no tag" not in capsys.readouterr().out


def test_discover_does_not_ask_for_tags_of_other_kinds(capsys, config_path):
    # An AVD, or an instance of a discover hook, has a name that its operator chose.
    found = json.dumps({"schema": 1, "instances": [{"name": "qa_phone_api35", "accounts": []}]})
    write_config(
        config_path,
        f"[kinds.lab]\ndiscover = {json.dumps(hook(PRINT_ARGUMENT, found))}\npreselect = ['*']\n",
    )
    assert main(["admin", "discover", "--yes"]) == 0
    assert "no tag" not in capsys.readouterr().out
