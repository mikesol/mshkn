from __future__ import annotations

import json

import httpx
import pytest

from mshkn.host.firecracker import BOOT_ARGS, FirecrackerClient, FirecrackerConfig


def _recording_transport(
    status: int = 204,
) -> tuple[httpx.MockTransport, list[tuple[str, str, dict[str, object]]]]:
    seen: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, json.loads(request.content or b"{}")))
        return httpx.Response(status)

    return httpx.MockTransport(handler), seen


async def test_configure_and_boot_issues_the_five_puts_in_order() -> None:
    transport, seen = _recording_transport()
    client = FirecrackerClient("/tmp/x.socket", transport=transport)
    await client.configure_and_boot(
        FirecrackerConfig(
            socket_path="/tmp/x.socket",
            kernel_path="/k",
            rootfs_path="/dev/mapper/d",
            tap_device="tap254",
            guest_mac="06:00:AC:10:FE:02",
            vcpu_count=4,
            mem_size_mib=1024,
        )
    )
    await client.close()
    assert [(m, p) for m, p, _ in seen] == [
        ("PUT", "/machine-config"),
        ("PUT", "/boot-source"),
        ("PUT", "/drives/rootfs"),
        ("PUT", "/network-interfaces/eth0"),
        ("PUT", "/actions"),
    ]
    assert seen[0][2] == {"vcpu_count": 4, "mem_size_mib": 1024}
    assert seen[1][2] == {"kernel_image_path": "/k", "boot_args": BOOT_ARGS}
    assert seen[2][2] == {
        "drive_id": "rootfs",
        "path_on_host": "/dev/mapper/d",
        "is_root_device": True,
        "is_read_only": False,
    }
    assert seen[3][2] == {
        "iface_id": "eth0",
        "guest_mac": "06:00:AC:10:FE:02",
        "host_dev_name": "tap254",
    }
    assert seen[4][2] == {"action_type": "InstanceStart"}


async def test_snapshot_calls_and_pause_resume() -> None:
    transport, seen = _recording_transport()
    client = FirecrackerClient("/tmp/x.socket", transport=transport)
    await client.pause()
    await client.create_snapshot("/s/vmstate", "/s/memory")
    await client.resume()
    await client.load_snapshot("/s/vmstate", "/s/memory", resume_vm=True)
    await client.close()
    assert [(m, p) for m, p, _ in seen] == [
        ("PATCH", "/vm"),
        ("PUT", "/snapshot/create"),
        ("PATCH", "/vm"),
        ("PUT", "/snapshot/load"),
    ]
    assert seen[0][2] == {"state": "Paused"}
    assert seen[2][2] == {"state": "Resumed"}
    assert seen[1][2] == {
        "snapshot_type": "Full",
        "snapshot_path": "/s/vmstate",
        "mem_file_path": "/s/memory",
    }
    assert seen[3][2] == {
        "snapshot_path": "/s/vmstate",
        "mem_backend": {"backend_type": "File", "backend_path": "/s/memory"},
        "resume_vm": True,
    }


async def test_non_2xx_raises_http_status_error() -> None:
    transport, _ = _recording_transport(status=400)
    client = FirecrackerClient("/tmp/x.socket", transport=transport)
    with pytest.raises(httpx.HTTPStatusError):
        await client.pause()
    await client.close()
