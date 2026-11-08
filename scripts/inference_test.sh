#!/usr/bin/env bash
# Usage: MODEL_ID=<id> ./scripts/inference_test.sh   (key from /etc/policyreplay.env)
set -euo pipefail
set -a; source /etc/policyreplay.env; set +a
BASE="${VULTR_INFERENCE_BASE_URL:-https://api.vultrinference.com/v1}"
echo "== models =="
curl -s "$BASE/models" -H "Authorization: Bearer $VULTR_INFERENCE_KEY" | head -c 2000; echo
[ -z "${MODEL_ID:-}" ] && { echo "Set MODEL_ID to test JSON output"; exit 0; }
echo "== json test =="
curl -s "$BASE/chat/completions" \
  -H "Authorization: Bearer $VULTR_INFERENCE_KEY" -H "Content-Type: application/json" \
  -d "{\"model\":\"$MODEL_ID\",\"temperature\":0,\"messages\":[
        {\"role\":\"system\",\"content\":\"Return only valid JSON.\"},
        {\"role\":\"user\",\"content\":\"Extract: Refunds above INR 10000 require manager approval. Keys: actor, action, threshold.\"}]}"
echo
