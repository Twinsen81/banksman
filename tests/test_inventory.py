import os
import pwd
import stat
import tomllib
from pathlib import Path

import pytest

from banksman.inventory import (
    Decisions,
    Inventory,
    InventoryError,
    decide,
    default_inventory_path,
    load_inventory,
    preselected,
    save_inventory,
    to_toml,
)

INVENTORY = Inventory(
    kinds={
        "emulator": Decisions(
            allowed=("qa_phone_api35", "qa_tablet_api33"), refused=("Pixel_7",)
        ),
        "device": Decisions(allowed=("R5CR1234ABC",)),
    },
    accounts=Decisions(allowed=("qa+1@example.test",), refused=("someone@example.com",)),
    # Patterns are any text: they are written so that TOML reads them back unchanged.
    tags={"lab": ("qa_*", "R5CR1234ABC"), "odd-1": ('Pixel_"7\\*', "Téléphone 📱*")},
)


def test_without_a_file_agents_may_use_no_discovered_instance(inventory_path):
    inventory = load_inventory()
    assert inventory == Inventory()
    assert not inventory.allows("emulator", "qa_phone_api35")


def test_the_inventory_is_written_and_read_back(inventory_path):
    assert save_inventory(INVENTORY) == inventory_path
    assert load_inventory() == INVENTORY
    assert stat.S_IMODE(inventory_path.stat().st_mode) == 0o600
    assert INVENTORY.allows("emulator", "qa_phone_api35")
    assert not INVENTORY.allows("emulator", "Pixel_7")
    assert not INVENTORY.allows("device", "qa_phone_api35")
    assert INVENTORY.allows_account("qa+1@example.test")
    assert not INVENTORY.allows_account("someone@example.com")


def test_the_file_is_toml_that_a_person_can_read(inventory_path):
    text = to_toml(INVENTORY)
    assert text.startswith("# The discovered resources that agents may use on this machine.")
    names = '[\n    "qa_phone_api35",\n    "qa_tablet_api33",\n]'
    assert f"[kinds.emulator]\nallowed = {names}" in text
    assert tomllib.loads(text)["accounts"]["refused"] == ["someone@example.com"]


def test_tags_name_resources_by_patterns(inventory_path):
    assert INVENTORY.tags_of("qa_phone_api35") == ("lab",)
    assert INVENTORY.tags_of("R5CR1234ABC") == ("lab",)
    assert INVENTORY.tags_of("Pixel_7") == ()
    text = to_toml(INVENTORY)
    assert '[tags]\nlab = [\n    "qa_*",\n    "R5CR1234ABC",\n]' in text
    assert tomllib.loads(to_toml(Inventory()))["tags"] == {}


def test_the_directory_is_created_private(tmp_path, monkeypatch):
    path = tmp_path / "config" / "banksman" / "inventory.toml"
    save_inventory(Inventory(), path)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert load_inventory(path) == Inventory()


def test_the_path_is_next_to_a_configuration_for_tests_and_otherwise_in_the_home(monkeypatch):
    monkeypatch.delenv("BANKSMAN_INVENTORY", raising=False)
    monkeypatch.setenv("BANKSMAN_CONFIG", "/tmp/test/config.toml")
    assert default_inventory_path() == Path("/tmp/test/inventory.toml")
    monkeypatch.delenv("BANKSMAN_CONFIG")
    monkeypatch.setenv("HOME", "/somewhere/else")
    home = pwd.getpwuid(os.geteuid()).pw_dir
    assert default_inventory_path() == Path(home) / ".config" / "banksman" / "inventory.toml"


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("[kind.emulator]\nallowed = []", "kind"),
        ("kinds = 3", "kinds"),
        ("[kinds.Emulator]\nallowed = []", "kinds.Emulator"),
        ("[kinds.emulator]\nallow = []", "kinds.emulator.allow"),
        ("[kinds.emulator]\nallowed = 'qa'", "kinds.emulator.allowed"),
        ("[kinds.emulator]\nallowed = ['../qa']", "kinds.emulator.allowed"),
        ("[kinds.emulator]\nallowed = ['a']\nrefused = ['a']", "kinds.emulator"),
        ("accounts = []", "accounts"),
        ("[accounts]\nrefused = ['a b']", "accounts.refused"),
        ("tags = []", "tags"),
        ("[tags]\nLab = ['qa_*']", "tags.Lab"),
        ("[tags]\nlab = 'qa_*'", "tags.lab"),
        ("[tags]\nlab = []", "tags.lab"),
        ("[tags]\nlab = ['']", "tags.lab"),
        ('[tags]\nlab = ["qa\\u001b*"]', "tags.lab"),
    ],
)
def test_an_inventory_that_is_not_valid_is_refused(inventory_path, text, key):
    inventory_path.write_text(text)
    inventory_path.chmod(0o600)
    with pytest.raises(InventoryError) as exc:
        load_inventory()
    assert str(exc.value).startswith(f"{inventory_path}: {key}: ")


def test_an_inventory_that_others_can_write_to_is_refused(inventory_path):
    save_inventory(INVENTORY)
    inventory_path.chmod(0o666)
    with pytest.raises(InventoryError, match="can write"):
        load_inventory()


def test_an_inventory_of_another_user_is_refused(inventory_path, monkeypatch):
    save_inventory(INVENTORY)
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(InventoryError, match="another user"):
        load_inventory()


def test_an_inventory_that_is_not_toml_is_refused(inventory_path):
    inventory_path.write_text("[kinds.emulator\n")
    inventory_path.chmod(0o600)
    with pytest.raises(InventoryError, match=str(inventory_path)):
        load_inventory()


DECISIONS = Decisions(allowed=("a",), refused=("r",))


@pytest.mark.parametrize(
    ("name", "patterns", "select_all", "expected"),
    [
        ("a", (), False, True),
        ("r", ("*",), True, False),
        ("qa_phone", ("qa_*",), False, True),
        ("QA_phone", ("qa_*",), False, False),
        ("Pixel_7", ("qa_*",), False, False),
        ("Pixel_7", (), True, True),
    ],
)
def test_a_refusal_wins_over_patterns_and_all(name, patterns, select_all, expected):
    assert preselected(DECISIONS, name, patterns, select_all) is expected


def test_a_person_who_leaves_a_name_unselected_refuses_it():
    after = decide(DECISIONS, ["a", "r", "new", "other"], {"r", "new"}, refuse_unselected=True)
    assert after == Decisions(allowed=("new", "r"), refused=("a", "other"))


def test_without_a_person_an_unselected_name_gets_no_decision():
    after = decide(DECISIONS, ["a", "r", "new", "other"], {"a", "new"}, refuse_unselected=False)
    assert after == Decisions(allowed=("a", "new"), refused=("r",))


def test_a_name_that_was_not_seen_keeps_its_decision():
    assert decide(DECISIONS, [], set(), refuse_unselected=True) == DECISIONS

def test_a_name_belongs_to_only_one_kind(tmp_path):
    path = tmp_path / "inventory.toml"
    path.write_text("[kinds.lab]\nallowed = ['a']\n[kinds.rack]\nrefused = ['a']\n")
    path.chmod(0o600)
    with pytest.raises(InventoryError, match="kinds.rack: 'a' is also listed under kinds.lab"):
        load_inventory(path)
    both = Inventory(kinds={"lab": Decisions(allowed=("a",)), "rack": Decisions(allowed=("a",))})
    with pytest.raises(InventoryError, match="'a' is also listed under kinds.lab"):
        save_inventory(both, path)

