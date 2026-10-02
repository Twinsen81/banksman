import pytest

from banksman import assign as assign_module
from banksman.assign import Option, Part, Pick, TooComplex, assign


def part(*options, accounts=0):
    """A part whose options are resource names, or (resource, accounts) pairs."""
    return Part(
        tuple(
            Option(option) if isinstance(option, str) else Option(option[0], tuple(option[1]))
            for option in options
        ),
        accounts,
    )


def picks(*parts):
    result = assign(parts)
    return None if result is None else [(pick.resource, pick.accounts) for pick in result]


def test_each_part_takes_its_first_option_that_no_earlier_part_took():
    assert picks(part("a", "b"), part("a", "b", "c")) == [("a", ()), ("b", ())]


def test_an_earlier_part_takes_another_option_when_a_later_part_needs_its_first():
    assert picks(part("a", "b"), part("a")) == [("b", ()), ("a", ())]


def test_parts_that_cannot_all_be_met_together_get_nothing():
    assert picks(part("a"), part("a")) is None
    assert picks(part("a", "b"), part("a", "b"), part("a", "b")) is None
    assert picks(part("a"), part()) is None


def test_a_request_without_parts_is_met():
    assert assign([]) == []


def test_a_part_gets_the_accounts_on_its_resource_in_the_order_of_choice():
    assert picks(part(("h1", ["x", "y", "z"]), accounts=2)) == [("h1", ("x", "y"))]
    assert picks(part(("h1", ["x"]), ("h2", ["x", "y"]), accounts=2)) == [("h2", ("x", "y"))]
    assert picks(part(("h1", ["x"]), accounts=2)) is None


def test_an_account_goes_to_one_part_only():
    # The same account is signed in on both hosts, so only one part can get it.
    assert picks(part(("h1", ["x"]), accounts=1), part(("h2", ["x"]), accounts=1)) is None
    assert picks(part(("h1", ["x"]), accounts=1), part(("h2", ["x", "y"]), accounts=1)) == [
        ("h1", ("x",)),
        ("h2", ("y",)),
    ]


def test_an_account_on_several_hosts_goes_to_the_part_that_has_no_other():
    assert picks(part(("h1", ["x", "y"]), accounts=1), part(("h2", ["x"]), accounts=1)) == [
        ("h1", ("y",)),
        ("h2", ("x",)),
    ]


def test_a_part_without_accounts_can_take_a_host_with_accounts():
    assert picks(part(("h1", ["x"])), part(("h2", ["x"]), accounts=1)) == [
        ("h1", ()),
        ("h2", ("x",)),
    ]


def test_a_request_that_cannot_be_met_ends_quickly():
    # Seven resources for eight parts: without the matching check, the search would try every
    # order of the seven resources before it gave up.
    resources = [f"r{index}" for index in range(7)]
    assert picks(*[part(*resources) for _ in range(8)]) is None
    # Fifty hosts that share two accounts, for three parts that each need one.
    hosts = [(f"h{index}", ["x", "y"]) for index in range(50)]
    assert picks(*[part(*hosts, accounts=1) for _ in range(3)]) is None


def test_a_search_that_takes_too_long_is_an_error(monkeypatch):
    monkeypatch.setattr(assign_module, "MAX_SEARCH_SECONDS", -1)
    with pytest.raises(TooComplex, match="fewer parts"):
        assign([part("a"), part("b")])


def test_a_pick_names_its_resource_and_accounts():
    assert assign([part(("h1", ["x"]), accounts=1)]) == [Pick("h1", ("x",))]
