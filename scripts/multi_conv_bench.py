#!/usr/bin/env python3
"""5-conversation × 5-turn realistic workload bench.

Setup
-----
- All 5 conversations share the same long document (~10k tokens) — this
  models the "team multiplier" use case where N agents ask different
  questions about the same shared context (system prompts, RAG docs,
  knowledge bases). Within a conversation, prompts grow turn-over-turn
  as the dialogue accumulates.
- Conversation i uses question-set i (5 distinct angles on the doc).
- Server is restarted between turns so each turn is a fresh process —
  this isolates the WombatKV value-add (cross-restart S3 restore).
  ds4-native turn-2+ has to cold-prefill from scratch every time.

Compares
--------
- ds4-native (no WombatKV): every turn = cold prefill from scratch.
- ds4 + WombatKV: turn 1 of conv 1 cold; subsequent turns hit Tier B
  block cache (within-conv) AND cross-conv prefix-share (doc-shared).

Artifacts
---------
- bench_data/multi_conv_<timestamp>/
    summary.md             — headline numbers
    per_turn.csv           — every (mode, conv, turn) row with TTFT etc.
    conversations.json     — full dialogue history per (mode, conv)
    server_logs/           — per (mode, conv, turn) ds4-server stderr
"""

import csv
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import boto3
import urllib.request

DS4_DIR = Path("/Users/venkat/Documents/p/venkat-github/myelon-launch/ds4")
DS4_BIN = DS4_DIR / "ds4-server"
MODEL = "gguf/DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix.gguf"
PROMPT_FILE = Path("/tmp/pg1184.txt")
PORT = 8000
S3_ENDPOINT = "http://127.0.0.1:9200"
DOC_CHAR_BUDGET = 40000  # ~10k tokens — long enough that cold prefill is meaningful
MAX_NEW_TOKENS = 50  # cap response to keep per-turn time bounded
N_CONVS = 5
N_TURNS = 5

QUESTION_SETS = [
    [
        "What are the key themes of this passage?",
        "How does revenge motivate the central characters?",
        "Discuss the tension between justice and vengeance.",
        "What role does betrayal play in this narrative?",
        "Explore the theme of hope across this opening.",
    ],
    [
        "Describe the protagonist Edmond Dantes.",
        "What kind of person is the Abbe Faria?",
        "Analyze the character of Fernand Mondego.",
        "How is Mercedes portrayed in this passage?",
        "Discuss Danglars and his role in the plot.",
    ],
    [
        "Summarize the opening scene.",
        "Why is Dantes arrested?",
        "How does he encounter Abbe Faria?",
        "What happens at the Chateau d'If?",
        "How does the escape sequence unfold?",
    ],
    [
        "What does the Chateau d'If symbolize?",
        "Discuss the symbolism of the hidden treasure.",
        "What does the sea represent in this story?",
        "Analyze the recurring motif of disguise.",
        "What does the name 'Count of Monte Cristo' signify?",
    ],
    [
        "How does the novel address social class?",
        "What is the role of religion in the passage?",
        "Discuss the morality of revenge in this story.",
        "How is identity explored?",
        "What is the novel's view of fate versus free will?",
    ],
]


def wipe_minio() -> None:
    s3 = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id="minioadmin",
        aws_secret_access_key="minioadmin",
        region_name="us-east-1",
    )
    for b in ("wombatkv-demo-native", "wombatkv-demo-wombatkv"):
        try:
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=b):
                keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if keys:
                    s3.delete_objects(Bucket=b, Delete={"Objects": keys})
            s3.delete_bucket(Bucket=b)
        except Exception as e:
            if "NoSuchBucket" not in str(e):
                print(f"  wipe {b}: {type(e).__name__}: {e}")


def kill_servers() -> None:
    subprocess.run(["pkill", "-f", "ds4-server"], capture_output=True)
    for _ in range(40):
        r = subprocess.run(["pgrep", "-f", "ds4-server"], capture_output=True)
        if r.returncode != 0:
            time.sleep(0.5)
            return
        time.sleep(0.2)
    subprocess.run(["pkill", "-9", "-f", "ds4-server"], capture_output=True)
    time.sleep(2)


def start_server(mode: str, logfile: Path) -> None:
    kvdir = Path(f"/tmp/multiconv-ds4-{mode}")
    puffer = Path(f"/tmp/multiconv-puffer-{mode}")
    for d in (kvdir, puffer):
        if d.exists():
            subprocess.run(["rm", "-rf", str(d)])
        d.mkdir(parents=True)
    env = os.environ.copy()
    if mode == "wombatkv":
        env.update(
            {
                "DS4_WOMBATKV_ENABLE": "1",
                "WMBT_KV_S3_ENDPOINT": S3_ENDPOINT,
                "WMBT_KV_BUCKET": "wombatkv-demo-wombatkv",
                "WMBT_KV_PUFFER_DIR": str(puffer),
                "WMBT_KV_TIMING": "1",
            }
        )
    cmd = [
        str(DS4_BIN),
        "--model",
        MODEL,
        "--ctx",
        "32768",
        "--kv-disk-dir",
        str(kvdir),
        "--kv-cache-min-tokens",
        "256",
        "--kv-disk-space-mb",
        "16384",
        "--port",
        str(PORT),
    ]
    with open(logfile, "w") as f:
        subprocess.Popen(
            cmd,
            cwd=str(DS4_DIR),
            env=env,
            stdout=f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    for _ in range(120):
        try:
            if "listening on http" in logfile.read_text():
                time.sleep(0.5)
                return
        except FileNotFoundError:
            pass
        time.sleep(1)
    raise RuntimeError(f"server failed to start; see {logfile}")


def chat_complete(messages: list[dict]) -> tuple[float, float, str]:
    """Returns (ttft_ms, total_ms, response_text)."""
    payload = json.dumps(
        {
            "model": "deepseek-v4-flash",
            "messages": messages,
            "max_tokens": MAX_NEW_TOKENS,
            "temperature": 0.0,
            "stream": True,
        }
    ).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    started = time.perf_counter()
    ttft_ms = None
    content_parts = []
    with urllib.request.urlopen(req) as resp:
        for raw in resp:
            line = raw.decode(errors="replace").strip()
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            if ttft_ms is None:
                ttft_ms = (time.perf_counter() - started) * 1000.0
            try:
                obj = json.loads(body)
                delta = obj.get("choices", [{}])[0].get("delta", {})
                if "content" in delta and delta["content"]:
                    content_parts.append(delta["content"])
            except Exception:
                pass
    total_ms = (time.perf_counter() - started) * 1000.0
    return (ttft_ms or float("nan"), total_ms, "".join(content_parts))


def run_one_turn(mode: str, messages: list[dict], log_path: Path) -> dict:
    kill_servers()
    start_server(mode, log_path)
    ttft, total, response = chat_complete(messages)
    kill_servers()
    return {
        "ttft_ms": round(ttft, 1) if ttft else None,
        "total_ms": round(total, 1) if total else None,
        "response": response.strip(),
    }


def main() -> None:
    if not PROMPT_FILE.exists():
        sys.exit(f"FATAL: {PROMPT_FILE} missing")
    doc_text = PROMPT_FILE.read_bytes()[:DOC_CHAR_BUDGET].decode(errors="replace")

    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    artifact_dir = DS4_DIR / "bench_data" / f"multi_conv_{ts}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "server_logs").mkdir(exist_ok=True)
    print(f"\nArtifact dir: {artifact_dir}\n")

    all_rows: list[dict] = []
    all_conversations: dict = {}

    for mode in ("native", "wombatkv"):
        # Wipe MinIO at start of each mode so wombatkv-mode trial 1 is
        # cold (no prior state from a previous run).
        wipe_minio()
        for conv_id in range(N_CONVS):
            questions = QUESTION_SETS[conv_id]
            dialogue: list[dict] = [
                {"role": "system", "content": "You are a literary assistant."},
                {
                    "role": "user",
                    "content": f"Here is a passage:\n\n{doc_text}\n\n{questions[0]}",
                },
            ]
            print(f"=== {mode} conv {conv_id + 1}/{N_CONVS} ===")
            for turn in range(N_TURNS):
                if turn > 0:
                    # Append next user question with prior assistant context.
                    dialogue.append({"role": "user", "content": questions[turn]})
                log_path = (
                    artifact_dir
                    / "server_logs"
                    / f"{mode}_c{conv_id + 1}_t{turn + 1}.log"
                )
                result = run_one_turn(mode, dialogue, log_path)
                dialogue.append({"role": "assistant", "content": result["response"]})

                # Approximate prompt token count from char length (4 chars/token).
                total_chars = sum(len(m["content"]) for m in dialogue[:-1])
                prompt_tokens_approx = total_chars // 4
                row = {
                    "mode": mode,
                    "conv": conv_id + 1,
                    "turn": turn + 1,
                    "prompt_tokens_approx": prompt_tokens_approx,
                    "ttft_ms": result["ttft_ms"],
                    "total_ms": result["total_ms"],
                    "response_preview": result["response"][:120].replace("\n", " "),
                }
                all_rows.append(row)
                print(
                    f"  turn {turn + 1}/{N_TURNS}: ~{prompt_tokens_approx:>5} tokens, "
                    f"ttft={result['ttft_ms']:>7.1f} ms, total={result['total_ms']:>7.1f} ms"
                )
            all_conversations[f"{mode}_c{conv_id + 1}"] = dialogue

    # === artifacts ===
    csv_path = artifact_dir / "per_turn.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
        w.writeheader()
        for r in all_rows:
            w.writerow(r)
    (artifact_dir / "conversations.json").write_text(
        json.dumps(all_conversations, indent=2, ensure_ascii=False)
    )

    # === aggregate ===
    def med(rows, mode, turn=None):
        vals = [
            r["ttft_ms"]
            for r in rows
            if r["mode"] == mode
            and r["ttft_ms"]
            and (turn is None or r["turn"] == turn)
        ]
        return statistics.median(vals) if vals else None

    summary = [
        "# Multi-conversation bench summary\n",
        f"**Generated:** {ts}\n",
        f"**Setup:** {N_CONVS} conversations × {N_TURNS} turns, ~{DOC_CHAR_BUDGET // 4}-token doc shared across convs,\n"
        f"max_new_tokens={MAX_NEW_TOKENS}, server restart between every turn (cross-process scenario).\n",
        "## TTFT median by turn position\n",
        "| turn | native median (ms) | wombatkv median (ms) | speedup |",
        "|---|---:|---:|---:|",
    ]
    for t in range(1, N_TURNS + 1):
        n = med(all_rows, "native", t)
        w = med(all_rows, "wombatkv", t)
        if n and w and w > 0:
            summary.append(f"| {t} | {n:.0f} | {w:.0f} | {n / w:.1f}× |")
        else:
            summary.append(f"| {t} | {n} | {w} | — |")
    summary.append("\n## Overall TTFT median\n")
    overall_n = med(all_rows, "native")
    overall_w = med(all_rows, "wombatkv")
    summary.append(
        f"- ds4-native: {overall_n:.0f} ms" if overall_n else "- ds4-native: n/a"
    )
    summary.append(
        f"- ds4 + WombatKV: {overall_w:.0f} ms"
        if overall_w
        else "- ds4 + WombatKV: n/a"
    )
    if overall_n and overall_w and overall_w > 0:
        summary.append(f"- **Speedup: {overall_n / overall_w:.1f}×**")
    (artifact_dir / "summary.md").write_text("\n".join(summary) + "\n")
    print("\n" + "\n".join(summary))


if __name__ == "__main__":
    main()
