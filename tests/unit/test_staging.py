from mshkn.host.firecracker import (
    STAGING_DRIVE_NAME,
    STAGING_HOST_IP,
    STAGING_MAC,
    STAGING_SLOT,
    STAGING_TAP,
    STAGING_VM_IP,
)


def test_staging_constants() -> None:
    assert STAGING_SLOT == 254
    assert STAGING_TAP == "tap254"
    assert STAGING_HOST_IP == "172.16.254.1"
    assert STAGING_VM_IP == "172.16.254.2"
    assert STAGING_MAC == "06:00:AC:10:FE:02"
    assert STAGING_DRIVE_NAME == "mshkn-restore-staging"
