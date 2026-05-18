#!/usr/bin/env python3
"""WombatKV-aware bench across modes + context sweep.

Complements ds4-bench (engine compute only, no WombatKV) and
scripts/multi_trial_bench.py (5-trial statistical cell-B at a
single context). This script measures the cell-B story across
a sweep of context sizes, per WombatKV mode.

Per (mode, ctx) cell:
  1. Setup — start daemon if needed, wipe state.
  2. COLD turn — send prompt of length ≈ ctx tokens, capture
     elapsed_ms (full request including prefill + small decode).
  3. Kill ds4-server. Wipe local kvdir. WombatKV state (puffer +
     S3 + daemon) survives so iter 2 does warm restore.
  4. WARM turn — restart ds4-server, send same prompt, capture
     elapsed_ms. For native this is just another cold prefill.
  5. Compute speedup = cold_ms / warm_ms.

Output:
  - per-mode CSV at <outdir>/<mode>.csv with columns:
      ctx_chars, est_tokens, cold_ms, warm_ms, speedup
  - aggregate Markdown summary printed to stdout

Wall-time budget per cell: ~30-90 s depending on ctx + mode.
For (4 modes × 4 ctx sizes): ~15-30 minutes total.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import time
import urllib.request
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
import mode_smoke as ms  # noqa: E402

# ~4 chars per token for the GGUF tokenizer + pg1184 ASCII English text.
# Slight under-estimate; good enough for a labelled ctx column.
CHARS_PER_TOKEN_EST = 4

DEFAULT_CTX_SIZES_TOKENS = [512, 1024, 2048, 4096]
DEFAULT_GEN_TOKENS = 8


def _load_prompt(target_tokens: int) -> str:
    """Slice pg1184.txt to approximately the target token count."""
    pf = Path("/tmp/pg1184.txt")
    if pf.exists():
        body = pf.read_text(encoding="utf-8", errors="ignore")
    else:
        body = ("The Count of Monte Cristo is a novel by Alexandre Dumas. " * 2000)
    chars = target_tokens * CHARS_PER_TOKEN_EST
    head = body[:chars]
    return f"Here is a passage:\n\n{head}\n\nSummarize in 5 words."


def _send_turn(prompt: str, max_tokens: int) -> tuple[float, str]:
    payload = json.dumps({
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "system", "content": "You are a literary assistant."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{ms.PORT}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        body = r.read()
    elapsed = time.time() - t0
    resp = json.loads(body.decode())
    text = resp["choices"][0]["message"]["content"]
    return elapsed, text


def _wipe_mode_state(mode: str, kvdir: Path, puffer: Path, daemon_puffer: Path) -> None:
    """Full state wipe — local kvdir + local puffer + (for daemon modes)
    daemon puffer + S3 bucket. Used between bench cells so each
    (mode, ctx) measurement is INDEPENDENT of the previous cell's
    saved blocks. Without this, sweeping prompts that share a prefix
    (as our pg1184-sliced prompts do) leaks warm restore from cell N-1
    into cell N's cold measurement."""
    for d in (kvdir, puffer, daemon_puffer):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir()
    if mode == "embedded":
        ms.wipe_bucket("wombatkv-smoke-embedded")
    elif mode == "daemon-shm":
        ms.wipe_bucket("wombatkv-smoke-smoke-shm")
    elif mode == "daemon-tcp":
        ms.wipe_bucket("wombatkv-smoke-smoke-tcp")
    # daemon-tcp-remote / native: nothing to wipe on this side


def bench_cell(mode: str, target_tokens: int, gen_tokens: int,
               daemon_puffer: Path, daemon_proc) -> dict:
    """One (mode, ctx) bench cell: cold + warm measurement.

    IMPORTANT: full state wipe at the start of every cell so cell N's
    cold timing isn't polluted by cell N-1's saved blocks. For daemon
    modes this means restarting the daemon (otherwise the daemon's
    foyer cache still has stale blocks even after S3 wipe).
    """
    ms.log(f"  [{mode}] ctx≈{target_tokens} tokens")
    prompt = _load_prompt(target_tokens)
    kvdir = Path(f"/tmp/bench-kvdir-{mode}")
    puffer = Path(f"/tmp/bench-puffer-{mode}")
    serverlog = Path(f"/tmp/bench-{mode}-server.log")
    daemonlog = Path(f"/tmp/bench-{mode}-daemon.log")

    ms.kill_all_ds4()
    # Restart daemon to flush its in-process puffer / SlateDB index too.
    if mode in ("daemon-shm", "daemon-tcp") and daemon_proc is not None:
        try:
            daemon_proc.terminate()
            daemon_proc.wait(timeout=5)
        except Exception:
            daemon_proc.kill()
        ms.kill_all_daemon()
    _wipe_mode_state(mode, kvdir, puffer, daemon_puffer)
    new_daemon_proc = daemon_proc
    if mode == "daemon-shm":
        new_daemon_proc = ms.start_daemon("shm", "smoke-shm", daemonlog, daemon_puffer)
    elif mode == "daemon-tcp":
        new_daemon_proc = ms.start_daemon("tcp", "smoke-tcp", daemonlog, daemon_puffer)

    # Cold turn — fresh everything
    ms.start_server(mode, kvdir, puffer, serverlog)
    cold_ms_s, _ = _send_turn(prompt, gen_tokens)
    ms.kill_all_ds4()
    # Warm turn — wipe ONLY local kvdir; WombatKV puffer + S3 + daemon survive.
    if kvdir.exists():
        shutil.rmtree(kvdir)
        kvdir.mkdir()
    ms.start_server(mode, kvdir, puffer, serverlog)
    warm_ms_s, _ = _send_turn(prompt, gen_tokens)
    ms.kill_all_ds4()

    cold_ms = int(cold_ms_s * 1000)
    warm_ms = int(warm_ms_s * 1000)
    speedup = round(cold_ms_s / max(warm_ms_s, 1e-6), 2)
    return ({
        "mode": mode,
        "est_tokens": target_tokens,
        "ctx_chars": target_tokens * CHARS_PER_TOKEN_EST,
        "cold_ms": cold_ms,
        "warm_ms": warm_ms,
        "speedup": speedup,
    }, new_daemon_proc)


def setup_mode(mode: str) -> "Optional":
    """Mode-level setup BEFORE the per-ctx loop.
    Wipes S3 buckets + (for daemon modes) starts the daemon.
    Returns the daemon Popen handle (or None) so the caller can stop it."""
    daemon_puffer = Path(f"/tmp/bench-daemonpuffer-{mode}")
    daemonlog = Path(f"/tmp/bench-{mode}-daemon.log")

    if daemon_puffer.exists():
        shutil.rmtree(daemon_puffer)
    daemon_puffer.mkdir()

    if mode == "embedded":
        ms.wipe_bucket("wombatkv-smoke-embedded")
        return None
    elif mode == "daemon-shm":
        ms.wipe_bucket("wombatkv-smoke-smoke-shm")
        ms.kill_all_daemon()
        return ms.start_daemon("shm", "smoke-shm", daemonlog, daemon_puffer)
    elif mode == "daemon-tcp":
        ms.wipe_bucket("wombatkv-smoke-smoke-tcp")
        ms.kill_all_daemon()
        return ms.start_daemon("tcp", "smoke-tcp", daemonlog, daemon_puffer)
    elif mode == "daemon-tcp-remote":
        # remote bucket is the user's responsibility; remote daemon should
        # already be running. We don't wipe or start anything.
        return None
    elif mode == "native":
        return None
    raise ValueError(mode)


def teardown_mode(mode: str, daemon_proc) -> None:
    ms.kill_all_ds4()
    if daemon_proc is not None:
        try:
            daemon_proc.terminate()
            daemon_proc.wait(timeout=5)
        except Exception:
            daemon_proc.kill()
    if mode in ("daemon-shm", "daemon-tcp"):
        ms.kill_all_daemon()


def run_bench(modes: list[str], ctx_tokens_list: list[int],
              gen_tokens: int, outdir: Path) -> dict[str, list[dict]]:
    outdir.mkdir(parents=True, exist_ok=True)
    all_results: dict[str, list[dict]] = {}
    for mode in modes:
        ms.log(f"=== mode={mode} setup ===")
        daemon_puffer = Path(f"/tmp/bench-daemonpuffer-{mode}")
        daemon_proc = setup_mode(mode)
        try:
            cells = []
            for tt in ctx_tokens_list:
                cell, daemon_proc = bench_cell(mode, tt, gen_tokens, daemon_puffer, daemon_proc)
                ms.log(
                    f"    → cold={cell['cold_ms']} ms, warm={cell['warm_ms']} ms, "
                    f"speedup={cell['speedup']}×"
                )
                cells.append(cell)
            all_results[mode] = cells
            # Write per-mode CSV
            csv_path = outdir / f"{mode}.csv"
            with csv_path.open("w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["est_tokens", "ctx_chars", "cold_ms", "warm_ms", "speedup"])
                for c in cells:
                    w.writerow([c["est_tokens"], c["ctx_chars"],
                                c["cold_ms"], c["warm_ms"], c["speedup"]])
            ms.log(f"  wrote {csv_path}")
        finally:
            teardown_mode(mode, daemon_proc)
    return all_results


def print_markdown_summary(results: dict[str, list[dict]]) -> None:
    if not results:
        return
    # Per-mode table
    print("\n## Per-mode results\n")
    for mode, cells in results.items():
        print(f"\n### {mode}\n")
        print("| est_tokens | cold_ms | warm_ms | speedup |")
        print("|---:|---:|---:|---:|")
        for c in cells:
            print(f"| {c['est_tokens']} | {c['cold_ms']} | {c['warm_ms']} | {c['speedup']}× |")

    # Cross-mode warm comparison at each ctx
    print("\n## Cross-mode comparison (warm latency)\n")
    all_ctx = sorted({c["est_tokens"] for cells in results.values() for c in cells})
    header = "| est_tokens | " + " | ".join(results.keys()) + " |"
    sep = "|---:|" + "|".join("---:" for _ in results) + "|"
    print(header)
    print(sep)
    for tt in all_ctx:
        row = [str(tt)]
        for mode, cells in results.items():
            cell = next((c for c in cells if c["est_tokens"] == tt), None)
            row.append(f"{cell['warm_ms']}" if cell else "—")
        print("| " + " | ".join(row) + " |")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--modes",
        type=lambda s: s.split(","),
        default="native,embedded,daemon-shm,daemon-tcp",
        help="comma-separated modes (default: 4 same-host)",
    )
    p.add_argument(
        "--ctx",
        type=lambda s: [int(x) for x in s.split(",")],
        default=DEFAULT_CTX_SIZES_TOKENS,
        help=f"comma-separated target token counts (default {DEFAULT_CTX_SIZES_TOKENS})",
    )
    p.add_argument(
        "--gen-tokens",
        type=int,
        default=DEFAULT_GEN_TOKENS,
        help=f"max decode tokens per request (default {DEFAULT_GEN_TOKENS}; small to isolate prefill/restore)",
    )
    p.add_argument(
        "--outdir",
        type=Path,
        default=Path("bench_data/wombatkv_sweep"),
        help="dir for per-mode CSVs (default bench_data/wombatkv_sweep)",
    )
    p.add_argument(
        "--remote-tcp",
        metavar="HOST:PORT",
        default=os.environ.get("MODE_SMOKE_REMOTE_TCP", ""),
        help="for daemon-tcp-remote: remote daemon address",
    )
    args = p.parse_args()

    if not ms.DS4_BIN.exists():
        print(f"ERROR: {ms.DS4_BIN} not found", file=sys.stderr)
        return 2
    if "daemon-tcp-remote" in args.modes and not args.remote_tcp:
        print("ERROR: daemon-tcp-remote needs --remote-tcp HOST:PORT", file=sys.stderr)
        return 2
    if args.remote_tcp:
        ms.REMOTE_TCP_ADDR = args.remote_tcp

    results = run_bench(args.modes, args.ctx, args.gen_tokens, args.outdir)
    print_markdown_summary(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
