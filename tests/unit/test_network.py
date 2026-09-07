from __future__ import annotations

import pytest

from mshkn.host.network import (
    create_tap,
    destroy_tap,
    slot_to_ip,
    slot_to_mac,
    slot_to_tap,
    tap_exists,
)
from mshkn.host.shell import ShellError
from tests.support import ShellRecorder


def test_slot_to_ip() -> None:
    assert slot_to_ip(0) == ("172.16.0.1", "172.16.0.2")
    assert slot_to_ip(5) == ("172.16.5.1", "172.16.5.2")
    assert slot_to_ip(255) == ("172.16.255.1", "172.16.255.2")


def test_slot_to_mac() -> None:
    assert slot_to_mac(0) == "06:00:AC:10:00:02"
    assert slot_to_mac(5) == "06:00:AC:10:05:02"
    assert slot_to_mac(255) == "06:00:AC:10:FF:02"


def test_slot_to_tap() -> None:
    assert slot_to_tap(0) == "tap0"
    assert slot_to_tap(42) == "tap42"


async def test_create_tap_issues_the_expected_commands() -> None:
    run = ShellRecorder()
    await create_tap(5, run=run)
    cmds = [c for c, _ in run.calls]
    assert cmds[0] == "ip link del tap5"
    assert run.calls[0][1] is False
    assert cmds[1] == "ip tuntap add dev tap5 mode tap"
    assert cmds[2] == (
        "ip addr add 172.16.5.1/30 dev tap5 && ip link set tap5 up && "
        "ip neigh replace 172.16.5.2 lladdr 06:00:AC:10:05:02 dev tap5 nud permanent && "
        "iptables -I FORWARD -i tap5 -s 172.16.5.2 ! -d 172.16.0.0/12 -j ACCEPT && "
        "iptables -I FORWARD -i tap5 -s 172.16.5.2 -d 172.16.0.0/12 -j DROP"
    )


async def test_create_tap_retries_the_add_twice_then_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", no_sleep)
    failing = ShellError("ip tuntap add dev tap5 mode tap", 2, "busy")
    run = ShellRecorder(responses={"ip tuntap add": failing})
    with pytest.raises(ShellError):
        await create_tap(5, run=run)
    assert [c for c, _ in run.calls].count("ip tuntap add dev tap5 mode tap") == 3
    assert [c for c, _ in run.calls].count("ip link del tap5") == 3  # one initial + two retries


async def test_destroy_tap_logs_and_swallows_a_failed_delete(
    caplog: pytest.LogCaptureFixture,
) -> None:
    run = ShellRecorder(
        taps={"tap5"},
        responses={"ip link del tap5": ShellError("ip link del tap5", 1, "RTNETLINK busy")},
    )
    await destroy_tap(5, run=run)  # no raise
    assert any("Failed to delete tap tap5" in r.getMessage() for r in caplog.records)
    assert [c for c, _ in run.calls][:2] == [
        "iptables -D FORWARD -i tap5 -s 172.16.5.2 ! -d 172.16.0.0/12 -j ACCEPT",
        "iptables -D FORWARD -i tap5 -s 172.16.5.2 -d 172.16.0.0/12 -j DROP",
    ]


async def test_tap_exists_reads_ip_link_show() -> None:
    assert await tap_exists("tap5", run=ShellRecorder(taps={"tap5"}))
    assert not await tap_exists("tap5", run=ShellRecorder())
