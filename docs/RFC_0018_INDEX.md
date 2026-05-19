# RFC 0018 — ds4-side work tracker

**Canonical RFC**: `myelon-launch/ai-chat-exports/.0_agentic_engineering/5_tensor_puffer/0_rfcs/0018_wire_storage_versioning_transactional_discipline.md`

This file tracks the ds4-side implementation phases for RFC 0018 (wire & storage format versioning + transactional discipline for WombatKV). The RFC itself is canonical; this file is the per-repo execution tracker.

## TL;DR of the RFC

Adopt mini-tpuf/openpuffer's wire-format discipline + SlateDB's transactional primitives, adapted for our max-2-versions policy:

1. **16-byte fixed envelope** on every persisted byte sequence: `[magic 4][version u32 LE 4][crc32 u32 LE 4][len u32 LE 4][payload]`
2. **Strict-equal version check** at decode; max 2 versions in code (current + previous); drop N-2 on bump
3. **Pinned-byte tests** for every format × every shipped version. *"Do NOT update this constant"* in the error message
4. **`aligned_decode<T>` helper** at every rkyv decode boundary
5. **`publish_cas` helper** for rare mutable-pointer rotation; no internal retry; explicit caller-restart-on-conflict
6. **Storage keys put version in path segment** (`wombatkv/v<N>/...`), not filename suffix
7. **Boundary-file GC pattern** when LRU eviction ships
8. **Single-writer-per-prefix** concurrency model + epoch fencing for daemon hand-off
9. **Fizzbee specs** for any protocol with stale-writer-race surface

## ds4-side work — what we touch in this repo

ds4 owns the per-format byte layout for the sidecar (raw_tail) and blocks (KVB1). Tensorpuffer owns the cabi response envelope, S3 keys, SlateDB metadata records, and the `publish_cas` helper.

### Phase 1 — Sidecar (raw_tail) envelope (v3 → v4)

- [ ] Add CRC + len-prefix to the sidecar header (today: just magic + version + body + END sentinel; missing the 8 bytes of CRC + len)
- [ ] Bump sidecar version to v4
- [ ] Implement strict-version dispatcher: v4 (current), v3 (PREV — accepted for read on the alpha→1.0 transition but never written by 0.1.0-alpha.10+)
- [ ] Pinned-byte test `kvblock_raw_tail_v4_archive_layout_pinned` — hash a known-input sidecar payload, assert CRC32 + length match a frozen constant
- [ ] Cross-backend test: CPU `kvblock_save_raw_tail_cpu` and GPU `kvblock_save_raw_tail_gpu` produce byte-identical output for the same session
- [ ] Update `docs/MODE_VALIDATION.md` with the envelope change

Estimated: 1 day.

### Phase 2 — Block (KVB1) envelope (v1 → v2)

- [ ] Add CRC + len-prefix to the block header (currently: magic + version + block_seq + block_tokens + n_layers + reserved + body + zero-CRC-placeholder)
- [ ] Bump block version to v2 (replace the zero-CRC-placeholder with a real CRC; convert reserved u32 to the len field)
- [ ] Pinned-byte test `kvblock_v2_archive_layout_pinned` (one canonical block content)
- [ ] Dispatcher with v1 reader → returns `Err(LegacyFormatDropped)` (since alpha.9 v1 blocks are wiped; no real-data v1 in production)
- [ ] Cross-backend test

Estimated: half day.

### Phase 3 — Migration / breaking-window doc

- [ ] CHANGELOG entry per format bump, with explicit "v(N-1) sidecars/blocks in S3 will fail to load — wipe buckets when upgrading"
- [ ] `docs/CONSISTENCY.md` documenting the single-writer-per-prefix concurrency model + epoch-fencing future plan

Estimated: half day.

## Cross-repo dependencies

- Tensorpuffer's `wombatkv-cabi/src/ffi.rs` needs the envelope-aware decoder if/when we wrap cabi responses in the envelope (Phase 3 of the canonical RFC — tensorpuffer-side, not blocking ds4 phases above).
- SlateDB metadata index schema versioning is inherited from SlateDB itself (we don't redefine).

## Out of scope for this branch

- LRU eviction + boundary-file GC (Phase 5 of canonical RFC — defer until eviction graduates from off-by-default).
- DST harness for daemon concurrency (Phase 6 of canonical RFC — defer until daemon mode hardens).
- Fizzbee spec for any protocol (only required when boundary-file ships).

## References

Canonical RFC at `myelon-launch/ai-chat-exports/.0_agentic_engineering/5_tensor_puffer/0_rfcs/0018_wire_storage_versioning_transactional_discipline.md`. Triggering work was the v5 → v8 fidelity walk (commit `dba83ea` — bit-parity with ds4 huge-blob warm restore).
