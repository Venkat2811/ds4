#!/usr/bin/env python3
"""LLM-as-judge for WombatKV-mode output quality.

A stop-gap before tensor-level fidelity testing. Takes the per-mode
text outputs (from coherence_test.py's JSON dump or any compatible
shape), formats a structured judgment prompt for a Claude/OpenAI
model, and emits a per-mode verdict.

Two run modes:
  --api anthropic    call the Anthropic API (needs ANTHROPIC_API_KEY)
  --print            print the formatted judgment prompt to stdout
                     for manual paste into claude-code/codex (default)

Why: WombatKV-restored K/V → ds4 attention → decoded tokens. Pure
text-vs-text comparison drowns in Metal sampling noise (different
argmax choices on near-tied logits). An LLM judge can do "quality
preserved?" semantic-level scoring that survives small token-by-
token differences. Catches the obvious failure modes — gibberish,
wrong-language output, lobotomized model — that bench numbers don't
catch and char-level coherence metrics under-cover.

Tier B (logit_fidelity_test.py + ds4-server /v1/internal/logits
endpoint) is the real WombatKV-fidelity test. This script is the
stop-gap. Both ship side-by-side: tier-B for byte-fidelity
correctness, this for "does the model still produce useful output."
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path


JUDGE_SYSTEM_PROMPT = """You are evaluating whether enabling a KV-cache substrate
(WombatKV) has degraded the output quality of an LLM (DeepSeek-V4-Flash via ds4).

You will see one prompt that was sent to ds4-server, and multiple model responses:
the "native" mode (no WombatKV, our baseline) and one or more WombatKV modes
(embedded / daemon-SHM / daemon-TCP). For each mode there are N iterations of the
SAME prompt at temperature 0.

WombatKV's correctness claim: warm-restored K/V from S3/daemon is numerically
identical to cold-computed K/V, modulo:
  (a) Metal scheduling non-determinism in the prefill kernels
  (b) The suffix-forward recompute (last token of prompt is recomputed)

Text divergence between iterations is EXPECTED — Metal isn't bit-deterministic.
You are NOT scoring strict-equality. You are scoring whether each WombatKV-mode
iteration produces output of EQUIVALENT QUALITY to the native baseline:
  - English fluency
  - Reasoning ability (does it correctly understand and address the prompt?)
  - Factual accuracy where applicable
  - Absence of degenerate failure modes: gibberish, wrong-language output,
    single-token loops, hallucinations beyond what the prompt suggests

For each WombatKV mode, output a JSON object with:
  "quality_score": 1-5 (5 = indistinguishable from baseline)
  "verdict": "EQUIVALENT" | "DEGRADED" | "BROKEN"
  "evidence": short string citing specific observations
  "concerns": optional array of issues

Then an overall "overall_verdict": "PASS" | "FAIL" with reasoning.

Be honest. If WombatKV degraded the model, say so. If it didn't, say so."""


def build_judgment_prompt(data: dict) -> str:
    """Format the JSON results into a judgment-ready prompt."""
    results = data["results"]
    # Find the prompt that was sent. coherence_test stores it indirectly
    # via mode_smoke; we don't capture it in the JSON. For now, embed a
    # placeholder hint; future versions should save the prompt too.
    prompt_hint = data.get("prompt", "(prompt not captured in JSON; see coherence_test.py for the canonical pg1184 summarization prompt)")

    sections = [f"## Prompt sent to ds4-server\n\n{prompt_hint}\n"]
    sections.append("## Responses by mode")
    for r in results:
        sections.append(f"\n### mode: {r['mode']}")
        for it in r["iterations"]:
            sections.append(f"\n#### iter {it['iter']} ({len(it['text'])} chars, {it['elapsed_ms']} ms)")
            sections.append("```")
            sections.append(it["text"])
            sections.append("```")
    sections.append("""
## Your task

For EACH WombatKV mode (not native — that's the baseline), output a JSON object
as specified in the system prompt. Then a final overall_verdict line.

Return ONLY a JSON object of shape:
{
  "modes": {
    "embedded":   {"quality_score": N, "verdict": "...", "evidence": "...", "concerns": [...]},
    "daemon-shm": {"quality_score": N, "verdict": "...", "evidence": "...", "concerns": [...]},
    "daemon-tcp": {"quality_score": N, "verdict": "...", "evidence": "...", "concerns": [...]}
  },
  "overall_verdict": "PASS" | "FAIL",
  "overall_reasoning": "..."
}
""")
    return "\n".join(sections)


def call_anthropic(system_prompt: str, user_prompt: str) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    body = json.dumps({
        "model": "claude-opus-4-7",
        "max_tokens": 2000,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        resp_body = r.read()
    resp = json.loads(resp_body.decode())
    # Claude API: response.content is a list of blocks
    text_blocks = [b["text"] for b in resp.get("content", []) if b.get("type") == "text"]
    text = "\n".join(text_blocks).strip()
    # Try to parse JSON from the response — strip markdown fences if present
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json\n") or text.startswith("json "):
            text = text[5:]
        text = text.rsplit("```", 1)[0].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        return {"raw_text": text, "parse_error": str(e)}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "input",
        type=Path,
        help="coherence_test JSON dump (e.g. bench_data/coherence_alpha7.json)",
    )
    p.add_argument(
        "--mode",
        choices=["api", "print"],
        default="print",
        help="api: call Anthropic API (needs ANTHROPIC_API_KEY). print: emit prompt for manual paste.",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="optional: write verdict JSON to this path",
    )
    args = p.parse_args()

    if not args.input.exists():
        print(f"ERROR: input {args.input} not found", file=sys.stderr)
        return 2

    data = json.loads(args.input.read_text())
    user_prompt = build_judgment_prompt(data)

    if args.mode == "print":
        print("=" * 80)
        print("SYSTEM PROMPT (set this as the judge's system context):")
        print("=" * 80)
        print(JUDGE_SYSTEM_PROMPT)
        print()
        print("=" * 80)
        print("USER PROMPT (paste into claude-code, codex, or any LLM):")
        print("=" * 80)
        print(user_prompt)
        return 0

    # API mode
    try:
        verdict = call_anthropic(JUDGE_SYSTEM_PROMPT, user_prompt)
    except Exception as exc:
        print(f"ERROR calling Anthropic API: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    out_json = json.dumps(verdict, indent=2)
    print(out_json)
    if args.output:
        args.output.write_text(out_json)
    if isinstance(verdict, dict) and verdict.get("overall_verdict") == "PASS":
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
