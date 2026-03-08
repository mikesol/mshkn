from mshkn.vm.firecracker import FirecrackerConfig


def test_firecracker_config_to_api_calls() -> None:
    """Test that config generates the correct API call sequence."""
    config = FirecrackerConfig(
        socket_path="/tmp/fc-test.socket",
        kernel_path="/opt/firecracker/vmlinux.bin",
        rootfs_path="/dev/mapper/test-vol",
        tap_device="tap5",
        guest_mac="06:00:AC:10:05:02",
        vcpu_count=2,
        mem_size_mib=512,
    )
    assert config.boot_args.startswith("console=ttyS0")
    assert config.vcpu_count == 2
    assert config.mem_size_mib == 512
