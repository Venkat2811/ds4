#!/usr/bin/env python3
"""WombatKV showcase shared library.

Common utilities for the multi-session showcase scenarios:
  - 3 deployment modes (C1 native, C2 WombatKV M0 embedded, C3 WombatKV M1 daemon)
  - ds4-server lifecycle (start/stop on N ports)
  - Inference request with TTFT + total-time + cached_tokens metrics
  - Multi-turn conversation context building
  - MinIO bucket cleanup helpers

Patterns inherited verbatim from scripts/run_5mode_bench.py (env_for/start/stop/req).
"""

import json
import http.client
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import time

# -----------------------------------------------------------------------------
# Static config
# -----------------------------------------------------------------------------

DS4_DIR = pathlib.Path("/Users/venkat/Documents/p/venkat-github/myelon-launch/ds4")
DS4_BIN = DS4_DIR / "ds4-server"

# wombatkv-daemon binary lives in the wombatkv (the WombatKV upstream) tree.
WOMBATKV_DAEMON_BIN = pathlib.Path(
    "/Users/venkat/Documents/p/venkat-github/tensorpuffer/target/release/wombatkv-daemon"
)

MODEL = "gguf/DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix.gguf"
PROMPT_FILE = pathlib.Path("/tmp/pg1184.txt")

# Showcase ports: 8000-8004 (5 concurrent ds4-server instances)
SHOWCASE_PORTS = [8000, 8001, 8002, 8003, 8004]

# MinIO defaults (matches run_5mode_bench.py + demo_wombatkv.sh).
S3_ENDPOINT = "http://127.0.0.1:9200"
S3_ACCESS_KEY = "minioadmin"
S3_SECRET_KEY = "minioadmin"
S3_NAMESPACE = "ds4-metal"
FINGERPRINT24 = "deadbeefcafe1234567890ab"

MAX_TOKENS = int(os.environ.get("SHOWCASE_MAX_TOKENS", "50"))


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# -----------------------------------------------------------------------------
# Env construction per mode
# -----------------------------------------------------------------------------


def _common_wombatkv_env():
    """Shared WombatKV S3 + tuning env. Mirrors run_5mode_bench.common_wkv_env."""
    return {
        "DS4_WOMBATKV_FINGERPRINT24": FINGERPRINT24,
        "WMBT_KV_TIMING": "1",
        "WMBT_KV_S3_ENDPOINT": S3_ENDPOINT,
        "WMBT_KV_S3_ACCESS_KEY": S3_ACCESS_KEY,
        "WMBT_KV_S3_SECRET_KEY": S3_SECRET_KEY,
        "WMBT_KV_NAMESPACE": S3_NAMESPACE,
        "WMBT_KV_EMBEDDED_ASYNC_S3": "1",
        "WMBT_KV_CHUNK_VERIFY": "0",
        "WMBT_KV_S3_PREWARM": "8",
        "WMBT_KV_CHUNK_BYTES": "8388608",
        "WMBT_KV_TIER_B_BLOCK_TOKENS": "128",
        "WMBT_KV_BOOTSTRAP_SLATEDB": "1",
    }


def env_for_mode(mode, *, puffer_dir, bucket, daemon_prefix=None):
    """Build full env for a single ds4-server in the given showcase mode.

    Mode keys: c1_native | c2_embedded | c3_daemon.
    `puffer_dir` is the WombatKV foyer disk dir (one per ds4 instance).
    `bucket` is the shared S3 bucket name (same across instances in a mode).
    `daemon_prefix` is required for c3_daemon (SHM ring prefix to talk to the daemon).
    """
    env = os.environ.copy()
    if mode == "c1_native":
        # Pure local ds4. Strip any WombatKV vars from the parent env so a
        # caller with stale exports does not accidentally enable WombatKV.
        for k in list(env.keys()):
            if k.startswith("WMBT_KV_") or k == "DS4_WOMBATKV_ENABLE":
                env.pop(k, None)
        return env

    if mode in ("c2_embedded", "c3_daemon"):
        env.update(_common_wombatkv_env())
        env["DS4_WOMBATKV_ENABLE"] = "1"
        env["WMBT_KV_BUCKET"] = bucket
        env["WMBT_KV_PUFFER_DIR"] = puffer_dir

        if mode == "c3_daemon":
            assert daemon_prefix, "c3_daemon requires daemon_prefix"
            env["WMBT_KV_REMOTE_PREFIX"] = daemon_prefix
            # Tier B engagement: daemon already keeps state; no need for ds4
            # to run its own bootstrap. Keep timing on for parity.
        return env

    raise ValueError(f"unknown showcase mode: {mode}")


# -----------------------------------------------------------------------------
# ds4-server lifecycle
# -----------------------------------------------------------------------------


def start_server(env, *, port, kvdisk, log_path, ctx=32768, boot_timeout=120):
    """Start one ds4-server instance on `port`. Returns Popen handle.

    Mirrors run_5mode_bench.start(); the only addition is a per-port arg so we
    can run 5 concurrent instances for scenario 1.
    """
    pathlib.Path(kvdisk).mkdir(parents=True, exist_ok=True)
    pathlib.Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    args = [
        str(DS4_BIN),
        "--model",
        MODEL,
        "--ctx",
        str(ctx),
        "--kv-disk-dir",
        str(kvdisk),
        "--kv-cache-min-tokens",
        "256",
        "--kv-disk-space-mb",
        "16384",
        "--port",
        str(port),
    ]
    lf = open(log_path, "wb")
    p = subprocess.Popen(
        args, cwd=str(DS4_DIR), env=env, stdout=lf, stderr=subprocess.STDOUT
    )
    deadline = time.time() + boot_timeout
    while time.time() < deadline:
        try:
            txt = open(log_path, "rb").read().decode("utf-8", errors="replace")
            if "refusing to start" in txt:
                p.kill()
                lf.close()
                raise RuntimeError(f"ds4-server refused: {log_path}")
            if "listening on http" in txt:
                lf.close()
                time.sleep(0.6)
                return p
        except FileNotFoundError:
            pass
        time.sleep(0.2)
    p.kill()
    lf.close()
    raise RuntimeError(f"ds4-server did not boot in {boot_timeout}s: {log_path}")


def stop_server(p):
    """Graceful SIGTERM + SIGKILL fallback. Mirrors run_5mode_bench.stop."""
    if p is None or p.poll() is not None:
        return
    try:
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            p.send_signal(signal.SIGKILL)
            p.wait(timeout=3)
        except Exception:
            pass


# -----------------------------------------------------------------------------
# WombatKV daemon (M1 mode) lifecycle
# -----------------------------------------------------------------------------


def start_wombatkv_daemon(*, prefixes, bucket, puffer_dir, log_path, boot_timeout=30):
    """Start a single wombatkv-daemon serving N SHM prefixes.

    All N ds4-server instances in c3_daemon mode connect to this one daemon
    via WMBT_KV_REMOTE_PREFIX=<prefix_i>. They share a single foyer + S3
    backend, so cross-agent KV-block reuse happens in-process at the daemon.
    """
    pathlib.Path(puffer_dir).mkdir(parents=True, exist_ok=True)
    pathlib.Path(log_path).parent.mkdir(parents=True, exist_ok=True)

    args = [str(WOMBATKV_DAEMON_BIN)]
    for pfx in prefixes:
        args += ["--prefix", pfx]

    daemon_env = os.environ.copy()
    daemon_env.update(_common_wombatkv_env())
    daemon_env["WMBT_KV_BUCKET"] = bucket
    daemon_env["WMBT_KV_S3_BUCKET"] = bucket  # daemon reads from this var name
    daemon_env["WMBT_KV_PUFFER_DIR"] = puffer_dir

    lf = open(log_path, "wb")
    p = subprocess.Popen(args, env=daemon_env, stdout=lf, stderr=subprocess.STDOUT)

    # Daemon doesn't emit a clean "listening" banner in all versions; poll for
    # SHM segment creation OR a successful liveness window. We use a short
    # fixed boot delay (give it 3s to wire up rings) then assume up. Boot
    # failure surfaces fast through ds4-server's connect attempt anyway.
    time.sleep(3.0)
    if p.poll() is not None:
        lf.close()
        raise RuntimeError(f"wombatkv-daemon exited early: log={log_path}")
    lf.close()
    return p


def stop_wombatkv_daemon(p):
    stop_server(p)


# -----------------------------------------------------------------------------
# Inference request
# -----------------------------------------------------------------------------


def send_chat(port, messages, *, max_tokens=None, timeout_s=600):
    """Send an OpenAI-format /v1/chat/completions request, stream the SSE,
    return metrics. `messages` is the full prior conversation history.

    Returns dict with:
      ttft_ms      first-byte-of-`data:` latency (None on failure)
      total_ms     full request → completion latency
      input_chars  rough estimate (sum of all content chars in messages)
      cached_tokens_seen   prompt_tokens_details.cached_tokens if present in final usage event
      raw_chars    bytes received from server (useful for debugging)
    """
    if max_tokens is None:
        max_tokens = MAX_TOKENS

    body = {
        "model": "deepseek-v4-flash",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": True,
    }
    payload = json.dumps(body)
    input_chars = sum(len(m.get("content", "")) for m in messages)

    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout_s)
    t0 = time.perf_counter()
    try:
        c.request(
            "POST",
            "/v1/chat/completions",
            payload,
            headers={"Content-Type": "application/json"},
        )
        r = c.getresponse()
    except Exception as e:
        c.close()
        return {
            "ttft_ms": None,
            "total_ms": None,
            "input_chars": input_chars,
            "cached_tokens_seen": None,
            "raw_chars": 0,
            "error": f"request failed: {e}",
        }

    ttft_ms = None
    raw_bytes = bytearray()
    while True:
        ch = r.read1(4096)
        if not ch:
            break
        raw_bytes.extend(ch)
        if ttft_ms is None and b"data:" in ch:
            ttft_ms = (time.perf_counter() - t0) * 1000.0
    total_ms = (time.perf_counter() - t0) * 1000.0
    c.close()

    raw = raw_bytes.decode("utf-8", errors="replace")
    cached = _scrape_cached_tokens(raw)

    return {
        "ttft_ms": ttft_ms,
        "total_ms": total_ms,
        "input_chars": input_chars,
        "cached_tokens_seen": cached,
        "raw_chars": len(raw),
    }


_CACHED_RE = re.compile(r'"cached_tokens"\s*:\s*(\d+)')


def _scrape_cached_tokens(sse_text):
    """ds4-server emits usage.prompt_tokens_details.cached_tokens in the
    final SSE event when the server side observes a Tier A / Tier B hit
    (per task #60: "Server: add cached_tokens to Usage.prompt_tokens_details").
    Take the MAX across all events seen (Tier B can grow it across chunks).
    """
    vals = [int(m.group(1)) for m in _CACHED_RE.finditer(sse_text)]
    return max(vals) if vals else 0


# -----------------------------------------------------------------------------
# Multi-turn conversation helpers
# -----------------------------------------------------------------------------


def build_messages(
    system_prompt, prior_turns, new_user_msg, assistant_placeholder="(continuing)"
):
    """Compose the full prior-history prompt that ds4 sees for a turn.

    prior_turns: list of (user_msg, assistant_msg) tuples for turns 1..N-1.
    new_user_msg: this turn's user message.

    The assistant content from prior turns is a synthetic placeholder so
    ds4 sees a deterministic conversation prefix across all 3 modes. The
    actual model output is discarded (we measure TTFT, not generation
    quality). This keeps the input-token count identical mode-to-mode.
    """
    msgs = [{"role": "system", "content": system_prompt}]
    for u, _a in prior_turns:
        msgs.append({"role": "user", "content": u})
        msgs.append({"role": "assistant", "content": assistant_placeholder})
    msgs.append({"role": "user", "content": new_user_msg})
    return msgs


# -----------------------------------------------------------------------------
# Workspace + MinIO cleanup
# -----------------------------------------------------------------------------


def wipe(*paths):
    """Mirrors run_5mode_bench.wipe — remove + recreate dirs."""
    for p in paths:
        shutil.rmtree(p, ignore_errors=True)
        pathlib.Path(p).mkdir(parents=True, exist_ok=True)


def reset_minio_bucket(bucket, *, alias="local"):
    """Best-effort: rm all objects under bucket so a fresh trial starts clean.

    Uses `mc` from PATH; skip silently if not present. Mirrors the pattern in
    run_5mode_bench.py: each (mode, cell, trial) gets its own bucket name so
    cross-run isolation is automatic, but for repeated runs of the same
    showcase we still want a clean bucket per trial.
    """
    if not shutil.which("mc"):
        return
    try:
        subprocess.run(
            ["mc", "mb", "--ignore-existing", f"{alias}/{bucket}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        subprocess.run(
            ["mc", "rm", "-r", "--force", f"{alias}/{bucket}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except Exception:
        pass


# -----------------------------------------------------------------------------
# Scenario helpers exposed to scenario scripts
# -----------------------------------------------------------------------------


def kill_stale_servers():
    """Kill any leftover ds4-server / wombatkv-daemon from a prior aborted
    run. Bench scripts call this once at scenario start.
    """
    for binname in ("ds4-server", "wombatkv-daemon"):
        try:
            subprocess.run(
                ["pkill", "-f", binname],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except Exception:
            pass
    time.sleep(1.0)


def short_daemon_prefix(scenario_tag, idx):
    """Generate a daemon SHM prefix that fits the macOS POSIX SHM 31-char
    name budget. Per memory_macOS_shm_budget: prefix ≤ 18 chars.

    Examples: prefix=sc1a0, sc1a1, sc2b0, ...
    """
    # 2-letter scen tag + 1-letter run + index → ~5 chars total, ample budget.
    return f"sc{scenario_tag}{idx}"


def percentile(xs, q):
    if not xs:
        return float("nan")
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round((q / 100.0) * (len(s) - 1)))))
    return s[k]
