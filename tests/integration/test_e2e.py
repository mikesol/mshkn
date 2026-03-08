"""
End-to-end test against running orchestrator.

Run with: MSHKN_URL=http://localhost:8000 MSHKN_API_KEY=<key> pytest tests/integration/ -v
"""
import os

import httpx
import pytest


@pytest.fixture
def client() -> httpx.Client:
    url = os.environ.get("MSHKN_URL", "http://localhost:8000")
    key = os.environ.get("MSHKN_API_KEY", "")
    return httpx.Client(
        base_url=url,
        headers={"Authorization": f"Bearer {key}"},
        timeout=60.0,
    )


def test_full_lifecycle(client: httpx.Client) -> None:
    # Create
    resp = client.post("/computers", json={"uses": []})
    assert resp.status_code == 200
    data = resp.json()
    computer_id = data["computer_id"]
    assert computer_id.startswith("comp-")

    # Exec
    resp = client.post(f"/computers/{computer_id}/exec", json={"command": "echo hello"})
    assert resp.status_code == 200

    # Checkpoint
    resp = client.post(f"/computers/{computer_id}/checkpoint", json={"label": "test"})
    assert resp.status_code == 200
    ckpt_id = resp.json()["checkpoint_id"]

    # Fork
    resp = client.post(f"/checkpoints/{ckpt_id}/fork")
    assert resp.status_code == 200
    fork_id = resp.json()["computer_id"]

    # Destroy both
    client.delete(f"/computers/{fork_id}")
    client.delete(f"/computers/{computer_id}")

    # List checkpoints
    resp = client.get("/checkpoints")
    assert resp.status_code == 200
