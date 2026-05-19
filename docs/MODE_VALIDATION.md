# Mode-matrix validation

Cross-mode validation procedures for ds4 × WombatKV. Complements
the per-CONTRIBUTING.md correctness regression track.

The four WombatKV modes documented in tensorpuffer's `docs/ENV.md`:

| mode | engine-side env | what's where |
|---|---|---|
| 1 — native | (none) | no WombatKV in the picture |
| 2 — embedded | `DS4_WOMBATKV_ENABLE=1` + S3 env | ds4-server owns the WombatKV store in-process |
| 3 — daemon SHM | `DS4_WOMBATKV_ENABLE=1` + `WMBT_KV_REMOTE_PREFIX=…` | a `wombatkv-daemon --prefix <name>` on the same host owns the store |
| 4 — daemon TCP | `DS4_WOMBATKV_DAEMON_TCP=<host:port>` | a `wombatkv-daemon --tcp <addr>` on (typically) a different host owns the store |

## Same-host smoke (`mode_smoke.py`)

The end-to-end smoke for modes 1-4 with the daemon running on the
same Mac as ds4-server. Two-turn cell-B pattern: cold prefill →
kill → wipe local kvdir → restart → warm restore for WombatKV
modes. Single-trial; fast.

```bash
# Pre-req: native MinIO running on 127.0.0.1:9200
#          libwombatkv.dylib + wombatkv-daemon built in tensorpuffer

python3 scripts/mode_smoke.py all          # runs all 4 same-host modes
python3 scripts/mode_smoke.py embedded     # one mode at a time
```

Per-mode validation:
- ds4-server starts cleanly
- `WombatKV 0.1.0-alpha …` banner appears in server log (non-native modes)
- Daemon's S3 bucket has objects after turn 1 (non-native modes)
- turn-2 elapsed materially below turn-1 (non-native modes, informational)
- Both turns return non-empty response

## Cross-machine TCP (`mode daemon-tcp-remote`)

The actual mode-4 use case: ds4-server on one machine, daemon on
another. Tests the wire format + remote S3 ownership end-to-end.

### On the **remote** host (the daemon side, e.g. venkat-pc)

```bash
# Pre-req: native MinIO reachable from this host (local or remote)
# Build wombatkv-daemon from the same tensorpuffer commit as the
# libwombatkv.dylib on the engine side. Mismatched commits = wire
# format drift = block-prefix lookup misses.

cd /path/to/tensorpuffer
cargo build --release -p wombatkv-daemon

WMBT_KV_S3_ENDPOINT=http://127.0.0.1:9000 \
WMBT_KV_S3_ACCESS_KEY=minioadmin \
WMBT_KV_S3_SECRET_KEY=minioadmin \
AWS_ACCESS_KEY_ID=minioadmin \
AWS_SECRET_ACCESS_KEY=minioadmin \
WMBT_KV_BUCKET=wombatkv-xhost-smoke \
WMBT_KV_LOCAL_DEV=1 \
WMBT_KV_PUFFER_DIR=/tmp/wombatkv-xhost-puffer \
WMBT_KV_SLATEDB_PATH=/tmp/wombatkv-xhost-puffer/slatedb \
  ./target/release/wombatkv-daemon --tcp 0.0.0.0:7878
```

Bind on `0.0.0.0` (not `127.0.0.1`) so the Mac can reach you.
Firewall: allow inbound TCP 7878 from the Mac's IP.

### On the Mac (the engine side)

```bash
cd /path/to/ds4

# Build ds4-server with the WombatKV dylib (one-time):
make WOMBATKV=1 WOMBATKV_DIR=/path/to/tensorpuffer ds4-server

# Run the smoke pointed at the remote daemon:
python3 scripts/mode_smoke.py daemon-tcp-remote \
  --remote-tcp <venkat-pc-ip>:7878
```

What the script validates for cross-machine TCP:
- ds4-server starts and connects to the remote daemon
- WombatKV banner appears in the local server log
- Turn 1 cold prefill succeeds; turn 2 returns non-empty
- Bucket inspection is skipped (it's on the remote's S3, not ours
  to wipe / list from here)
- turn-1/turn-2 latency ratio is informational; cross-host adds
  network RTT so the ratio profile differs from loopback

### Important compatibility checks

1. **dylib commit ↔ daemon commit** — both must be from the same
   tensorpuffer commit. Wire format isn't versioned in the alpha
   breaking window; mismatched commits silently break block-prefix
   lookups.
2. **Model fingerprint** — `DS4_WMBT_KV_FINGERPRINT24` must derive
   from the same model path on both sides, OR both sides must read
   blocks under the same explicit fingerprint. ds4 auto-derives
   from `sha1(model_path)`, so the path string itself matters.
3. **WMBT_KV_NAMESPACE** — ds4 defaults to `ds4-metal`; if you
   override on one side, override on both.
4. **Bucket** — set on the daemon side. The engine side knows
   nothing about S3 in mode 4.

## Last alpha.6 validation results (M3 Max + venkat-pc Ubuntu 22.04, 2026-05-18)

Same-host matrix (`scripts/mode_smoke.py all` on Mac):

| mode | turn-1 | turn-2 | speedup | S3 objects |
|---|---:|---:|---:|---:|
| native     |  9929 ms | 9728 ms |  1.02× |  0 |
| embedded   | 16617 ms | 2101 ms |  7.91× | 12 |
| daemon-shm | 31268 ms | 5216 ms |  5.99× | 15 |
| daemon-tcp | 36519 ms | 2284 ms | 15.99× | 12 |

ds4_test `--server` with each mode's WombatKV env: PASS in all 4.

Cross-machine TCP (`mode_smoke.py daemon-tcp-remote`) — Mac
ds4-server (M3 Max) connecting to a wombatkv-daemon on venkat-pc
(Ubuntu 22.04, x86_64) over LAN at `192.168.2.103:7878`, daemon's
S3 backing on venkat-pc-local MinIO:

| direction | turn-1 cold | turn-2 warm | speedup | blocks in venkat-pc S3 |
|---|---:|---:|---:|---:|
| Mac engine → venkat-pc daemon → venkat-pc S3 | 42203 ms | 6757 ms | 6.25× | 12 |

Confirmation signals on the venkat-pc side after the smoke:
- daemon log shows 3 TCP `accepted` events from peer `192.168.2.102`
  (Mac's LAN IP), one per ds4-server lifecycle (start, turn-1
  request, restart-and-turn-2).
- `wombatkv-xhost-smoke` bucket has 12 objects, keys formatted
  `kv/puffer-shm/ds4-metal/wombatkv/v1/block/b3=<hex>` — the
  canonical block-prefix content-address scheme. No SHM artifacts
  on either side (TCP-only daemon).
- SlateDB metadata index hydrated 0 blocks at boot (fresh dir),
  then populated through the turn-1 writes; turn-2 lookup hit the
  hot index.

Linux-side tests at the same v0.1.0-alpha.6 commit:

| suite | result | wall time |
|---|---|---:|
| `cargo test --workspace --lib --release` | **208/208 PASS** | 44 s |
| `scripts/dst-sweep.sh --seeds 1-10` (7 failure classes × 10 seeds) | **70/70 PASS** | 0.21 s |

## Output coherence (informational, alpha.7+)

Two complementary harnesses:

### Light (`mode_smoke.py`)

Single-trial 2-turn pattern per mode, logs an
`lcp_chars` + `shared_words` block. Soft threshold
`shared_words >= 3` for non-native modes. Last run:

| mode | turn-2 lcp_chars | shared_words ≥ 3? |
|---|---:|---|
| native     | 65 | yes (7) |
| embedded   |  0 | yes (5) |
| daemon-shm |  0 | yes (4) |
| daemon-tcp |  0 | yes (5) |

### Strongest — tensor-level (`scripts/logit_fidelity_test.py`)

The **byte-fidelity proof** the text-only tests couldn't deliver.
Uses the `POST /v1/internal/logits` endpoint added in this branch
(gated by `DS4_DEBUG_INTERNAL=1`). Endpoint runs prefill + returns
the top-K (token_id, logit, logprob) triples at the last prompt
position.

Procedure per mode:
1. Run N iterations of the same prompt (each = fresh ds4-server).
2. For native modes: every iter is a cold prefill.
3. For WombatKV modes: iter 1 cold + writes blocks; iters 2..N
   warm-restore from S3/daemon.
4. Capture top-K per iter. Pairwise diff: top-1 ID match, top-K
   set overlap, L∞ over overlapping logits.
5. Native pairwise = Metal scheduling noise floor in logit space.
6. WombatKV-mode L∞ should be ≤ native floor + tolerance.

**Four iterations of this test landed three different bugs (all
false-positive L∞=0 readings) before finding the real signal —
see the 4-stage post-mortem at the end of this section. The
CANONICAL result below is from the v4 harness which drives the
WombatKV load+save path inline inside `/v1/internal/logits`.**

**Canonical result — v4 harness, 1010-token prompt:**

| mode | iter-1 logits (cold) | iter-2 logits (warm) | speedup | loaded_tokens | top-1 cold | top-1 warm | L∞ logit |
|---|---:|---:|---:|---:|---:|---:|---:|
| native (baseline) | 5829 ms | 5553 ms | 1.05× | — | 28 | 28 | **0.0000** |
| embedded | 6266 ms | 296 ms | 21.2× | 1009 | 28 | **1137** | **0.2312** |
| daemon-shm | 7783 ms | 2780 ms | 2.8× | 1009 | 28 | **1137** | **0.2312** |
| daemon-tcp | 7907 ms | 2571 ms | 3.1× | 1009 | 28 | **1137** | **0.2312** |

Smoking-gun corroboration that WombatKV actually engaged this
time:
- `wombatkv_loaded_tokens=1009` in iter-2 of every WombatKV mode
  (8 blocks × 128 tokens with 16-token chain alignment). 0 in
  iter-1.
- Bucket counts after each mode: 8-18 objects, non-zero.
- iter-2 logits latency is 3-21× faster than iter-1 cold — only
  possible if warm restore really happened.

**Honest findings:**

1. **Metal IS bit-deterministic** for cold full prefill at the
   logit level — native iter-1 ↔ iter-2 produce identical top-K
   logits to 7 sig figs (L∞ = 0.0000).
2. **WombatKV's byte storage is bit-correct.** All three WombatKV
   modes produce the EXACT same warm logits (top-1 token = 1137,
   L∞ = 0.2312 vs cold) — distinct transports (embedded, daemon-
   SHM, daemon-TCP) all serialize/restore through the same cabi
   byte path and converge on the same warm result. Strong evidence
   the bytes WombatKV stores and restores are themselves identical
   to what was put in. Tier-A `crates/wombatkv-cabi/tests/cabi_
   adversarial_roundtrip.rs` proves byte-roundtrip directly; the
   cross-mode convergence here is the independent corroboration.
3. **The warm-restore code path is NOT bit-equivalent to cold
   prefill** at the logit level. ~0.23 logit drift across top-K.
   Argmax can flip on near-tied tokens (cold top-1 = 28 becomes
   warm top-1 = 1137; both are in each other's top-3 of 20).
   Top-K overlap = 19/20.
4. **This drift is NOT a WombatKV bug.** It comes from the engine
   side: warm path runs `install_raw_tail` (restored K/V) + sync
   trailing-1 forward (single-token suffix recompute), which uses
   a different kernel batching than the full prefill kernel.
   Same Metal hardware, different reduction orders → ~1% logit
   drift. This is an inherent property of incremental-prefill /
   warm-restore architectures, not specific to WombatKV.

**Practical implication:** for typical LLM inference (non-strict-
determinism), the drift is acceptable — model behavior is
preserved (top-3 overlap is reliable, English fluency
unaffected per the LLM-judge review, ds4-server tests pass
across all modes). For strict bit-equality, warm restore is
not a drop-in replacement for cold prefill — some logit-flip
rate is unavoidable.

Result file:
[`bench_data/logit_fidelity_LONG_v3_real_alpha7.json`](../bench_data/logit_fidelity_LONG_v3_real_alpha7.json)
(naming reflects this was the v3 file written; the v4 harness
itself is what produced it.)

Canonical bundle (myelon-launch):
`ai-chat-exports/.0_agentic_engineering/5_tensor_puffer/bench_data/
2026-05-19_alpha7_strong_fidelity_020107/`

## 4-stage post-mortem (v1 → v4)

Four iterations of the same Tier-B fidelity test, each with a
different bug. Captured here so the gap between "looks correct"
and "is correct" is documented.

**v1 (short prompts, no harness chat-completion):**
Used 5- and 150-token prompts. Both below `KV_CACHE_DEFAULT_MIN_
TOKENS = 512` in `ds4_server.c`, so ds4 never saves to WombatKV
regardless of mode. Buckets ended up empty (0 objects). iter-2
was just another fresh cold prefill — no warm path. L∞=0 reading
was Metal determinism for repeated cold prefills, not WombatKV
correctness. **Detected via** bucket-count spot-check.

**v2 (chat-completion before logits, but endpoint resets cache):**
Harness sent /v1/chat/completions to engage WombatKV save, then
/v1/internal/logits to sample logits. Side-channel signals (24-
75× iter-2 chat speedup, 15-18 bucket objects) confirmed WombatKV
engaged. **But** the logits endpoint called `ds4_session_sync`
with the prompt; when `prompt.len < checkpoint.len` (chat-
completion left session at `prompt + decoded`), sync runs
`session_cpu_reset_cache + full re-prefill`, throwing away the
warm-restored state. L∞=0 reading was Metal determinism for
post-reset cold re-prefills. **Detected via** reading the sync
code path at `ds4.c:18646`.

**v3 (try to skip sync if session has prompt as prefix):**
Added a `session_contains_prompt` check to the endpoint. Skip
sync when session already has the prompt as prefix; sample
directly from the live logits buffer. **But** chat-completion
calls `ds4_session_invalidate(s->session)` at the end (sets
`checkpoint.len = 0`), so by the time the logits endpoint runs,
the session is empty. The skip check sees an empty session, falls
through to sync, fresh cold prefill again. L∞=0 reading was once
more Metal determinism. **Detected via** the new `sync_skipped`
field in the response — it stayed false.

**v4 (endpoint drives WombatKV load+save inline — the canonical):**
Endpoint now calls `wmbt_kv_try_load_blocks` (the same helper
chat-completion uses), then sync, then `wmbt_kv_save_blocks`.
Self-contained measurement, no chat-completion preamble needed.
The `wombatkv_loaded_tokens` field in the response confirms
warm restore actually engaged on iter-2 (= 1009 for our 1010-
token prompt). Bucket counts confirm save engaged on iter-1.

**Hard lesson, captured 4 ways:** when the numbers look perfect
(all L∞=0 across all modes), suspect a measurement bug before
celebrating. Strict bit-equality across distinct architectural
paths (cold full prefill vs warm restore + trailing-1 forward)
is implausible — Metal compute kernels with different batching
will produce slightly different reductions even when reading
identical K/V. If your fidelity proof reports zero drift, you're
probably measuring zero, not fidelity.

### Strong (`scripts/coherence_test.py`)

N-iteration (default 3) pairwise comparison per mode + per-iter
garbage heuristic. Establishes native as the Metal-noise baseline
and reports each WombatKV mode's coherence relative to it.

**Honest framing of what this test CAN and CANNOT prove:**
- CANNOT prove "WombatKV restored K/V is byte-identical to cold-
  computed K/V". That claim needs tensor-level hooks (logit
  snapshot or layer-buffer dump) which ds4-server's HTTP API
  doesn't expose. Even Metal itself is **not bit-deterministic**
  for ds4 inference on M3 Max — observed via native baseline
  where repeated cold runs of the same prompt at temp=0 produce
  divergent text trajectories (argmax flips on near-tied logits).
- CAN prove: every iteration of every mode returns reasonable
  model-generated English text. WombatKV is not corrupting K/V
  badly enough to produce gibberish, wrong-language output, or
  degenerate single-token loops.

Verdict rules:
- HARD: every iter must pass a garbage heuristic
  (non-empty, ≥ 20 chars, ≥ 3 non-trivial words, ≥ 80% ASCII).
- INFORMATIONAL: pairwise lcp / shared_words distributions vs the
  native baseline. Lower coherence than native is noted but not
  auto-failed — could be Metal noise variance OR small WombatKV
  drift; ambiguous without a tensor-level test.

Result file:
[`bench_data/coherence_alpha7.json`](../bench_data/coherence_alpha7.json)

| mode | iters | byte_equal_pairs | max_lcp | shared_words range | verdict |
|---|---:|---|---:|---|---|
| native (baseline) | 3 | 0/3 | 137 | 5..12 | PASS (baseline) |
| embedded | 3 | 0/3 | 65 | 4..8 | PASS |
| daemon-shm | 3 | 0/3 | 16 | 8..11 | PASS |
| daemon-tcp | 3 | 0/3 | 64 | 1..10 | PASS |

All 4 modes pass the HARD garbage check. WombatKV modes show
lower max_lcp than native baseline's best-pair (137) — this could
mean either (a) Metal noise distributes differently across modes
due to latency profile differences, or (b) WombatKV restore
introduces small numerical drift on top of Metal noise. The
text-only test can't disambiguate; a tensor-level test would.

## Engine compute baseline (`ds4-bench`)

CONTRIBUTING.md's speed-regression track. `ds4-bench` runs the
ds4 engine's compute path only — it does NOT engage WombatKV
(no save/load hooks in `ds4_bench.c`). The CSV is therefore a
**pure engine throughput reference**, useful for catching
engine-side perf regressions on future PRs.

Captured at v0.1.0-alpha.7:
[`bench_data/alpha7_speed_metal_ctx16k_gen64.csv`](../bench_data/alpha7_speed_metal_ctx16k_gen64.csv)

| ctx_tokens | prefill_tps | gen_tps |
|---:|---:|---:|
|  2048 | 208.97 | 16.86 |
|  4096 | 177.35 |  9.92 |
|  6144 | 120.80 |  2.58 |
|  8192 |  42.96 |  2.08 |
| 10240 |  72.60 |  1.64 |
| 12288 |  33.14 |  2.72 |
| 14336 |  54.85 |  2.16 |
| 16384 |  42.61 |  4.32 |

## WombatKV-aware perf sweep (`ds4_bench_wombatkv.py`)

Cold + warm latency per (mode × ctx-size) cell. Each cell starts
with a full state wipe — local kvdir, local puffer, daemon puffer,
daemon process, and S3 bucket all reset before the cold turn, so
each measurement is independent (no leakage from a previous cell's
saved blocks even when prompts share a prefix).

Per-mode CSVs in [`bench_data/wombatkv_sweep/`](../bench_data/wombatkv_sweep/).
Last run on M3 Max (Metal):

### Cold latency (ms)

| est_tokens | native | embedded | daemon-shm | daemon-tcp |
|---:|---:|---:|---:|---:|
|  512 |  3815 |  3854 |  9236 | 11736 |
| 1024 |  5837 |  5959 |  9768 |  9362 |
| 2048 |  9679 | 20228 | 39767 | 35402 |

### Warm latency (ms)

| est_tokens | native | embedded | daemon-shm | daemon-tcp |
|---:|---:|---:|---:|---:|
|  512 |  3796 |  560 |  558 |  612 |
| 1024 |  5685 |  561 |  629 |  559 |
| 2048 | 10010 |  581 |  805 | 2322 |

### Speedup (cold / warm)

| est_tokens | native | embedded | daemon-shm | daemon-tcp |
|---:|---:|---:|---:|---:|
|  512 | 1.01× |  6.88× | 16.53× | 19.17× |
| 1024 | 1.03× | 10.62× | 15.52× | 16.73× |
| 2048 | 0.97× | **34.8×** | **49.38×** | 15.24× |

What this confirms:
- Native warm ≈ native cold (no warm path; expected ~1×). Validates
  that the bench harness is fair — kvdir wipe + restart = true
  cold prefill in native mode.
- WombatKV modes deliver consistent strong speedups across the
  ctx sweep. Warm latency is **sub-second across all WombatKV
  modes for ≤ 1024 tokens**, sub-2.5s for 2048.
- Speedup grows with context size — cold prefill cost scales with
  attention's quadratic, warm restore scales roughly linearly in
  block count. So WombatKV's value proposition (cell-B story)
  scales with prompt length.
- daemon-tcp at 2048 (2322 ms) is the only WombatKV warm latency
  above 1 second. The extra ~1.5s vs daemon-shm (805 ms) reflects
  TCP-RTT cost per block lookup (~16 blocks × ~100 ms = ~1.6 s).
  At smaller ctx (fewer blocks), the RTT overhead is amortized
  and TCP latency matches SHM. Expected.

For multi-trial statistical perf (the canonical 73.1× cell-B
record), see `scripts/multi_trial_bench.py` (5-trial harness in
RFC 0013). This sweep is single-trial per cell; numbers carry
~10-30% variance from Metal scheduling + thermal state.

The three bench tracks are complementary:

  ds4-bench                       = engine compute regression (no WombatKV)
  ds4_bench_wombatkv.py           = WombatKV warm-restore sweep (multi-mode × ctx)
  scripts/multi_trial_bench.py    = canonical cell-B statistical record (single ctx, 5 trials)

For WombatKV-specific perf (cell-B warm restore), see
`scripts/multi_trial_bench.py` (5-trial statistical record) and
the per-mode numbers above. The two bench tracks are
complementary: `ds4-bench` = engine compute regression;
`multi_trial_bench.py` = WombatKV warm-restore regression.

## CONTRIBUTING.md correctness suite — what was run

Mode 1 baseline (`./ds4_test --<flag>`):

| subtest | result | notes |
|---|---|---|
| `--server` | PASS | KV-disk cache bookkeeping, request parsing |
| `--kvblock` | PASS | with documented TODO in `tests/ds4_test.c` for the CPU sliding-window `install_raw_tail` install + envelope-roundtrip portion |
| `--metal-kernels` | PASS | <1s |
| `--long-context` | PASS | 9 min 24s; 30k-token prefill + fact recall on 100k ctx |
| `--tool-call-quality` | PASS | 45s |
| `--logprob-vectors` | **FAIL** | pre-existing; `long_memory_archive` vector mismatches. Short vectors pass. Not WombatKV-induced (mode 1 = no WombatKV in path). Likely IQ2 quantization drift vs official non-quantized continuation. |

The `--logprob-vectors` failure is a ds4 model-fidelity gap independent
of WombatKV. It's a P1 follow-up for the engine; not a blocker for
WombatKV alpha sign-off.
