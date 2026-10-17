#!/usr/bin/env bash
# Run a JevK5-family decision model locally: llama-server on 127.0.0.1:8080, the shim on :8090.
#
#   ./serve.sh setup              clone the jevk5 runtime at the pinned tag into vendor/
#   ./serve.sh fetch jevk5-4b     download the model into models/ and verify its SHA-256
#   ./serve.sh start jevk5-4b     start llama-server and the /v1/systemone shim (loopback only)
#   ./serve.sh stop
#
# Needs llama-server on PATH (for example `brew install llama.cpp`) and python3 >= 3.10.
set -euo pipefail
cd "$(dirname "$0")"

field() {  # field <model|""> <key>: read models.json
  python3 - "$1" "$2" <<'PY'
import json, sys
config = json.load(open("models.json"))
model, key = sys.argv[1], sys.argv[2]
entry = config["models"].get(model) if model else config
if entry is None:
    sys.exit(f"unknown model {model}; choose from {', '.join(config['models'])}")
print(entry[key])
PY
}

sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }

stop() {
  for f in logs/shim.pid logs/llama.pid; do
    if [[ -f $f ]]; then kill "$(cat "$f")" 2>/dev/null || true; rm -f "$f"; fi
  done
}

case "${1:-}" in
  setup)
    tag=$(field "" jevk5_tag)
    [[ -d vendor/jevk5 ]] || git clone --depth 1 --branch "$tag" https://github.com/allebee/jevk5 vendor/jevk5
    echo "jevk5 $tag in vendor/jevk5"
    ;;
  fetch)
    model=${2:?model}; file=$(field "$model" file); repo=$(field "$model" repo)
    mkdir -p models
    if [[ ! -f "models/$file" ]]; then
      curl -fL --retry 3 -o "models/$file.part" "https://huggingface.co/$repo/resolve/main/$file"
      mv "models/$file.part" "models/$file"
    fi
    want=$(field "$model" sha256); got=$(sha256 "models/$file")
    [[ "$want" == "$got" ]] || { echo "SHA-256 mismatch for $file: $got" >&2; exit 1; }
    echo "models/$file verified"
    ;;
  start)
    model=${2:?model}; file=$(field "$model" file); name=$(field "$model" name)
    temp=$(field "$model" temperature)
    [[ -f "models/$file" ]] || { echo "run ./serve.sh fetch $model first" >&2; exit 1; }
    [[ -d vendor/jevk5 ]] || { echo "run ./serve.sh setup first" >&2; exit 1; }
    stop
    mkdir -p logs
    llama-server -m "models/$file" -c 8192 -ngl 99 --host 127.0.0.1 --port 8080 > "logs/llama.log" 2>&1 &
    echo $! > logs/llama.pid
    for _ in $(seq 1 120); do
      curl -s -m 2 127.0.0.1:8080/health 2>/dev/null | grep -q '"ok"' && break
      sleep 1
    done
    PYTHONPATH=vendor/jevk5 python3 serve_gguf.py --name "$name" --temperature "$temp" --port 8090 \
      > logs/shim.log 2>&1 &
    echo $! > logs/shim.pid
    sleep 2
    curl -s -m 5 127.0.0.1:8090/health
    echo
    ;;
  stop) stop; echo stopped ;;
  *) sed -n '2,10p' "$0"; exit 2 ;;
esac
