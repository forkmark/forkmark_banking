#!/usr/bin/env bash
# Short diagnostic: does a model produce sane text under each llama.cpp setting?
# Usage: bash scripts/llama_diag_mac.sh [hf-repo] [hf-file]
# Writes reports/acceptance/live/llama_diag_<time>.txt. Takes ~5-10 minutes.
set -uo pipefail
cd "$(dirname "$0")/.."
REPO_ID="${1:-inception42/Jais-2-8B-Chat-GGUF}"
FILE="${2:-Q4_K_M.gguf}"
OUT="reports/acceptance/live/llama_diag_$(date +%Y%m%d_%H%M).txt"
mkdir -p "$(dirname "$OUT")"
source .venv/bin/activate

NEW="$HOME/.forkmark/llama.cpp/b12359"
OLD_TAG=b8106; OLD_SHA=9ceb4514a45007c0770929fe3823820595248ae99207e0be23d57bae008e3070
OLD="$HOME/.forkmark/llama.cpp/$OLD_TAG"
if [ ! -x "$OLD/llama-server" ]; then
  mkdir -p "$OLD"; tgz="$OLD/l.tgz"
  curl -fsSL -o "$tgz" "https://github.com/ggml-org/llama.cpp/releases/download/$OLD_TAG/llama-$OLD_TAG-bin-macos-arm64.tar.gz"
  [ "$(shasum -a 256 "$tgz" | awk '{print $1}')" = "$OLD_SHA" ] || { echo "checksum mismatch"; exit 1; }
  tar xzf "$tgz" -C "$OLD" --strip-components=1; rm -f "$tgz"
  xattr -dr com.apple.quarantine "$OLD" 2>/dev/null || true
fi

MODEL=$(python -c "from huggingface_hub import hf_hub_download as d; print(d('$REPO_ID','$FILE'))")
{ echo "model: $REPO_ID/$FILE"; echo "path: $MODEL"; date; } > "$OUT"

try() {  # name, binary dir, extra flags...
  local name="$1" dir="$2"; shift 2
  echo; echo "=== $name  ($dir/llama-server $*)"
  "$dir/llama-server" -m "$MODEL" --host 127.0.0.1 --port 8099 -c 2048 --parallel 1 --jinja "$@" \
      > "/tmp/fm_diag_$name.log" 2>&1 &
  local pid=$!
  for _ in $(seq 1 120); do curl -sf http://127.0.0.1:8099/health >/dev/null && break; sleep 2; done
  python - <<'PY'
import json, time, urllib.request
for q in ["What is the capital of the UAE? Answer in one sentence.",
          "ما هي عاصمة الإمارات؟ أجب بجملة واحدة."]:
    t = time.time()
    try:
        r = json.loads(urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:8099/v1/chat/completions",
            data=json.dumps({"max_tokens": 40, "temperature": 0,
                             "messages": [{"role": "user", "content": q}]}).encode(),
            headers={"Content-Type": "application/json"}), timeout=300).read())
        txt = r["choices"][0]["message"]["content"]; n = r.get("usage", {}).get("completion_tokens", 0)
        print(f"  {n} tok in {time.time()-t:.1f}s -> {txt[:160]!r}")
    except Exception as e:
        print(f"  ERROR {e}")
PY
  grep -iE "offloaded|flash_attn|error" "/tmp/fm_diag_$name.log" | head -4 | cut -c1-160
  kill $pid; wait $pid 2>/dev/null
}

{
  try new_gpu        "$NEW" -ngl 99
  try new_gpu_fa_off "$NEW" -ngl 99 -fa off
  try new_cpu        "$NEW" -ngl 0
  try old_gpu        "$OLD" -ngl 99
  try old_cpu        "$OLD" -ngl 0
} >> "$OUT" 2>&1
echo; echo "Done: $OUT"; cat "$OUT"
