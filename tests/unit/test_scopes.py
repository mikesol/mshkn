"""The scope document of a scoped API key (#88): three optional fields, an
absent field means none, unknown fields are rejected."""

from __future__ import annotations

import pytest

from mshkn.errors import InvalidInput
from mshkn.models import Scopes, parse_scopes

BRAIN = {
    "recipes": {"create": True, "read": True},
    "computers": {"create_from": "*"},
    "labels": ["verb/"],
}


def test_parses_the_brain_key_document() -> None:
    scopes = parse_scopes(BRAIN)
    assert scopes == Scopes(
        recipes_create=True, recipes_read=True, create_from="*", labels=("verb/",)
    )
    assert scopes.to_document() == BRAIN


def test_an_empty_document_allows_nothing() -> None:
    scopes = parse_scopes({})
    assert scopes == Scopes()
    assert not scopes.recipes_create and not scopes.recipes_read
    assert not scopes.may_create_from(None)
    assert not scopes.may_create_from("rcp-1")
    assert not scopes.covers_label("verb/x")
    assert scopes.to_document() == {}


@pytest.mark.parametrize(
    "document",
    [
        {"pets": True},
        {"recipes": {"delete": True}},
        {"computers": {"exec": True}},
        {"recipes": "yes"},
        {"computers": {"create_from": 1}},
        {"computers": {"create_from": ["rcp-1", 2]}},
        {"labels": "verb/"},
        {"labels": [1]},
        {"recipes": {"create": "yes"}},
        [],
    ],
)
def test_rejects_unknown_fields_and_wrong_shapes(document: object) -> None:
    with pytest.raises(InvalidInput):
        parse_scopes(document)


def test_create_from_star_allows_any_recipe_but_not_bare() -> None:
    scopes = parse_scopes({"computers": {"create_from": "*"}})
    assert scopes.may_create_from("rcp-1")
    assert not scopes.may_create_from(None)


def test_create_from_list_names_recipes_and_bare() -> None:
    scopes = parse_scopes({"computers": {"create_from": ["rcp-1", "bare"]}})
    assert scopes.may_create_from("rcp-1")
    assert scopes.may_create_from(None)
    assert not scopes.may_create_from("rcp-2")
    assert scopes.to_document() == {"computers": {"create_from": ["rcp-1", "bare"]}}


def test_labels_are_prefixes_and_unlabelled_is_never_covered() -> None:
    scopes = parse_scopes({"labels": ["verb/", "tool-"]})
    assert scopes.covers_label("verb/x")
    assert scopes.covers_label("verb/")
    assert scopes.covers_label("tool-a")
    assert not scopes.covers_label("brain")
    assert not scopes.covers_label("xverb/")
    assert not scopes.covers_label(None)
    assert not scopes.covers_label("")


def test_an_empty_prefix_is_rejected() -> None:
    with pytest.raises(InvalidInput):
        parse_scopes({"labels": [""]})
