#!/usr/bin/env python3
"""5-mode head-to-head: ds4-native vs WombatKV variants.

Modes:
  1. ds4_native:       no WombatKV
  2. wkv_monolithic:   WombatKV, single-blob (no chunking, no Tier B)
  3. wkv_chunked:      WombatKV + 8 MB Tier A chunking
  4. wkv_tier_b:       WombatKV + Tier B (and monolithic legacy save)
  5. wkv_full_g1:      WombatKV + Tier A + Tier B + G1 Tier-A-first probe

Cells (each mode × each cell):
  A: warm same-prompt (no restart between turns)
  B: cross-restart same-prompt (wipe flat+kvdisk between turns; same prompt)
  D: cross-restart prefix-share (wipe between turns; turn 2 prompt shares
     6 blocks of prefix with turn 1, but is overall different)
"""
import json, http.client, time, subprocess, os, pathlib, shutil, signal, re, sys
from datetime import datetime
from statistics import median

DS4_DIR = pathlib.Path('/Users/venkat/Documents/p/venkat-github/myelon-launch/ds4')
DS4_BIN = DS4_DIR / 'ds4-server'
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

# (mode_name, env_overlay, kvdisk_per_mode)
MODES = [
    ('mode1_ds4_native',     {},                                                                                                                    True),
    ('mode2_wkv_monolithic', {'DS4_WOMBATKV_ENABLE': '1'},                                                                                            True),
    ('mode3_wkv_chunked',    {'DS4_WOMBATKV_ENABLE': '1', 'WMBT_KV_CHUNK_BYTES': '8388608'},                                                          True),
    ('mode4_wkv_tier_b',     {'DS4_WOMBATKV_ENABLE': '1', 'WMBT_KV_TIER_B': '1', 'WMBT_KV_TIER_B_BLOCK_TOKENS': '128'},                                True),
    ('mode5_wkv_full_g1',    {'DS4_WOMBATKV_ENABLE': '1', 'WMBT_KV_CHUNK_BYTES': '8388608', 'WMBT_KV_TIER_B': '1', 'WMBT_KV_TIER_B_BLOCK_TOKENS': '128'}, True),
]

# (cell_name, restart_between_turns, (prompt1, prompt2))
CELLS = [
    ('A_warm_same',      False, (PROMPT_SAME, PROMPT_SAME)),
    ('B_restart_same',   True,  (PROMPT_SAME, PROMPT_SAME)),
    ('D_restart_prefix', True,  (D_PROMPT1, D_PROMPT2)),
]


def log(m):
    print(f'[{time.strftime("%H:%M:%S")}] {m}', flush=True)


def common_wkv_env():
    return {
        'DS4_WMBT_KV_FINGERPRINT24': 'deadbeefcafe1234567890ab',
        'WMBT_KV_TIMING': '1',
        'WMBT_KV_S3_ENDPOINT': 'http://127.0.0.1:9200',
        'WMBT_KV_S3_ACCESS_KEY': 'minioadmin',
        'WMBT_KV_S3_SECRET_KEY': 'minioadmin',
        'WMBT_KV_NAMESPACE': 'ds4-metal',
        'WMBT_KV_EMBEDDED_ASYNC_S3': '1',
        'WMBT_KV_CHUNK_VERIFY': '0',
        'WMBT_KV_S3_PREWARM': '8',
    }


def env_for(mode_overlay, puffer, bucket):
    e = os.environ.copy()
    if mode_overlay.get('DS4_WOMBATKV_ENABLE'):
        e.update(common_wkv_env())
        e['WMBT_KV_BUCKET'] = bucket
        e['WMBT_KV_PUFFER_DIR'] = puffer
    e.update(mode_overlay)
    return e


def start(env, kvdisk, log_path):
    args = [str(DS4_BIN), '--model', MODEL, '--ctx', '32768',
            '--kv-disk-dir', kvdisk,
            '--kv-cache-min-tokens', '256',
            '--kv-disk-space-mb', '16384',
            '--port', str(PORT)]
    lf = open(log_path, 'wb')
    p = subprocess.Popen(args, cwd=str(DS4_DIR), env=env,
                         stdout=lf, stderr=subprocess.STDOUT)
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
            p.send_signal(signal.SIGTERM)
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                p.send_signal(signal.SIGKILL)
                p.wait(timeout=3)
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


# --- Main run ---
log(f'Artifacts dir: {ART}')
log(f'5 modes × 3 cells × {TRIALS} trials')

results = []  # list of dicts per (mode, cell)

for mode_name, mode_overlay, _ in MODES:
    for cell_name, restart_between_turns, (p1, p2) in CELLS:
        log(f'=== {mode_name} / {cell_name}  same={p1==p2} restart={restart_between_turns} ===')
        puffer = f'/tmp/wombatkv-5way-{mode_name}-{cell_name}'
        kvdisk = f'/tmp/ds4-5way-{mode_name}-{cell_name}'
        bucket = f'wombatkv-5way-{mode_name}-{cell_name}'.lower().replace('_', '-')
        wipe(puffer, kvdisk)

        env = env_for(mode_overlay, puffer, bucket)
        ts = datetime.now().strftime('%H%M%S')
        cold_log = LOGS / f'{mode_name}_{cell_name}_coldprefill.log'
        p = start(env, kvdisk, cold_log)
        try:
            log('  cold prefill ...')
            tcold = req(p1)
            log(f'    cold t1={int(tcold[0]) if tcold[0]==tcold[0] else "?"} ms')
        finally:
            stop(p)

        t1s, t2s = [], []
        for trial in range(1, TRIALS + 1):
            log(f'  trial {trial}/{TRIALS} ...')
            if restart_between_turns:
                # both turns each get a fresh process
                wipe(kvdisk)
                # for WKV modes also wipe the puffer (forces S3-only)
                if mode_overlay.get('DS4_WOMBATKV_ENABLE'):
                    wipe(puffer)
            t1_log = LOGS / f'{mode_name}_{cell_name}_trial{trial:02d}_turn1.log'
            p1p = start(env, kvdisk, t1_log)
            try:
                t1, _ = req(p1)
                t1s.append(t1)
            finally:
                stop(p1p)
            if restart_between_turns:
                wipe(kvdisk)
                if mode_overlay.get('DS4_WOMBATKV_ENABLE'):
                    wipe(puffer)
            t2_log = LOGS / f'{mode_name}_{cell_name}_trial{trial:02d}_turn2.log'
            p2p = start(env, kvdisk, t2_log)
            try:
                t2, _ = req(p2)
                t2s.append(t2)
            finally:
                stop(p2p)
            log(f'    t1={int(t1)} t2={int(t2)}')

        result = {
            'mode': mode_name, 'cell': cell_name,
            't1_min': min(t1s) if t1s else float('nan'),
            't1_med': median(t1s) if t1s else float('nan'),
            't1_p95': percentile(t1s, 95),
            't2_min': min(t2s) if t2s else float('nan'),
            't2_med': median(t2s) if t2s else float('nan'),
            't2_p95': percentile(t2s, 95),
            'trials': TRIALS,
            't1_all': t1s, 't2_all': t2s,
        }
        results.append(result)

# Write JSON + CSV summary
with open(ART / 'results.json', 'w') as f:
    json.dump(results, f, indent=2, default=str)

with open(ART / 'summary.csv', 'w') as f:
    f.write('mode,cell,t1_med,t1_p95,t1_min,t2_med,t2_p95,t2_min\n')
    for r in results:
        f.write(f'{r["mode"]},{r["cell"]},'
                f'{r["t1_med"]:.1f},{r["t1_p95"]:.1f},{r["t1_min"]:.1f},'
                f'{r["t2_med"]:.1f},{r["t2_p95"]:.1f},{r["t2_min"]:.1f}\n')

# Pretty grid
log('')
log('=== AGGREGATE (t2_med = turn-2 TTFT median, ms) ===')
log(f'{"mode":<24} {"A_warm":>10} {"B_rsame":>10} {"D_prefix":>10}')
for mode_name, _, _ in MODES:
    row = {r['cell']: r['t2_med'] for r in results if r['mode'] == mode_name}
    log(f'{mode_name:<24} '
        f'{row.get("A_warm_same","?"):>10.0f} '
        f'{row.get("B_restart_same","?"):>10.0f} '
        f'{row.get("D_restart_prefix","?"):>10.0f}')

log('')
log(f'Artifacts: {ART}')
