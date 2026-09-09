#!/usr/bin/env bash
# Push, deploy, clean orphaned VM resources, ensure the test account, run the live E2E suite.
# Usage: MSHKN_SERVER=root@<ip> scripts/e2e.sh [extra pytest args]
set -euo pipefail

: "${MSHKN_SERVER:?set MSHKN_SERVER to root@<ip> (or an ssh config alias) of the live KVM server}"
# The API host comes from the ssh config, never from the string: with an alias,
# "${MSHKN_SERVER#*@}" is the alias itself, and http://<alias>:8000 resolves to
# nothing, or through a search domain to a stranger (#85). Every test then fails
# on a connect timeout after the deploy and the health check on the host succeeded.
# `|| true` keeps a failing ssh from ending the script inside the assignment
# (set -e, pipefail) before the diagnostic below; ssh's own stderr stays visible.
SERVER_HOST="$(ssh -TG "$MSHKN_SERVER" </dev/null | awk '/^hostname /{print $2}' || true)"
[ -n "$SERVER_HOST" ] || {
  echo "cannot resolve the host for MSHKN_SERVER=$MSHKN_SERVER through ssh -G; check the ssh config" >&2
  exit 1
}
API_URL="${MSHKN_API_URL:-http://${SERVER_HOST}:8000}"
API_KEY="${MSHKN_API_KEY:-mk-test-key-2026}"
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/.."

git push
"$HERE/deploy.sh"

# Stop the service, kill leftover VMs, remove orphaned taps and computer/staging thin
# devices (checkpoint and recipe volumes are persistent and must survive), then start.
ssh "$MSHKN_SERVER" bash -s <<REMOTE
set -euo pipefail
systemctl stop mshkn litestream
pkill -x firecracker || true
sleep 1
rm -f /tmp/fc-*.socket
for tap in \$(ip -o link show type tun | awk -F': ' '{print \$2}' | grep -E '^tap[0-9]+\$' || true); do
  ip link del "\$tap" || true
done
for vol in \$(dmsetup ls --target thin | awk '{print \$1}' | grep -E '^mshkn-(comp-|restore-staging)' || true); do
  dmsetup remove "\$vol" || true
done
cd /opt/mshkn && set -a && . /opt/mshkn/.env && set +a && (.venv/bin/python -m mshkn accounts list | grep -q '^acct-mike	' \
  || .venv/bin/python -m mshkn accounts create --id acct-mike --api-key '${API_KEY}' --vm-limit 20)
systemctl start mshkn litestream
for _ in \$(seq 1 120); do
  curl -fsS http://localhost:8000/health >/dev/null 2>&1 && break
  sleep 0.5
done
curl -fsS http://localhost:8000/health
echo
REMOTE

# The health check above ran on the host. This one runs here, against the URL
# the suite will use, so an unreachable address costs seconds, not a full run.
for _ in $(seq 1 10); do
  curl -fsS --max-time 5 "$API_URL/health" >/dev/null 2>&1 && break
  sleep 2
done
curl -fsS --max-time 5 "$API_URL/health" >/dev/null \
  || { echo "$API_URL/health does not answer from this machine; not running the suite" >&2; exit 1; }

echo "running E2E against $API_URL"
MSHKN_API_URL="$API_URL" MSHKN_API_KEY="$API_KEY" uv run pytest tests/e2e -m e2e -v --tb=short "$@"
