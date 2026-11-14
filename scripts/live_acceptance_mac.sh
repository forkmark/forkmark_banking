#!/usr/bin/env bash
# Live acceptance on an Apple-silicon Mac: Jais 2 8B, then Falcon-H1 3B, one at a time.
# Run from the repo root:  bash scripts/live_acceptance_mac.sh [jais|falcon|both]
#   SUITES=safety bash scripts/live_acceptance_mac.sh jais   # one model, safety suite only
# Results: reports/acceptance/live/<date>/<model>/  (see scripts/live_acceptance.py)
set -euo pipefail
WHICH="${1:-both}"
SUITES="${SUITES:-safety,stress}"
PARALLEL="${PARALLEL:-4}"   # requests answered at once; ~2x faster than 1 on the same machine
case "$WHICH" in jais|falcon|both) ;; *) echo "usage: $0 [jais|falcon|both]"; exit 1;; esac

cd "$(dirname "$0")/.."
REPO="$(pwd)"
DAY="$(date +%Y-%m-%d_%H%M)"
OUT="$REPO/reports/acceptance/live/$DAY"
mkdir -p "$OUT"
exec > >(tee -a "$OUT/run_$WHICH.log") 2>&1

LLAMA_TAG="b12359"
LLAMA_SHA="82b00abc740160e1b9980e6cc79c117345fa34af058c80e6c0a69aa8ddccde75"
LLAMA_DIR="$HOME/.forkmark/llama.cpp/$LLAMA_TAG"

echo "== ForkMark live acceptance — $(date)"
[ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ] || { echo "This script is for Apple-silicon Macs."; exit 1; }

free_gb=$(df -g "$HOME" | awk 'NR==2{print $4}')
echo "Free disk: ${free_gb} GB"
[ "$free_gb" -ge 8 ] || { echo "Need at least 8 GB free disk."; exit 1; }
if pgrep -xq ollama || pgrep -fq "Ollama.app"; then
  echo "Ollama is running and may hold memory. Quit it (menu bar > Quit Ollama), then re-run."; exit 1
fi

# 1. llama.cpp, pinned build, checksum verified
if [ ! -x "$LLAMA_DIR/llama-server" ]; then
  echo "Downloading llama.cpp $LLAMA_TAG ..."
  mkdir -p "$LLAMA_DIR"
  tgz="$LLAMA_DIR/llama.tgz"
  curl -fL --progress-bar -o "$tgz" \
    "https://github.com/ggml-org/llama.cpp/releases/download/$LLAMA_TAG/llama-$LLAMA_TAG-bin-macos-arm64.tar.gz"
  got=$(shasum -a 256 "$tgz" | awk '{print $1}')
  [ "$got" = "$LLAMA_SHA" ] || { echo "llama.cpp checksum mismatch ($got)"; rm -f "$tgz"; exit 1; }
  tar xzf "$tgz" -C "$LLAMA_DIR" --strip-components=1
  xattr -dr com.apple.quarantine "$LLAMA_DIR" 2>/dev/null || true
  rm -f "$tgz"
fi
"$LLAMA_DIR/llama-server" --version 2>&1 | tail -2

# 2. Python environment
[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q --disable-pip-version-check -r requirements.txt huggingface_hub

# 3. Hugging Face sign-in (needed for the Jais 2 download)
if ! python -c "from huggingface_hub import whoami; whoami()" >/dev/null 2>&1; then
  echo
  echo "Sign in to Hugging Face. Create a READ token at https://huggingface.co/settings/tokens and paste it below."
  hf auth login
fi
python -c "from huggingface_hub import whoami; print('Hugging Face user:', whoami()['name'])"

# Check Jais access before spending hours on anything else
if [ "$WHICH" != falcon ] && ! python - <<'PY'
from huggingface_hub import get_hf_file_metadata, hf_hub_url
get_hf_file_metadata(hf_hub_url("inception42/Jais-2-8B-Chat-GGUF", "Q4_K_M.gguf"))
PY
then
  echo
  echo "No access to Jais 2 yet. Open https://huggingface.co/inception42/Jais-2-8B-Chat-GGUF"
  echo "while signed in, accept the terms, then run this script again."
  exit 1
fi
echo "Jais 2 access: OK"

# 4. Keep the Mac awake while this script runs
caffeinate -dimsu -w $$ &

run() {
  python scripts/live_acceptance.py --llama-server "$LLAMA_DIR/llama-server" --suites "$SUITES" --parallel "$PARALLEL" --out "$OUT/$1" --label "$@"
}

status=0
# Jais 2 on CPU: on Apple-silicon Metal it emits only unknown-byte tokens with both
# b12359 and b8106 (scripts/llama_diag_mac.sh, 2026-11-13); CPU output is correct.
[ "$WHICH" = falcon ] || run jais-2-8b --hf-repo inception42/Jais-2-8B-Chat-GGUF --hf-file Q4_K_M.gguf --ngl 0 --port 8089 \
    --vendor "Inception (G42)" --family jais --licence "Jais 2 licence" || status=1
[ "$WHICH" = jais ] || run falcon-h1-3b --hf-repo tiiuae/Falcon-H1-3B-Instruct-GGUF --hf-file Falcon-H1-3B-Instruct-Q4_K_M.gguf --port 8090 \
    --vendor "Technology Innovation Institute" --family falcon --licence "Falcon LLM licence" || status=1

echo
echo "== Finished $(date). Results in $OUT"
exit $status
