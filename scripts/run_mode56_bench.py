#!/usr/bin/env python3
"""Validation bench: re-run mode 5 (G1) and mode 6 (G1+G2) with the right binaries.

Reads DS4_BIN_PATH and MODE_NAME from env so a wrapper can run two passes.
3 cells (A_warm_same, B_restart_same, D_restart_prefix) x 3 trials each.
"""
import json, http.client, time, subprocess, os, pathlib, shutil, signal, re, sys
from datetime import datetime
from statistics import median

DS4_DIR = pathlib.Path('/Users/venkat/Documents/p/venkat-github/myelon-launch/ds4')
DS4_BIN = pathlib.Path(os.environ['DS4_BIN_PATH'])
MODE_NAME = os.environ.get('MODE_NAME', 'mode5_g1_v2')
MODEL = 'gguf/DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix.gguf'
PROMPT_FILE = pathlib.Path('/tmp/pg1184.txt')
ART = pathlib.Path(os.environ['BENCH_ART_DIR'])
LOGS = ART / 'server_logs'
LOGS.mkdir(parents=True, exist_ok=True)
TRIALS = int(os.environ.get('BENCH_TRIALS', '3'))
PORT = 8000

BASE = PROMPT_FILE.read_text()
PROMPT_SAME = BASE[:5200]
SHARED_PREFIX_D = BASE[:2700]
DIVERGENT_A = " QUERY-A about distant lands and architecture: " + BASE[40000:40000 + 2500]
DIVERGENT_B = " QUERY-B about navigation and weather patterns: " + BASE[60000:60000 + 1800]
D_PROMPT1 = SHARED_PREFIX_D + DIVERGENT_A
D_PROMPT2 = SHARED_PREFIX_D + DIVERGENT_B

CELLS = [
    ('A_warm_same',      False, (PROMPT_SAME, PROMPT_SAME)),
    ('B_restart_same',   True,  (PROMPT_SAME, PROMPT_SAME)),
    ('D_restart_prefix', True,  (D_PROMPT1, D_PROMPT2)),
]


def log(m):
    print(f'[{time.strftime("%H:%M:%S")}] {m}', flush=True)


def env_for(puffer, bucket):
    e = os.environ.copy()
    e.update({
        'DS4_WOMBATKV_ENABLE': '1',
        'DS4_WMBT_KV_FINGERPRINT24': 'deadbeefcafe1234567890ab',
        'WMBT_KV_TIMING': '1',
        'WMBT_KV_S3_ENDPOINT': 'http://127.0.0.1:9200',
        'WMBT_KV_BUCKET': bucket,
        'WMBT_KV_S3_ACCESS_KEY': 'minioadmin',
        'WMBT_KV_S3_SECRET_KEY': 'minioadmin',
        'WMBT_KV_PUFFER_DIR': puffer,
        'WMBT_KV_NAMESPACE': 'ds4-metal',
        'WMBT_KV_EMBEDDED_ASYNC_S3': '1',
        'WMBT_KV_CHUNK_VERIFY': '0',
        'WMBT_KV_S3_PREWARM': '8',
        'WMBT_KV_CHUNK_BYTES': '8388608',
        'WMBT_KV_TIER_B': '1',
        'WMBT_KV_TIER_B_BLOCK_TOKENS': '128',
    })
    return e


def start(env, kvdisk, log_path):
    args = [str(DS4_BIN), '--model', MODEL, '--ctx', '32768',
            '--kv-disk-dir', kvdisk,
            '--kv-cache-min-tokens', '256',
            '--kv-disk-space-mb', '16384',
            '--port', str(PORT)]
    lf = open(log_path, 'wb')
    p = subprocess.Popen(args, cwd=str(DS4_DIR), env=env, stdout=lf, stderr=subprocess.STDOUT)
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            txt = open(log_path, 'rb').read().decode('utf-8', errors='replace')
            if 'refusing to start' in txt:
                p.kill(); lf.close()
                raise RuntimeError(f'refused: {log_path}')
            if 'listening on http' in txt:
                lf.close(); time.sleep(0.6); return p
        except FileNotFoundError:
            pass
        time.sleep(0.2)
    p.kill(); lf.close()
    raise RuntimeError(f'no boot: {log_path}')


def stop(p):
    if p.poll() is None:
        try:
            p.send_signal(signal.SIGTERM); p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                p.send_signal(signal.SIGKILL); p.wait(timeout=3)
            except Exception:
                pass


def req(prompt_text, max_tokens=50, timeout_s=300):
    msg = {
        'model': 'deepseek-v4-flash',
        'messages': [
            {'role': 'system', 'content': 'You are a literary assistant.'},
            {'role': 'user', 'content': f'Here is a passage:\n\n{prompt_text}\n\nSummarize the key themes in 50 words.'},
        ],
        'max_tokens': max_tokens, 'temperature': 0.0, 'stream': True,
    }
    c = http.client.HTTPConnection('127.0.0.1', PORT, timeout=timeout_s)
    t0 = time.perf_counter()
    c.request('POST', '/v1/chat/completions', json.dumps(msg),
              headers={'Content-Type': 'application/json'})
    r = c.getresponse()
    ttft = None
    while True:
        ch = r.read1(4096)
        if not ch: break
        if ttft is None and b'data:' in ch:
            ttft = (time.perf_counter() - t0) * 1000
    tot = (time.perf_counter() - t0) * 1000
    c.close()
    return ttft if ttft is not None else float('nan'), tot


def wipe(*paths):
    for p in paths:
        shutil.rmtree(p, ignore_errors=True)
        pathlib.Path(p).mkdir(parents=True, exist_ok=True)


def percentile(xs, q):
    if not xs: return float('nan')
    s = sorted(xs); k = max(0, min(len(s)-1, int(round((q/100.0)*(len(s)-1)))))
    return s[k]


log(f'Binary: {DS4_BIN}')
log(f'Mode label: {MODE_NAME}')
log(f'Cells: {[c[0] for c in CELLS]}, Trials: {TRIALS}')

results = []
for cell_name, restart_between_turns, (p1, p2) in CELLS:
    log(f'=== {MODE_NAME} / {cell_name} ===')
    puffer = f'/tmp/wkv-{MODE_NAME}-{cell_name}'
    kvdisk = f'/tmp/ds4-{MODE_NAME}-{cell_name}'
    bucket = f'wkv-{MODE_NAME}-{cell_name}'.lower().replace('_', '-')
    wipe(puffer, kvdisk)
    env = env_for(puffer, bucket)
    cold_log = LOGS / f'{MODE_NAME}_{cell_name}_coldprefill.log'
    p = start(env, kvdisk, cold_log)
    try:
        log('  cold prefill ...')
        tcold = req(p1)
        log(f'    cold t1={int(tcold[0])} ms')
    finally:
        stop(p)

    t1s, t2s = [], []
    for trial in range(1, TRIALS + 1):
        if restart_between_turns:
            wipe(puffer, kvdisk)
        t1_log = LOGS / f'{MODE_NAME}_{cell_name}_trial{trial:02d}_turn1.log'
        p1p = start(env, kvdisk, t1_log)
        try:
            t1, _ = req(p1); t1s.append(t1)
        finally:
            stop(p1p)
        if restart_between_turns:
            wipe(puffer, kvdisk)
        t2_log = LOGS / f'{MODE_NAME}_{cell_name}_trial{trial:02d}_turn2.log'
        p2p = start(env, kvdisk, t2_log)
        try:
            t2, _ = req(p2); t2s.append(t2)
        finally:
            stop(p2p)
        log(f'  trial {trial}: t1={int(t1)} t2={int(t2)}')

    result = {
        'mode': MODE_NAME, 'cell': cell_name,
        't1_min': min(t1s) if t1s else float('nan'),
        't1_med': median(t1s) if t1s else float('nan'),
        't1_p95': percentile(t1s, 95),
        't2_min': min(t2s) if t2s else float('nan'),
        't2_med': median(t2s) if t2s else float('nan'),
        't2_p95': percentile(t2s, 95),
        'trials': TRIALS, 't1_all': t1s, 't2_all': t2s,
    }
    results.append(result)

with open(ART / f'{MODE_NAME}_results.json', 'w') as f:
    json.dump(results, f, indent=2, default=str)

with open(ART / f'{MODE_NAME}_summary.csv', 'w') as f:
    f.write('mode,cell,t1_med,t1_p95,t2_med,t2_p95\n')
    for r in results:
        f.write(f'{r["mode"]},{r["cell"]},{r["t1_med"]:.1f},{r["t1_p95"]:.1f},{r["t2_med"]:.1f},{r["t2_p95"]:.1f}\n')

log('')
log(f'=== {MODE_NAME} RESULTS ===')
for r in results:
    log(f'  {r["cell"]:<22} t2_med={r["t2_med"]:.0f} t2_p95={r["t2_p95"]:.0f}')
log(f'Artifacts: {ART}')
