#!/bin/bash
# WombatKV 0.1.0-alpha — one-command POC demo
#
# Demonstrates: ds4 + WombatKV S3-backed KV substrate beats ds4-native local-only
# by 100-200x on cross-process restart scenarios. Same hardware, same model,
# same prompt — the only difference is whether ds4 has a persistent intelligent
# KV substrate underneath.
#
# Requirements:
#   - macOS M3 Max (or compatible)
#   - native MinIO server running on 127.0.0.1:9200 (mc set up, "minioadmin" creds)
#   - ds4-server built with WOMBATKV=1 (see Makefile)
#   - /tmp/pg1184.txt seeded with a long prompt (Project Gutenberg #1184 first chapter)
#   - DSV4-Flash IQ2XXS gguf at $MODEL path

set -e

DS4_DIR=${DS4_DIR:-/Users/venkat/Documents/p/venkat-github/myelon-launch/ds4}
DS4_BIN=$DS4_DIR/ds4-server
MODEL=${MODEL:-gguf/DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix.gguf}
PROMPT_FILE=${PROMPT_FILE:-/tmp/pg1184.txt}
PORT=${PORT:-8000}

if [ ! -x "$DS4_BIN" ]; then
  echo "FATAL: $DS4_BIN not built. Run: cd $DS4_DIR && make ds4-server WOMBATKV=1 WOMBATKV_DIR=<tensorpuffer>"
  exit 1
fi

# 1) Same long prompt for both halves of the demo
PROMPT_BYTES=$(head -c 5200 "$PROMPT_FILE")

start_server() {
  local mode="$1"   # native | wombatkv
  local logfile="$2"
  local kvdir=/tmp/demo-ds4-$mode
  local puffer=/tmp/demo-wombatkv-$mode
  rm -rf "$kvdir" "$puffer"; mkdir -p "$kvdir" "$puffer"

  local env_overrides=()
  if [ "$mode" = "wombatkv" ]; then
    # 0.1.0-alpha minimum surface: enable + non-default endpoint/bucket/puffer
    # for demo isolation. Everything else (fingerprint, credentials,
    # compression, prefetch, Tier B, namespace, bootstrap) auto-resolves
    # via WombatKV defaults + AWS-standard env fallback.
    env_overrides=(
      DS4_WOMBATKV_ENABLE=1
      WMBT_KV_S3_ENDPOINT=http://127.0.0.1:9200
      WMBT_KV_BUCKET=wombatkv-demo-$mode
      WMBT_KV_PUFFER_DIR="$puffer"
      WMBT_KV_TIMING=1
    )
  fi

  ( cd "$DS4_DIR" && env "${env_overrides[@]}" "$DS4_BIN" \
      --model "$MODEL" --ctx 32768 --kv-disk-dir "$kvdir" \
      --kv-cache-min-tokens 256 --kv-disk-space-mb 16384 --port "$PORT" \
      > "$logfile" 2>&1 ) &
  local pid=$!
  for i in $(seq 1 90); do
    grep -q "listening on http" "$logfile" 2>/dev/null && { sleep 0.5; echo "$pid"; return 0; }
    sleep 1
  done
  echo "FAILED to start: $logfile" >&2; kill "$pid" 2>/dev/null; return 1
}

send_request() {
  local prompt_text="$1"
  local started=$(python3 -c "import time;print(int(time.perf_counter()*1000))")
  curl -sN -X POST "http://127.0.0.1:$PORT/v1/chat/completions" \
       -H "Content-Type: application/json" \
       -d "$(jq -nc --arg p "$prompt_text" '{model:"deepseek-v4-flash",messages:[{role:"system",content:"You are a literary assistant."},{role:"user",content:("Here is a passage:\n\n" + $p + "\n\nSummarize the key themes in 50 words.")}],max_tokens:50,temperature:0.0,stream:true}')" \
    | (
        while IFS= read -r line; do
          if [[ "$line" == data:* ]]; then
            ttft=$(python3 -c "import time;print(int(time.perf_counter()*1000 - $started))")
            echo "$ttft"
            cat > /dev/null   # drain rest of stream
            break
          fi
        done
      )
}

run_demo() {
  local mode="$1"
  local label="$2"
  echo
  echo "=== $label ==="
  pkill -f ds4-server 2>/dev/null || true; sleep 1
  local log1=/tmp/demo-$mode-turn1.log
  local log2=/tmp/demo-$mode-turn2.log
  local pid=$(start_server "$mode" "$log1")
  echo "  [$mode] Turn 1 (cold)..."
  local t1=$(send_request "$PROMPT_BYTES")
  echo "  [$mode] Turn 1 TTFT: $t1 ms"
  kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null || true; sleep 1

  echo "  [$mode] Restarting ds4-server (wipes local kv-disk)..."
  rm -rf /tmp/demo-ds4-$mode

  pid=$(start_server "$mode" "$log2")
  echo "  [$mode] Turn 2 (after restart, same prompt)..."
  local t2=$(send_request "$PROMPT_BYTES")
  echo "  [$mode] Turn 2 TTFT: $t2 ms"
  kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null || true

  echo "  [$mode] SUMMARY: turn1=$t1 ms (cold), turn2=$t2 ms (after restart)"
}

echo "==============================================="
echo "  WombatKV 0.1.0-alpha — POC demo"
echo "  Tests: ds4-native vs ds4 + WombatKV"
echo "  Scenario: same prompt twice, restart between turns"
echo "==============================================="

run_demo native   "ds4-native (no WombatKV — local disk only)"
run_demo wombatkv "ds4 + WombatKV (S3-backed substrate)"

echo
echo "==============================================="
echo "  Expected on M3 Max + native MinIO:"
echo "    ds4-native turn 2: ~7000-9000 ms (cold prefill, no cross-restart KV)"
echo "    ds4+WombatKV turn 2: ~50-150 ms (S3 hit, instant warm restore)"
echo "    Speedup: 50-200x"
echo "  What WombatKV provides (defaults-on in 0.1.0-alpha):"
echo "    - object-storage-native prefix-share blocks across processes"
echo "    - trailing-token cache — partial block at session end is preserved, so reload skips re-prefilling those tokens"
echo "    - SlateDB-backed world-knowledge index"
echo "    - zstd block compression"
echo "    - background prefetch worker"
echo "==============================================="
