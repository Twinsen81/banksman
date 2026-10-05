import pytest

from banksman.config import ANDROID_DEVICE, ANDROID_EMULATOR, Config, Kind
from banksman.discovery import parse
from banksman.errors import BanksmanError
from banksman.inventory import Decisions, Inventory
from banksman.request import Clause, Part, matches, parse_clause, search

EMULATORS = [
    {"name": "qa_tablet", "facts": {"form": "tablet", "api": 33, "running": False}},
    {
        "name": "qa_phone_b",
        "facts": {"form": "phone", "api": 35, "running": True, "serial": "emulator-5556"},
        "accounts": [],
    },
    {
        "name": "qa_phone_a",
        "facts": {"form": "phone", "api": 34, "running": True, "serial": "emulator-5554"},
        "accounts": ["qa@example.test", "someone@example.com"],
    },
    {"name": "Personal_AVD", "facts": {"form": "phone", "api": 35, "running": True}},
]
DEVICES = [
    {
        "name": "R5CR1234ABC",
        "facts": {"form": "phone", "api": 36, "manufacturer": "samsung", "model": "SM-S926B"},
        "accounts": ["someone@example.com"],
    },
    {"name": "R5CR5678DEF", "facts": {"form": "phone", "api": 34, "model": "Pixel 7"}},
]
CONFIG = Config(
    kinds={
        "emulator": Kind("emulator", preset=ANDROID_EMULATOR),
        "device": Kind("device", preset=ANDROID_DEVICE, rank=1),
        "build": Kind("build", count=2),
        "port": Kind("port", listed=("9101",)),
    }
)
INVENTORY = Inventory(
    kinds={
        "emulator": Decisions(
            allowed=("qa_phone_a", "qa_phone_b", "qa_tablet", "qa_gone"),
            refused=("Personal_AVD",),
        ),
        "device": Decisions(allowed=("R5CR1234ABC", "R5CR5678DEF")),
    },
    accounts=Decisions(allowed=("qa@example.test",), refused=("someone@example.com",)),
    tags={"lab": ("qa_phone_*", "R5CR5678DEF"), "slow": ("qa_tablet",)},
)


class FakeDiscovery:
    def __init__(self, instances=None):
        self.instances = {"emulator": EMULATORS, "device": DEVICES, **(instances or {})}
        self.kinds = []

    def __call__(self, kind, config):
        self.kinds.append(kind.name)
        notes = ["0A1B2C3D is unauthorized, so it is not offered"] if kind.name == "device" else []
        document = {"schema": 1, "instances": self.instances[kind.name], "notes": notes}
        return parse(kind.name, document)


def found(*clauses, config=CONFIG, inventory=INVENTORY, discovery=None):
    result = search(
        config,
        inventory,
        [Part(tuple(parse_clause(clause) for clause in clauses))],
        discover=discovery or FakeDiscovery(),
    )
    (candidates,) = result.candidates
    return [candidate.resource for candidate in candidates]


@pytest.mark.parametrize(
    ("text", "clause"),
    [
        ("form=tablet", Clause("form", "=", "tablet")),
        ("api>=33", Clause("api", ">=", "33")),
        ("api<=-1", Clause("api", "<=", "-1")),
        ("serial!=emulator-5554", Clause("serial", "!=", "emulator-5554")),
        ("model~SM-*", Clause("model", "~", "SM-*")),
        ("display_name=Pixel 7 API 35", Clause("display_name", "=", "Pixel 7 API 35")),
        ("note=a=b", Clause("note", "=", "a=b")),
    ],
)
def test_a_clause_is_an_attribute_an_operator_and_a_value(text, clause):
    assert parse_clause(text) == clause
    assert str(clause) == text


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        # A shell that is not told otherwise reads > as a redirect and passes only this.
        ("api", "an operator"),
        ("api>33", "an operator"),
        ("Form=tablet", "an operator"),
        ("=tablet", "an operator"),
        ("form=", "1 to 256 characters"),
        ("form=tab\x1b[2Jlet", "no control characters"),
        ("api>=thirty", "whole numbers only"),
        ("tag<=3", "whole numbers only"),
    ],
)
def test_a_clause_that_is_not_valid_is_refused(text, problem):
    with pytest.raises(ValueError, match=problem):
        parse_clause(text)


@pytest.mark.parametrize(
    ("clause", "facts", "expected"),
    [
        ("manufacturer=Samsung", {"manufacturer": "samsung"}, True),
        ("manufacturer!=SAMSUNG", {"manufacturer": "samsung"}, False),
        ("model~sm-s9*", {"model": "SM-S926B"}, True),
        ("model~Pixel*", {"model": "SM-S926B"}, False),
        ("api>=33", {"api": 35}, True),
        ("api>=33", {"api": 30}, False),
        ("api<=33", {"api": 33}, True),
        ("api=035", {"api": 35}, True),
        ("api!=35", {"api": 35}, False),
        ("api~3*", {"api": 35}, True),
        # A text fact is never a number, and true and false are not numbers either.
        ("api>=33", {"api": "35"}, False),
        ("running>=0", {"running": True}, False),
        ("running=TRUE", {"running": True}, True),
        ("running=false", {"running": True}, False),
        ("running=1", {"running": True}, False),
        # A fact that the resource does not have matches no clause.
        ("form=tablet", {}, False),
        ("form!=tablet", {}, False),
        ("form~*", {}, False),
    ],
)
def test_a_clause_compares_a_fact_by_its_type(clause, facts, expected):
    assert matches(parse_clause(clause), facts) is expected


@pytest.mark.parametrize(
    ("clause", "tags", "expected"),
    [
        ("tag=lab", ("lab", "slow"), True),
        ("tag=LAB", ("lab",), True),
        ("tag~l*", ("lab",), True),
        ("tag!=lab", ("lab", "slow"), False),
        ("tag!=lab", ("slow",), True),
        ("tag!=lab", (), True),
        ("tag=lab", (), False),
    ],
)
def test_a_tag_clause_asks_whether_the_resource_has_the_tag(clause, tags, expected):
    assert matches(parse_clause(clause), {}, tags) is expected


def test_only_allowed_instances_that_are_present_now_match():
    assert found("form=phone") == ["qa_phone_a", "qa_phone_b", "R5CR1234ABC", "R5CR5678DEF"]
    # A refused instance is never granted, also when the request names it.
    assert found("avd=Personal_AVD") == []
    assert found("serial=emulator-5554") == ["qa_phone_a"]


def test_an_allowed_instance_that_is_not_present_is_reported_not_offered():
    part = Part((parse_clause("kind=emulator"),))
    result = search(CONFIG, INVENTORY, [part], discover=FakeDiscovery())
    assert "qa_gone" not in [candidate.resource for candidate in result.candidates[0]]
    assert ("emulator", "qa_gone") in result.absent


def test_the_notes_of_discovery_name_their_kind():
    result = search(CONFIG, INVENTORY, [Part()], discover=FakeDiscovery())
    assert "kind device: 0A1B2C3D is unauthorized, so it is not offered" in result.notes


def test_without_an_inventory_no_discovered_instance_matches():
    assert found(inventory=Inventory()) == ["9101", "build-0", "build-1"]


def test_counted_and_listed_kinds_need_no_inventory_and_no_discovery():
    discovery = FakeDiscovery()
    assert found("kind=build", discovery=discovery) == ["build-0", "build-1"]
    assert found("kind~p*", discovery=discovery) == ["9101"]
    assert discovery.kinds == []


def test_a_request_for_a_kind_that_is_not_declared_is_an_error():
    with pytest.raises(BanksmanError, match="declares no kind phone; the kinds are: build"):
        found("kind=phone")


def test_an_unknown_attribute_is_an_error_not_an_empty_match():
    with pytest.raises(BanksmanError, match="unknown attribute colour"):
        found("colour=red")
    # The facts of the presets are known also when no instance has them now.
    assert found("display_name=Pixel 7 API 35") == []
    # A fact that a discover hook gives is known while an instance has it.
    discovery = FakeDiscovery({"emulator": [{"name": "qa_phone_a", "facts": {"colour": "red"}}]})
    assert found("colour=red", discovery=discovery) == ["qa_phone_a"]


def test_an_attribute_of_a_kind_that_the_request_leaves_out_is_unknown():
    discovery = FakeDiscovery({"emulator": [{"name": "qa_phone_a", "facts": {"colour": "red"}}]})
    with pytest.raises(BanksmanError, match="unknown attribute colour"):
        found("kind=build", "colour=red", discovery=discovery)


def test_the_account_fact_says_whether_an_allowed_account_is_signed_in():
    assert found("account=true") == ["qa_phone_a"]
    assert found("account=false") == ["qa_phone_b", "R5CR1234ABC"]
    # The accounts of the others are not known, so neither clause matches them.
    assert "R5CR5678DEF" not in found("account!=true")


def test_a_hook_cannot_say_that_an_allowed_account_is_signed_in():
    instances = {"emulator": [{"name": "qa_tablet", "facts": {"account": True}}]}
    assert found("account=true", discovery=FakeDiscovery(instances)) == []


def test_tags_come_from_the_inventory():
    assert found("tag=lab") == ["qa_phone_a", "qa_phone_b", "R5CR5678DEF"]
    assert found("tag=slow") == ["qa_tablet"]
    assert found("kind=emulator", "tag!=slow") == ["qa_phone_a", "qa_phone_b"]


def test_the_order_of_choice_is_rank_then_running_then_name():
    # Emulators before physical devices, and among them, one that runs before a cold one.
    assert found("api>=33") == [
        "qa_phone_a",
        "qa_phone_b",
        "qa_tablet",
        "R5CR1234ABC",
        "R5CR5678DEF",
    ]
    # A request for a physical device gets one.
    assert found("kind=device", "api>=33") == ["R5CR1234ABC", "R5CR5678DEF"]


def test_a_name_that_another_kind_declares_is_not_an_instance_of_a_discovered_kind():
    instances = {"emulator": [{"name": "9101", "facts": {}}]}
    inventory = Inventory(kinds={"emulator": Decisions(allowed=("9101",))})
    result = search(CONFIG, inventory, [Part()], discover=FakeDiscovery(instances))
    assert [(candidate.resource, candidate.kind) for candidate in result.candidates[0]] == [
        ("9101", "port"),
        ("build-0", "build"),
        ("build-1", "build"),
    ]


def test_each_part_of_a_request_has_its_own_matches():
    parts = [Part((parse_clause("form=tablet"),), "tablet"), Part((parse_clause("kind=build"),))]
    result = search(CONFIG, INVENTORY, parts, discover=FakeDiscovery())
    assert [[candidate.resource for candidate in each] for each in result.candidates] == [
        ["qa_tablet"],
        ["build-0", "build-1"],
    ]


def test_discovery_runs_once_for_each_kind_of_a_request():
    discovery = FakeDiscovery()
    parts = [Part((parse_clause("form=phone"),)), Part((parse_clause("form=tablet"),))]
    search(CONFIG, INVENTORY, parts, discover=discovery)
    assert sorted(discovery.kinds) == ["device", "emulator"]


def test_a_candidate_has_the_allowed_accounts_that_are_signed_in_on_it_now():
    result = search(CONFIG, INVENTORY, [Part()], discover=FakeDiscovery())
    accounts = {candidate.resource: candidate.accounts for candidate in result.candidates[0]}
    assert accounts["qa_phone_a"] == ("qa@example.test",)
    assert accounts["qa_phone_b"] == ()
    # Not known: the instance did not list its accounts, or its kind has none.
    assert (accounts["R5CR5678DEF"], accounts["build-0"]) == (None, None)


def test_a_part_that_needs_accounts_matches_only_resources_with_that_many_allowed_accounts():
    def matching(accounts):
        result = search(CONFIG, INVENTORY, [Part(accounts=accounts)], discover=FakeDiscovery())
        return [candidate.resource for candidate in result.candidates[0]]

    assert matching(1) == ["qa_phone_a"]
    assert matching(2) == []


def test_each_part_is_checked_against_the_facts_of_its_own_kinds():
    discovery = FakeDiscovery({"emulator": [{"name": "qa_phone_a", "facts": {"colour": "red"}}]})
    parts = [
        Part((parse_clause("kind=emulator"),)),
        Part((parse_clause("kind=build"), parse_clause("colour=red"))),
    ]
    with pytest.raises(BanksmanError, match="unknown attribute colour"):
        search(CONFIG, INVENTORY, parts, discover=discovery)
