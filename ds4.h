#ifndef DS4_H
#define DS4_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

/* Public engine boundary.
 *
 * The CLI and server should treat ds4_engine as the loaded model and
 * ds4_session as one mutable inference timeline.  A session owns the live KV
 * cache and logits; callers provide full token prefixes and let
 * ds4_session_sync() reuse, extend, or rebuild the graph state.  Keep this
 * header narrow so HTTP/CLI code does not depend on tensor internals. */

typedef enum {
    DS4_BACKEND_METAL,
    DS4_BACKEND_CUDA,
    DS4_BACKEND_CPU,
} ds4_backend;

typedef enum {
    DS4_THINK_NONE,
    DS4_THINK_HIGH,
    DS4_THINK_MAX,
} ds4_think_mode;

typedef enum {
    DS4_LOG_DEFAULT,
    DS4_LOG_PREFILL,
    DS4_LOG_GENERATION,
    DS4_LOG_KVCACHE,
    DS4_LOG_TOOL,
    DS4_LOG_WARNING,
    DS4_LOG_TIMING,
    DS4_LOG_OK,
    DS4_LOG_ERROR,
} ds4_log_type;

typedef struct {
    int *v;
    int len;
    int cap;
} ds4_tokens;

typedef struct {
    int id;
    float logit;
    float logprob;
} ds4_token_score;

#define DS4_DEFAULT_TEMPERATURE 1.0f
#define DS4_DEFAULT_TOP_P 1.0f
#define DS4_DEFAULT_MIN_P 0.05f

typedef struct ds4_engine ds4_engine;
typedef struct ds4_session ds4_session;

typedef void (*ds4_session_progress_fn)(void *ud, const char *event, int current, int total);

typedef struct {
    const char *model_path;
    const char *mtp_path;
    ds4_backend backend;
    int n_threads;
    int mtp_draft_tokens;
    float mtp_margin;
    const char *directional_steering_file;
    float directional_steering_attn;
    float directional_steering_ffn;
    bool warm_weights;
    bool quality;
} ds4_engine_options;

typedef void (*ds4_token_emit_fn)(void *ud, int token);
typedef void (*ds4_generation_done_fn)(void *ud);

typedef struct {
    uint64_t total_bytes;
    uint64_t raw_bytes;
    uint64_t compressed_bytes;
    uint64_t scratch_bytes;
    uint32_t prefill_cap;
    uint32_t raw_cap;
    uint32_t comp_cap;
} ds4_context_memory;

typedef struct {
    uint8_t *ptr;
    uint64_t len;
    uint64_t cap;
} ds4_session_snapshot;

int ds4_engine_open(ds4_engine **out, const ds4_engine_options *opt);
void ds4_engine_close(ds4_engine *e);
void ds4_engine_summary(ds4_engine *e);
const char *ds4_backend_name(ds4_backend backend);
bool ds4_think_mode_enabled(ds4_think_mode mode);
const char *ds4_think_mode_name(ds4_think_mode mode);
const char *ds4_think_max_prefix(void);
uint32_t ds4_think_max_min_context(void);
ds4_think_mode ds4_think_mode_for_context(ds4_think_mode mode, int ctx_size);
ds4_context_memory ds4_context_memory_estimate(ds4_backend backend, int ctx_size);
bool ds4_log_is_tty(FILE *fp);
void ds4_log(FILE *fp, ds4_log_type type, const char *fmt, ...);
int ds4_engine_generate_argmax(ds4_engine *e, const ds4_tokens *prompt,
                               int n_predict, int ctx_size,
                               ds4_token_emit_fn emit,
                               ds4_generation_done_fn done,
                               void *emit_ud,
                               ds4_session_progress_fn progress,
                               void *progress_ud);
int ds4_engine_collect_imatrix(ds4_engine *e,
                               const char *dataset_path,
                               const char *output_path,
                               int ctx_size,
                               int max_prompts,
                               int max_tokens);
void ds4_engine_dump_tokens(ds4_engine *e, const ds4_tokens *tokens);
int ds4_dump_text_tokenization(const char *model_path, const char *text, FILE *fp);
int ds4_engine_head_test(ds4_engine *e, const ds4_tokens *prompt);
int ds4_engine_first_token_test(ds4_engine *e, const ds4_tokens *prompt);
int ds4_engine_metal_graph_test(ds4_engine *e, const ds4_tokens *prompt);
int ds4_engine_metal_graph_full_test(ds4_engine *e, const ds4_tokens *prompt);
int ds4_engine_metal_graph_prompt_test(ds4_engine *e, const ds4_tokens *prompt, int ctx_size);

void ds4_tokens_push(ds4_tokens *tv, int token);
void ds4_tokens_free(ds4_tokens *tv);
void ds4_tokens_copy(ds4_tokens *dst, const ds4_tokens *src);
bool ds4_tokens_starts_with(const ds4_tokens *tokens, const ds4_tokens *prefix);

void ds4_tokenize_text(ds4_engine *e, const char *text, ds4_tokens *out);
void ds4_tokenize_rendered_chat(ds4_engine *e, const char *text, ds4_tokens *out);
void ds4_chat_begin(ds4_engine *e, ds4_tokens *tokens);
void ds4_encode_chat_prompt(
        ds4_engine *e,
        const char *system,
        const char *prompt,
        ds4_think_mode think_mode,
        ds4_tokens *out);
void ds4_chat_append_max_effort_prefix(ds4_engine *e, ds4_tokens *tokens);
void ds4_chat_append_message(ds4_engine *e, ds4_tokens *tokens, const char *role, const char *content);
void ds4_chat_append_assistant_prefix(ds4_engine *e, ds4_tokens *tokens, ds4_think_mode think_mode);

char *ds4_token_text(ds4_engine *e, int token, size_t *len);
int ds4_token_eos(ds4_engine *e);
int ds4_token_user(ds4_engine *e);
int ds4_token_assistant(ds4_engine *e);

int ds4_session_create(ds4_session **out, ds4_engine *e, int ctx_size);
void ds4_session_free(ds4_session *s);
void ds4_session_set_progress(ds4_session *s, ds4_session_progress_fn fn, void *ud);

typedef enum {
    DS4_SESSION_REWRITE_ERROR = -1,
    DS4_SESSION_REWRITE_OK = 0,
    /* The live backend state cannot be rewritten safely in place.  The caller should
     * restore an older checkpoint if it has one, then sync to the prompt. */
    DS4_SESSION_REWRITE_REBUILD_NEEDED = 1,
} ds4_session_rewrite_result;

/* Synchronize the live session to a full prompt token prefix.  If the current
 * checkpoint is a prefix, only the suffix is evaluated; otherwise the backend
 * state is refilled from scratch. */
int ds4_session_sync(ds4_session *s, const ds4_tokens *prompt, char *err, size_t errlen);
bool ds4_session_rewrite_requires_rebuild(int live_len, int canonical_len, int common);
ds4_session_rewrite_result ds4_session_rewrite_from_common(
        ds4_session *s, const ds4_tokens *prompt, int common,
        char *err, size_t errlen);
int ds4_session_common_prefix(ds4_session *s, const ds4_tokens *prompt);
int ds4_session_argmax(ds4_session *s);
int ds4_session_argmax_excluding(ds4_session *s, int excluded_id);
int ds4_session_sample(ds4_session *s, float temperature, int top_k, float top_p, float min_p, uint64_t *rng);
int ds4_session_top_logprobs(ds4_session *s, ds4_token_score *out, int k);
int ds4_session_token_logprob(ds4_session *s, int token, ds4_token_score *out);
int ds4_session_eval(ds4_session *s, int token, char *err, size_t errlen);
int ds4_session_eval_speculative_argmax(ds4_session *s, int first_token,
                                        int max_tokens, int eos_token,
                                        int *accepted, int accepted_cap,
                                        char *err, size_t errlen);
void ds4_session_invalidate(ds4_session *s);
void ds4_session_rewind(ds4_session *s, int pos);
int ds4_session_pos(ds4_session *s);
int ds4_session_ctx(ds4_session *s);
int ds4_engine_routed_quant_bits(ds4_engine *e);
bool ds4_engine_has_mtp(ds4_engine *e);
int ds4_engine_mtp_draft_tokens(ds4_engine *e);
const ds4_tokens *ds4_session_tokens(ds4_session *s);

/* Disk KV cache payload helpers.  The server owns the outer file header and
 * policy; the engine owns the DS4-specific serialized graph state. */
uint64_t ds4_session_payload_bytes(ds4_session *s);
int ds4_session_save_payload(ds4_session *s, FILE *fp, char *err, size_t errlen);
int ds4_session_load_payload(ds4_session *s, FILE *fp, uint64_t payload_bytes, char *err, size_t errlen);
int ds4_session_save_snapshot(ds4_session *s, ds4_session_snapshot *snap, char *err, size_t errlen);
int ds4_session_load_snapshot(ds4_session *s, const ds4_session_snapshot *snap, char *err, size_t errlen);
void ds4_session_snapshot_free(ds4_session_snapshot *snap);

/* ============================================================================
 * Token-aligned KV blocks (RFC 0007 Tier B — KVBlock/0.1)
 * ----------------------------------------------------------------------------
 * Slice the session's KV state by token range. Used by WombatKV to store
 * content-addressed token-aligned blocks on object storage, enabling
 * prefix sharing across prompts that share token prefixes (the vLLM /
 * SGLang / Dynamo block-cache pattern).
 *
 * IMPORTANT ALIGNMENT CONSTRAINT (per the audit of ds4.c:15988-16179
 * + the per-layer compressor frontier semantics):
 *   block_tokens = token_end - token_start  MUST satisfy:
 *     - multiple of LCM(4, 128) = 128
 *   (Original draft said {4..128 divisor-of-128 and multiple-of-4};
 *    audit of save_payload's per-layer compressed-row emit logic
 *    revealed that ratio-128 layers emit 1 row every 128 tokens,
 *    so block_tokens < 128 yields 0 or fractional compressed rows
 *    for those layers — would require shipping partial frontier
 *    state per block. Deferred; require multiple-of-128 for now.)
 *   → allowed values: {4, 8, 16, 32, 64, 128}.
 * Using a misaligned block_tokens corrupts the compressor frontier state
 * for one or more layers. The save/load entry points enforce this.
 *
 * Recommended default: block_tokens = 128 (the minimum aligned size).
 * Allowed: any positive multiple of 128 up to 8192.
 *
 * These APIs are SKELETON DECLARATIONS as of the _kvblocks branch —
 * implementation lands incrementally with associated tests. Callers
 * should defer hard production reliance on them until Tier B is marked
 * stable in the WombatKV CHANGELOG.
 *
 * Server-side env-var precedence (consumed by ds4_server, not the libds4 API):
 *   WMBT_KV_TIER_B=1            opt in to the block-chain load/store path.
 *   WMBT_KV_BOOTSTRAP_WORLD=1   re-index S3 manifests at handle init so
 *                               Tier B can engage on the FIRST request
 *                               after a process restart.
 *   WMBT_KV_TIER_B=1 implicitly enables WMBT_KV_BOOTSTRAP_WORLD=1 unless
 *   the user has set it explicitly (e.g. =0 to opt out of the implicit
 *   bootstrap when they know the in-process index is already warm).
 * ============================================================================ */

/* One block in a load batch. token_end is exclusive. */
typedef struct ds4_block_handle {
    int    token_start;     /* inclusive; aligned to block_tokens */
    int    token_end;       /* exclusive; aligned to block_tokens */
    FILE  *fp;              /* points to start of block body (after envelope header if any) */
    size_t payload_bytes;   /* exact body bytes for this block */
} ds4_block_handle;

/* Save the KV state for exactly one block (tokens [token_start, token_end))
 * into `fp` in the per-block body format (see RFC 0007 §3.2 / 5.4 and the
 * inline wire-format documentation at `kvblock_save_block_cpu` in ds4.c).
 *
 * Preconditions:
 *   - (token_end - token_start) must be a positive multiple of 128 (<=8192)
 *   - token_start must be aligned to (token_end - token_start)
 *   - token_end must be <= ds4_session_tokens(s)->len
 *   - The session must have a valid checkpoint (a sync()/eval() has run)
 *
 * Implementation status:
 *   - CPU backend: IMPLEMENTED. Writes per-layer compressed K/V (and indexer
 *     K/V for ratio-4 layers) scoped to [token_start, token_end). Raw K/V
 *     and compressor frontier state are intentionally omitted: load
 *     regenerates raw KV via prefill, and block_tokens%128==0 guarantees
 *     the frontier is empty at the boundary.
 *   - Graph backend (Metal / CUDA): IMPLEMENTED. Same wire format as the
 *     CPU path — bytes saved on either backend can be loaded on either
 *     backend. Bounds rule: only compressed rows that the engine has
 *     already committed (g->layer_n_comp[il]) can be saved; a block range
 *     that lands inside the raw-only frontier is rejected with an
 *     informative error.
 *
 * Returns 0 on success; -1 on error with err populated.
 */
int ds4_session_save_block(ds4_session *s, FILE *fp,
                           int token_start, int token_end,
                           char *err, size_t errlen);

/* RFC 0007 §10.P5 raw-tail sidecar — save the session's SWA-window raw
 * KV to a memory FILE* as a standalone sidecar payload. The block chain
 * is independent — this is meant to be uploaded under
 * `wkv/v1/sidecar/raw_tail/b3=<chain_tip_hash>` by the caller (typically
 * ds4_server) right after a successful wmbt_kv_put_kv_blocks() call.
 *
 * The on-disk envelope is the RTT1 layout documented at
 * DS4_KVBLOCK_RAW_TAIL_MAGIC in ds4.c:
 *   24 B header (magic, version, n_layers, n_raw_rows, head_dim,
 *                bytes_per_elem)
 *   n_layers × n_raw_rows × head_dim × bytes_per_elem bytes of raw KV
 *   4 B end sentinel
 *
 * For DSV4 with n_raw_rows=DS4_N_SWA=128: 11,272,220 bytes total.
 *
 * Preconditions: the session has a valid checkpoint (saw at least one
 * prefill since creation/invalidate). The SWA ring is read from the
 * CPU/Metal layer caches in the current backend.
 *
 * Returns 0 on success; -1 on error with err populated.
 */
/* `prompt_tokens_count` is the prompt length to record in the envelope's
 * `original_total_tokens` field. Callers that save after generation has
 * extended the live checkpoint past the prompt MUST pass the prompt
 * length here; otherwise the install path on restore would extend the
 * checkpoint past the next request's prompt and `ds4_session_sync` would
 * fall through to a full re-prefill. Pass 0 to use `s->checkpoint.len`
 * (the legacy behaviour, only correct when save runs at end of prefill). */
int ds4_session_save_raw_tail(ds4_session *s, FILE *fp,
                              uint32_t prompt_tokens_count,
                              char *err, size_t errlen);

/* RFC 0007 §10.P5 raw-tail sidecar — install raw KV bytes into the
 * session's SWA ring from a sidecar payload (the inverse of
 * ds4_session_save_raw_tail). Used by ds4_server after a Tier B
 * load_blocks() succeeded, when the matching sidecar GET also hit.
 *
 * After successful install, the session's CPU/Metal raw KV cache holds
 * authoritative SWA-window state for the trailing `n_raw_rows` tokens;
 * the downstream `ds4_session_sync()` short-circuits its suffix
 * re-prefill.
 *
 * IMPORTANT: this MUST be called AFTER `ds4_session_load_blocks` has
 * populated the engine for the matched-prefix length, because the GPU
 * path uses ds4_session_tokens(s)->len to compute where in the SWA ring
 * to write (phys = pos % raw_cap).
 *
 * Returns 0 on success; -1 on error with err populated (bad envelope,
 * layout mismatch, capacity overflow). On error the session's SWA ring
 * may be in a partially-installed state — callers should
 * ds4_session_invalidate() and fall back to a cold prefill in that case.
 */
int ds4_session_install_raw_tail(ds4_session *s,
                                 const uint8_t *bytes, size_t len,
                                 char *err, size_t errlen);

/* Install N consecutive blocks into the session, starting at token 0.
 * After successful return, ds4_session_tokens(s)->len reflects the sum
 * of token ranges covered by the blocks (must be contiguous, ascending,
 * starting at 0; gaps not allowed). Subsequent ds4_session_sync()
 * correctly prefills any suffix tokens beyond the loaded range.
 *
 * Preconditions:
 *   - blocks[i].token_start = i == 0 ? 0 : blocks[i-1].token_end
 *   - blocks[i].token_end - blocks[i].token_start ∈ {4..128 allowed set}
 *   - All blocks share the same block_tokens granularity (no mixed mode)
 *
 * Returns 0 on success; -1 on error with err populated.
 *
 * Implementation status:
 *   - CPU + Graph backend: IMPLEMENTED. Installs per-layer compressed K/V
 *     (and indexer K/V for ratio-4 layers) into the live cache. Raw KV is
 *     not preserved by the block format — the load leaves n_raw at zero
 *     and relies on the next ds4_session_sync() to prefill suffix tokens
 *     (which also re-emits the SWA ring).
 *   - Token IDs are NOT carried in the block payload. After load_blocks
 *     returns, ds4_session_tokens(s)->v[] is a placeholder filled with
 *     zeros sized to the total token count. Callers (ds4_server /
 *     WombatKV bindings) own the real token IDs out-of-band and are
 *     responsible for either (a) overlaying the real token IDs onto the
 *     placeholder vector before ds4_session_sync() if they want
 *     common-prefix short-circuit behaviour, or (b) accepting that
 *     ds4_session_sync() will treat the loaded prefix as a non-match and
 *     refill from scratch.
 *   - Partial-prefix install is not yet supported; the first block must
 *     start at token 0 and blocks must be contiguous.
 */
int ds4_session_load_blocks(ds4_session *s,
                            const ds4_block_handle *blocks, size_t block_count,
                            char *err, size_t errlen);

/* Report the per-layer byte stride per token. Used by WombatKV to plan
 * block payload sizes and to validate block-payload byte lengths against
 * the engine's layout.
 *
 *   *out_n_layers          = number of layers (e.g., 43 for DSV4)
 *   *out_raw_bytes_per_tok = K+V bytes for one token in the raw KV ring,
 *                            across all layers (e.g., 43 * 2048 = 88 KB)
 *   *out_indexer_bytes_per_tok = same but for indexer KV (ratio-4 layers
 *                                only contribute; e.g., 22 * 512 = 11 KB)
 *
 * The compressed-KV stride depends on per-layer ratio so this fn cannot
 * give a single number for it; see ds4_session_layer_compression_ratio.
 *
 * Returns 0 on success; -1 on error.
 */
int ds4_session_block_layout(ds4_session *s,
                             int *out_n_layers,
                             size_t *out_raw_bytes_per_tok,
                             size_t *out_indexer_bytes_per_tok,
                             char *err, size_t errlen);

/* Compression ratio for layer `layer_idx` (0 = no compression / raw only,
 * 4 = ratio-4 attention with indexer KV, 128 = ratio-128 attention).
 * Used by WombatKV when alignment checks need per-layer information.
 *
 * Returns the ratio or -1 on error.
 */
int ds4_session_layer_compression_ratio(ds4_session *s, int layer_idx);

/* Validate a block_tokens value against the DS4 alignment rule
 * (positive, <= 8192, multiple of 128). Exposed so WombatKV and tests
 * can probe the rule without constructing a session.
 *
 * Returns 0 if valid; -1 otherwise.
 */
int ds4_kvblock_validate_block_tokens(int block_tokens);

#endif
