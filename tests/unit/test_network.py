from mshkn.vm.network import slot_to_ip, slot_to_mac, slot_to_tap


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
