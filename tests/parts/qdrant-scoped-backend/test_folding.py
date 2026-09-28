from __future__ import annotations

import itertools

import pytest

from qdrant_scoped_backend import build_scoped_tag, canonicalize_scope, fold_agent_name


def test_scope_canonicalization_is_order_independent_and_exact() -> None:
    left = {"project_id": "案件:1|2", "company_id": "会社=甲;乙"}
    right = {"company_id": "会社=甲;乙", "project_id": "案件:1|2"}

    assert canonicalize_scope(left) == canonicalize_scope(right)
    assert canonicalize_scope(left) == "10:company_id=6:会社=甲;乙;10:project_id=6:案件:1|2;"


def test_adversarial_scope_values_do_not_collide() -> None:
    values = [":", "|", ";", "=", ",", "\u200b", "区画Ｘ", "区画X🚀"]
    triples = [
        (namespace, agent_name, {"partition_id": value})
        for namespace, agent_name, value in itertools.product(
            ["ns", "n:s"],
            ["AG-A", "AG|A"],
            values,
        )
    ]
    tags = [build_scoped_tag(namespace, agent_name, scope) for namespace, agent_name, scope in triples]

    assert len(tags) == len(set(tags))


def test_same_partition_for_different_agents_has_different_tag() -> None:
    scope = {"partition_id": "区画X"}

    assert build_scoped_tag("customer", "AG-A", scope) != build_scoped_tag("customer", "AG-B", scope)


def test_length_prefix_prevents_separator_boundary_collisions() -> None:
    first = build_scoped_tag("hr", "AG:A", {"partition_id": "x|y;z=1,2"})
    second = build_scoped_tag("hr:AG", "A", {"partition_id": "x|y;z=1,2"})

    assert first != second
    assert fold_agent_name("AG:A", {"partition_id": "x|y;z=1,2"}).startswith("4:AG:A|")


def test_naive_agent_and_scope_separator_collision_pair_stays_distinct() -> None:
    first = build_scoped_tag("customer", "AG", {"partition_id": "A|B"})
    second = build_scoped_tag("customer", "AG|A", {"partition_id": "B"})

    assert first != second


def test_naive_scope_key_value_separator_collision_pair_stays_distinct() -> None:
    first = build_scoped_tag("customer", "AG", {"a": "b;c=d"})
    second = build_scoped_tag("customer", "AG", {"a": "b", "c": "d"})

    assert first != second


@pytest.mark.parametrize(
    "scope",
    [
        {},
        {"partition_id": ""},
        {"partition_id": " "},
        {"partition_id": " leading"},
        {"partition_id": "line\nbreak"},
        {"bad-key": "x"},
    ],
)
def test_invalid_scope_fails_closed(scope: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="scope"):
        canonicalize_scope(scope)
