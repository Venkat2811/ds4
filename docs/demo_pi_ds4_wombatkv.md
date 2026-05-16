# Pi + ds4 + WombatKV — no-fork demo walkthrough

This walkthrough enables WombatKV under the upstream
[`mitsuhiko/pi-ds4`](https://github.com/mitsuhiko/pi-ds4) plugin **without
forking it**. We use the plugin's existing `DS4_SERVER_BINARY` override plus
shell env-var inheritance — Pi spawns whatever `ds4-server` you point it at,
and your env vars flow through.

## Why

A single contributor on the team builds a WombatKV-enabled `ds4-server` once
and shares the binary path. Every other `pi` user on the team picks it up by
setting one config field plus six env vars in their shell. No fork, no
upstream-patch dance, no per-user build of ds4.

The team multiplier: warm KV-blocks captured by one teammate's session are
available to every other teammate hitting the same S3 bucket — so cold prefill
on a long shared prompt happens once across the team, not once per developer.

## Prerequisites

- macOS on Apple Silicon (M3 or M4 recommended; M3 Max validated)
- Native MinIO running on `127.0.0.1:9200` with `minioadmin` / `minioadmin`
  credentials, or any S3-compatible endpoint
- [`pi`](https://github.com/mitsuhiko/pi) CLI installed (`pi --version` works)
- A DSV4-Flash IQ2XXS GGUF model file on disk

## Step 1 — Build WombatKV-enabled `ds4-server`

From the `ds4` repo with the `tensorpuffer` workspace checked out alongside:

```bash
cd /path/to/ds4
make ds4-server WOMBATKV=1 WOMBATKV_DIR=/path/to/tensorpuffer
```

This produces `./ds4-server` linked against the WombatKV C ABI (`wmbt_kv_*`
symbols). Confirm with:

```bash
./ds4-server --version 2>&1 | head -3
```

You should see a `WombatKV 0.1.0-alpha` banner line on the first stderr
output.

## Step 2 — Install upstream `pi-ds4`

```bash
pi install https://github.com/mitsuhiko/pi-ds4
```

This installs the plugin at `~/.pi/ds4/`. Do not fork it.

## Step 3 — Point `pi-ds4` at the WombatKV-enabled binary

Edit `~/.pi/ds4/settings.json` and add the `DS4_SERVER_BINARY` field (create
the file if it does not exist yet):

```json
{
  "DS4_SERVER_BINARY": "/path/to/ds4/ds4-server",
  "DS4_MODEL": "/path/to/DeepSeek-V4-Flash-IQ2XXS.gguf"
}
```

That's the entire override. Pi will now spawn your binary instead of the
default one, inheriting environment from the shell that runs `pi`.

## Step 4 — Export six WombatKV env vars, then run `pi`

In whatever shell you run `pi` from (or in `~/.zshrc` / `~/.bashrc`):

```bash
# 1) Master switch — flips ds4 to use WombatKV for KV save/restore
export DS4_WOMBATKV_ENABLE=1

# 2) S3 endpoint for the WombatKV substrate
export WMBT_KV_S3_ENDPOINT=http://127.0.0.1:9200

# 3) S3 credentials
export WMBT_KV_S3_ACCESS_KEY=minioadmin
export WMBT_KV_S3_SECRET_KEY=minioadmin

# 4) Bucket the team shares — pick any name, all teammates use the same one
export WMBT_KV_BUCKET=wombatkv-team-shared

# 5) Local foyer directory (puffer) — fast tier between S3 and ds4
export WMBT_KV_PUFFER_DIR=$HOME/.wombatkv/puffer

# 6) Namespace — keeps different models / dtypes apart in the same bucket
export WMBT_KV_NAMESPACE=ds4-metal
```

After the defaults flip in a later alpha, several of these are expected to
become optional (endpoint, namespace, puffer-dir will gain sensible
defaults). The six above are the minimal set today.

Now run a normal Pi session:

```bash
pi
```

…and use ds4 as you normally would.

## Verify it engaged

On the first `pi` invocation, `ds4-server`'s stderr (visible in the Pi
log pane, or `~/.pi/ds4/server.log` depending on plugin version) should
show:

- A `WombatKV 0.1.0-alpha` banner line near the top
- One of: `wombatkv: tier_b engaged` or `wombatkv: tier_a hit` once a
  prompt finishes prefill
- On the second run with the same long shared prompt: a noticeably faster
  TTFT compared to a fresh-bucket cold start

For headline numbers on the speedup, see the bench artifacts in
`scripts/scenarios/` outputs (run by the demo harness) rather than relying
on this doc to stay current.

## Troubleshooting

- **Plugin still spawns the default binary** — check
  `~/.pi/ds4/settings.json` is valid JSON and the `DS4_SERVER_BINARY`
  path is absolute and executable (`ls -l` it)
- **No WombatKV banner in stderr** — your `ds4-server` was built without
  `WOMBATKV=1`. Rebuild and re-link
- **`S3 connect refused`** — MinIO is not running, or the endpoint URL
  is wrong. `curl $WMBT_KV_S3_ENDPOINT` should return a non-empty
  response
- **Mac SHM-name budget errors at startup** — daemon mode uses short
  SHM names; if you set a `WMBT_KV_DAEMON_PREFIX` keep it under
  18 chars
