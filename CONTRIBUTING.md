# Contributing

DwarfStar4 changes should be tested against the failure mode they can realistically
affect. The project has two regression tracks: correctness and speed. Please
include the commands you ran, the machine/backend, the model quant, and any
notable failures in the PR or commit notes.

Do not send PRs affecting one or more inference backends without checking if the
resulting code is still correct and fast. The only acceptable regression speed
is when an important correctness bug is fixed and it requires some speed penalty.

## Correctness Regression Tests

Build the default backend first:

```sh
make clean
make
```

The C test runner is `ds4_test`. Running it without arguments is equivalent to
`--all`:

```sh
make test
```

Useful narrower checks:

```sh
./ds4_test --server
./ds4_test --logprob-vectors
./ds4_test --long-context
./ds4_test --tool-call-quality
./ds4_test --metal-kernels
```

What they cover:

- `--server`: request parsing, chat rendering, streaming, tool-call parsing,
  thinking controls, KV disk-cache bookkeeping, and other server-side logic.
  This is the best quick check for API and prompt-rendering changes.
- `--logprob-vectors`: compares local token bytes and top-logprob slices against
  official DeepSeek V4 Flash continuation vectors. This catches tokenizer,
  template, attention, and logits regressions.
- `--long-context`: runs a long-context story fact-recall regression from
  `tests/long_context_story_prompt.txt`. The model must retrieve spelled-out
  person-number assignments from a long prose prompt and return `Name=number`
  lines that the test parses.
- `--tool-call-quality`: exercises actual model behavior for DSML tool-call
  emission in both fast and exact paths.
- `--metal-kernels`: isolated Metal kernel numeric checks.

The runner defaults to `ds4flash.gguf`. Override paths when needed:

```sh
DS4_TEST_MODEL=/path/to/model.gguf ./ds4_test --logprob-vectors
DS4_TEST_VECTOR_FILE=/path/to/official.vec ./ds4_test --logprob-vectors
DS4_TEST_LONG_PROMPT=/path/to/prompt.txt ./ds4_test --long-context
```

For CUDA-specific changes, test on a CUDA machine:

```sh
make
make cuda-regression
```

For CPU portability, at least verify that the CPU target still builds:

```sh
make cpu
```

The CPU backend is a reference/debug path, not the production performance
target. Remember that executing the CPU path on Metal can crash the system
because of a kernel bug in macOS.

## Quality Checks For Quantization Changes

For GGUF or quantization work, use the official-continuation scorer in
`gguf-tools/quality-testing`. The test compares how much probability a local
GGUF assigns to official DeepSeek V4 Flash continuations, token by token.

Build the scorer:

```sh
make -C gguf-tools quality-score
```

Then score old and new GGUFs against the same manifest and compare:

```sh
gguf-tools/quality-testing/score_official OLD.gguf \
  gguf-tools/quality-testing/data/manifest.tsv /tmp/old.tsv 4096

gguf-tools/quality-testing/score_official NEW.gguf \
  gguf-tools/quality-testing/data/manifest.tsv /tmp/new.tsv 4096

python3 gguf-tools/quality-testing/compare_scores.py /tmp/old.tsv /tmp/new.tsv
```

Lower `avg_nll` is better. See
`gguf-tools/quality-testing/README.md` for collecting or refreshing official
continuations.

## Speed Regression Tests

Use `ds4-bench` for throughput regressions. It reports instantaneous prefill and
generation speed at context frontiers, not one whole-run average. Prefill is
incremental: each row measures only the newly processed suffix since the
previous frontier.

Default linear sweep:

```sh
./ds4-bench \
  -m ds4flash.gguf \
  --prompt-file speed-bench/promessi_sposi.txt \
  --ctx-start 2048 \
  --ctx-max 65536 \
  --step-incr 2048 \
  --gen-tokens 128 \
  --csv /tmp/ds4-speed.csv
```

Use the same machine, backend, model file, context sweep, power/thermal state,
and background load when comparing two commits. For backend work, run at least
one before/after CSV and compare both `prefill_tps` and `gen_tps`. Generation is
greedy and skips EOS so each frontier gets the same number of generated tokens.

To generate a graph for a CSV:

```sh
python3 speed-bench/plot_speed.py /tmp/ds4-speed.csv --title "Machine t/s"
```

## WombatKV integration tests

WombatKV testing is **both** extensions of the existing ds4_test
suite **and** new harnesses we added on top of the upstream ds4
correctness framework. Run the relevant ones for any change that
touches: the WombatKV C ABI, sidecar / block on-disk format, save/load
hooks in `ds4_server.c`, or block-prefix kvblocks code in `ds4.c`.

### Extended `ds4_test` subtests (upstream framework, our additions)

The `ds4_test --kvblock` subtest registered in `tests/ds4_test.c`'s
`test_kvblock_group` is the upstream-shape extension. We added:

| function | what |
|---|---|
| `test_kvblock_validation` | block_tokens range validation (upstream-shape, pre-existing) |
| `test_crc32c_known_vector` | CRC32C polynomial sanity (alpha.11+; see RFC 0018 §13 C4) |
| `test_kvblock_cpu_roundtrip` | block save/load_blocks round-trip (upstream-shape) |
| `test_kvblock_raw_tail_sidecar_roundtrip` | sidecar save + magic check (alpha.7+) |
| `test_kvblock_raw_tail_v4_corruption_rejected` | RFC 0018 Phase 1 — v4 sidecar envelope rejects bad CRC / bad version / bad magic / truncation (alpha.11+) |
| `test_kvblock_block_v2_corruption_rejected` | RFC 0018 Phase 2 — v2 block envelope rejects bad CRC / bad version (alpha.11+) |

Run them with: `./ds4_test --kvblock` (after `make ds4_test
WOMBATKV=1 WOMBATKV_DIR=<path>`). The corruption rejection tests are
the "this CRC actually catches things" proof — without them we'd have
CRC computation but no proof it gates.

### New harnesses (no upstream analog)

Five Python harnesses cover the ds4 × WombatKV substrate × five
transports (native, embedded, daemon-shm, daemon-tcp, daemon-http):

Pre-req for non-native modes: native MinIO on `127.0.0.1:9200` +
`libwombatkv.dylib` and `wombatkv-daemon` built in tensorpuffer (see
docs/MODE_VALIDATION.md for the env setup).

```sh
# Quick same-host smoke across all transports (~10-15 min):
python3 scripts/mode_smoke.py all

# Per-mode (~2 min/mode):
python3 scripts/mode_smoke.py daemon-http
python3 scripts/mode_smoke.py daemon-tcp
# ...

# Output coherence under repeated cold/warm cycles (~5-8 min/mode):
python3 scripts/coherence_test.py
python3 scripts/coherence_test.py daemon-http --iters 5

# Tensor-level logit fidelity (THE bit-parity proof; ~8-12 min):
DS4_DEBUG_INTERNAL=1 python3 scripts/logit_fidelity_test.py

# Multi-user multi-turn (5 distinct users × 3 turns each × all 5
# transports; ~10-15 min):
python3 scripts/scenarios/multi_user_multiturn.py --mode all \
    --output /tmp/multi-user-results.json

# Cross-session restore stress (kills+restarts ds4-server between
# users — exercises the load-from-S3 path explicitly):
python3 scripts/scenarios/multi_user_multiturn.py --mode all \
    --restart-between-users
```

### tensorpuffer-side test runs (Rust)

Tier-A byte-roundtrip + envelope corruption rejection + transport-
layer negative-path + DST schedule determinism (fast, sandbox-safe):

```sh
cd <tensorpuffer>
# All lib tests across the workspace (~50s, currently 236 tests at
# alpha.11+1: 226 from alpha.11 + 10 envelope-corruption tests):
cargo test --workspace --lib --release

# DST schedule sweep (~3s for 500 plans = 50 seeds × 10 fault classes
# including the 3 alpha.11 transport-layer classes):
./scripts/dst-sweep.sh --seeds 1-50

# Larger sweep before tagging (10,000 plans, ~30s):
./scripts/dst-sweep.sh --seeds 1-1000
```

The 10 envelope-corruption tests (5 per HTTP + 5 per TCP transport)
are the proof that bad magic / wrong version / CRC mismatch / oversize
/ truncation all produce clean rejection (drop connection or 4xx)
without panic. See RFC 0018 §13 for the full audit.

What each harness proves:

| harness | level | proves |
|---|---|---|
| `mode_smoke.py` | text | server boots cleanly per mode, ≥1 block written to S3, turn-2 warm latency < turn-1 cold, response non-empty |
| `coherence_test.py` | text | N repeated cold/warm cycles produce reasonable English (no garbage); pairwise LCP / shared-words within native noise floor |
| `logit_fidelity_test.py` | tensor (logits) | L∞ logit drift cold-vs-warm matches ds4's huge-blob native-warm baseline (≤ 0.05), `wombatkv_loaded_tokens` confirms restore engaged, top-1 token preserved |
| `multi_user_multiturn.py` | text + concurrency | 5 distinct users with separate histories all produce coherent output; cross-user contamination check; intra-user turn-(2..N) warm-restore wins |
| `cargo test --lib` (tensorpuffer) | bytes + wire | Tier-A adversarial-payload byte roundtrip + envelope encode/decode + transport-layer corruption rejection (TCP + HTTP) |
| `dst-sweep.sh` (tensorpuffer) | DST | 50 seeds × 10 fault classes → 500 deterministic fault plans, all generate cleanly |
| `ds4_test --kvblock` | C-side bytes | sidecar v4 + block v2 round-trip AND corruption rejection (alpha.11+ adds CRC32C gating tests) |

If you only have time for one, `mode_smoke.py all` is the right
broad spot-check. If your change touches the K/V tensor layout
(layer counts, head dim, compressor ratios), also run
`logit_fidelity_test.py` — it's the test that caught the v5/v7 →
v8 partial-tail / compressor-state gap. If your change touches an
on-disk format (sidecar / block) or wire envelope, run
`ds4_test --kvblock` for the C-side corruption-rejection proof and
`cargo test --workspace --lib` for the Rust-side envelope proof.

Cross-machine TCP (mode-4 cross-host) and HTTP (mode-5 cross-host)
have remote-daemon variants — see docs/MODE_VALIDATION.md.

### Known testing gaps

See RFC 0018 §13 (in
`myelon-launch/ai-chat-exports/.0_agentic_engineering/5_tensor_puffer/
0_rfcs/0018_wire_storage_versioning_transactional_discipline.md`) for
the honest gap audit: CPU↔GPU byte-equality tests for sidecar/block,
DST Stage 3.5 live-runner harness, OS-level fault injection, and
cross-host post-envelope re-validation are all queued but not yet
closed.

## Reporting sessions bugs

For debugging a failing generation, keep the trace:

```sh
./ds4-server --trace /tmp/ds4-trace.txt ...
```
