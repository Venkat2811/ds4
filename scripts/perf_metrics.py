#!/usr/bin/env python3
"""Proper perf measurement for ds4 + WombatKV: TTFT, TPOT, e2e, throughput.

The headline metric anyone outside this repo will ask for is:
  "How much faster is WombatKV warm-restore vs ds4-native cold prefill
   of the SAME prompt?"

That's `native_turn2_e2e / wombatkv_turn2_e2e` — NOT the intra-mode
ratio mode_smoke reports. We capture this here.

Per mode, per turn we measure:
  TTFT_ms             time-to-first-token (prefill; from SSE first chunk)
  TPOT_ms             mean inter-token time across decode tokens
  e2e_ms              total request wall time
  prompt_tokens       from final usage block
  completion_tokens   from final usage block
  prompt_throughput   prompt_tokens / TTFT (tok/s on prefill)
  output_throughput   completion_tokens / decode_time (tok/s on decode)

Two turns per mode (cell-B shape):
  turn 1: cold prefill (kvdisk + puffer + bucket wiped, fresh server)
  turn 2: warm restore (WombatKV) or cold re-prefill (native, no warm path)

Headline: speedup_vs_native_turn2 = native_turn2_e2e / mode_turn2_e2e

Reuses mode_smoke.py's low-level lifecycle helpers
(start_daemon / start_server / kill_all_*).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import urllib.request
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
import mode_smoke as ms  # noqa: E402

# Cell-B-shaped synthetic prompt (~150 tokens). Same across all modes
# + turns so the comparison is apples-to-apples.
PROMPT = (
    "Summarise the key themes of The Count of Monte Cristo in roughly "
    "200 words. Focus on revenge, identity, and the role of social class. "
    "Mention specific characters: Edmond Dantès, the Abbé Faria, "
    "Mercédès, Fernand Mondego, Danglars, Villefort. Quote at least "
    "one short line from the novel if you remember one. Conclude with "
    "a one-sentence verdict on whether the novel's view of revenge is "
    "endorsed or critiqued by Dumas."
)
MAX_TOKENS = 128


def measure_streaming(port: int) -> dict:
    """Stream a chat completion; capture TTFT/TPOT/e2e/throughput."""
    body = {
        "model": ms.MODEL,
        "messages": [
            {"role": "system", "content": "You are concise and direct."},
            {"role": "user", "content": PROMPT},
        ],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
    )

    t_send = time.perf_counter()
    chunk_times: list[float] = []
    text_pieces: list[str] = []
    usage: dict | None = None

    with urllib.request.urlopen(req, timeout=600) as resp:
        for raw_line in resp:
            if not raw_line:
                continue
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if payload == "[DONE]":
                break
            try:
                evt = json.loads(payload)
            except json.JSONDecodeError:
                continue
            chunk_times.append(time.perf_counter() - t_send)
            for choice in evt.get("choices", []):
                delta = choice.get("delta", {})
                content = delta.get("content")
                if content:
                    text_pieces.append(content)
            if "usage" in evt and evt["usage"]:
                usage = evt["usage"]

    e2e_ms = (time.perf_counter() - t_send) * 1000.0
    text = "".join(text_pieces)
    prompt_tokens = (usage or {}).get("prompt_tokens", 0)
    completion_tokens = (usage or {}).get("completion_tokens", 0)
    ttft_ms = (chunk_times[0] * 1000.0) if chunk_times else e2e_ms
    decode_ms = max(e2e_ms - ttft_ms, 0.0)
    tpot_ms = (decode_ms / completion_tokens) if completion_tokens > 0 else 0.0
    prompt_throughput = (
        (prompt_tokens / (ttft_ms / 1000.0))
        if (ttft_ms > 0 and prompt_tokens > 0)
        else 0.0
    )
    output_throughput = (
        (completion_tokens / (decode_ms / 1000.0))
        if (decode_ms > 0 and completion_tokens > 0)
        else 0.0
    )

    return {
        "ttft_ms": round(ttft_ms, 1),
        "tpot_ms": round(tpot_ms, 2),
        "e2e_ms": round(e2e_ms, 1),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "prompt_throughput_tps": round(prompt_throughput, 1),
        "output_throughput_tps": round(output_throughput, 1),
        "text_first40": text[:40],
        "text_len": len(text),
    }


def _bucket_for(mode: str) -> str | None:
    return {
        "embedded": "wombatkv-smoke-embedded",
        "daemon-shm": "wombatkv-smoke-smoke-shm",
        "daemon-tcp": "wombatkv-smoke-smoke-tcp",
        "daemon-http": "wombatkv-smoke-smoke-http",
    }.get(mode)


def run_mode(mode: str) -> dict:
    ms.log(f"=== perf_metrics: mode={mode} ===")
    kvdir = Path(f"/tmp/perf-kvdir-{mode}")
    puffer = Path(f"/tmp/perf-puffer-{mode}")
    daemon_puffer = Path(f"/tmp/perf-daemonpuffer-{mode}")
    serverlog = Path(f"/tmp/perf-{mode}-server.log")
    daemonlog = Path(f"/tmp/perf-{mode}-daemon.log")

    ms.kill_all_ds4()
    if mode in ("daemon-shm", "daemon-tcp", "daemon-http"):
        ms.kill_all_daemon()

    for d in (kvdir, puffer, daemon_puffer):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir()

    bucket = _bucket_for(mode)
    if bucket:
        ms.wipe_bucket(bucket)

    daemon_proc = None
    try:
        if mode == "daemon-shm":
            daemon_proc = ms.start_daemon("shm", "smoke-shm", daemonlog, daemon_puffer)
        elif mode == "daemon-tcp":
            daemon_proc = ms.start_daemon("tcp", "smoke-tcp", daemonlog, daemon_puffer)
        elif mode == "daemon-http":
            daemon_proc = ms.start_daemon(
                "http", "smoke-http", daemonlog, daemon_puffer
            )

        ms.start_server(mode, kvdir, puffer, serverlog)

        ms.log("  turn 1 (cold)")
        t1 = measure_streaming(ms.PORT)
        ms.log(
            f"    ttft={t1['ttft_ms']}ms tpot={t1['tpot_ms']}ms e2e={t1['e2e_ms']}ms "
            f"pt={t1['prompt_tokens']} ct={t1['completion_tokens']} "
            f"in={t1['prompt_throughput_tps']}t/s out={t1['output_throughput_tps']}t/s"
        )

        # Restart ds4-server (kvdisk wiped); puffer kept for warm-restore.
        ms.kill_all_ds4()
        if kvdir.exists():
            shutil.rmtree(kvdir)
        kvdir.mkdir()
        ms.start_server(mode, kvdir, puffer, serverlog)

        ms.log("  turn 2 (warm restore for wombatkv modes)")
        t2 = measure_streaming(ms.PORT)
        ms.log(
            f"    ttft={t2['ttft_ms']}ms tpot={t2['tpot_ms']}ms e2e={t2['e2e_ms']}ms "
            f"pt={t2['prompt_tokens']} ct={t2['completion_tokens']} "
            f"in={t2['prompt_throughput_tps']}t/s out={t2['output_throughput_tps']}t/s"
        )

        return {"mode": mode, "turn1": t1, "turn2": t2}
    finally:
        ms.kill_all_ds4()
        if daemon_proc is not None:
            try:
                daemon_proc.terminate()
                daemon_proc.wait(timeout=5)
            except Exception:
                ms.kill_all_daemon()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "modes",
        nargs="*",
        default=["native", "embedded", "daemon-shm", "daemon-tcp", "daemon-http"],
    )
    p.add_argument("--output", type=Path, default=None)
    args = p.parse_args()

    records: dict[str, dict] = {}
    for mode in args.modes:
        try:
            records[mode] = run_mode(mode)
        except Exception as exc:  # noqa: BLE001
            ms.log(f"  EXCEPTION: {type(exc).__name__}: {exc}")
            records[mode] = {"mode": mode, "error": f"{type(exc).__name__}: {exc}"}

    # Headline: speedup vs native turn-2 baseline (the right comparison).
    speedups: dict[str, float] = {}
    native = records.get("native") if "native" in records else None
    if native and "turn2" in native:
        base = native["turn2"]["e2e_ms"]
        for mode, rec in records.items():
            if mode == "native" or "turn2" not in rec:
                continue
            t2 = rec["turn2"]["e2e_ms"]
            if t2 > 0:
                speedups[mode] = round(base / t2, 2)

    print()
    print("=== per-mode perf metrics (cell-B shape, prompt ≈ 150 tok) ===")
    print(
        f"{'mode':<14} {'turn':<5} {'ttft_ms':>9} {'tpot_ms':>9} {'e2e_ms':>9} "
        f"{'pt':>5} {'ct':>5} {'in_tps':>8} {'out_tps':>9}"
    )
    for mode, rec in records.items():
        if "error" in rec:
            print(f"{mode:<14}  ERROR: {rec['error']}")
            continue
        for tname in ("turn1", "turn2"):
            t = rec[tname]
            print(
                f"{mode:<14} {tname:<5} {t['ttft_ms']:>9} {t['tpot_ms']:>9} "
                f"{t['e2e_ms']:>9} {t['prompt_tokens']:>5} "
                f"{t['completion_tokens']:>5} {t['prompt_throughput_tps']:>8} "
                f"{t['output_throughput_tps']:>9}"
            )

    if speedups:
        print()
        print("=== headline: e2e speedup vs ds4-native turn-2 ===")
        print(f"{'mode':<14} {'speedup':>10}")
        for mode, sp in speedups.items():
            print(f"{mode:<14} {sp:>9}×")

    out_path = args.output or Path(f"/tmp/perf-metrics-{int(time.time())}.json")
    out_path.write_text(
        json.dumps({"records": records, "speedup_vs_native_turn2": speedups}, indent=2)
    )
    print(f"\nJSON: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
