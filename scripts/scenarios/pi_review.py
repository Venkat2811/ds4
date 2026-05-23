#!/usr/bin/env python3
"""WombatKV showcase scenario 1 — `pi_review`: the killer cell.

Five concurrent code-review agents share:
  - the SAME long (~1500-token) system prompt (style guide + ReAct preamble)
  - 5 different code snippets (~500 tokens each — slices of /tmp/pg1184.txt)
  - 5 turns per agent (initial review + 4 fixed follow-ups)

Modes:
  c1_native:   5 ds4-servers, no WombatKV. Each agent is isolated. The shared
               system prompt is re-prefilled on every (agent × turn) — 25 total
               prefills.
  c2_embedded: 5 ds4-servers, each with WombatKV M0 embedded. All 5 share the
               SAME S3 bucket. Tier B SHOULD hit the shared system-prompt blocks
               across agents AND across turns within an agent.
  c3_daemon:   5 ds4-servers point at ONE wombatkv-daemon. The daemon's foyer
               holds the shared blocks in-process; no S3 round-trip for cross-
               agent reuse.

Why this is the killer cell:
  Native cost grows linearly with #agents × #turns (each must re-prefill the
  shared prefix). WombatKV cost is bounded by the unique-prefix fraction —
  the long system prompt is prefilled ONCE on the first agent's turn 1 and
  reused by all 24 subsequent (agent, turn) combinations.

Output:
  <outdir>/results.json       structured metrics
  <outdir>/HEADLINE.md        markdown table + speedup vs native baseline
  <outdir>/server_logs/       per-port ds4-server + (mode=c3) daemon log
"""

import argparse
import concurrent.futures
import json
import os
import pathlib
import sys
import time
from statistics import median

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import demo_showcase_lib as lib

# Cross-restart variant (RFC 0010 §5.3): between trials, kill ds4 + restart.
#
# WIPE_LOCAL_BETWEEN_TRIALS controls what survives the restart:
#   1 (default when RESTART=1): wipe local kvdisk + local foyer (keep S3 only).
#       This simulates a fresh pod / cross-machine / ephemeral-storage scenario.
#       ds4-native has NOTHING to restore from → cold prefill every request.
#       WombatKV restores from S3.
#   0: preserve local kvdisk + foyer. Simulates same-user closing+reopening
#       their pi process on the SAME machine (persistent local disk).
#       ds4-native reads .kv files from disk → fast warm restore.
#       WombatKV reads foyer → fast warm restore.
#       Both should be comparable here — this is the parity / honest case.
RESTART_BETWEEN_TRIALS = os.environ.get("RESTART_BETWEEN_TRIALS", "0") == "1"
WIPE_LOCAL_BETWEEN_TRIALS = os.environ.get("WIPE_LOCAL_BETWEEN_TRIALS", "1") == "1"


# -----------------------------------------------------------------------------
# Scenario constants
# -----------------------------------------------------------------------------

NUM_AGENTS = 5
# pi.dev model: one ds4-server, N concurrent HTTP clients (pi processes lease
# the single ds4 instance, subsequent pi sessions attach as HTTP clients).
# This is what the killer cell tests — shared system-prompt blocks across all
# 5 agents sharing the SAME ds4 RAM cache + WombatKV substrate.
PORTS = [lib.SHOWCASE_PORTS[0]] * NUM_AGENTS  # all agents share :8000
SCENARIO_TAG = "1a"  # used to build the short daemon prefix

# A realistic ~1500-token system prompt. Construction goal:
#   - identical bytes used by all 5 agents (this is what Tier B matches on)
#   - long enough to make a ds4 cold prefill actually feel the win
#   - shape is "you are reviewer X, follow style Y, output JSON Z, ReAct
#     preamble W". Not load-bearing as a prompt — the content is just a stable
#     prefix.
SYSTEM_PROMPT = """You are a senior staff software engineer performing a careful code review.

Style guide:
  1. Read the file end-to-end before responding. Note the file's stated purpose,
     surface contract, and key invariants.
  2. Form a hypothesis about the most important risk in the change. Risks rank
     in this order: data corruption, security flaws, undefined behavior,
     incorrect concurrency, performance regressions, API breakage,
     maintainability, then stylistic concerns.
  3. Evidence: cite line numbers and quote ≤ 5 words per excerpt.
  4. Severity: tag each comment one of {blocker, must-fix, nit, praise}.
  5. Suggest a concrete fix for any blocker or must-fix. Do not propose
     rewriting code unless a smaller change is impossible.
  6. Output a JSON object with the shape:
       {
         "summary": "≤ 60 words on what the code does and the one biggest risk",
         "ratings": {
           "correctness": 1..5,
           "clarity":     1..5,
           "tests":       1..5,
           "performance": 1..5
         },
         "comments": [
           {
             "line": int,
             "severity": "blocker"|"must-fix"|"nit"|"praise",
             "category": "correctness"|"security"|"performance"|"style"|"docs",
             "quote": "≤ 5 words verbatim from the code",
             "issue": "≤ 80 words explaining the problem",
             "suggestion": "≤ 80 words sketching the fix; null for praise"
           }
         ],
         "approve": bool
       }
  7. Pacing: budget 2 minutes for skimming, 5 minutes for the first pass,
     3 minutes for a second pass that re-reads each blocker with fresh eyes.
  8. Tone: collegial, direct, no hedging filler. Avoid the words "perhaps",
     "maybe", "consider possibly", "it might be the case that".

ReAct preamble (think → act → observe → repeat):
  Thought: state one observation about the change and one open question.
  Action: choose one of {read_file, search_callers, run_tests, ask_author}.
  Observation: cite the concrete result.
  Repeat until you have evidence for every blocker or must-fix comment.
  Final: emit the JSON object above.

When you disagree with the author's framing in the PR description, say so
plainly in the summary field; the team values dissent that is backed by code,
not posture. When you praise something, be specific about which line and
why — vague praise is noise.

Reviewer name: senior-staff-reviewer-v1
Style version: v3.1.4-with-react
Cutoff date: today's date
"""


# 5 different ~500-char code-like snippets — slices of /tmp/pg1184.txt at
# spaced offsets. ds4 sees them as user-message content, so they become the
# divergent-prefix region (per-agent unique). With Tier B block tokens at 128,
# 500 chars ≈ ~120 tokens ≈ ~1 block boundary; these are short on purpose so
# the shared system prompt dominates the prefix.
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


# 4 fixed follow-up questions (turns 2..5). Same questions for every agent so
# the cross-agent-share story is clean: the conversation TAIL becomes shared
# bytes too, once an agent has run through turn N once.
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
# Single-agent worker
# -----------------------------------------------------------------------------


def run_one_agent(agent_idx, port, snippet, num_turns=5):
    """Runs `num_turns` turns sequentially on `port`. Each turn sends the
    full prior conversation (system + prior turns + new user msg). Returns
    a list of per-turn metric dicts.
    """
    prior = []
    initial_user_msg = _initial_prompt(snippet)

    results = []
    for turn in range(1, num_turns + 1):
        if turn == 1:
            new_user = initial_user_msg
        else:
            new_user = FOLLOWUPS[turn - 2]
        msgs = lib.build_messages(SYSTEM_PROMPT, prior, new_user)

        metrics = lib.send_chat(port, msgs)
        metrics["agent"] = agent_idx
        metrics["port"] = port
        metrics["turn"] = turn
        results.append(metrics)

        # Append a placeholder assistant turn so subsequent turns see a stable
        # conversation history. (We do not need real model output for the
        # bench — only the input-token prefix matters for cache reuse.)
        prior.append((new_user, "(continuing)"))

    return results


# -----------------------------------------------------------------------------
# Per-mode orchestration
# -----------------------------------------------------------------------------


def run_mode(mode, outdir, trials):
    """Spin up N ds4-servers for `mode`, run `trials` concurrent sweeps,
    write `results.json` to outdir.
    """
    outdir = pathlib.Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    logs_dir = outdir / "server_logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    snippets = _load_code_snippets()
    bucket = f"wombatkv-showcase-{mode.replace('_', '-')}-pi-review".lower()
    lib.reset_minio_bucket(bucket)

    # Single ds4 instance + single puffer/kvdisk pair — all 5 agents share via
    # concurrent HTTP. This matches pi.dev's per-PID lease model: first pi
    # spawns ds4, subsequent pi sessions attach as HTTP clients to the same
    # instance. WombatKV's value here is cross-restart durability + the
    # shared-prefix cache across agents (system-prompt blocks save once,
    # serve every subsequent agent's turn-1).
    puffer_dir = f"/tmp/showcase-{mode}-pi-review-foyer"
    kvdisk_dir = f"/tmp/showcase-{mode}-pi-review-kvd"
    lib.wipe(puffer_dir, kvdisk_dir)

    daemon_proc = None
    daemon_prefix = None
    if mode == "c3_daemon":
        daemon_prefix = lib.short_daemon_prefix(SCENARIO_TAG, 0)
        daemon_puffer = f"/tmp/showcase-{mode}-pi-review-daemon-foyer"
        lib.wipe(daemon_puffer)
        daemon_log = logs_dir / "wombatkv-daemon.log"
        lib.log(f"  starting wombatkv-daemon with 1 prefix")
        daemon_proc = lib.start_wombatkv_daemon(
            prefixes=[daemon_prefix],
            bucket=bucket,
            puffer_dir=daemon_puffer,
            log_path=daemon_log,
        )

    # Build the env once — reused on every server (re)start.
    env = lib.env_for_mode(
        mode,
        puffer_dir=puffer_dir,
        bucket=bucket,
        daemon_prefix=daemon_prefix,
    )

    def _start_server(trial_suffix=""):
        log_path = logs_dir / f"ds4-server-port8000{trial_suffix}.log"
        lib.log(f"  starting ds4-server :8000  ({mode}{trial_suffix})")
        return lib.start_server(
            env, port=lib.SHOWCASE_PORTS[0], kvdisk=kvdisk_dir, log_path=log_path
        )

    # Start one ds4-server on :8000.
    current_server = _start_server()
    server_procs = [current_server]
    try:
        # Run `trials` independent passes; each pass fires 5 agents in parallel.
        # Trial 1 is the cold (cache-miss) pass for WombatKV modes; trial 2..N
        # are the steady-state warm passes the headline reads from.
        #
        # When RESTART_BETWEEN_TRIALS=1: between trials, kill ds4 + wipe local
        # kvdisk + local foyer (KEEP S3 bucket). This isolates ds4-native from
        # its in-RAM/disk cache, so the trial-2 measurement is "what can the
        # substrate (or lack thereof) restore from scratch?" — the killer cell
        # per RFC 0010 §5.2.
        all_trial_results = []
        for trial in range(1, trials + 1):
            lib.log(f"  trial {trial}/{trials}: firing {NUM_AGENTS} agents in parallel")
            t_trial_start = time.perf_counter()
            with concurrent.futures.ThreadPoolExecutor(max_workers=NUM_AGENTS) as ex:
                futs = [
                    ex.submit(run_one_agent, i + 1, PORTS[i], snippets[i])
                    for i in range(NUM_AGENTS)
                ]
                agent_results = [f.result() for f in futs]
            t_trial_wall = (time.perf_counter() - t_trial_start) * 1000.0

            for agent_idx, per_turn in enumerate(agent_results):
                for m in per_turn:
                    m["trial"] = trial
            all_trial_results.append(
                {
                    "trial": trial,
                    "wall_ms": t_trial_wall,
                    "agents": agent_results,
                }
            )
            lib.log(f"    trial {trial} wall={t_trial_wall:.0f}ms")

            # Cross-restart between trials (not after the last trial).
            if RESTART_BETWEEN_TRIALS and trial < trials:
                if WIPE_LOCAL_BETWEEN_TRIALS:
                    lib.log(
                        f"  [xrestart] killing ds4 + wiping local kvdisk + foyer (S3 retained)"
                    )
                else:
                    lib.log(
                        f"  [xrestart] killing ds4 + restarting; local kvdisk + foyer PRESERVED"
                    )
                lib.stop_server(current_server)
                server_procs.remove(current_server)
                if WIPE_LOCAL_BETWEEN_TRIALS:
                    lib.wipe(puffer_dir, kvdisk_dir)
                # NOTE: do NOT wipe the S3 bucket — that's the substrate.
                # In c3_daemon mode the daemon keeps running with its own
                # foyer (the substrate-side cache). That's intentional —
                # daemons stay up while clients come and go.
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

    out_doc = {
        "scenario": "pi_review",
        "mode": mode,
        "num_agents": NUM_AGENTS,
        "num_turns_per_agent": 5,
        "trials": trials,
        "restart_between_trials": RESTART_BETWEEN_TRIALS,
        "wipe_local_between_trials": WIPE_LOCAL_BETWEEN_TRIALS,
        "trial_results": all_trial_results,
        "config": {
            "ports": PORTS,
            "bucket": bucket,
            "daemon_prefix": daemon_prefix,
            "system_prompt_len_chars": len(SYSTEM_PROMPT),
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
        help="Trials per mode. Trial 1 = cold; subsequent = warm.",
    )
    args = ap.parse_args()

    lib.log(f"pi_review: mode={args.mode}  outdir={args.outdir}")
    lib.kill_stale_servers()
    run_mode(args.mode, args.outdir, args.trials)
    lib.log(f"pi_review: mode={args.mode} DONE")


if __name__ == "__main__":
    main()
