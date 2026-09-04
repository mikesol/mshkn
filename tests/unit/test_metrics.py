from httpx import ASGITransport, AsyncClient

from mshkn.main import app


async def test_metrics_endpoint_returns_200() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"] or "text/plain" in resp.headers.get(
        "content-type", ""
    )


async def test_metrics_contains_expected_names() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics")
    text = resp.text
    assert "mshkn_computers_active" in text
    assert "mshkn_computers_created_total" in text
    assert "mshkn_checkpoints_total" in text
    assert "mshkn_exec_duration_seconds" in text
    assert "# HELP" in text
    assert "# TYPE" in text
