# WombatKV integration in this ds4 fork

This `ds4` repo is a fork of [antirez/ds4](https://github.com/antirez/ds4) with
opt-in [WombatKV](https://github.com/Venkat2811/tensorpuffer) integration:
cross-process, cross-machine prefix-share KV restore that survives
ds4-server restarts.

**The integration is fully opt-in.** Without the `WOMBATKV=1` Make flag,
this ds4 build is byte-identical to upstream antirez/ds4. Existing ds4
users see no behavior change, no new dependencies, no new env vars.

When you opt in (`make ds4-server WOMBATKV=1 WOMBATKV_DIR=<...>`) and set
`DS4_WOMBATKV_ENABLE=1` at runtime, ds4-server links libwombatkv and
participates in the cross-process KV cache substrate.

## Headline numbers (M3 Max + native MinIO loopback)

**73.1× cell-B median speedup, 5-trial, warmup-primed.** ds4 + WombatKV
restores a 1.7k-token prompt's KV state in 108 ms (median) vs 7929 ms for
ds4-native cold-prefill on the same prompt after a process restart.

| Scenario | ds4-native turn-2 | ds4 + WombatKV turn-2 | Speedup |
|---|---:|---:|---:|
| 1.7k-token prompt, cross-restart, kvdisk wiped | 7929 ms | **108 ms** | **73.1×** |
| Multi-conv 5×5, ~9.7k-token shared doc (cross-conversation prefix-share) | 110 535 ms | **1883 ms** | **58.7×** |
| WombatKV-warm TTFT floor (clean S3 run) | — | **99 ms** | up to **82.7×** |

See `docs/MODE_VALIDATION.md` for the full mode matrix + verification
procedures.

## Quick start

### 1. Build (one-time)

You need a checkout of the
[`tensorpuffer`](https://github.com/Venkat2811/tensorpuffer) workspace
alongside this repo.

```sh
# Build the WombatKV substrate's C ABI cdylib + daemon
cd /path/to/tensorpuffer
cargo build --release -p wombatkv-cabi -p wombatkv-daemon

# Build ds4-server with the WombatKV path enabled
cd /path/to/ds4
make ds4-server WOMBATKV=1 WOMBATKV_DIR=/path/to/tensorpuffer
```

The Makefile adds `-DDS4_WOMBATKV` to the C compile + links
`-lwombatkv` with rpath set to find `libwombatkv.{dylib,so}` either
in `<binary>/../lib` or at the build-time WOMBATKV_DIR.

### 2. Pick a mode (4 supported)

| mode | what / when | minimum runtime env |
|---|---|---|
| **embedded** | ds4-server owns the WombatKV store in-process. Simplest. | `DS4_WOMBATKV_ENABLE=1` + `WMBT_KV_S3_*` |
| **daemon-shm** | a `wombatkv-daemon` on the same host owns the store; ds4-server connects via myelon disruptor SHM ring. Use when multiple engines on one box share one cache. | `DS4_WOMBATKV_ENABLE=1` + `WMBT_KV_REMOTE_PREFIX=<name>` |
| **daemon-tcp** | daemon on a different host. ds4-server speaks length-prefixed rkyv frames over TCP. Validated Mac↔Linux. | `DS4_WOMBATKV_DAEMON_TCP=<host:port>` |
| **daemon-http** | same wire envelope wrapped in HTTP/1.1 POSTs. For load-balanced / proxy deployments. | `DS4_WOMBATKV_DAEMON_HTTP=<host:port>` |

### 3. Required common env

All modes require S3-compatible credentials + bucket:

```sh
export WMBT_KV_S3_ENDPOINT=http://127.0.0.1:9000      # MinIO or S3 endpoint
export WMBT_KV_S3_ACCESS_KEY=minioadmin
export WMBT_KV_S3_SECRET_KEY=minioadmin
export WMBT_KV_BUCKET=wombatkv-demo
```

### 4. Run the validation smoke

```sh
# Same-host smoke for embedded / daemon-shm / daemon-tcp / daemon-http
python3 scripts/mode_smoke.py all

# Cross-host TCP smoke (daemon on a different machine)
python3 scripts/mode_smoke.py daemon-tcp-remote --remote-tcp host:port
```

`scripts/mode_smoke.py` is the canonical "did this work?" gate — two-turn
cell-B pattern, restart between turns, verifies turn-2 restores KV from
WombatKV instead of cold-prefilling.

## ds4-side env vars

| env var | meaning | default |
|---|---|---|
| `DS4_WOMBATKV_ENABLE` | Activate WombatKV; required for embedded + daemon-shm | unset (no-op) |
| `DS4_WOMBATKV_DAEMON_TCP` | `host:port` of remote `wombatkv-daemon --tcp` listener | unset |
| `DS4_WOMBATKV_DAEMON_HTTP` | `host:port` of remote `wombatkv-daemon --http` listener | unset |
| `DS4_WOMBATKV_FINGERPRINT24` | 24-hex model fingerprint (key derivation factor) | derived from `sha1(model_path)[:24]` |

All other env vars (S3, namespace, cache sizing, eviction, prefetch, etc.)
are tensorpuffer-side and use the `WMBT_KV_*` prefix. See tensorpuffer's
`docs/ENV.md` for the full inventory.

## Architecture: where the integration lives

All WombatKV-related ds4 modifications are gated behind `#ifdef DS4_WOMBATKV`.
Source modifications:

- **`ds4_server.c`** — `wmbt_kv_init_hooks()` (lines ~130-260): mode
  selection from env, opens the WombatKV handle. All `wmbt_kv_*` calls in
  the server hot paths are `#ifdef`-gated.
- **`ds4.c`** — the CRC32C trio (`ds4_crc32c_init_state / _update / _finalize`)
  routes through `wmbt_kv_crc32c_append` in `DS4_WOMBATKV` builds (hardware
  acceleration via libwombatkv's `crc32c` Rust crate runtime dispatch).
  Pure-ds4 builds keep the original software-table impl byte-for-byte
  unchanged.
- **`ds4_kvstore.c`** — block-prefix save/load helpers that call the
  `wmbt_kv_get_kv_blocks_borrowed` / `wmbt_kv_put_kv_blocks` C ABI.
- **`Makefile`** — `WOMBATKV=1 WOMBATKV_DIR=<path>` adds `-DDS4_WOMBATKV` +
  `-I.../wombatkv-cabi/include` + `-L.../target/release -lwombatkv` + rpath.

The default ds4 build (no `WOMBATKV=1`) doesn't compile any of this in.

## Walkthroughs

- `docs/MODE_VALIDATION.md` — full mode-matrix validation procedures + the
  expected verification signals per mode.
- `docs/demo_pi_ds4_wombatkv.md` — enabling WombatKV under
  [pi-ds4](https://github.com/mitsuhiko/pi-ds4) without forking that plugin.

## Status

WombatKV alpha.14-prep, validated on:
- Mac M3 Max + native MinIO (embedded, daemon-shm, daemon-tcp, daemon-http loopback)
- Cross-host Mac ↔ Linux (AMD Ryzen 5800X) over LAN (daemon-tcp, daemon-http remote)
- L∞=0 bit-parity across all 5 modes (logit_fidelity_test gate)
- DST harness (17 failure classes) passing seeded sweeps

Wire format, on-disk envelopes, and C ABI are in the alpha breaking window
— changes can land without back-compat shims until the OSS tag. After that,
breaking changes follow standard semver.

## Reporting issues

WombatKV-integration issues: open against
[Venkat2811/tensorpuffer](https://github.com/Venkat2811/tensorpuffer/issues)
(the substrate) or this repo (the ds4-side integration).

Upstream antirez/ds4 issues unrelated to WombatKV: open against
[antirez/ds4](https://github.com/antirez/ds4/issues).
