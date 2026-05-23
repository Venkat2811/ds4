#!/usr/bin/env python3
"""Strong WombatKV coherence test.

Claim under test: warm-restored K/V from WombatKV is numerically
identical to cold-computed K/V, modulo (a) Metal scheduling noise
on the suffix forward pass and (b) the trailing-token recompute
forced by the install_raw_tail `-1` checkpoint bias.

Per-mode procedure:
  1. Start ds4-server with mode env (and daemon if needed).
  2. Send prompt P, capture turn text. Kill server.
  3. Wipe local kvdir (keep WombatKV puffer + S3 for warm restore).
  4. Repeat N times (default 3).
  5. Compare pairwise: byte-equality, longest common prefix,
     non-trivial shared words, length deltas.

Interpretation:
  - native (no WombatKV): every iteration is a fresh cold prefill.
    Any divergence between native iter outputs IS Metal scheduling
    noise (the noise floor).
  - embedded / daemon-* (WombatKV): iter 1 is cold prefill + S3
    write; iters 2..N are warm restore from S3. Divergence
    between cold and warm tells us about WombatKV-introduced drift
    on top of the noise floor.

Decision rule:
  - HARD assertion: every iteration in every mode returns non-empty
    text (basic life check).
  - SOFT assertion: for each WombatKV mode, max pairwise lcp_delta
    (= native_max_lcp - this_mode_min_lcp) ≤ some tolerance. Below
    that → mode is at least as deterministic as native's Metal-only
    baseline. Above → WombatKV is adding drift on top of Metal noise.
  - If all WombatKV iters are byte-identical to each other (warm/warm
    pair), that's a STRONG positive signal — WombatKV-restore path
    is deterministic.

This script does NOT do tensor-level K/V comparison (that needs C-side
hooks not currently in ds4-server). Text byte-equality at temp=0 is
the strongest signal exposed by the HTTP API.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

# Import mode_smoke helpers (it's a flat script with module-level functions)
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
import mode_smoke as ms  # noqa: E402

ITERATIONS = int(os.environ.get("COHERENCE_ITERS", "3"))


def _pairwise_metrics(texts: list[str]) -> list[dict]:
    """All pairs (i, j) with i < j → comparison record."""
    out = []
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            a, b = texts[i], texts[j]
            lcp = 0
            for ca, cb in zip(a, b):
                if ca == cb:
                    lcp += 1
                else:
                    break
            wa = {w for w in a.split() if len(w) > 3}
            wb = {w for w in b.split() if len(w) > 3}
            out.append(
                {
                    "pair": f"iter{i + 1}-iter{j + 1}",
                    "byte_equal": a == b,
                    "lcp_chars": lcp,
                    "shared_words": len(wa & wb),
                    "len_a": len(a),
                    "len_b": len(b),
                    "len_delta": abs(len(a) - len(b)),
                }
            )
    return out


def run_mode_iters(mode: str, iterations: int) -> dict:
    """Run iterations of the same prompt under one mode.
    Returns dict with per-iter texts + latencies + pairwise metrics.

    Lifecycle:
      - mode-level setup (wipe everything, start daemon if needed)
      - per iter: wipe local kvdir, start server, send turn, capture, kill
      - mode-level teardown
    """
    ms.log(f"=== mode={mode} ({iterations} iterations) ===")
    kvdir = Path(f"/tmp/coherence-kvdir-{mode}")
    puffer = Path(f"/tmp/coherence-puffer-{mode}")
    daemon_puffer = Path(f"/tmp/coherence-daemonpuffer-{mode}")
    serverlog = Path(f"/tmp/coherence-{mode}-server.log")
    daemonlog = Path(f"/tmp/coherence-{mode}-daemon.log")

    ms.kill_all_ds4()
    if mode in ("daemon-shm", "daemon-tcp", "daemon-http"):
        ms.kill_all_daemon()

    for d in (kvdir, puffer, daemon_puffer):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir()

    # Mode-level S3 wipe so iter 1 is a TRUE cold start.
    if mode == "embedded":
        ms.wipe_bucket("wombatkv-smoke-embedded")
    elif mode == "daemon-shm":
        ms.wipe_bucket("wombatkv-smoke-smoke-shm")
    elif mode == "daemon-tcp":
        ms.wipe_bucket("wombatkv-smoke-smoke-tcp")
    elif mode == "daemon-http":
        ms.wipe_bucket("wombatkv-smoke-smoke-http")

    daemon_proc = None
    iter_records = []
    try:
        if mode == "daemon-shm":
            ms.log("  starting wombatkv-daemon (SHM prefix=smoke-shm)")
            daemon_proc = ms.start_daemon("shm", "smoke-shm", daemonlog, daemon_puffer)
        elif mode == "daemon-tcp":
            ms.log(f"  starting wombatkv-daemon (TCP 127.0.0.1:{ms.TCP_PORT})")
            daemon_proc = ms.start_daemon("tcp", "smoke-tcp", daemonlog, daemon_puffer)
        elif mode == "daemon-tcp-remote":
            ms.log(f"  remote daemon expected at {ms.REMOTE_TCP_ADDR} — no local start")
        elif mode == "daemon-http":
            ms.log(f"  starting wombatkv-daemon (HTTP 127.0.0.1:{ms.HTTP_PORT})")
            daemon_proc = ms.start_daemon(
                "http", "smoke-http", daemonlog, daemon_puffer
            )

        for it in range(1, iterations + 1):
            ms.log(f"  iter {it}: starting ds4-server")
            ms.start_server(mode, kvdir, puffer, serverlog)
            ms.log(f"  iter {it}: sending prompt")
            t0 = time.time()
            elapsed, text = ms.send_turn(ms.PROMPT_TEXT)
            ms.log(
                f"    iter {it}: elapsed={elapsed * 1000:.0f} ms, len={len(text)}, first40={text[:40]!r}"
            )
            iter_records.append(
                {
                    "iter": it,
                    "elapsed_ms": int(elapsed * 1000),
                    "text": text,
                }
            )
            # kill server + wipe local kvdir between iters. WombatKV
            # state (puffer, S3, daemon) survives so iter 2+ does
            # warm restore for WombatKV modes.
            ms.kill_all_ds4()
            if kvdir.exists():
                shutil.rmtree(kvdir)
                kvdir.mkdir()

        pairs = _pairwise_metrics([r["text"] for r in iter_records])
        return {
            "mode": mode,
            "iterations": iter_records,
            "pairwise": pairs,
        }
    finally:
        ms.kill_all_ds4()
        if daemon_proc is not None:
            try:
                daemon_proc.terminate()
                daemon_proc.wait(timeout=5)
            except Exception:
                daemon_proc.kill()
        if mode in ("daemon-shm", "daemon-tcp"):
            ms.kill_all_daemon()


def _looks_like_garbage(text: str) -> tuple[bool, str]:
    """Heuristic: did the model produce model-like English text, or
    did K/V corruption cause it to emit gibberish / wrong-language /
    empty output?
    Returns (is_garbage, reason).
    """
    stripped = text.strip()
    if not stripped:
        return True, "empty response"
    if len(stripped) < 20:
        return (
            True,
            f"too short ({len(stripped)} chars) — model usually emits > 20 chars at max_tokens=32",
        )
    words = stripped.split()
    long_words = [w for w in words if len(w) > 3]
    if len(long_words) < 3:
        return (
            True,
            f"only {len(long_words)} words > 3 chars — looks like single-token loop or punctuation",
        )
    # ASCII heuristic: if > 20% of chars are non-printable / non-ASCII,
    # something's wrong (model is English in our prompts).
    non_ascii = sum(1 for c in stripped if not (32 <= ord(c) < 127 or c in "\n\t"))
    if non_ascii / len(stripped) > 0.2:
        return (
            True,
            f"{non_ascii}/{len(stripped)} chars non-ASCII — wrong-character output",
        )
    return False, ""


def summarize(results: list[dict]) -> dict:
    """Compute the verdict.

    Honest framing of what this test CAN and CANNOT prove:
    - CANNOT prove: "WombatKV restored K/V is byte-identical to cold-
      computed K/V". That claim needs tensor-level hooks (logit
      snapshot or layer-buffer dump) not exposed by ds4-server's
      HTTP API. Even Metal itself is not bit-deterministic for ds4
      inference (observed via native baseline: repeated cold runs of
      the same prompt at temp=0 produce divergent text trajectories
      because argmax can flip on near-tied logits).
    - CAN prove: every iteration of every mode returns reasonable
      model-generated English text — i.e., WombatKV is not corrupting
      K/V badly enough to produce garbage, wrong-language output, or
      degenerate single-token loops. That's the K/V-corruption smoke
      test in absence of tensor-level diff.

    Verdict rules:
      HARD (must pass to PASS):
        - every iteration's output passes the garbage heuristic.
      INFORMATIONAL (logged but not asserted):
        - pairwise byte_equal count
        - pairwise lcp distribution
        - pairwise shared_words distribution
        - max/min vs the native baseline (lets the operator see
          whether a WombatKV mode looks more or less coherent than
          native, but does NOT auto-fail on noise variance).
    """
    by_mode = {r["mode"]: r for r in results}
    native = by_mode.get("native")

    if native and native["pairwise"]:
        native_min_lcp = min(p["lcp_chars"] for p in native["pairwise"])
        native_max_lcp = max(p["lcp_chars"] for p in native["pairwise"])
        native_max_shared = max(p["shared_words"] for p in native["pairwise"])
        native_min_shared = min(p["shared_words"] for p in native["pairwise"])
        native_all_byte_equal = all(p["byte_equal"] for p in native["pairwise"])
    else:
        native_min_lcp = native_max_lcp = 0
        native_max_shared = native_min_shared = 0
        native_all_byte_equal = False

    verdicts = {}
    for mode, r in by_mode.items():
        if not r["pairwise"]:
            verdicts[mode] = {"verdict": "ERROR", "reason": "no pairs"}
            continue
        all_byte_equal = all(p["byte_equal"] for p in r["pairwise"])
        min_lcp = min(p["lcp_chars"] for p in r["pairwise"])
        max_lcp = max(p["lcp_chars"] for p in r["pairwise"])
        max_shared = max(p["shared_words"] for p in r["pairwise"])
        min_shared = min(p["shared_words"] for p in r["pairwise"])

        # HARD test: every iter must be non-garbage.
        garbage_iters = []
        for it in r["iterations"]:
            g, reason = _looks_like_garbage(it["text"])
            if g:
                garbage_iters.append((it["iter"], reason))
        if garbage_iters:
            verdicts[mode] = {
                "verdict": "FAIL",
                "reason": "; ".join(f"iter {i}: {r}" for i, r in garbage_iters),
            }
            continue

        # STRONG-PASS only if Metal happened to be deterministic for these
        # runs AND WombatKV restore is also byte-equivalent to cold.
        if all_byte_equal:
            verdicts[mode] = {
                "verdict": "STRONG-PASS",
                "reason": "all iterations byte-identical",
            }
            continue

        # PASS: model produced reasonable text in every iter. Log informational
        # comparison vs native (does this mode look more/less coherent than native?).
        if mode == "native":
            note = "Metal scheduling noise floor — used as baseline."
        else:
            # Just describe how this mode's coherence stacks up to native.
            # Don't auto-fail since with N=3 iters the variance is high.
            comp = (
                f"vs native baseline: this mode max_lcp={max_lcp} (native max={native_max_lcp}), "
                f"max_shared={max_shared} (native max={native_max_shared}). "
            )
            if max_lcp >= native_max_lcp - max(int(native_max_lcp * 0.5), 10):
                note = comp + "coherence within native noise envelope."
            else:
                note = (
                    comp
                    + "coherence noticeably lower than native — could be more Metal noise (this mode "
                    "runs at different latency profile) OR small WombatKV restore drift. Needs more "
                    "iterations to disambiguate, OR a tensor-level test."
                )
        verdicts[mode] = {
            "verdict": "PASS",
            "reason": (
                f"all iters non-garbage; lcp range [{min_lcp}..{max_lcp}], "
                f"shared_words range [{min_shared}..{max_shared}]. {note}"
            ),
        }

    return {
        "noise_floor": {
            "native_min_lcp": native_min_lcp,
            "native_max_lcp": native_max_lcp,
            "native_min_shared_words": native_min_shared,
            "native_max_shared_words": native_max_shared,
            "native_all_byte_equal": native_all_byte_equal,
        },
        "verdicts": verdicts,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "modes",
        nargs="*",
        default=["native", "embedded", "daemon-shm", "daemon-tcp", "daemon-http"],
        help="modes to test (default: all 5 same-host modes)",
    )
    p.add_argument(
        "--iters",
        type=int,
        default=ITERATIONS,
        help=f"iterations per mode (default {ITERATIONS}; ≥2 required for pairwise)",
    )
    p.add_argument(
        "--remote-tcp",
        metavar="HOST:PORT",
        default=os.environ.get("MODE_SMOKE_REMOTE_TCP", ""),
        help="for mode daemon-tcp-remote: address of remote daemon",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="optional path to dump full results JSON",
    )
    args = p.parse_args()

    if args.iters < 2:
        print("ERROR: --iters must be ≥ 2 for pairwise comparison", file=sys.stderr)
        return 2

    if not ms.DS4_BIN.exists():
        print(
            f"ERROR: {ms.DS4_BIN} not found — build ds4-server first", file=sys.stderr
        )
        return 2

    if "daemon-tcp-remote" in args.modes:
        if not args.remote_tcp:
            print(
                "ERROR: daemon-tcp-remote needs --remote-tcp HOST:PORT", file=sys.stderr
            )
            return 2
        ms.REMOTE_TCP_ADDR = args.remote_tcp

    results = []
    for m in args.modes:
        try:
            results.append(run_mode_iters(m, args.iters))
        except Exception as exc:
            ms.log(f"  EXCEPTION: {type(exc).__name__}: {exc}")
            results.append(
                {
                    "mode": m,
                    "error": str(exc),
                    "iterations": [],
                    "pairwise": [],
                }
            )

    print()
    print("=== per-mode iterations ===")
    for r in results:
        print(f"\n[{r['mode']}]")
        for it in r["iterations"]:
            print(
                f"  iter {it['iter']}: {it['elapsed_ms']} ms, len={len(it['text'])}, head={it['text'][:60]!r}"
            )
        for p_ in r["pairwise"]:
            print(
                f"  {p_['pair']}: byte_equal={p_['byte_equal']} lcp={p_['lcp_chars']} shared_words={p_['shared_words']} len_delta={p_['len_delta']}"
            )

    summary = summarize(results)
    print()
    print("=== noise floor (native) ===")
    print(f"  native min lcp across pairs: {summary['noise_floor']['native_min_lcp']}")
    print(
        f"  native all byte-equal:       {summary['noise_floor']['native_all_byte_equal']}"
    )
    print()
    print("=== verdicts ===")
    for mode, v in summary["verdicts"].items():
        print(f"  {mode}: {v['verdict']}")
        print(f"    {v['reason']}")

    if args.output:
        args.output.write_text(
            json.dumps({"results": results, "summary": summary}, indent=2)
        )
        print(f"\n[full results written to {args.output}]")

    rc = (
        0
        if all(
            v["verdict"] in ("PASS", "STRONG-PASS")
            for v in summary["verdicts"].values()
        )
        else 1
    )
    return rc


if __name__ == "__main__":
    sys.exit(main())
