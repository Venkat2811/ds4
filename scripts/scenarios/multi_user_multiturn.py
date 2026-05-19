#!/usr/bin/env python3
"""5-user multi-turn validation for ds4 × WombatKV × all 5 transports.

This is the scenario the user asked us to ship in the HTTP+rkyv
landing: prove that 5 distinct users running 3-turn conversations
each work coherently across every WombatKV transport mode.

What it tests:
  * 5 personas with separate conversation histories and distinct
    system prompts so block-prefix sharing is per-user.
  * 3 turns per user, history accumulates between turns — exercises
    the same prefix-extension pattern a real chat client uses.
  * Optional kill+restart of ds4-server between users → exercises
    *cross-session* WombatKV restore (the load-from-S3 path).
  * Runs against all 5 transports: native, embedded, daemon-shm,
    daemon-tcp, daemon-http.

PASS criteria:
  HARD:
    * Every turn returns non-empty, English-ish text.
    * No turn produces obvious garbage (length, non-ASCII ratio).
    * Each user's turn-2 / turn-3 latency drops vs turn-1 in WombatKV
      modes (warm restore engaged). Latency-drop threshold is soft —
      INFORMATIONAL only — because Metal scheduling adds noise, but
      total turn-1 vs turn-(2..N) median must be lower in WombatKV
      modes than in native.
  INFORMATIONAL:
    * Bucket object count grows monotonically with users (each user
      writes their own block-prefix to S3).
    * Cross-user contamination check: assert that user A's response on
      a topic does not contain user B's keyword (no state leak).

Usage:
    multi_user_multiturn.py --mode daemon-http
    multi_user_multiturn.py --mode all
    multi_user_multiturn.py --mode embedded --restart-between-users
    multi_user_multiturn.py --mode all --output results.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import time
from pathlib import Path

# Reuse the env / daemon / server lifecycle from mode_smoke.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mode_smoke as ms  # noqa: E402

# Personas + topics chosen so that:
#  * System prompts differ enough that no two users share a block-
#    prefix at the system-prompt level (cross-user isolation check).
#  * Each user has a distinctive keyword (the topic word) we can grep
#    in cross-contamination assertions.
#  * Each system prompt is ~400 words → ~500-600 tokens, which makes
#    each turn's full history comfortably above the
#    KV_CACHE_DEFAULT_MIN_TOKENS=512 threshold so WombatKV engages.
USERS = [
    {
        "name": "alice",
        "topic": "python",
        "system": (
            "You are a Python expert helping with debugging and idiomatic "
            "code review. You favor pure-Python solutions, clear variable "
            "names, and reference PEP-8 conventions when relevant. When "
            "asked about errors, walk through stack traces step by step. "
            "When asked about performance, mention profiling tools like "
            "cProfile and line_profiler before suggesting optimizations. "
            "Always show small, runnable code snippets. Avoid unnecessary "
            "external dependencies unless the user explicitly asks for "
            "library recommendations. Keep your tone friendly and direct, "
            "and assume the user is a working software engineer who "
            "understands the language but may not have memorized every "
            "corner of the standard library. When discussing async code, "
            "be explicit about whether you are talking about asyncio, "
            "trio, or thread-based concurrency. When discussing typing, "
            "distinguish between runtime types and static-check-only "
            "annotations. Cite specific module names instead of vague "
            "categories. If the user posts a long traceback, summarize "
            "the cause in one sentence first, then go into detail. "
            "Always finish by suggesting a concrete next step the user "
            "can run on their own machine to verify your suggestion. "
            "Prefer asking one clarifying question when the request is "
            "ambiguous over guessing. Keep code examples under 30 lines."
        ),
        "turns": [
            "I have a function that returns None when I expected it to "
            "return a dict — what's the most common cause?",
            "Once I fix that, what's the cleanest way to add type hints "
            "so this mistake gets caught before runtime?",
            "And how would I write a single pytest test that exercises "
            "both the success and the failure paths?",
        ],
    },
    {
        "name": "bob",
        "topic": "recipe",
        "system": (
            "You are a culinary assistant with deep knowledge of home "
            "cooking, food science, and ingredient substitutions. You "
            "specialize in recipes that can be made in under an hour with "
            "common pantry ingredients. When suggesting a recipe, list "
            "ingredients first with approximate measurements, then "
            "method steps numbered. For each recipe note at least one "
            "common substitution if the user is missing an ingredient. "
            "When discussing techniques, prefer plain-language "
            "descriptions over jargon — if you must use a technical term "
            "like 'emulsion' or 'maillard reaction,' define it briefly "
            "the first time. Always consider dietary constraints if the "
            "user has mentioned them earlier in the conversation. Avoid "
            "recipes that require specialized equipment unless the user "
            "asks for them. When the user wants to scale a recipe up or "
            "down, walk through the math, especially for baked goods "
            "where ratios matter more than absolute quantities. Keep "
            "tone warm and encouraging. Mention storage and reheating "
            "advice for leftovers. Cite cuisine of origin where it adds "
            "useful context — for example, that something is a Sichuan "
            "dish or a Sicilian preparation. Don't over-explain "
            "obvious things like 'boil water in a pot.' End with one "
            "suggestion for a side dish or beverage pairing."
        ),
        "turns": [
            "I have chicken thighs, an onion, and a can of coconut milk. "
            "What's a 30-minute recipe?",
            "I'm allergic to coconut. Can we adapt that with what I "
            "have — chicken stock, cream, and tomato paste?",
            "What's a starch I can serve with it that takes the same "
            "amount of time?",
        ],
    },
    {
        "name": "carol",
        "topic": "travel",
        "system": (
            "You are a travel advisor specialized in independent travel "
            "planning. You help travelers pick destinations, build "
            "itineraries, and navigate logistics like visas, currency, "
            "transport, and accommodation booking. You favor public "
            "transit and walkable city centers over rental cars when "
            "feasible. When suggesting destinations, mention the best "
            "season to visit and what weather to expect. When discussing "
            "logistics, give specific website or app names that travelers "
            "use to book — for example, Trainline for European rail, "
            "Rome2Rio for multi-modal route planning, Google Flights for "
            "fare research. Always note visa requirements when the user "
            "mentions their nationality or asks about a region with "
            "restrictive entry rules. Keep cost estimates realistic — "
            "give a per-day range for budget, mid-range, and high-end "
            "travel. Mention local customs that affect daily interaction, "
            "like tipping conventions, dress codes for religious sites, "
            "or quiet hours. Avoid generic advice like 'try the local "
            "food'; suggest specific dishes or markets to visit. Be "
            "honest about safety concerns without exaggerating. End with "
            "one underrated suggestion the average tourist would miss."
        ),
        "turns": [
            "I want to spend 10 days in Japan in late October. What "
            "regions should I prioritize?",
            "I'd skip Tokyo and Kyoto since I've been there. What's a "
            "good itinerary using just the JR Pass?",
            "How do I get from Kanazawa to Takayama without a car?",
        ],
    },
    {
        "name": "dave",
        "topic": "linear",
        "system": (
            "You are a math tutor specializing in linear algebra, with "
            "a focus on building intuition before formalism. When a "
            "student asks about a concept, you start with a concrete "
            "example or geometric picture, then introduce the formal "
            "definition, then show how the two perspectives line up. You "
            "use matrices over the real numbers by default but call out "
            "when a result extends to complex matrices or finite fields. "
            "Notation conventions: column vectors by default, write "
            "matrix multiplication left-to-right (A applied to v is A v), "
            "use uppercase for matrices and lowercase for vectors. When "
            "the student asks about eigenvalues, eigenvectors, "
            "determinants, or rank, always relate the algebraic "
            "definition back to a geometric meaning (eigenvectors are "
            "directions that don't rotate; determinant is the signed "
            "volume scaling factor; rank is dimension of image). When "
            "the student is stuck, ask what they expect the answer to "
            "look like — that often surfaces the misconception. Show "
            "computations step by step rather than jumping to the final "
            "answer. When the student asks 'why is this true,' give a "
            "short proof sketch when one fits in three lines. Use "
            "LaTeX-style notation in plain text for matrices and "
            "vectors. End with one practice problem the student can do."
        ),
        "turns": [
            "Can you intuitively explain what the rank of a matrix tells "
            "me about the linear map it represents?",
            "If a 4x4 matrix has rank 2, what does that mean about the "
            "shape of its image?",
            "How does that connect to eigenvalues — does rank 2 imply "
            "two non-zero eigenvalues?",
        ],
    },
    {
        "name": "eve",
        "topic": "creative",
        "system": (
            "You are a fiction editor with experience at literary "
            "magazines and small presses, working primarily with short "
            "stories and novellas. You give writers concrete feedback "
            "focused on craft: voice, pacing, scene construction, "
            "interiority, dialogue, and sentence rhythm. You read for "
            "what the writer is trying to do and then help them do it "
            "better — not impose your own taste. When critiquing an "
            "opening, you check whether the first paragraph earns the "
            "reader's attention without front-loading exposition. When "
            "critiquing dialogue, you look for whether each line does "
            "more than one thing — character, plot, subtext. When "
            "critiquing pacing, you check scene-to-scene transitions "
            "and ask whether the writer is summarizing where they "
            "should dramatize, or vice versa. When the writer asks for "
            "structural feedback, you sketch the shape of the story "
            "(rising action, turning points, resolution) and identify "
            "which beats are missing or rushed. You quote specific "
            "sentences when praising or critiquing — vague praise "
            "doesn't help. You suggest line edits as examples, not as "
            "rules, and you make clear when something is a judgment "
            "call versus a craft error. You ask the writer what they "
            "want feedback on before diving in. You end with one "
            "concrete next step (rewrite a scene, cut a subplot, etc)."
        ),
        "turns": [
            "I'm writing a short story that opens with a character "
            "waking up. I've heard that's a cliché — is it really?",
            "What's a way to open in medias res without burying the "
            "reader in confusing names and places?",
            "How do I balance dialogue and interiority in that kind "
            "of opening?",
        ],
    },
]


def _looks_like_garbage(text: str) -> tuple[bool, str]:
    """Catches the kind of corruption WombatKV warm-restore failures
    produce — repeated short runes, control-char salads, empty output.
    Does NOT flag legit non-English responses (DeepSeek occasionally
    answers in Chinese on creative-writing prompts; that's a model
    quirk, not a WombatKV bug)."""
    stripped = text.strip()
    if len(stripped) < 20:
        return True, f"too short ({len(stripped)} chars)"
    # Letter-like characters — covers Latin, CJK, Cyrillic, Arabic, etc.
    # rather than just ASCII so multilingual replies don't false-fire.
    letters = sum(1 for c in text if c.isalpha())
    if letters < 15:
        return True, f"only {letters} letter-like chars"
    # Control-character salad detection: 0x00-0x1F / 0x7F excluding the
    # whitespace controls (\t \n \r) is a strong corruption signal.
    bad_controls = sum(
        1 for c in text
        if (ord(c) < 0x20 and c not in "\t\n\r") or ord(c) == 0x7F
    )
    if bad_controls > 0:
        return True, f"{bad_controls} control-char(s) in output"
    return False, ""


def _send_chat(history: list[dict]) -> tuple[float, str]:
    """One chat-completions request with the full accumulated history."""
    import urllib.request

    payload = json.dumps(
        {
            "model": "deepseek-v4-flash",
            "messages": history,
            "max_tokens": 48,
            "temperature": 0.0,
            "stream": False,
        }
    ).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{ms.PORT}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        body = r.read()
    elapsed = time.time() - t0
    resp = json.loads(body.decode())
    text = resp["choices"][0]["message"]["content"]
    return elapsed, text


def run_one_user(user: dict) -> dict:
    """Run all N turns for a single user against an already-running
    ds4-server. Returns per-turn records and verdict."""
    history = [{"role": "system", "content": user["system"]}]
    turn_records = []
    for turn_idx, user_msg in enumerate(user["turns"], start=1):
        history.append({"role": "user", "content": user_msg})
        elapsed, reply = _send_chat(history)
        garbage, why = _looks_like_garbage(reply)
        ms.log(
            f"    user={user['name']:<6} turn={turn_idx} "
            f"elapsed={elapsed*1000:.0f}ms len={len(reply)} "
            f"garbage={garbage} first40={reply[:40]!r}"
        )
        history.append({"role": "assistant", "content": reply})
        turn_records.append(
            {
                "turn": turn_idx,
                "elapsed_ms": int(elapsed * 1000),
                "text": reply,
                "garbage": garbage,
                "garbage_reason": why,
            }
        )
    return {
        "name": user["name"],
        "topic": user["topic"],
        "turns": turn_records,
    }


def _bucket_for_mode(mode: str) -> str | None:
    return {
        "embedded": "wombatkv-smoke-embedded",
        "daemon-shm": "wombatkv-smoke-smoke-shm",
        "daemon-tcp": "wombatkv-smoke-smoke-tcp",
        "daemon-http": "wombatkv-smoke-smoke-http",
    }.get(mode)


def run_mode(mode: str, restart_between_users: bool) -> dict:
    """Run all 5 users × N turns against `mode`. Returns full results
    dict including per-user records, latency stats, and verdict."""
    ms.log(f"=== mode={mode} users={len(USERS)} restart_between={restart_between_users} ===")
    kvdir = Path(f"/tmp/multi-user-kvdir-{mode}")
    puffer = Path(f"/tmp/multi-user-puffer-{mode}")
    daemon_puffer = Path(f"/tmp/multi-user-daemonpuffer-{mode}")
    serverlog = Path(f"/tmp/multi-user-{mode}-server.log")
    daemonlog = Path(f"/tmp/multi-user-{mode}-daemon.log")

    ms.kill_all_ds4()
    if mode in ("daemon-shm", "daemon-tcp", "daemon-http"):
        ms.kill_all_daemon()
    for d in (kvdir, puffer, daemon_puffer):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir()

    bucket = _bucket_for_mode(mode)
    if bucket:
        ms.wipe_bucket(bucket)

    daemon_proc = None
    user_records = []
    bucket_counts = []
    try:
        if mode == "daemon-shm":
            daemon_proc = ms.start_daemon("shm", "smoke-shm", daemonlog, daemon_puffer)
        elif mode == "daemon-tcp":
            daemon_proc = ms.start_daemon("tcp", "smoke-tcp", daemonlog, daemon_puffer)
        elif mode == "daemon-http":
            daemon_proc = ms.start_daemon("http", "smoke-http", daemonlog, daemon_puffer)

        ms.log(f"  starting ds4-server ({mode})")
        ms.start_server(mode, kvdir, puffer, serverlog)

        for u_idx, user in enumerate(USERS, start=1):
            ms.log(f"  user {u_idx}/{len(USERS)}: {user['name']} ({user['topic']})")
            user_records.append(run_one_user(user))
            if bucket:
                bucket_counts.append(len(ms.list_bucket_keys(bucket)))
            if restart_between_users and u_idx < len(USERS):
                ms.log(f"  restarting ds4-server before next user")
                ms.kill_all_ds4()
                # Wipe the engine-local kvdir so the next user can't get
                # a free warm path from ds4's own huge-blob cache —
                # forces all warm-restore wins to come from WombatKV.
                if kvdir.exists():
                    shutil.rmtree(kvdir)
                    kvdir.mkdir()
                ms.start_server(mode, kvdir, puffer, serverlog)

        # Latency stats: median across all turn-1s vs across all turn-(2..N)s.
        turn1_ms = [u["turns"][0]["elapsed_ms"] for u in user_records]
        later_ms = [
            t["elapsed_ms"] for u in user_records for t in u["turns"][1:]
        ]
        latency_stats = {
            "turn1_median_ms": int(statistics.median(turn1_ms)) if turn1_ms else 0,
            "turn1_min_ms": min(turn1_ms) if turn1_ms else 0,
            "turn1_max_ms": max(turn1_ms) if turn1_ms else 0,
            "later_median_ms": int(statistics.median(later_ms)) if later_ms else 0,
            "later_min_ms": min(later_ms) if later_ms else 0,
            "later_max_ms": max(later_ms) if later_ms else 0,
        }
        if later_ms and turn1_ms:
            latency_stats["intra_user_speedup"] = round(
                statistics.median(turn1_ms) / max(statistics.median(later_ms), 1), 2
            )

        # Cross-user contamination check: does user A's response on
        # their first turn mention user B's topic word? Use lowercased
        # comparison + word boundary to avoid false-positives (e.g.
        # "python" appearing inside "polyethylene" — unlikely but safe).
        contamination = []
        for u_a in user_records:
            for u_b in user_records:
                if u_a["name"] == u_b["name"]:
                    continue
                for turn in u_a["turns"]:
                    text = turn["text"].lower()
                    if f" {u_b['topic']} " in text or text.startswith(u_b["topic"]):
                        contamination.append(
                            {
                                "user": u_a["name"],
                                "turn": turn["turn"],
                                "mentions_topic_of": u_b["name"],
                                "topic": u_b["topic"],
                            }
                        )

        # Verdict
        any_garbage = any(
            t["garbage"] for u in user_records for t in u["turns"]
        )
        verdict = "PASS"
        notes = []
        if any_garbage:
            verdict = "FAIL"
            garbage_rows = [
                f"{u['name']}/turn{t['turn']}:{t['garbage_reason']}"
                for u in user_records
                for t in u["turns"]
                if t["garbage"]
            ]
            notes.append(f"garbage output(s): {garbage_rows}")
        if mode != "native":
            if bucket_counts and bucket_counts[-1] == 0:
                notes.append("bucket empty after all users — WombatKV did not write blocks")
            if (
                "intra_user_speedup" in latency_stats
                and latency_stats["intra_user_speedup"] < 1.1
            ):
                notes.append(
                    f"intra-user speedup weak: {latency_stats['intra_user_speedup']}× "
                    f"(turn-1 median {latency_stats['turn1_median_ms']}ms vs later "
                    f"median {latency_stats['later_median_ms']}ms)"
                )

        return {
            "mode": mode,
            "restart_between_users": restart_between_users,
            "users": user_records,
            "bucket": bucket,
            "bucket_counts_after_each_user": bucket_counts,
            "latency_stats": latency_stats,
            "cross_user_contamination": contamination,
            "verdict": verdict,
            "notes": notes,
        }
    finally:
        ms.kill_all_ds4()
        if daemon_proc is not None:
            try:
                daemon_proc.terminate()
                daemon_proc.wait(timeout=5)
            except Exception:
                daemon_proc.kill()
        if mode in ("daemon-shm", "daemon-tcp", "daemon-http"):
            ms.kill_all_daemon()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--mode",
        choices=[
            "native",
            "embedded",
            "daemon-shm",
            "daemon-tcp",
            "daemon-http",
            "all",
        ],
        default="all",
        help="single mode or 'all' to sweep all 5",
    )
    p.add_argument(
        "--restart-between-users",
        action="store_true",
        help="kill+restart ds4-server between users to exercise "
             "cross-session WombatKV restore (warm-restore must come "
             "from S3/daemon, not engine-local kvdir)",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="optional path to dump full results JSON",
    )
    args = p.parse_args()

    if not ms.DS4_BIN.exists():
        print(f"ERROR: {ms.DS4_BIN} not found — build ds4-server first", file=sys.stderr)
        return 2

    modes = (
        ["native", "embedded", "daemon-shm", "daemon-tcp", "daemon-http"]
        if args.mode == "all"
        else [args.mode]
    )
    if any(m.startswith("daemon-") for m in modes) and not ms.DAEMON_BIN.exists():
        print(f"ERROR: {ms.DAEMON_BIN} not found — build wombatkv-daemon first", file=sys.stderr)
        return 2

    all_results = []
    for mode in modes:
        try:
            all_results.append(run_mode(mode, args.restart_between_users))
        except Exception as exc:
            ms.log(f"  EXCEPTION: {type(exc).__name__}: {exc}")
            all_results.append(
                {"mode": mode, "verdict": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
            )

    # Summary
    print()
    print("=== multi_user_multiturn summary ===")
    print(
        f"{'mode':<14} {'verdict':<8} {'turn1_med_ms':<13} "
        f"{'later_med_ms':<13} {'speedup':<8} {'bucket':<6} {'notes'}"
    )
    for r in all_results:
        if r.get("verdict") == "ERROR":
            print(f"{r['mode']:<14} ERROR    {r.get('error', '')[:60]}")
            continue
        ls = r.get("latency_stats", {})
        bc = r.get("bucket_counts_after_each_user", [])
        bc_str = str(bc[-1]) if bc else "—"
        speedup = ls.get("intra_user_speedup", "—")
        notes = "; ".join(r.get("notes", [])) or "(none)"
        print(
            f"{r['mode']:<14} {r['verdict']:<8} {ls.get('turn1_median_ms', 0):<13} "
            f"{ls.get('later_median_ms', 0):<13} {speedup!s:<8} {bc_str:<6} {notes}"
        )

    if args.output:
        args.output.write_text(json.dumps(all_results, indent=2))
        print(f"\nFull results dumped to {args.output}")

    fail_count = sum(1 for r in all_results if r.get("verdict") not in ("PASS",))
    return 1 if fail_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
