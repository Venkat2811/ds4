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

Five harnesses cover the ds4 × WombatKV substrate × five transports
(native, embedded, daemon-shm, daemon-tcp, daemon-http). Run the
relevant ones for any change that touches: the WombatKV C ABI,
sidecar / block on-disk format, save/load hooks in `ds4_server.c`,
or block-prefix kvblocks code in `ds4.c`.

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

Tier-A byte-roundtrip (the bit-parity foundation, fast, sandbox-safe):

```sh
cd <tensorpuffer>
cargo test --workspace --lib --release    # ~50s
```

What each harness proves:

| harness | level | proves |
|---|---|---|
| `mode_smoke.py` | text | server boots cleanly per mode, ≥1 block written to S3, turn-2 warm latency < turn-1 cold, response non-empty |
| `coherence_test.py` | text | N repeated cold/warm cycles produce reasonable English (no garbage); pairwise LCP / shared-words within native noise floor |
| `logit_fidelity_test.py` | tensor (logits) | L∞ logit drift cold-vs-warm matches ds4's huge-blob native-warm baseline (≤ 0.05), `wombatkv_loaded_tokens` confirms restore engaged, top-1 token preserved |
| `multi_user_multiturn.py` | text + concurrency | 5 distinct users with separate histories all produce coherent output; cross-user contamination check; intra-user turn-(2..N) warm-restore wins |
| `cargo test --lib` | bytes | Tier-A adversarial-payload byte roundtrip through foyer + S3 |

If you only have time for one, `mode_smoke.py all` is the right
broad spot-check. If your change touches the K/V tensor layout
(layer counts, head dim, compressor ratios), also run
`logit_fidelity_test.py` — it's the test that caught the v5/v7 →
v8 partial-tail / compressor-state gap.

Cross-machine TCP (mode-4 cross-host) and HTTP (mode-5 cross-host)
have remote-daemon variants — see docs/MODE_VALIDATION.md.

## Reporting sessions bugs

For debugging a failing generation, keep the trace:

```sh
./ds4-server --trace /tmp/ds4-trace.txt ...
```
