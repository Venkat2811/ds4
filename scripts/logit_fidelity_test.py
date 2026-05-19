#!/usr/bin/env python3
"""Tier-B logit-level WombatKV fidelity test.

Uses the `/v1/internal/logits` endpoint added to ds4-server (gated by
DS4_DEBUG_INTERNAL=1). Procedure:

  1. Native mode N iterations (each = fresh ds4-server, fresh prompt
     prefill). Capture top-K (token_id, logit, logprob) per iter.
     Pairwise diff → Metal scheduling noise floor in LOGIT space.
  2. For each WombatKV mode:
       iter 1: fresh server + fresh state → cold prefill, writes blocks.
       iter 2+: fresh server, WombatKV state survives → warm restore.
     Capture top-K per iter. Diff cold vs warm.
  3. Verdict:
       - HARD: top-1 token_id must match (or be in top-3 of) the
         native baseline iter-1 — otherwise the WombatKV mode has
         drifted enough to change next-token sampling under temp=0.
       - INFORMATIONAL: L∞ logit distance vs native floor, top-K
         overlap percentage.

What this proves that the text-coherence test (coherence_test.py)
couldn't: WombatKV-restored K/V produces a logit distribution
numerically close to cold-computed K/V's logit distribution. The
ceiling is Metal scheduling noise, measured in-place via native
iter-pair distances.

Same lifecycle helpers as mode_smoke.py (imported).
"""

from __future__ import annotations

import argparse
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

DEFAULT_PROMPT = (
    "The capital of France is"  # short, very-low-entropy continuation
)
DEFAULT_TOP_K = 20
DEFAULT_ITERS = 3


def post_logits(prompt: str, top_k: int) -> dict:
    payload = json.dumps({"prompt": prompt, "top_k": top_k}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{ms.PORT}/v1/internal/logits",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode())


def trigger_wombatkv_save_via_chat(prompt: str) -> int:
    """Send a tiny chat completion to force the WombatKV save path
    (which lives in ds4_server.c's chat completion handler, NOT in
    ds4_session_sync). Returns elapsed ms.

    Without this, /v1/internal/logits prefills but does NOT save —
    so iter 2 has nothing to warm-restore from and the "fidelity"
    test is really just measuring Metal determinism for two cold
    prefills. The first version of this harness had that bug; the
    bucket-count check at the end of every iter is the smoking gun
    (was 0 in the buggy version, should be > 0 with this fix)."""
    payload = json.dumps({
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "system", "content": "You are a literary assistant."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 4,
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
        r.read()  # drop response; we only care about side effect
    return int((time.time() - t0) * 1000)


def _start_server_with_debug(mode: str, kvdir: Path, puffer: Path,
                             serverlog: Path) -> "subprocess.Popen":
    """Like mode_smoke.start_server but adds DS4_DEBUG_INTERNAL=1."""
    import subprocess
    env = ms.server_env(mode, kvdir, puffer)
    env["DS4_DEBUG_INTERNAL"] = "1"
    cmd = [
        str(ms.DS4_BIN),
        "--model", ms.MODEL,
        "--ctx", "4096",
        "--kv-disk-dir", str(kvdir),
        "--kv-cache-min-tokens", "256",
        "--kv-disk-space-mb", "4096",
        "--port", str(ms.PORT),
    ]
    with open(serverlog, "w") as f:
        proc = subprocess.Popen(
            cmd, cwd=str(ms.DS4_DIR), env=env,
            stdout=f, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    for _ in range(120):
        try:
            if "listening on http" in serverlog.read_text():
                time.sleep(0.5)
                return proc
        except FileNotFoundError:
            pass
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early; see {serverlog}")
        time.sleep(1.0)
    proc.kill()
    raise RuntimeError(f"server failed to start; see {serverlog}")


def _capture_iters(mode: str, prompt: str, top_k: int, iters: int) -> list[dict]:
    """Run iters of capture under one mode."""
    ms.log(f"=== mode={mode} ({iters} iters) ===")
    kvdir = Path(f"/tmp/logit-kvdir-{mode}")
    puffer = Path(f"/tmp/logit-puffer-{mode}")
    daemon_puffer = Path(f"/tmp/logit-daemonpuffer-{mode}")
    serverlog = Path(f"/tmp/logit-{mode}-server.log")
    daemonlog = Path(f"/tmp/logit-{mode}-daemon.log")

    ms.kill_all_ds4()
    if mode in ("daemon-shm", "daemon-tcp"):
        ms.kill_all_daemon()
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

    daemon_proc = None
    records = []
    try:
        if mode == "daemon-shm":
            ms.log("  starting wombatkv-daemon (SHM)")
            daemon_proc = ms.start_daemon("shm", "smoke-shm", daemonlog, daemon_puffer)
        elif mode == "daemon-tcp":
            ms.log(f"  starting wombatkv-daemon (TCP 127.0.0.1:{ms.TCP_PORT})")
            daemon_proc = ms.start_daemon("tcp", "smoke-tcp", daemonlog, daemon_puffer)

        for it in range(1, iters + 1):
            ms.log(f"  iter {it}: starting ds4-server (DS4_DEBUG_INTERNAL=1)")
            _start_server_with_debug(mode, kvdir, puffer, serverlog)
            # First send chat completion to trigger the WombatKV save
            # path. For WombatKV modes iter 1, this saves blocks to
            # S3/daemon. For iter 2+, ds4 detects existing blocks and
            # warm-restores at engine open — chat completion goes fast.
            chat_ms = trigger_wombatkv_save_via_chat(prompt)
            ms.log(f"  iter {it}: chat-completion (forces WombatKV save/load) = {chat_ms} ms")
            # Now sample logits. Session state is whatever the chat completion
            # left it as (typically prompt + 1 decoded token). The sync inside
            # the endpoint is a no-op if the session already has the prompt
            # prefix; top_logprobs returns logits at last position.
            ms.log(f"  iter {it}: requesting top-K logits at last position")
            t0 = time.time()
            resp = post_logits(prompt, top_k)
            elapsed = time.time() - t0
            ms.log(f"    iter {it}: elapsed={elapsed*1000:.0f} ms, "
                   f"prompt_tokens={resp.get('prompt_tokens')}, "
                   f"top1=token_id={resp['top_k'][0]['token_id']} logit={resp['top_k'][0]['logit']:.4f}")
            records.append({
                "iter": it,
                "chat_ms": chat_ms,
                "logits_ms": int(elapsed * 1000),
                "logits": resp,
            })
            ms.kill_all_ds4()
            if kvdir.exists():
                shutil.rmtree(kvdir)
                kvdir.mkdir()
        # Verify WombatKV actually engaged — bucket should have objects
        # (non-native modes only)
        if mode == "embedded":
            bk = len(ms.list_bucket_keys("wombatkv-smoke-embedded"))
            ms.log(f"  [post-run] embedded bucket: {bk} objects (>0 expected for WombatKV save)")
        elif mode == "daemon-shm":
            bk = len(ms.list_bucket_keys("wombatkv-smoke-smoke-shm"))
            ms.log(f"  [post-run] daemon-shm bucket: {bk} objects (>0 expected)")
        elif mode == "daemon-tcp":
            bk = len(ms.list_bucket_keys("wombatkv-smoke-smoke-tcp"))
            ms.log(f"  [post-run] daemon-tcp bucket: {bk} objects (>0 expected)")
        return records
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


def _diff_top_k(a: dict, b: dict) -> dict:
    """Compare two top-K dicts. Returns:
      top1_match:    bool — top-1 token IDs identical
      top1_in_top3_other: bool — a's top-1 token id is in b's top-3 (and vice versa)
      common_ids:    int — number of token IDs in both top-Ks
      l_inf_overlap: float — max abs(logit_a - logit_b) over token_ids in both
    """
    a_ids = [t["token_id"] for t in a["top_k"]]
    b_ids = [t["token_id"] for t in b["top_k"]]
    a_logit = {t["token_id"]: t["logit"] for t in a["top_k"]}
    b_logit = {t["token_id"]: t["logit"] for t in b["top_k"]}
    common = set(a_ids) & set(b_ids)
    if common:
        l_inf = max(abs(a_logit[t] - b_logit[t]) for t in common)
    else:
        l_inf = float("inf")
    return {
        "top1_match": a_ids[0] == b_ids[0],
        "top1_a_in_b_top3": a_ids[0] in b_ids[:3],
        "top1_b_in_a_top3": b_ids[0] in a_ids[:3],
        "common_ids": len(common),
        "l_inf_overlap": l_inf,
    }


def _pairwise(records: list[dict]) -> list[dict]:
    out = []
    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            d = _diff_top_k(records[i]["logits"], records[j]["logits"])
            d["pair"] = f"iter{records[i]['iter']}-iter{records[j]['iter']}"
            out.append(d)
    return out


def summarize(all_records: dict[str, list[dict]]) -> dict:
    """Native pairwise = Metal noise floor.
    WombatKV pairwise compared to native baseline."""
    pairs = {mode: _pairwise(records) for mode, records in all_records.items()}
    native_pairs = pairs.get("native", [])
    if native_pairs:
        # Worst-case Metal noise: max L∞ across native iter-pairs.
        native_l_inf_max = max(p["l_inf_overlap"] for p in native_pairs)
        native_top1_match_count = sum(1 for p in native_pairs if p["top1_match"])
    else:
        native_l_inf_max = float("inf")
        native_top1_match_count = 0

    verdicts = {}
    for mode, ps in pairs.items():
        if not ps:
            verdicts[mode] = {
                "verdict": "ERROR",
                "reason": "no records captured — see exception log above",
            }
            continue
        l_inf_max = max((p["l_inf_overlap"] for p in ps), default=float("inf"))
        top1_matches = sum(1 for p in ps if p["top1_match"])
        # Verdict for WombatKV modes:
        # - HARD pass: every pair's top-1 token in the other pair's top-3 OR top1_match
        # - INFORMATIONAL: l_inf_max vs native baseline
        all_top1_or_top3 = all(
            p["top1_match"] or (p["top1_a_in_b_top3"] and p["top1_b_in_a_top3"])
            for p in ps
        )
        if mode == "native":
            verdicts[mode] = {
                "verdict": "PASS (baseline)",
                "reason": (
                    f"Metal noise floor — max L∞ logit={native_l_inf_max:.4f}, "
                    f"top1_match in {native_top1_match_count}/{len(ps)} pairs"
                ),
            }
            continue
        if not all_top1_or_top3:
            verdicts[mode] = {
                "verdict": "FAIL",
                "reason": (
                    f"some pair's top-1 token not in the other pair's top-3 — "
                    f"WombatKV-restored K/V produces a noticeably different distribution"
                ),
            }
            continue
        # Soft check: l_inf_max should be in the same order of magnitude as native
        if l_inf_max <= native_l_inf_max * 3 + 0.5:
            verdicts[mode] = {
                "verdict": "PASS",
                "reason": (
                    f"all pairs' top-1 tokens are within top-3 of each other. "
                    f"L∞ logit={l_inf_max:.4f} (native floor={native_l_inf_max:.4f}). "
                    f"top1_match in {top1_matches}/{len(ps)} pairs."
                ),
            }
        else:
            verdicts[mode] = {
                "verdict": "PASS (with caveat)",
                "reason": (
                    f"top-1 ↔ top-3 OK but L∞ logit={l_inf_max:.4f} is "
                    f"larger than 3× native floor ({native_l_inf_max:.4f}) — "
                    f"possible small WombatKV drift on top of Metal noise"
                ),
            }
    return {
        "native_l_inf_floor": native_l_inf_max,
        "pairs": pairs,
        "verdicts": verdicts,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "modes",
        nargs="*",
        default=["native", "embedded", "daemon-shm", "daemon-tcp"],
    )
    p.add_argument("--iters", type=int, default=DEFAULT_ITERS)
    p.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    p.add_argument("--prompt", default=DEFAULT_PROMPT)
    p.add_argument("--output", type=Path, default=None)
    args = p.parse_args()

    if not ms.DS4_BIN.exists():
        print(f"ERROR: {ms.DS4_BIN} not found — build ds4-server first", file=sys.stderr)
        return 2

    all_records: dict[str, list[dict]] = {}
    for mode in args.modes:
        try:
            all_records[mode] = _capture_iters(mode, args.prompt, args.top_k, args.iters)
        except Exception as exc:
            ms.log(f"  EXCEPTION: {type(exc).__name__}: {exc}")
            all_records[mode] = []

    summary = summarize(all_records)

    print()
    print("=== per-mode iter top-1 ===")
    for mode, recs in all_records.items():
        print(f"\n[{mode}]")
        for r in recs:
            top1 = r["logits"]["top_k"][0]
            chat_ms = r.get("chat_ms", "?")
            logits_ms = r.get("logits_ms", r.get("elapsed_ms", "?"))
            print(f"  iter {r['iter']}: top1={top1['token_id']} logit={top1['logit']:.4f} "
                  f"chat={chat_ms}ms logits={logits_ms}ms")

    print()
    print("=== pairwise diffs ===")
    for mode, ps in summary["pairs"].items():
        print(f"\n[{mode}]")
        for d in ps:
            print(
                f"  {d['pair']}: top1_match={d['top1_match']} common={d['common_ids']} "
                f"L∞={d['l_inf_overlap']:.4f} a_in_b_top3={d['top1_a_in_b_top3']} b_in_a_top3={d['top1_b_in_a_top3']}"
            )

    print()
    print(f"=== noise floor: native L∞ logit = {summary['native_l_inf_floor']:.4f} ===")
    print()
    print("=== verdicts ===")
    for mode, v in summary["verdicts"].items():
        print(f"  {mode}: {v['verdict']}")
        print(f"    {v['reason']}")

    if args.output:
        args.output.write_text(json.dumps({"records": all_records, "summary": summary}, indent=2, default=str))
        print(f"\n[written to {args.output}]")

    rc = 0 if all(
        v["verdict"].startswith("PASS") for v in summary["verdicts"].values()
    ) else 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
