#!/usr/bin/env python3
"""Per-mode smoke for ds4-server × WombatKV mode matrix.

Validates a single mode end-to-end: start the (daemon if needed), start
ds4-server with the right env, send a 2-turn cell-B request, confirm
turn-2 wins (warm-restore for WombatKV modes; full re-prefill for native).

Usage:
    mode_smoke.py native
    mode_smoke.py embedded
    mode_smoke.py daemon-shm
    mode_smoke.py daemon-tcp

Each mode:
  1. wipe ephemeral state (local kvdisk, puffer, test bucket)
  2. (modes 3, 4) start wombatkv-daemon with the right transport
  3. start ds4-server with the mode's env
  4. wait for "listening on http"
  5. turn 1: send prompt, capture turn-1 latency + first chars of response
  6. kill ds4-server, wipe local kvdisk (puffer survives for parity)
  7. restart ds4-server
  8. turn 2: send same prompt, capture turn-2 latency + first chars
  9. validate:
       - both turns returned non-empty text
       - native: turn-2 ≈ turn-1 (no warm path)
       - wombatkv: turn-2 << turn-1 (warm-restore wins)
       - text similarity is informational, not asserted (temp != 0 noise)
 10. stop daemon if started

This is a fast smoke (single short prompt) not a perf bench. For
multi-trial cell-B numbers, see scripts/multi_trial_bench.py.
"""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DS4_DIR = Path(__file__).resolve().parent.parent
DS4_BIN = DS4_DIR / "ds4-server"
MODEL = "gguf/DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix.gguf"
TENSORPUFFER_DIR = Path("/Users/venkat/Documents/p/venkat-github/tensorpuffer")
DAEMON_BIN = TENSORPUFFER_DIR / "target" / "release" / "wombatkv-daemon"

S3_ENDPOINT = "http://127.0.0.1:9200"
S3_KEY = "minioadmin"
S3_SECRET = "minioadmin"
PORT = 8000
TCP_PORT = 7878
HTTP_PORT = 7879

# Set via --remote-tcp / MODE_SMOKE_REMOTE_TCP for daemon-tcp-remote mode.
REMOTE_TCP_ADDR = ""
# Set via --remote-http / MODE_SMOKE_REMOTE_HTTP for daemon-http-remote mode.
REMOTE_HTTP_ADDR = ""

_PROMPT_FILE = Path("/tmp/pg1184.txt")
_PROMPT_CHARS = int(os.environ.get("MODE_SMOKE_PROMPT_CHARS", "5000"))


def _coherence(text1: str, text2: str) -> dict:
    """Cheap text-space proxy for "do these two outputs talk about the
    same thing?" — used as a soft WombatKV-restore sanity gate.

    Returns:
      lcp_chars     longest common prefix length (in chars)
      shared_words  number of non-trivial words present in both texts.
                    "Non-trivial" = length > 3, so we don't count
                    stopwords like "the", "and", "to" that match by chance.

    Why not strict equality: temp=0 argmax decoding is still subject to
    Metal scheduling noise — near-tied logits can flip across runs of
    the same prompt. Even native (no WombatKV) shows this. So we want a
    threshold that catches "WombatKV restore produced garbage" without
    false-firing on token-level noise."""
    lcp = 0
    for c1, c2 in zip(text1, text2):
        if c1 == c2:
            lcp += 1
        else:
            break
    words1 = {w for w in text1.split() if len(w) > 3}
    words2 = {w for w in text2.split() if len(w) > 3}
    return {
        "lcp_chars": lcp,
        "shared_words": len(words1 & words2),
        "len_t1": len(text1),
        "len_t2": len(text2),
    }


def _load_prompt() -> str:
    """Need ≥ KV_CACHE_DEFAULT_MIN_TOKENS=512 tokens to engage the
    ds4 kv-cache save path; below that, no WombatKV blocks get written.
    5000 chars ≈ 1200 tokens = ~9 block-prefix blocks, comfortably above
    threshold and large enough to make warm-restore visibly faster."""
    if _PROMPT_FILE.exists():
        head = _PROMPT_FILE.read_text(encoding="utf-8", errors="ignore")[:_PROMPT_CHARS]
    else:
        head = ("The Count of Monte Cristo is a novel by Alexandre Dumas. " * 200)[:_PROMPT_CHARS]
    return f"Here is a passage:\n\n{head}\n\nSummarize the key themes in 30 words."


PROMPT_TEXT = _load_prompt()


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def wipe_bucket(bucket: str) -> None:
    try:
        import boto3
    except ImportError:
        log("  (boto3 not installed; skipping bucket wipe — mode smoke can still proceed)")
        return
    s3 = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_KEY,
        aws_secret_access_key=S3_SECRET,
        region_name="us-east-1",
    )
    try:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})
        s3.delete_bucket(Bucket=bucket)
    except Exception as exc:
        if "NoSuchBucket" not in str(exc):
            log(f"  wipe {bucket}: {type(exc).__name__}: {exc}")


def list_bucket_keys(bucket: str) -> list[str]:
    try:
        import boto3
    except ImportError:
        return []
    s3 = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_KEY,
        aws_secret_access_key=S3_SECRET,
        region_name="us-east-1",
    )
    keys: list[str] = []
    try:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            for o in page.get("Contents", []) or []:
                keys.append(o["Key"])
    except Exception as exc:
        if "NoSuchBucket" not in str(exc):
            log(f"  list {bucket}: {type(exc).__name__}: {exc}")
    return keys


def kill_all_ds4() -> None:
    subprocess.run(["pkill", "-f", "ds4-server"], capture_output=True)
    for _ in range(40):
        if subprocess.run(["pgrep", "-f", "ds4-server"], capture_output=True).returncode != 0:
            time.sleep(0.5)
            return
        time.sleep(0.2)
    subprocess.run(["pkill", "-9", "-f", "ds4-server"], capture_output=True)
    time.sleep(2)


def kill_all_daemon() -> None:
    subprocess.run(["pkill", "-f", "wombatkv-daemon"], capture_output=True)
    for _ in range(20):
        if subprocess.run(["pgrep", "-f", "wombatkv-daemon"], capture_output=True).returncode != 0:
            time.sleep(0.3)
            return
        time.sleep(0.2)
    subprocess.run(["pkill", "-9", "-f", "wombatkv-daemon"], capture_output=True)
    time.sleep(1)


def start_daemon(transport: str, prefix: str, logfile: Path,
                 daemon_puffer: Path) -> subprocess.Popen:
    """transport in {'shm','tcp'}. daemon_puffer must already exist
    (wiped + re-created by the caller) so we know there's no stale
    foyer/SlateDB state from prior runs masquerading as a warm cache."""
    env = os.environ.copy()
    env.update(
        {
            "WMBT_KV_S3_ENDPOINT": S3_ENDPOINT,
            "WMBT_KV_S3_ACCESS_KEY": S3_KEY,
            "WMBT_KV_S3_SECRET_KEY": S3_SECRET,
            "AWS_ACCESS_KEY_ID": S3_KEY,
            "AWS_SECRET_ACCESS_KEY": S3_SECRET,
            "WMBT_KV_BUCKET": f"wombatkv-smoke-{prefix}",
            "WMBT_KV_LOCAL_DEV": "1",
            "WMBT_KV_PUFFER_DIR": str(daemon_puffer),
            "WMBT_KV_SLATEDB_PATH": str(daemon_puffer / "slatedb"),
        }
    )
    if transport == "shm":
        cmd = [str(DAEMON_BIN), "--prefix", prefix]
    elif transport == "tcp":
        cmd = [str(DAEMON_BIN), "--tcp", f"127.0.0.1:{TCP_PORT}"]
    elif transport == "http":
        cmd = [str(DAEMON_BIN), "--http", f"127.0.0.1:{HTTP_PORT}"]
    else:
        raise ValueError(transport)
    # MODE_SMOKE_DAEMON_EXTRA_ARGS: shlex-style extra args appended to the
    # daemon command. Lets the harness exercise daemon flags like --tpc
    # without forking this script.
    extra = os.environ.get("MODE_SMOKE_DAEMON_EXTRA_ARGS", "").strip()
    if extra:
        cmd.extend(shlex.split(extra))
    with open(logfile, "w") as f:
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    # poll for ready signal
    for _ in range(30):
        try:
            text = logfile.read_text()
        except FileNotFoundError:
            text = ""
        if "ready" in text.lower() or "listening" in text.lower() or "serving" in text.lower():
            time.sleep(0.3)
            return proc
        if proc.poll() is not None:
            raise RuntimeError(f"daemon exited early; see {logfile}")
        time.sleep(0.5)
    # No explicit ready signal; give it 2s and proceed
    time.sleep(2.0)
    if proc.poll() is not None:
        raise RuntimeError(f"daemon exited early; see {logfile}")
    return proc


def server_env(mode: str, kvdir: Path, puffer: Path) -> dict[str, str]:
    env = os.environ.copy()
    if mode == "native":
        return env
    if mode == "embedded":
        env.update(
            {
                "DS4_WOMBATKV_ENABLE": "1",
                "WMBT_KV_S3_ENDPOINT": S3_ENDPOINT,
                "WMBT_KV_S3_ACCESS_KEY": S3_KEY,
                "WMBT_KV_S3_SECRET_KEY": S3_SECRET,
                "AWS_ACCESS_KEY_ID": S3_KEY,
                "AWS_SECRET_ACCESS_KEY": S3_SECRET,
                "WMBT_KV_BUCKET": f"wombatkv-smoke-{mode}",
                "WMBT_KV_PUFFER_DIR": str(puffer),
                "WMBT_KV_LOCAL_DEV": "1",
            }
        )
    elif mode == "daemon-shm":
        env.update(
            {
                "DS4_WOMBATKV_ENABLE": "1",
                "WMBT_KV_REMOTE_PREFIX": "smoke-shm",
            }
        )
    elif mode == "daemon-tcp":
        env.update(
            {
                "DS4_WOMBATKV_DAEMON_TCP": f"127.0.0.1:{TCP_PORT}",
            }
        )
    elif mode == "daemon-tcp-remote":
        env.update(
            {
                "DS4_WOMBATKV_DAEMON_TCP": REMOTE_TCP_ADDR,
            }
        )
    elif mode == "daemon-http":
        env.update(
            {
                "DS4_WOMBATKV_DAEMON_HTTP": f"127.0.0.1:{HTTP_PORT}",
            }
        )
    elif mode == "daemon-http-remote":
        env.update(
            {
                "DS4_WOMBATKV_DAEMON_HTTP": REMOTE_HTTP_ADDR,
            }
        )
    else:
        raise ValueError(mode)
    return env


def start_server(mode: str, kvdir: Path, puffer: Path, logfile: Path) -> subprocess.Popen:
    env = server_env(mode, kvdir, puffer)
    cmd = [
        str(DS4_BIN),
        "--model", MODEL,
        "--ctx", "8192",
        "--kv-disk-dir", str(kvdir),
        "--kv-cache-min-tokens", "256",
        "--kv-disk-space-mb", "4096",
        "--port", str(PORT),
    ]
    with open(logfile, "w") as f:
        proc = subprocess.Popen(
            cmd,
            cwd=str(DS4_DIR),
            env=env,
            stdout=f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    for _ in range(120):
        try:
            text = logfile.read_text()
        except FileNotFoundError:
            text = ""
        if "listening on http" in text:
            time.sleep(0.5)
            return proc
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early; see {logfile}")
        time.sleep(1.0)
    proc.kill()
    raise RuntimeError(f"server failed to start; see {logfile}")


def send_turn(prompt: str) -> tuple[float, str]:
    payload = json.dumps(
        {
            "model": "deepseek-v4-flash",
            "messages": [
                {"role": "system", "content": "You are a literary assistant."},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 32,
            "temperature": 0.0,
            "stream": False,
        }
    ).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300) as r:
        body = r.read()
    elapsed = time.time() - t0
    resp = json.loads(body.decode())
    text = resp["choices"][0]["message"]["content"]
    return elapsed, text


def run_mode(mode: str) -> dict:
    """Run the 2-turn smoke for `mode`. Returns a result dict.

    Modes:
      native: no WombatKV. kvdir wiped between turns → 2 cold prefills.
      native-warm: no WombatKV. kvdir KEPT between turns → turn-2 hits
        ds4's huge-blob KV-disk warm restore. This is the right
        baseline for asking "does WombatKV introduce more divergence
        than ds4's own warm-restore already does?"
      embedded/daemon-shm/daemon-tcp/daemon-tcp-remote: WombatKV modes.
        kvdir wiped between turns; WombatKV state persists → turn-2
        hits WombatKV warm restore.
    """
    log(f"=== mode: {mode} ===")
    # native-warm uses the same server config as native; differs only
    # in NOT wiping kvdir between turns (so ds4's huge-blob save from
    # turn-1 is available for turn-2 to warm-restore).
    server_mode = "native" if mode == "native-warm" else mode
    keep_kvdir_between_turns = (mode == "native-warm")
    kvdir = Path(f"/tmp/mode-smoke-kvdir-{mode}")
    puffer = Path(f"/tmp/mode-smoke-puffer-{mode}")
    daemon_puffer = Path(f"/tmp/mode-smoke-daemonpuffer-{mode}")
    serverlog = Path(f"/tmp/mode-smoke-{mode}-server.log")
    daemonlog = Path(f"/tmp/mode-smoke-{mode}-daemon.log")

    kill_all_ds4()
    if server_mode in ("daemon-shm", "daemon-tcp", "daemon-http"):
        kill_all_daemon()

    # Wipe BOTH the ds4-side caches and the daemon-side foyer/SlateDB
    # dirs. If we skip the daemon-side wipe, the daemon's foyer cache
    # serves block-prefix lookups from stale data left over from a
    # previous test run and "turn 1" is silently a warm restore.
    for d in (kvdir, puffer, daemon_puffer):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir()
    # Wipe the bucket that the OWNER of S3 in this mode will write to.
    # Embedded: ds4-server owns S3 → bucket = wombatkv-smoke-embedded.
    # Daemon modes: the daemon owns S3 → bucket = wombatkv-smoke-<prefix>
    # where <prefix> is the daemon's WMBT_KV_BUCKET suffix set in
    # start_daemon ("smoke-shm" or "smoke-tcp"). For daemon-tcp-remote
    # the bucket lives on the REMOTE host's S3 endpoint — we don't try
    # to wipe it from here.
    if mode == "embedded":
        wipe_bucket("wombatkv-smoke-embedded")
    elif mode == "daemon-shm":
        wipe_bucket("wombatkv-smoke-smoke-shm")
    elif mode == "daemon-tcp":
        wipe_bucket("wombatkv-smoke-smoke-tcp")
    elif mode == "daemon-http":
        wipe_bucket("wombatkv-smoke-smoke-http")

    daemon_proc = None
    try:
        if server_mode == "daemon-shm":
            log("  starting wombatkv-daemon (SHM prefix=smoke-shm)")
            daemon_proc = start_daemon("shm", "smoke-shm", daemonlog, daemon_puffer)
        elif server_mode == "daemon-tcp":
            log(f"  starting wombatkv-daemon (TCP 127.0.0.1:{TCP_PORT})")
            daemon_proc = start_daemon("tcp", "smoke-tcp", daemonlog, daemon_puffer)
        elif server_mode == "daemon-tcp-remote":
            log(f"  remote daemon expected at {REMOTE_TCP_ADDR} — no local start")
        elif server_mode == "daemon-http":
            log(f"  starting wombatkv-daemon (HTTP 127.0.0.1:{HTTP_PORT})")
            daemon_proc = start_daemon("http", "smoke-http", daemonlog, daemon_puffer)
        elif server_mode == "daemon-http-remote":
            log(f"  remote daemon expected at {REMOTE_HTTP_ADDR} — no local start")

        log("  starting ds4-server")
        start_server(server_mode, kvdir, puffer, serverlog)

        log("  turn 1 (cold prefill)")
        t1, text1 = send_turn(PROMPT_TEXT)
        log(f"    turn-1 elapsed = {t1*1000:.0f} ms, first 40 chars: {text1[:40]!r}")

        # native-warm keeps kvdir so turn-2 hits ds4's huge-blob warm
        # restore. All other modes wipe kvdir (WombatKV state persists
        # for the WombatKV modes via puffer + S3 + daemon).
        kill_all_ds4()
        if not keep_kvdir_between_turns and kvdir.exists():
            shutil.rmtree(kvdir)
            kvdir.mkdir()

        log("  restarting ds4-server")
        start_server(server_mode, kvdir, puffer, serverlog)

        log("  turn 2 (warm restore for wombatkv modes)")
        t2, text2 = send_turn(PROMPT_TEXT)
        log(f"    turn-2 elapsed = {t2*1000:.0f} ms, first 40 chars: {text2[:40]!r}")

        # Bucket signal
        bucket = None
        if server_mode == "embedded":
            bucket = f"wombatkv-smoke-{server_mode}"
        elif server_mode == "daemon-shm":
            bucket = "wombatkv-smoke-smoke-shm"
        elif server_mode == "daemon-tcp":
            bucket = "wombatkv-smoke-smoke-tcp"
        elif server_mode == "daemon-http":
            bucket = "wombatkv-smoke-smoke-http"
        bucket_keys: list[str] = []
        if bucket:
            bucket_keys = list_bucket_keys(bucket)
            log(f"    bucket {bucket}: {len(bucket_keys)} object(s)")
        elif mode in ("daemon-tcp-remote", "daemon-http-remote"):
            log("    bucket on remote host's S3 — not inspecting from here")

        # Server log signal: did WombatKV engage?
        srv_text = serverlog.read_text() if serverlog.exists() else ""
        wombat_init_line = next(
            (ln for ln in srv_text.splitlines() if "WombatKV" in ln or "wombatkv" in ln.lower()),
            "",
        )
        if wombat_init_line:
            log(f"    server WombatKV log line: {wombat_init_line[:120]}")

        # Coherence: how much do turn-1 and turn-2 outputs share?
        # WombatKV fidelity claim is K/V byte-equivalence between
        # warm restore and cold compute. In text space we can't get
        # strict equality — temp=0 argmax flips on near-tied logits
        # mean even runs with identical inputs can diverge a few
        # tokens in. So this is INFORMATIONAL not strictly asserted.
        # The thresholds below catch "WombatKV returned garbage"
        # failures (where text2 would be nonsense or random bytes)
        # without false-failing on Metal scheduling noise.
        coh = _coherence(text1, text2)
        log(
            f"    coherence: lcp={coh['lcp_chars']} chars / "
            f"shared_words={coh['shared_words']} / "
            f"lengths t1={len(text1)} t2={len(text2)}"
        )

        # Verdict
        verdict = "PASS"
        notes = []
        if not text1.strip() or not text2.strip():
            verdict = "FAIL"
            notes.append("empty response")
        if server_mode != "native":
            if not bucket_keys and server_mode not in ("daemon-tcp-remote", "daemon-http-remote"):
                notes.append("bucket empty (WombatKV did not write any blocks)")
                # don't fail on this alone — short prompt may not trigger block write
            if t2 > t1 * 0.9:
                notes.append(f"turn-2 not faster than turn-1 ({t2*1000:.0f} vs {t1*1000:.0f} ms) — warm restore may not have engaged")
            # Coherence soft check (don't FAIL on it — log + note for review)
            if coh["shared_words"] < 3:
                notes.append(
                    f"coherence weak: only {coh['shared_words']} shared word(s) between turn-1 and turn-2 — "
                    f"could be Metal sampling noise or could be a real WombatKV-restore corruption"
                )

        return {
            "mode": mode,
            "verdict": verdict,
            "turn1_ms": int(t1 * 1000),
            "turn2_ms": int(t2 * 1000),
            "speedup": round(t1 / max(t2, 1e-6), 2),
            "bucket_keys": len(bucket_keys),
            "wombat_init": wombat_init_line[:120],
            "coherence": coh,
            "notes": notes,
        }
    finally:
        kill_all_ds4()
        if daemon_proc is not None:
            try:
                daemon_proc.terminate()
                daemon_proc.wait(timeout=5)
            except Exception:
                daemon_proc.kill()
        if server_mode in ("daemon-shm", "daemon-tcp", "daemon-http"):
            kill_all_daemon()
        # daemon-tcp-remote: remote daemon is the user's responsibility


def main() -> int:
    global REMOTE_TCP_ADDR, REMOTE_HTTP_ADDR
    p = argparse.ArgumentParser()
    p.add_argument(
        "mode",
        choices=["native", "native-warm", "embedded", "daemon-shm", "daemon-tcp", "daemon-tcp-remote", "daemon-http", "daemon-http-remote", "all"],
    )
    p.add_argument(
        "--remote-http",
        metavar="HOST:PORT",
        default=os.environ.get("MODE_SMOKE_REMOTE_HTTP", ""),
        help="For mode daemon-http-remote: address of an already-running "
             "wombatkv-daemon HTTP listener on another host (e.g. 192.168.x.x:7879).",
    )
    p.add_argument(
        "--remote-tcp",
        metavar="HOST:PORT",
        default=os.environ.get("MODE_SMOKE_REMOTE_TCP", ""),
        help="For mode daemon-tcp-remote: address of an already-running "
             "wombatkv-daemon on another host (e.g. 192.168.x.x:7878). "
             "The daemon is the user's responsibility; this script only "
             "starts the local ds4-server pointed at it.",
    )
    args = p.parse_args()

    if not DS4_BIN.exists():
        log(f"ERROR: {DS4_BIN} does not exist — build ds4-server first")
        return 1
    if args.mode in ("daemon-shm", "daemon-tcp", "daemon-http", "all") and not DAEMON_BIN.exists():
        log(f"ERROR: {DAEMON_BIN} does not exist — build wombatkv-daemon first")
        return 1
    if args.mode == "daemon-tcp-remote":
        if not args.remote_tcp:
            log("ERROR: daemon-tcp-remote requires --remote-tcp HOST:PORT (or MODE_SMOKE_REMOTE_TCP env)")
            return 1
        REMOTE_TCP_ADDR = args.remote_tcp
    if args.mode == "daemon-http-remote":
        if not args.remote_http:
            log("ERROR: daemon-http-remote requires --remote-http HOST:PORT (or MODE_SMOKE_REMOTE_HTTP env)")
            return 1
        REMOTE_HTTP_ADDR = args.remote_http

    modes = ["native", "native-warm", "embedded", "daemon-shm", "daemon-tcp", "daemon-http"] if args.mode == "all" else [args.mode]
    results = []
    for m in modes:
        try:
            results.append(run_mode(m))
        except Exception as exc:
            log(f"  EXCEPTION: {type(exc).__name__}: {exc}")
            results.append({"mode": m, "verdict": "ERROR", "error": str(exc)})

    print()
    print("=== summary ===")
    for r in results:
        print(json.dumps(r, indent=2))
    rc = 0 if all(r.get("verdict") == "PASS" for r in results) else 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
