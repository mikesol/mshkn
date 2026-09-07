"""The two endpoints that answer with more than one status code document both."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tests.unit.conftest import make_app, make_runtime

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.config import Config


def _model(response: dict[str, Any]) -> str:
    ref: str = response["content"]["application/json"]["schema"]["$ref"]
    return ref.rsplit("/", 1)[-1]


async def test_two_status_endpoints_declare_both_response_schemas(
    db: aiosqlite.Connection, runtime_config: Config
) -> None:
    schema = make_app(make_runtime(db, config=runtime_config)).openapi()
    fork = schema["paths"]["/checkpoints/{checkpoint_id}/fork"]["post"]["responses"]
    assert _model(fork["200"]) == "ForkResponse"
    assert _model(fork["202"]) == "DeferredResponse"
    recipes = schema["paths"]["/recipes"]["post"]["responses"]
    assert _model(recipes["202"]) == "RecipeResponse"
    assert _model(recipes["200"]) == "RecipeResponse"


async def test_operation_ids_are_unique(db: aiosqlite.Connection, runtime_config: Config) -> None:
    app = make_app(make_runtime(db, config=runtime_config))
    ids = [
        op["operationId"]
        for path in app.openapi()["paths"].values()
        for op in path.values()
        if isinstance(op, dict) and "operationId" in op
    ]
    assert len(ids) == len(set(ids)), sorted(i for i in ids if ids.count(i) > 1)
