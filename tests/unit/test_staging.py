from mshkn.vm.staging import STAGING_SLOT, STAGING_TAP, STAGING_HOST_IP, STAGING_VM_IP, STAGING_MAC, STAGING_DRIVE_NAME


def test_staging_constants():
    assert STAGING_SLOT == 254
    assert STAGING_TAP == "tap254"
    assert STAGING_HOST_IP == "172.16.254.1"
    assert STAGING_VM_IP == "172.16.254.2"
    assert STAGING_MAC == "06:00:AC:10:FE:02"
    assert STAGING_DRIVE_NAME == "mshkn-restore-staging"
