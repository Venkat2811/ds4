#!/usr/bin/env python3
"""Multi-trial cell-B bench: ds4-native vs ds4 + WombatKV warm restore.

For each trial:
 1. Wipe MinIO demo buckets + tmp dirs
 2. (native + wombatkv mode separately)
    - Start ds4-server, send turn-1 (cold), record TTFT, kill
    - Wipe kvdisk only (puffer kept for wombatkv parity test), restart
    - Send turn-2 (warm via WombatKV or cold prefill again for native), record TTFT
    - Kill

After N trials, print median + min + max per cell.
"""

import json
import os
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

DS4_DIR = Path("/Users/venkat/Documents/p/venkat-github/myelon-launch/ds4")
DS4_BIN = DS4_DIR / "ds4-server"
MODEL = "gguf/DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix.gguf"
PROMPT_FILE = Path("/tmp/pg1184.txt")
PORT = 8000
N_TRIALS = 5

import boto3
import urllib.request

S3_ENDPOINT = "http://127.0.0.1:9200"


def wipe_minio():
    s3 = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id="minioadmin",
        aws_secret_access_key="minioadmin",
        region_name="us-east-1",
    )
    for b in ("wombatkv-demo-native", "wombatkv-demo-wombatkv"):
        try:
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=b):
                keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if keys:
                    s3.delete_objects(Bucket=b, Delete={"Objects": keys})
            s3.delete_bucket(Bucket=b)
        except Exception as e:
            if "NoSuchBucket" not in str(e):
                print(f"  wipe {b}: {type(e).__name__}")


def kill_servers():
    """Kill any running ds4-server processes and WAIT for them to actually
    exit. ds4-server's shutdown handler does a final WombatKV save + Metal
    flush that can take several hundred milliseconds; a too-short sleep
    leaves the singleton lockfile in place and the next start refuses
    with "another ds4 process is already running"."""
    subprocess.run(["pkill", "-f", "ds4-server"], capture_output=True)
    # Poll until no ds4-server is running, or escalate to SIGKILL.
    for poll in range(40):  # up to 8s
        result = subprocess.run(["pgrep", "-f", "ds4-server"], capture_output=True)
        if result.returncode != 0:
            time.sleep(0.5)  # let the lockfile (if any) get cleaned up
            return
        time.sleep(0.2)
    # Still alive after 8s — force kill.
    subprocess.run(["pkill", "-9", "-f", "ds4-server"], capture_output=True)
    time.sleep(2)


def start_server(mode: str, logfile: Path) -> int:
    kvdir = Path(f"/tmp/multibench-ds4-{mode}")
    puffer = Path(f"/tmp/multibench-puffer-{mode}")
    for d in (kvdir, puffer):
        if d.exists():
            subprocess.run(["rm", "-rf", str(d)])
        d.mkdir()
    env = os.environ.copy()
    if mode == "wombatkv":
        env.update({
            "DS4_WOMBATKV_ENABLE": "1",
            "WMBT_KV_S3_ENDPOINT": S3_ENDPOINT,
            "WMBT_KV_BUCKET": f"wombatkv-demo-{mode}",
            "WMBT_KV_PUFFER_DIR": str(puffer),
            "WMBT_KV_TIMING": "1",
            "WMBT_KV_SKIP_TIER_A_PROBE": "1",
        })
    cmd = [
        str(DS4_BIN),
        "--model", MODEL,
        "--ctx", "32768",
        "--kv-disk-dir", str(kvdir),
        "--kv-cache-min-tokens", "256",
        "--kv-disk-space-mb", "16384",
        "--port", str(PORT),
    ]
    with open(logfile, "w") as f:
        proc = subprocess.Popen(
            cmd, cwd=str(DS4_DIR), env=env,
            stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    for _ in range(90):
        try:
            if "listening on http" in logfile.read_text():
                time.sleep(0.5)
                return proc.pid
        except FileNotFoundError:
            pass
        time.sleep(1)
    proc.kill()
    raise RuntimeError(f"server failed to start; see {logfile}")


def send_request_ttft(prompt_text: str) -> float:
    payload = json.dumps({
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "system", "content": "You are a literary assistant."},
            {"role": "user", "content": f"Here is a passage:\n\n{prompt_text}\n\nSummarize the key themes in 50 words."},
        ],
        "max_tokens": 50,
        "temperature": 0.0,
        "stream": True,
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    started = time.perf_counter()
    with urllib.request.urlopen(req) as resp:
        for raw in resp:
            line = raw.decode(errors="replace").strip()
            if line.startswith("data:"):
                ttft_ms = (time.perf_counter() - started) * 1000.0
                # drain rest of stream
                for _ in resp:
                    pass
                return ttft_ms
    return float("nan")


def warmup_metal() -> float:
    """Fire a tiny unrelated request to JIT Metal kernels + warm the model
    runtime. Returns elapsed ms for telemetry; not used for headline numbers.
    The bench prompt's cache key is unaffected because the content differs."""
    payload = json.dumps({
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "user", "content": "warmup ping"},
        ],
        "max_tokens": 1,
        "temperature": 0.0,
        "stream": True,
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    started = time.perf_counter()
    with urllib.request.urlopen(req) as resp:
        for raw in resp:
            line = raw.decode(errors="replace").strip()
            if line.startswith("data:"):
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                for _ in resp:
                    pass
                return elapsed_ms
    return float("nan")


def run_trial(mode: str, prompt_text: str, trial: int) -> tuple[float, float]:
    """Return (turn1_ttft, turn2_ttft) for a single trial."""
    kill_servers()
    log1 = Path(f"/tmp/multibench-{mode}-trial{trial}-turn1.log")
    log2 = Path(f"/tmp/multibench-{mode}-trial{trial}-turn2.log")
    pid = start_server(mode, log1)
    t1 = send_request_ttft(prompt_text)
    kill_servers()
    # Wipe kvdisk between turns (same as demo_wombatkv.sh).
    subprocess.run(["rm", "-rf", f"/tmp/multibench-ds4-{mode}"])
    start_server(mode, log2)
    # Warm Metal kernels with a tiny unrelated request so turn-2 measures
    # steady-state restore + decode, not first-forward-pass JIT noise.
    warm_ms = warmup_metal()
    print(f"    warmup={warm_ms:.0f} ms (Metal JIT primed)")
    t2 = send_request_ttft(prompt_text)
    kill_servers()
    return t1, t2


def main():
    if not PROMPT_FILE.exists():
        sys.exit(f"FATAL: prompt file missing: {PROMPT_FILE}")
    prompt_text = PROMPT_FILE.read_bytes()[:5200].decode(errors="replace")

    results = {"native": [], "wombatkv": []}
    for mode in ("native", "wombatkv"):
        print(f"\n=== {mode} mode ({N_TRIALS} trials) ===")
        for trial in range(1, N_TRIALS + 1):
            # Wipe MinIO between trials so each turn-1 is a true cold S3 state.
            wipe_minio()
            t1, t2 = run_trial(mode, prompt_text, trial)
            print(f"  trial {trial}/{N_TRIALS}: turn1={t1:.0f} ms (cold), turn2={t2:.0f} ms (after restart)")
            results[mode].append((t1, t2))

    print("\n===== RESULTS =====")
    for mode in ("native", "wombatkv"):
        turns1 = [t1 for t1, _ in results[mode]]
        turns2 = [t2 for _, t2 in results[mode]]
        print(f"\n{mode}:")
        print(f"  turn1 cold:    median={statistics.median(turns1):.0f} ms  "
              f"min={min(turns1):.0f}  max={max(turns1):.0f}  "
              f"n={len(turns1)}")
        print(f"  turn2 restart: median={statistics.median(turns2):.0f} ms  "
              f"min={min(turns2):.0f}  max={max(turns2):.0f}  "
              f"n={len(turns2)}")

    nat_t2 = statistics.median([t2 for _, t2 in results["native"]])
    wmbt_t2 = statistics.median([t2 for _, t2 in results["wombatkv"]])
    if wmbt_t2 > 0:
        print(f"\n  CELL B SPEEDUP (median turn-2 native / median turn-2 wombatkv): {nat_t2/wmbt_t2:.1f}x")


if __name__ == "__main__":
    main()
