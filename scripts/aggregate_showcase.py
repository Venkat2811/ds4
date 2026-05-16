#!/usr/bin/env python3
"""WombatKV showcase aggregator.

Walks $BENCH_ART_DIR (passed as argv[1]), parses every results.json under
mode/scenario subdirs, and emits:
  - headline.csv   one row per (mode, scenario, metric)
  - HEADLINE.md    side-by-side comparison table with speedup vs c1_native

Per-turn metrics summarized:
  ttft_ms          first-byte latency
  total_ms         end-to-end turn time
  cached_tokens    server-reported cached_tokens (Tier A / Tier B hit signal)
  wall_ms          full-trial wall time (per trial, not per turn)

For each (mode, scenario), we compute:
  median across all turns × trials (excluding trial 1 cold pass when trials>=2)
  p95 across the same population
  cold (trial 1) median
"""
import csv
import json
import pathlib
import sys
from statistics import median


def percentile(xs, q):
    if not xs:
        return float('nan')
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round((q / 100.0) * (len(s) - 1)))))
    return s[k]


def _safe(v):
    return v if isinstance(v, (int, float)) else float('nan')


def collect_turn_metrics(doc):
    """Yield per-turn dicts from a results.json doc, regardless of scenario."""
    scen = doc.get('scenario', '?')
    trial_results = doc.get('trial_results', [])
    for tr in trial_results:
        trial = tr.get('trial')
        wall_ms = tr.get('wall_ms')
        if scen == 'pi_review':
            for agent_idx, per_turn in enumerate(tr.get('agents', [])):
                for m in per_turn:
                    yield {
                        'trial': trial,
                        'wall_ms': wall_ms,
                        'ttft_ms': _safe(m.get('ttft_ms')),
                        'total_ms': _safe(m.get('total_ms')),
                        'cached_tokens': _safe(m.get('cached_tokens_seen', 0)),
                        'agent': m.get('agent'),
                        'turn': m.get('turn'),
                    }
        elif scen == 'sharegpt_multiturn':
            for conv in tr.get('conversations', []):
                for m in conv.get('turns', []):
                    yield {
                        'trial': trial,
                        'wall_ms': wall_ms,
                        'ttft_ms': _safe(m.get('ttft_ms')),
                        'total_ms': _safe(m.get('total_ms')),
                        'cached_tokens': _safe(m.get('cached_tokens_seen', 0)),
                        'conversation': conv.get('conversation'),
                        'turn': m.get('turn'),
                    }


def summarize(rows, trials):
    """Compute summary stats. If `trials` >= 2, we separate cold (trial 1) and
    warm (trials 2..N) so the headline can report both — the warm number is
    what users feel, the cold number shows the first-fill cost.
    """
    cold = [r for r in rows if r.get('trial') == 1]
    warm = [r for r in rows if r.get('trial', 0) >= 2] if trials >= 2 else rows

    def stats(pop, field):
        xs = [r[field] for r in pop if isinstance(r[field], (int, float))
              and r[field] == r[field]]
        if not xs:
            return float('nan'), float('nan')
        return median(xs), percentile(xs, 95)

    cold_ttft_med, cold_ttft_p95 = stats(cold, 'ttft_ms')
    warm_ttft_med, warm_ttft_p95 = stats(warm, 'ttft_ms')
    cold_total_med, _ = stats(cold, 'total_ms')
    warm_total_med, _ = stats(warm, 'total_ms')
    warm_cached_med, _ = stats(warm, 'cached_tokens')
    # Wall: average across trials (one per trial).
    cold_walls = sorted({r['wall_ms'] for r in cold
                         if isinstance(r['wall_ms'], (int, float))})
    warm_walls = sorted({r['wall_ms'] for r in warm
                         if isinstance(r['wall_ms'], (int, float))})
    cold_wall = cold_walls[0] if cold_walls else float('nan')
    warm_wall = median(warm_walls) if warm_walls else float('nan')

    return {
        'cold_ttft_med': cold_ttft_med, 'cold_ttft_p95': cold_ttft_p95,
        'warm_ttft_med': warm_ttft_med, 'warm_ttft_p95': warm_ttft_p95,
        'cold_total_med': cold_total_med, 'warm_total_med': warm_total_med,
        'warm_cached_tokens_med': warm_cached_med,
        'cold_wall_ms': cold_wall, 'warm_wall_ms': warm_wall,
        'n_cold_turns': len(cold), 'n_warm_turns': len(warm),
    }


def _fmt(v):
    if v != v:  # NaN
        return '-'
    if isinstance(v, float):
        if v >= 100:
            return f'{v:.0f}'
        return f'{v:.1f}'
    return str(v)


def _speedup(baseline, candidate):
    if baseline != baseline or candidate != candidate or candidate <= 0:
        return float('nan')
    return baseline / candidate


def main():
    if len(sys.argv) != 2:
        print('usage: aggregate_showcase.py <bench_art_dir>', file=sys.stderr)
        sys.exit(2)
    root = pathlib.Path(sys.argv[1])
    if not root.is_dir():
        print(f'not a dir: {root}', file=sys.stderr)
        sys.exit(2)

    summaries = {}  # (mode, scenario) -> stats dict
    for sub in sorted(root.iterdir()):
        if not sub.is_dir():
            continue
        rj = sub / 'results.json'
        if not rj.is_file():
            continue
        try:
            doc = json.loads(rj.read_text())
        except Exception as e:
            print(f'skip {rj}: {e}', file=sys.stderr)
            continue
        mode = doc.get('mode', sub.name)
        scenario = doc.get('scenario', '?')
        trials = int(doc.get('trials', 1))
        rows = list(collect_turn_metrics(doc))
        summaries[(mode, scenario)] = summarize(rows, trials)
        summaries[(mode, scenario)]['_trials'] = trials

    if not summaries:
        print(f'no results.json found under {root}', file=sys.stderr)
        sys.exit(1)

    # Write CSV: long format, one row per metric.
    csv_path = root / 'headline.csv'
    with csv_path.open('w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['mode', 'scenario', 'metric', 'value'])
        for (mode, scen), s in sorted(summaries.items()):
            for k, v in s.items():
                if k.startswith('_'):
                    continue
                w.writerow([mode, scen, k, v])

    # Write HEADLINE.md.
    scenarios = sorted({s for (_m, s) in summaries.keys()})
    modes_in_results = sorted({m for (m, _s) in summaries.keys()})
    # Stable order for the column layout.
    mode_order = [m for m in ('c1_native', 'c2_embedded', 'c3_daemon')
                  if m in modes_in_results]

    md = []
    md.append('# WombatKV showcase headline')
    md.append('')
    md.append(f'Source: `{root}`')
    md.append('')

    for scen in scenarios:
        md.append(f'## scenario: `{scen}`')
        md.append('')
        if not any((m, scen) in summaries for m in mode_order):
            md.append('_no results_')
            continue
        md.append('| metric | ' + ' | '.join(mode_order) + ' | speedup vs c1_native |')
        md.append('|---|' + '---|' * (len(mode_order) + 1))

        baseline = summaries.get(('c1_native', scen), {})
        for metric in ('warm_ttft_med', 'warm_ttft_p95',
                       'cold_ttft_med',
                       'warm_total_med', 'cold_total_med',
                       'warm_wall_ms', 'cold_wall_ms',
                       'warm_cached_tokens_med'):
            row = [metric]
            speedup_cells = []
            for mode in mode_order:
                s = summaries.get((mode, scen), {})
                row.append(_fmt(s.get(metric, float('nan'))))
            # Speedup vs c1_native (lower-is-better metrics; cached_tokens is
            # higher-is-better so we invert).
            best_speedup = '-'
            if 'c1_native' in mode_order and len(mode_order) >= 2:
                base_v = baseline.get(metric, float('nan'))
                speedups = []
                for mode in mode_order:
                    if mode == 'c1_native':
                        continue
                    cand = summaries.get((mode, scen), {}).get(
                        metric, float('nan'))
                    if metric == 'warm_cached_tokens_med':
                        # higher better; skip speedup
                        speedups.append(f'{mode}=n/a')
                    else:
                        sp = _speedup(base_v, cand)
                        speedups.append(f'{mode}={_fmt(sp)}x'
                                        if sp == sp else f'{mode}=-')
                best_speedup = ', '.join(speedups)
            row.append(best_speedup)
            md.append('| ' + ' | '.join(row) + ' |')
        md.append('')
        # Per-mode trial counts for transparency.
        md.append('Trials × turns observed:')
        md.append('')
        md.append('| mode | trials | warm turns | cold turns |')
        md.append('|---|---|---|---|')
        for mode in mode_order:
            s = summaries.get((mode, scen), {})
            md.append(f'| {mode} | {s.get("_trials", "-")} | '
                      f'{s.get("n_warm_turns", "-")} | '
                      f'{s.get("n_cold_turns", "-")} |')
        md.append('')

    md.append('---')
    md.append('')
    md.append('Notes:')
    md.append('  - `warm_*` = trials 2..N (steady-state, cache warm).')
    md.append('  - `cold_*` = trial 1 (first request after a fresh bucket '
              'and fresh ds4-server).')
    md.append('  - `warm_cached_tokens_med` = median cached_tokens reported '
              'by ds4 in the SSE usage object; non-zero confirms a '
              'Tier A / Tier B cache hit landed on the server side.')
    md.append('  - `*_wall_ms` is the full-trial wall clock '
              '(5 agents × 5 turns for pi_review; 3 conversations × ~6 turns '
              'for sharegpt_multiturn).')
    md.append('')

    (root / 'HEADLINE.md').write_text('\n'.join(md))

    print(f'wrote {csv_path}')
    print(f'wrote {root / "HEADLINE.md"}')


if __name__ == '__main__':
    main()
