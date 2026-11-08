#!/usr/bin/env bash
# Never prints the key: only its length and yes/no checks.
set -u
set -a; source /etc/policyreplay.env; set +a
BASE="${VULTR_INFERENCE_BASE_URL:-https://api.vultrinference.com/v1}"
MODEL="${PR_TEXT_MODEL:-glm-5.3-flash}"
python3 - <<'PY'
import os
k = os.environ.get("VULTR_INFERENCE_KEY", "")
print("key present:            ", bool(k))
print("key length:             ", len(k))
print("is the placeholder:     ", k == "your_key_here")
print("whitespace/quote/CR in it:", any(c in k for c in " \t\r\n\"'\\"))
print("non-ASCII characters:   ", not k.isascii())
print("base url:               ", os.environ.get("VULTR_INFERENCE_BASE_URL"))
PY
echo "-- 1) /models with NO key:      HTTP $(curl -s -o /dev/null -w '%{http_code}' "$BASE/models")"
echo "-- 2) /models WITH the key:     HTTP $(curl -s -o /dev/null -w '%{http_code}' "$BASE/models" -H "Authorization: Bearer $VULTR_INFERENCE_KEY")"
echo "-- 3) chat completion WITH key ($MODEL):"
curl -s -w "\n   HTTP %{http_code}\n" "$BASE/chat/completions" \
  -H "Authorization: Bearer $VULTR_INFERENCE_KEY" -H "Content-Type: application/json" \
  -d "{\"model\":\"$MODEL\",\"max_tokens\":5,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}" | head -c 500
