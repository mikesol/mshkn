#!/usr/bin/env bash
# Deploy the current local branch to the live server and restart the service.
# Usage: MSHKN_SERVER=root@<ip> scripts/deploy.sh
set -euo pipefail

: "${MSHKN_SERVER:?set MSHKN_SERVER to root@<ip> of the live KVM server}"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"

ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$MSHKN_SERVER" bash -s <<REMOTE
set -euo pipefail
cd /opt/mshkn
git fetch origin
git checkout -B "${BRANCH}" "origin/${BRANCH}"
~/.local/bin/uv sync --frozen
systemctl restart mshkn litestream
sleep 2
systemctl is-active mshkn litestream
REMOTE

echo "deployed $BRANCH to $MSHKN_SERVER"
