#!/bin/bash
set -euo pipefail

API="http://localhost:8000"
KEY="mk-test-key-2026"

echo "=========================================="
echo "  mshkn E2E Test — $(date -Iseconds)"
echo "=========================================="
echo ""

# 1. CREATE
echo "--- Create Computer (target: <=2000ms) ---"
START=$(date +%s%N)
CREATE_RESULT=$(curl -s -X POST "$API/computers" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"uses": []}')
END=$(date +%s%N)
CREATE_MS=$(( (END - START) / 1000000 ))
echo "Response: $CREATE_RESULT"
echo "CREATE: ${CREATE_MS}ms"
COMP_ID=$(echo "$CREATE_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['computer_id'])")
echo ""

# 2. EXEC
echo "--- Exec: echo hello ---"
START=$(date +%s%N)
EXEC1=$(curl -s -X POST "$API/computers/$COMP_ID/exec" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"command": "echo hello"}')
END=$(date +%s%N)
EXEC1_MS=$(( (END - START) / 1000000 ))
echo "$(echo "$EXEC1" | grep 'data:' | head -1)"
echo "EXEC: ${EXEC1_MS}ms"
echo ""

# 3. WRITE STATE
echo "--- Write state to VM ---"
curl -s -X POST "$API/computers/$COMP_ID/exec" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"command": "echo checkpoint-state-42 > /tmp/state.txt"}' > /dev/null
echo "Done"
echo ""

# 4. CHECKPOINT
echo "--- Checkpoint (target: <=1000ms) ---"
START=$(date +%s%N)
CKPT_RESULT=$(curl -s -X POST "$API/computers/$COMP_ID/checkpoint" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"label": "e2e-test"}')
END=$(date +%s%N)
CKPT_MS=$(( (END - START) / 1000000 ))
echo "Response: $CKPT_RESULT"
echo "CHECKPOINT: ${CKPT_MS}ms"
CKPT_ID=$(echo "$CKPT_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['checkpoint_id'])")
echo ""

# 5. FORK
echo "--- Fork from Checkpoint (target: <=2000ms) ---"
START=$(date +%s%N)
FORK_RESULT=$(curl -s -X POST "$API/checkpoints/$CKPT_ID/fork" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{}')
END=$(date +%s%N)
FORK_MS=$(( (END - START) / 1000000 ))
echo "Response: $FORK_RESULT"
echo "FORK: ${FORK_MS}ms"
FORK_ID=$(echo "$FORK_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['computer_id'])" 2>/dev/null || echo "")
echo ""

# 6. VERIFY FORK STATE
if [ -n "$FORK_ID" ]; then
    echo "--- Verify fork preserved checkpoint state ---"
    VERIFY=$(curl -s -X POST "$API/computers/$FORK_ID/exec" \
      -H "Authorization: Bearer $KEY" \
      -H "Content-Type: application/json" \
      -d '{"command": "cat /tmp/state.txt"}')
    STATE=$(echo "$VERIFY" | grep 'data:' | head -1 | sed 's/data: //')
    echo "Fork /tmp/state.txt = $STATE"
    echo ""

    # 7. PROVE INDEPENDENCE
    echo "--- Prove VMs are independent ---"
    curl -s -X POST "$API/computers/$COMP_ID/exec" \
      -H "Authorization: Bearer $KEY" \
      -H "Content-Type: application/json" \
      -d '{"command": "echo original > /tmp/identity.txt"}' > /dev/null
    curl -s -X POST "$API/computers/$FORK_ID/exec" \
      -H "Authorization: Bearer $KEY" \
      -H "Content-Type: application/json" \
      -d '{"command": "echo forked > /tmp/identity.txt"}' > /dev/null

    ORIG_V=$(curl -s -X POST "$API/computers/$COMP_ID/exec" \
      -H "Authorization: Bearer $KEY" \
      -H "Content-Type: application/json" \
      -d '{"command": "cat /tmp/identity.txt"}' | grep 'data:' | head -1 | sed 's/data: //')
    FORK_V=$(curl -s -X POST "$API/computers/$FORK_ID/exec" \
      -H "Authorization: Bearer $KEY" \
      -H "Content-Type: application/json" \
      -d '{"command": "cat /tmp/identity.txt"}' | grep 'data:' | head -1 | sed 's/data: //')
    echo "Original VM: $ORIG_V"
    echo "Forked VM:   $FORK_V"
    echo ""

    # 8. DESTROY FORK
    echo "--- Destroy fork ---"
    curl -s -X DELETE "$API/computers/$FORK_ID" -H "Authorization: Bearer $KEY"
    echo ""
fi

# 9. LIST CHECKPOINTS
echo "--- List checkpoints ---"
curl -s "$API/checkpoints" -H "Authorization: Bearer $KEY" | python3 -c "
import sys,json
data = json.load(sys.stdin)
print(f'{len(data)} checkpoint(s)')
for c in data:
    print(f'  {c[\"id\"]} label={c[\"label\"]} computer={c[\"computer_id\"]}')
"
echo ""

# 10. DESTROY ORIGINAL
echo "--- Destroy original ---"
curl -s -X DELETE "$API/computers/$COMP_ID" -H "Authorization: Bearer $KEY"
echo ""
echo ""

# SUMMARY
echo "=========================================="
echo "  BENCHMARK RESULTS"
echo "=========================================="
if [ "$CREATE_MS" -le 2000 ]; then CK="PASS"; else CK="FAIL"; fi
if [ "$CKPT_MS" -le 1000 ]; then CC="PASS"; else CC="FAIL"; fi
if [ "$FORK_MS" -le 2000 ]; then CF="PASS"; else CF="FAIL"; fi
printf "  CREATE:     %5dms  (target: <=2000ms) %s\n" "$CREATE_MS" "$CK"
printf "  EXEC:       %5dms\n" "$EXEC1_MS"
printf "  CHECKPOINT: %5dms  (target: <=1000ms) %s\n" "$CKPT_MS" "$CC"
printf "  FORK:       %5dms  (target: <=2000ms) %s\n" "$FORK_MS" "$CF"
echo "=========================================="
echo ""
echo "Note: Fork uses cold boot (systemd ~7s) not"
echo "snapshot restore, because Firecracker snapshot"
echo "restore requires same network config as original."
