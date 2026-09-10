"""The scope document of a scoped API key (#88): four optional fields, an
absent field means none, unknown fields are rejected."""

from __future__ import annotations

import pytest

from mshkn.errors import InvalidInput
from mshkn.models import RelayDelivery, Scopes, parse_scopes

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


RELAY = {
    "relay": {
        "targets": ["https://api.anthropic.com/"],
        "deliver": {"label": "brain", "exec": "membrane resume"},
    }
}


def test_the_relay_section_pins_targets_and_one_delivery() -> None:
    scopes = parse_scopes({**BRAIN, **RELAY})
    assert scopes.relay_targets == ("https://api.anthropic.com/",)
    assert scopes.relay_deliver == RelayDelivery(label="brain", exec="membrane resume")
    assert scopes.has_relay
    assert scopes.may_relay_to("https://api.anthropic.com/v1/messages")
    assert not scopes.may_relay_to("https://api.anthropic.com.evil.example/v1/messages")
    assert not scopes.may_relay_to("http://api.anthropic.com/v1/messages")
    assert scopes.to_document() == {**BRAIN, **RELAY}


def test_without_the_section_a_key_may_relay_nowhere() -> None:
    scopes = parse_scopes(BRAIN)
    assert not scopes.has_relay and scopes.relay_deliver is None
    assert not scopes.may_relay_to("https://api.anthropic.com/v1/messages")


@pytest.mark.parametrize(
    "section",
    [
        {},
        {"targets": []},
        {"targets": ["https://a/"]},
        {"deliver": {"label": "brain", "exec": "membrane resume"}},
        {"targets": "https://a/", "deliver": {"label": "brain", "exec": "x"}},
        {"targets": ["https://a/"], "deliver": {"label": "", "exec": "x"}},
        {"targets": ["https://a/"], "deliver": {"label": "brain", "exec": ""}},
        {"targets": ["https://a/"], "deliver": {"label": "brain"}},
        {"targets": ["https://a/"], "deliver": {"label": "brain", "exec": "x", "extra": 1}},
        {"targets": ["https://a/"], "deliver": "brain"},
        {"targets": [""], "deliver": {"label": "brain", "exec": "x"}},
    ],
)
def test_rejects_a_malformed_relay_section(section: dict[str, object]) -> None:
    with pytest.raises(InvalidInput):
        parse_scopes({"relay": section})


DELIVER = {"label": "brain", "exec": "membrane resume"}


def _relay(*targets: str) -> Scopes:
    return parse_scopes({"relay": {"targets": list(targets), "deliver": DELIVER}})


def test_a_target_matches_a_prefix_by_origin_and_path_not_by_text() -> None:
    """A prefix without a trailing slash is the dangerous shape: textually,
    `https://api.anthropic.com.evil.example` starts with `https://api.anthropic.com`
    and would be called with the forwarded credentials."""
    scopes = _relay("https://api.anthropic.com")
    assert scopes.may_relay_to("https://api.anthropic.com/v1/messages")
    # The default port is the same origin written out.
    assert scopes.may_relay_to("https://api.anthropic.com:443/v1/messages")
    assert not scopes.may_relay_to("https://api.anthropic.com.evil.example/v1/messages")
    assert not scopes.may_relay_to("https://evil.example/?u=https://api.anthropic.com/v1")


def test_a_scheme_downgrade_a_port_change_and_a_path_outside_the_prefix_are_refused() -> None:
    scopes = _relay("https://api.anthropic.com/v1/")
    assert scopes.may_relay_to("https://api.anthropic.com/v1/messages")
    assert not scopes.may_relay_to("http://api.anthropic.com/v1/messages")
    assert not scopes.may_relay_to("https://api.anthropic.com:8443/v1/messages")
    assert not scopes.may_relay_to("https://api.anthropic.com/v2/messages")
    assert not scopes.may_relay_to("/v1/messages")
    assert not scopes.may_relay_to("https://api.anthropic.com:notaport/v1/messages")


@pytest.mark.parametrize(
    "target",
    [
        "api.anthropic.com/v1/",
        "/v1/messages",
        "ftp://api.anthropic.com/",
        "https:///v1/",
        "https://",
    ],
)
def test_rejects_a_relay_target_that_is_not_an_absolute_http_url(target: str) -> None:
    with pytest.raises(InvalidInput):
        _relay(target)
