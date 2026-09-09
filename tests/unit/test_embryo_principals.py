"""Public input never becomes root (spec §10.1); hooks fail closed (§6)."""

from __future__ import annotations

import pytest
from membrane.principals import ANONYMOUS, ROOT, is_authenticated, namespace_of, principal_from_hook


def test_a_hook_names_a_principal_in_its_own_namespace() -> None:
    assert principal_from_hook("ssh", "mike\n", 0) == "ssh:mike"
    assert principal_from_hook("ssh", "\n  mike \nextra", 0) == "ssh:mike"


@pytest.mark.parametrize(
    ("namespace", "stdout", "code"),
    [
        ("ssh", "", 0),
        ("ssh", "mike", 1),
        ("ssh", "root", 0),
        ("ssh", "system", 0),
        ("ssh", "root:mike", 0),
        ("ssh", "mike bob", 0),
        ("root", "mike", 0),
        ("system", "mike", 0),
    ],
)
def test_everything_else_is_anonymous(namespace: str, stdout: str, code: int) -> None:
    assert principal_from_hook(namespace, stdout, code) == ANONYMOUS


def test_authenticated_and_namespaces() -> None:
    assert is_authenticated(ROOT) and is_authenticated("ssh:mike")
    assert not is_authenticated(ANONYMOUS)
    assert namespace_of("ssh:mike") == "ssh"
    assert namespace_of(ROOT) is None and namespace_of(ANONYMOUS) is None
