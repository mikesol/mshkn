#!/usr/bin/env bash
# Deploy the currently pushed branch to the live server and restart the service.
# Usage: MSHKN_SERVER=root@<ip> scripts/deploy.sh
set -euo pipefail

: "${MSHKN_SERVER:?set MSHKN_SERVER to root@<ip> of the live KVM server}"

ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$MSHKN_SERVER" bash -s <<'REMOTE'
set -euo pipefail
cd /opt/mshkn
git pull --ff-only
~/.local/bin/uv sync --frozen
systemctl restart mshkn litestream
sleep 2
systemctl is-active mshkn litestream
REMOTE

echo "deployed to $MSHKN_SERVER"
