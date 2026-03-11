#!/bin/bash
set -euo pipefail

SERVER="root@135.181.6.215"
SSH="ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 $SERVER"

echo "Deploying mshkn..."

# Push code
$SSH "cd /opt/mshkn && git pull"

# Install deps
$SSH "cd /opt/mshkn && .venv/bin/uv sync"

# Restart
$SSH "systemctl restart mshkn litestream"

echo "Deployed. Check: $SSH 'systemctl status mshkn'"
