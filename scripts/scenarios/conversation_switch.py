#!/usr/bin/env python3
"""WombatKV showcase scenario: `conversation_switch`: tests the regime that
ds4's README:602-605 makes explicit:

    "ds4-server first tries the cheap exact token-prefix check, then falls back
     to comparing rendered prompt bytes with decoded checkpoint bytes. The live
     in-memory checkpoint covers the current session; the disk KV cache makes
     useful prefixes survive session switches and server restarts."

ds4 holds ONE session in RAM at a time. Switching to a different session forces
either a disk read of that session's `.kv` file (if previously seen) or a cold
prefill (if not). WombatKV's foyer is a separate RAM-resident substrate cache
that can hold N sessions hot simultaneously, no swap thrashing.

This bench measures the per-switch latency:

  - N=5 distinct users, each with their own UNIQUE ~1500-token system prompt
    (so ds4 cannot prefix-match across users, every switch is a true mismatch
    against the in-RAM checkpoint).
  - Each user runs 5 turns.
  - Order is deterministic round-robin: u1.t1, u2.t1, ..., u5.t1, u1.t2, ...
    so the request *before* every (uK, tN) is from a DIFFERENT user.
  - Trial 1 = cold across the board (first time every user is seen).
  - Trial 2 = warm, c1_native must read from disk on every switch;
    c2_embedded should hit foyer in RAM; c3_daemon should hit daemon foyer.

Expected outcome (predicted, hence "the 5th honest win" in checklist §2.3):

  - Trial 2 c1 native:   per-switch ~50-200ms (disk read of other user's .kv)
  - Trial 2 c2 embedded: per-switch ~5-20ms  (foyer RAM hit)
  - Speedup: predicted 5-40× per switch.

Modes mirror pi_review.py: c1_native | c2_embedded | c3_daemon.

Output:
  <outdir>/results.json       per-request metrics + summary
  <outdir>/server_logs/       ds4-server (+ daemon for c3) logs
"""

import argparse
import json
import os
import pathlib
import sys
import time
from statistics import median

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import demo_showcase_lib as lib

# -----------------------------------------------------------------------------
# Restart / wipe knobs, mirror pi_review.py so the bench composes with the
# same RFC 0010 §5.3 cross-restart matrix.
# -----------------------------------------------------------------------------
RESTART_BETWEEN_TRIALS = os.environ.get("RESTART_BETWEEN_TRIALS", "0") == "1"
WIPE_LOCAL_BETWEEN_TRIALS = os.environ.get("WIPE_LOCAL_BETWEEN_TRIALS", "1") == "1"

# -----------------------------------------------------------------------------
# Scenario constants
# -----------------------------------------------------------------------------

NUM_USERS = int(os.environ.get("CONV_SWITCH_USERS", "5"))
NUM_TURNS = int(os.environ.get("CONV_SWITCH_TURNS", "5"))
SCENARIO_TAG = "cs"  # short daemon prefix

# pi.dev-style: 1 ds4-server, all users hit :8000 serially.
PORT = lib.SHOWCASE_PORTS[0]


def _user_system_prompt(user_idx):
    """Produce a UNIQUE ~1500-token system prompt for each user. The
    per-user phrasing differs enough that ds4 cannot prefix-match across
    users, every switch is a true in-RAM mismatch.
    """
    # Each user gets a different role + style + tag string interspersed
    # throughout the prompt to force per-user-unique tokens.
    role = [
        "senior security engineer",
        "principal performance engineer",
        "staff infrastructure engineer",
        "lead reliability engineer",
        "distinguished correctness engineer",
    ][user_idx % 5]
    focus = [
        "data corruption, security flaws, undefined behavior, incorrect concurrency, then everything else",
        "performance regressions, hot-path allocations, lock contention, then everything else",
        "deployment safety, configuration drift, capacity planning, then everything else",
        "failure-mode coverage, error propagation, retry storms, then everything else",
        "API contract conformance, invariants, edge-case coverage, then everything else",
    ][user_idx % 5]
    tag = f"reviewer-{user_idx + 1}-of-{NUM_USERS}-v3.1.{user_idx}"

    # Pad to ~1500 tokens with per-user-unique filler.
    extras = [
        f"Project codename for {tag} this cycle: project-{chr(ord('A') + user_idx)}-{user_idx * 7 + 3}.",
        f"Reviewer ledger anchor: ledger-anchor-{user_idx * 17 + 11}-{user_idx + 1}.",
        f"Style version: v{4 + user_idx}.{user_idx}.{user_idx * 3}-{tag}",
        f"Pacing budget: {2 + user_idx} minutes skim, {5 + user_idx} minutes first pass, {3 + user_idx} minutes second pass.",
        f"Output schema version reviewer-output-v{user_idx + 2}.",
    ]

    return (
        f"You are a {role}.\n\n"
        f"Style guide for {tag}:\n"
        f"  1. Read the file end-to-end before responding. Note the file's stated purpose,\n"
        f"     surface contract, and key invariants.\n"
        f"  2. Form a hypothesis about the most important risk in the change. Risks rank\n"
        f"     in this order: {focus}.\n"
        f"  3. Evidence: cite line numbers and quote <= 5 words per excerpt.\n"
        f"  4. Severity: tag each comment one of {{blocker, must-fix, nit, praise}}.\n"
        f"  5. Suggest a concrete fix for any blocker or must-fix. Do not propose\n"
        f"     rewriting code unless a smaller change is impossible.\n"
        f"  6. Output a JSON object with the shape:\n"
        f"       {{\n"
        f"         \"summary\": \"<= 60 words on what the code does and the one biggest risk\",\n"
        f"         \"ratings\": {{\n"
        f"           \"correctness\": 1..5,\n"
        f"           \"clarity\":     1..5,\n"
        f"           \"tests\":       1..5,\n"
        f"           \"performance\": 1..5\n"
        f"         }},\n"
        f"         \"comments\": [\n"
        f"           {{\n"
        f"             \"line\": int,\n"
        f"             \"severity\": \"blocker\"|\"must-fix\"|\"nit\"|\"praise\",\n"
        f"             \"category\": \"correctness\"|\"security\"|\"performance\"|\"style\"|\"docs\",\n"
        f"             \"quote\": \"<= 5 words verbatim from the code\",\n"
        f"             \"issue\": \"<= 80 words explaining the problem\",\n"
        f"             \"suggestion\": \"<= 80 words sketching the fix; null for praise\"\n"
        f"           }}\n"
        f"         ],\n"
        f"         \"approve\": bool\n"
        f"       }}\n"
        f"  7. " + extras[0] + "\n"
        f"  8. " + extras[1] + "\n"
        f"  9. " + extras[2] + "\n"
        f"  10. " + extras[3] + "\n"
        f"  11. " + extras[4] + "\n\n"
        f"ReAct preamble (think -> act -> observe -> repeat):\n"
        f"  Thought: state one observation about the change and one open question.\n"
        f"  Action: choose one of {{read_file, search_callers, run_tests, ask_author}}.\n"
        f"  Observation: cite the concrete result.\n"
        f"  Repeat until you have evidence for every blocker or must-fix comment.\n"
        f"  Final: emit the JSON object above.\n\n"
        f"When you disagree with the author's framing in the PR description, say so\n"
        f"plainly in the summary field; the team values dissent that is backed by code,\n"
        f"not posture. When you praise something, be specific about which line and\n"
        f"why - vague praise is noise.\n\n"
        f"Reviewer identity: {tag}\n"
        f"Reviewer ledger: {extras[1]}\n"
        f"Schema: {extras[4]}\n"
        f"Cutoff date: today's date.\n"
    )


# 5 different ~500-char code-like snippets, same as pi_review.py.
def _load_code_snippets():
    if not lib.PROMPT_FILE.exists():
        raise FileNotFoundError(f"showcase needs {lib.PROMPT_FILE}")
    base = lib.PROMPT_FILE.read_text()
    return [
        base[10000:10500],
        base[30000:30500],
        base[60000:60500],
        base[90000:90500],
        base[150000:150500],
    ]


# Fixed follow-ups identical across users so the differentiator is the
# per-user system prompt (which is what flips ds4's in-RAM mismatch).
FOLLOWUPS = [
    "Elaborate on point 2 in your review. What is the worst-case impact?",
    "Suggest a concrete fix for the highest-severity item. Include a diff.",
    "What is the test plan? List the new test cases you would add.",
    "Sign off: approve, request-changes, or comment? Justify in 30 words.",
]


def _initial_prompt(snippet):
    return (
        "Review this file. Apply the style guide exactly. Output ONLY the "
        "JSON object specified above, no prose around it.\n\n"
        "```\n" + snippet + "\n```\n"
    )


# -----------------------------------------------------------------------------
# Per-user state, keeps each user's prior conversation so turn N sees the
# accumulated history (the prior turns matter for ds4's prefix-match check).
# -----------------------------------------------------------------------------


def _new_user_state(user_idx, snippet):
    return {
        "user_idx": user_idx,
        "system_prompt": _user_system_prompt(user_idx),
        "snippet": snippet,
        "prior": [],
    }


def _request_for(state, turn):
    """Build the messages for `state`'s `turn` (1-indexed)."""
    new_user = _initial_prompt(state["snippet"]) if turn == 1 else FOLLOWUPS[turn - 2]
    msgs = lib.build_messages(state["system_prompt"], state["prior"], new_user)
    return new_user, msgs


def _record_turn(state, new_user):
    # Append a placeholder assistant turn so subsequent turns see growing history.
    state["prior"].append((new_user, "(continuing)"))


# -----------------------------------------------------------------------------
# Mode orchestration
# -----------------------------------------------------------------------------


def run_mode(mode, outdir, trials):
    outdir = pathlib.Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    logs_dir = outdir / "server_logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    snippets = _load_code_snippets()
    bucket = f"wombatkv-showcase-{mode.replace('_', '-')}-conv-switch".lower()
    lib.reset_minio_bucket(bucket)

    puffer_dir = f"/tmp/showcase-{mode}-conv-switch-foyer"
    kvdisk_dir = f"/tmp/showcase-{mode}-conv-switch-kvd"
    lib.wipe(puffer_dir, kvdisk_dir)

    daemon_proc = None
    daemon_prefix = None
    if mode == "c3_daemon":
        daemon_prefix = lib.short_daemon_prefix(SCENARIO_TAG, 0)
        daemon_puffer = f"/tmp/showcase-{mode}-conv-switch-daemon-foyer"
        lib.wipe(daemon_puffer)
        daemon_log = logs_dir / "wombatkv-daemon.log"
        lib.log("  starting wombatkv-daemon with 1 prefix")
        daemon_proc = lib.start_wombatkv_daemon(
            prefixes=[daemon_prefix],
            bucket=bucket,
            puffer_dir=daemon_puffer,
            log_path=daemon_log,
        )

    env = lib.env_for_mode(
        mode,
        puffer_dir=puffer_dir,
        bucket=bucket,
        daemon_prefix=daemon_prefix,
    )

    def _start_server(trial_suffix=""):
        log_path = logs_dir / f"ds4-server-port8000{trial_suffix}.log"
        lib.log(f"  starting ds4-server :{PORT}  ({mode}{trial_suffix})")
        return lib.start_server(env, port=PORT, kvdisk=kvdisk_dir, log_path=log_path)

    current_server = _start_server()
    server_procs = [current_server]
    try:
        all_trial_results = []
        for trial in range(1, trials + 1):
            lib.log(
                f"  trial {trial}/{trials}: round-robin {NUM_USERS} users x {NUM_TURNS} turns "
                f"({NUM_USERS * NUM_TURNS} requests serial)"
            )

            # Fresh per-user state every trial (state["prior"] is the
            # accumulating chat history; we reset so each trial measures the
            # same shape regardless of whether trial 1 ran first).
            states = [_new_user_state(i, snippets[i % len(snippets)]) for i in range(NUM_USERS)]
            per_request = []

            t_trial_start = time.perf_counter()
            # Round-robin: turn 1 for all users, then turn 2 for all users, etc.
            for turn in range(1, NUM_TURNS + 1):
                for user_idx in range(NUM_USERS):
                    state = states[user_idx]
                    new_user, msgs = _request_for(state, turn)
                    metrics = lib.send_chat(PORT, msgs)
                    metrics["user"] = user_idx + 1
                    metrics["turn"] = turn
                    metrics["trial"] = trial
                    # "switch_from" = the user that ran most recently before this one.
                    # turn 1 user 1 has no predecessor in this trial.
                    if turn == 1 and user_idx == 0:
                        metrics["switch_from"] = None
                    elif user_idx == 0:
                        # First user of a new turn cycle, predecessor was last user of prior turn.
                        metrics["switch_from"] = NUM_USERS
                    else:
                        metrics["switch_from"] = user_idx
                    per_request.append(metrics)
                    _record_turn(state, new_user)
            t_trial_wall = (time.perf_counter() - t_trial_start) * 1000.0

            all_trial_results.append(
                {
                    "trial": trial,
                    "wall_ms": t_trial_wall,
                    "per_request": per_request,
                }
            )
            lib.log(f"    trial {trial} wall={t_trial_wall:.0f}ms")

            # Optional cross-restart between trials (mirrors pi_review.py).
            if RESTART_BETWEEN_TRIALS and trial < trials:
                if WIPE_LOCAL_BETWEEN_TRIALS:
                    lib.log("  [xrestart] killing ds4 + wiping local kvdisk + foyer (S3 retained)")
                else:
                    lib.log("  [xrestart] killing ds4 + restarting; local kvdisk + foyer PRESERVED")
                lib.stop_server(current_server)
                server_procs.remove(current_server)
                if WIPE_LOCAL_BETWEEN_TRIALS:
                    lib.wipe(puffer_dir, kvdisk_dir)
                current_server = _start_server(trial_suffix=f"-after-trial{trial}")
                server_procs.append(current_server)

    finally:
        for p in server_procs:
            try:
                lib.stop_server(p)
            except Exception:
                pass
        if daemon_proc is not None:
            lib.stop_wombatkv_daemon(daemon_proc)

    # --- compute per-trial summary stats (for fast HEADLINE.md reading) ---
    summary = []
    for trial_doc in all_trial_results:
        trial = trial_doc["trial"]
        per_request = trial_doc["per_request"]
        ttfts = [r.get("ttft_ms") for r in per_request if r.get("ttft_ms") is not None]
        # Switches = every request whose `switch_from` is not None.
        switch_ttfts = [
            r.get("ttft_ms") for r in per_request
            if r.get("ttft_ms") is not None and r.get("switch_from") is not None
        ]
        # First-touch = first request from each user in each trial (turn 1).
        first_touch_ttfts = [
            r.get("ttft_ms") for r in per_request
            if r.get("ttft_ms") is not None and r.get("turn") == 1
        ]
        summary.append(
            {
                "trial": trial,
                "wall_ms": trial_doc["wall_ms"],
                "n_requests": len(per_request),
                "all_ttfts_median": median(ttfts) if ttfts else None,
                "switch_ttfts_median": median(switch_ttfts) if switch_ttfts else None,
                "switch_ttfts_p95": lib.percentile(switch_ttfts, 0.95) if switch_ttfts else None,
                "first_touch_ttfts_median": median(first_touch_ttfts) if first_touch_ttfts else None,
            }
        )

    out_doc = {
        "scenario": "conversation_switch",
        "mode": mode,
        "num_users": NUM_USERS,
        "num_turns_per_user": NUM_TURNS,
        "trials": trials,
        "restart_between_trials": RESTART_BETWEEN_TRIALS,
        "wipe_local_between_trials": WIPE_LOCAL_BETWEEN_TRIALS,
        "summary": summary,
        "trial_results": all_trial_results,
        "config": {
            "port": PORT,
            "bucket": bucket,
            "daemon_prefix": daemon_prefix,
        },
    }
    (outdir / "results.json").write_text(json.dumps(out_doc, indent=2, default=str))
    lib.log(f"  wrote {outdir / 'results.json'}")


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--mode", required=True, choices=["c1_native", "c2_embedded", "c3_daemon"]
    )
    ap.add_argument("--outdir", required=True)
    ap.add_argument(
        "--trials",
        type=int,
        default=2,
        help="Trials per mode. Trial 1 = cold; trial 2+ = warm (this is the regime that shows the win).",
    )
    args = ap.parse_args()

    lib.log(f"conversation_switch: mode={args.mode}  outdir={args.outdir}")
    lib.kill_stale_servers()
    run_mode(args.mode, args.outdir, args.trials)
    lib.log(f"conversation_switch: mode={args.mode} DONE")


if __name__ == "__main__":
    main()
