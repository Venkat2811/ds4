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
