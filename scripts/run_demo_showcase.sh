#!/bin/bash
# WombatKV showcase wrapper.
#
# Runs 3 modes (c1_native, c2_embedded, c3_daemon) × 2 scenarios
# (pi_review, sharegpt_multiturn). Each (mode × scenario) lands in its own
# subdirectory under $BENCH_ART_DIR. After all six runs complete, the
# aggregator emits a single HEADLINE.md + headline.csv at the top of
# $BENCH_ART_DIR.
#
# Per RFC 0003 Option A: same-host, 5 ds4-server instances on ports 8000-8004,
# all paths under /tmp/showcase-* so a single `rm -rf /tmp/showcase-*` resets
# the workspace.
#
# Prerequisites:
#   - target/release/libwombatkv.dylib + ds4-server already built (M0)
#   - tensorpuffer/target/release/wombatkv-daemon built (M1)
#   - native MinIO at 127.0.0.1:9200 with minioadmin/minioadmin
#   - /tmp/pg1184.txt present (Project Gutenberg #1184 plain text)
#   - port 8000-8004 free; PORT 8000 is OWNED by this bench, no overlap with
#     prior benches
#
# Usage:
#   BENCH_ART_DIR=/path/to/out  ./scripts/run_demo_showcase.sh
#
# Optional env:
#   SHOWCASE_TRIALS=2     trials per (mode, scenario). Default 2 (trial 1 cold,
#                         trial 2 warm).
#   SHOWCASE_MAX_TOKENS=50  decode budget per turn.
#   SHOWCASE_SKIP_MODES=   space-separated modes to skip, e.g. "c3_daemon"
#                         (useful when iterating)
#
# This script does NOT build anything. It assumes the binaries are present.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Fresh output dir if none provided.
DEFAULT_ART="/Users/venkat/Documents/p/venkat-github/myelon-launch/ai-chat-exports/.0_agentic_engineering/5_tensor_puffer/bench_data/2026-05-16_showcase_$(date +%H%M%S)"
ART="${BENCH_ART_DIR:-$DEFAULT_ART}"
mkdir -p "$ART"

TRIALS="${SHOWCASE_TRIALS:-2}"
SKIP_MODES="${SHOWCASE_SKIP_MODES:-}"

echo "==============================================="
echo "WombatKV showcase"
echo "  artifacts: $ART"
echo "  trials:    $TRIALS"
echo "  skip:      ${SKIP_MODES:-<none>}"
echo "==============================================="

# All combinations to run.
MODES=(c1_native c2_embedded c3_daemon)
SCENARIOS=(pi_review sharegpt_multiturn)

# Verify required binaries up front so we fail fast.
DS4_BIN="/Users/venkat/Documents/p/venkat-github/myelon-launch/ds4/ds4-server"
DAEMON_BIN="/Users/venkat/Documents/p/venkat-github/tensorpuffer/target/release/wombatkv-daemon"

if [ ! -x "$DS4_BIN" ]; then
  echo "FATAL: ds4-server not built at $DS4_BIN" >&2
  echo "       (this script does NOT build anything; build it first)" >&2
  exit 1
fi
if [ ! -x "$DAEMON_BIN" ]; then
  echo "WARNING: wombatkv-daemon not built at $DAEMON_BIN — c3_daemon will fail" >&2
fi

# Kill anything from a stale bench so port 8000-8004 are free.
pkill -f ds4-server     2>/dev/null || true
pkill -f wombatkv-daemon 2>/dev/null || true
sleep 1

for MODE in "${MODES[@]}"; do
  if echo " $SKIP_MODES " | grep -q " $MODE "; then
    echo
    echo "--- SKIPPING mode $MODE ---"
    continue
  fi
  for SCEN in "${SCENARIOS[@]}"; do
    echo
    echo "================================================================"
    echo "  $MODE  ×  $SCEN"
    echo "================================================================"
    OUT="$ART/${MODE}__${SCEN}"
    mkdir -p "$OUT"
    if ! python3 "$SCRIPT_DIR/scenarios/${SCEN}.py" \
            --mode "$MODE" --outdir "$OUT" --trials "$TRIALS"; then
      echo "FAILED: $MODE × $SCEN — continuing with next combination" >&2
    fi
    # Defensive: make sure nothing leaked into the next combination.
    pkill -f ds4-server     2>/dev/null || true
    pkill -f wombatkv-daemon 2>/dev/null || true
    sleep 1
  done
done

# Aggregate.
echo
echo "==============================================="
echo "Aggregating results into $ART"
echo "==============================================="
python3 "$SCRIPT_DIR/aggregate_showcase.py" "$ART"

echo
echo "==============================================="
echo "Done. See:"
echo "  $ART/HEADLINE.md"
echo "  $ART/headline.csv"
echo "==============================================="
