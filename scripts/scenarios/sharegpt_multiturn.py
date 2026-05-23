#!/usr/bin/env python3
"""WombatKV showcase scenario 2 — `sharegpt_multiturn`: real conversations.

Three sequential multi-turn conversations on a single ds4-server. Each
conversation has 5-7 turns on a different topic. Within a conversation, the
history grows turn-by-turn; the full prior history is re-sent each turn —
this is exactly what a chat client does.

Modes (same as pi_review):
  c1_native:   native ds4. Each turn-N prefill is (turn-N tokens) wide; the
               cost grows quadratically in turn index because earlier turns
               re-prefill.
  c2_embedded: WombatKV M0 embedded. Tier B caches the running prefix; each
               turn-N prefill only pays for the new bytes since turn-(N-1).
  c3_daemon:   WombatKV M1 daemon. Same as c2 mechanically; difference is the
               cache lives in a sidecar process.

This scenario is intra-session (one conversation, one ds4 instance); the
cross-agent / cross-process story is owned by scenario 1.

Output:
  <outdir>/results.json
"""

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import demo_showcase_lib as lib


# -----------------------------------------------------------------------------
# Three hand-written multi-turn conversations
# -----------------------------------------------------------------------------

SYSTEM_PROMPT = "You are a helpful assistant. Keep answers concise."

CONVERSATIONS = [
    {
        "name": "python_debugging",
        "turns": [
            "I'm hitting a UnicodeDecodeError when reading a CSV in pandas. "
            "The file came from a Windows export. What's the right way to "
            "diagnose which encoding it actually uses and decode it cleanly "
            "in a script that has to run on both macOS and Linux CI?",
            "I tried chardet and it returned a confidence of 0.73 for "
            "Windows-1252 but 0.27 for ISO-8859-1. The file has em-dashes "
            "and curly quotes. Is the chardet confidence reliable here or "
            "should I look at the bytes directly?",
            "Good — I'll switch to charset_normalizer. But I also see "
            "occasional rows that pandas thinks have 17 columns when the "
            "header has 16. Tipped me off there's a stray semicolon. Do I "
            "need a custom dialect or can I tell pandas to be lenient about "
            "this?",
            "We landed on on_bad_lines='warn' with quoting=csv.QUOTE_NONE. "
            "The job now runs to completion but I want a count of skipped "
            "rows in a structured log line for our observability stack. "
            "How would you wire that without writing a custom parser?",
            "Last question for this thread. The same job, on the same data, "
            "takes 4× longer on the GitHub Actions Linux runner than on my "
            "M3 Mac. Both have 16 GB RAM. What are the usual suspects when "
            "pandas IO is unexpectedly slow on CI specifically?",
            "Interesting — the runners are using ephemeral SSDs over the "
            "network. So I should pre-stage the file to /tmp first and read "
            "from there. Got it. Thanks for the systematic walk-through.",
        ],
    },
    {
        "name": "recipe_recommendation",
        "turns": [
            "I have a bunch of leftover roasted vegetables in the fridge — "
            "carrots, parsnips, brussels sprouts, and a half head of "
            "cauliflower. I'd like to use them in something tonight that "
            "isn't just 'reheated leftovers'. What would you make?",
            "I have eggs and feta. Will a frittata work even if the "
            "vegetables are already pretty caramelized? I'm worried they'll "
            "go mushy from the second cook. How would you handle that?",
            "Got it — low oven, short time, finish under broiler. What's "
            "the right egg-to-vegetable ratio for a 10-inch cast iron pan, "
            "and should I add any dairy beyond the feta? I have heavy cream "
            "and milk but not crème fraîche.",
            "I'd actually like to skip the cream and just use the milk — "
            "less rich. Will that hurt the texture much? Also: any herbs "
            "you'd add or skip given the caramelized-vegetable starting "
            "point? I have dried thyme and fresh parsley.",
            "Sounds great. One last detail — what salad on the side? "
            "Something quick and bright to balance the egg-richness. I "
            "don't want to do another oven dish. Bonus points if it uses "
            "things most people already have in the fridge.",
            "Perfect. Plan locked in: frittata + shaved fennel salad with "
            "lemon. Thanks for thinking through the texture tradeoff with "
            "me — I would have probably overcooked the vegetables.",
        ],
    },
    {
        "name": "travel_planning",
        "turns": [
            "Planning a 7-day trip to Portugal in late October with my "
            "partner. We've never been. We like food, walking, and "
            "afternoons at a café over a museum marathon. Lisbon-only or "
            "split between Lisbon and Porto? Budget is mid-range.",
            "Let's do Lisbon for 4 nights, Porto for 3. From Lisbon, are "
            "Sintra and Cascais worth a day-trip each, or just one of "
            "them? We're not big into theme-park-style castles but we do "
            "like coastal walks and azulejo tilework.",
            "Sintra it is, with the focus on Pena Park and Quinta da "
            "Regaleira gardens over the palace interiors. Train from "
            "Rossio, right? What time should we leave Lisbon to avoid "
            "the worst crowds, and is it sane to do the gardens in "
            "sneakers or do I need actual hiking shoes?",
            "Great. Switching to Porto — we're going to take the Alfa "
            "Pendular. We like natural wine. Where would you eat the "
            "two nights we're there, and is the port-cellar tour in Vila "
            "Nova de Gaia worth doing once even if we don't drink fortified "
            "wines much?",
            "Skipping the port tour then. Last detail: any neighborhood "
            "you'd specifically book the hotel in for Porto? We want "
            "walkable to dinner spots but not on the tourist drag. We don't "
            "drive so river-adjacent but quiet is the ideal.",
            "Cedofeita it is. Thanks for the back-and-forth — this is the "
            "most planning we've done in one sitting in months. We'll book "
            "tonight.",
        ],
    },
]


# -----------------------------------------------------------------------------
# Per-conversation runner
# -----------------------------------------------------------------------------


def run_conversation(port, conv):
    """Send each turn in sequence; full prior history is passed every time."""
    prior = []
    results = []
    for turn_idx, user_msg in enumerate(conv["turns"], start=1):
        msgs = lib.build_messages(SYSTEM_PROMPT, prior, user_msg)
        m = lib.send_chat(port, msgs)
        m["conversation"] = conv["name"]
        m["turn"] = turn_idx
        results.append(m)
        prior.append((user_msg, "(continuing)"))
    return results


# -----------------------------------------------------------------------------
# Per-mode orchestration
# -----------------------------------------------------------------------------


def run_mode(mode, outdir, trials):
    outdir = pathlib.Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    logs_dir = outdir / "server_logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    bucket = f"wombatkv-showcase-{mode.replace('_', '-')}-sharegpt".lower()
    lib.reset_minio_bucket(bucket)

    # Single-port scenario.
    port = lib.SHOWCASE_PORTS[0]  # 8000
    puffer = f"/tmp/showcase-{mode}-sharegpt-foyer"
    kvdisk = f"/tmp/showcase-{mode}-sharegpt-kvd"
    lib.wipe(puffer, kvdisk)

    daemon_proc = None
    daemon_prefix = None
    if mode == "c3_daemon":
        daemon_prefix = lib.short_daemon_prefix("2a", 0)
        daemon_puffer = f"/tmp/showcase-{mode}-sharegpt-daemon-foyer"
        lib.wipe(daemon_puffer)
        daemon_log = logs_dir / "wombatkv-daemon.log"
        lib.log(f"  starting wombatkv-daemon prefix={daemon_prefix}")
        daemon_proc = lib.start_wombatkv_daemon(
            prefixes=[daemon_prefix],
            bucket=bucket,
            puffer_dir=daemon_puffer,
            log_path=daemon_log,
        )

    env = lib.env_for_mode(
        mode, puffer_dir=puffer, bucket=bucket, daemon_prefix=daemon_prefix
    )
    log_path = logs_dir / f"ds4-server-port{port}.log"
    lib.log(f"  starting ds4-server :{port}  ({mode})")
    server_proc = lib.start_server(env, port=port, kvdisk=kvdisk, log_path=log_path)

    try:
        all_trial_results = []
        for trial in range(1, trials + 1):
            lib.log(
                f"  trial {trial}/{trials}: running {len(CONVERSATIONS)} "
                "conversations sequentially"
            )
            t_trial_start = time.perf_counter()
            conv_results = []
            for conv in CONVERSATIONS:
                lib.log(
                    f"    conversation: {conv['name']} ({len(conv['turns'])} turns)"
                )
                r = run_conversation(port, conv)
                for m in r:
                    m["trial"] = trial
                conv_results.append(
                    {
                        "conversation": conv["name"],
                        "turns": r,
                    }
                )
            t_trial_wall = (time.perf_counter() - t_trial_start) * 1000.0
            all_trial_results.append(
                {
                    "trial": trial,
                    "wall_ms": t_trial_wall,
                    "conversations": conv_results,
                }
            )
            lib.log(f"    trial {trial} wall={t_trial_wall:.0f}ms")
    finally:
        lib.stop_server(server_proc)
        if daemon_proc is not None:
            lib.stop_wombatkv_daemon(daemon_proc)

    out_doc = {
        "scenario": "sharegpt_multiturn",
        "mode": mode,
        "num_conversations": len(CONVERSATIONS),
        "trials": trials,
        "trial_results": all_trial_results,
        "config": {
            "port": port,
            "bucket": bucket,
            "daemon_prefix": daemon_prefix,
        },
    }
    (outdir / "results.json").write_text(json.dumps(out_doc, indent=2, default=str))
    lib.log(f"  wrote {outdir / 'results.json'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--mode", required=True, choices=["c1_native", "c2_embedded", "c3_daemon"]
    )
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--trials", type=int, default=2)
    args = ap.parse_args()

    lib.log(f"sharegpt_multiturn: mode={args.mode}  outdir={args.outdir}")
    lib.kill_stale_servers()
    run_mode(args.mode, args.outdir, args.trials)
    lib.log(f"sharegpt_multiturn: mode={args.mode} DONE")


if __name__ == "__main__":
    main()
