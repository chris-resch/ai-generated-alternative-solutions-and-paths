"""How many of the 11 published runs solved each issue: issues with a passing patch, issues solved by every model,
the best single model, and the comparison with the annotators' time estimates.

It reads only the 11 runs of the pilot: the folders under --verdicts.

Usage:
    python scripts/pilot/ladder.py --verdicts data/public/verdicts --verified data/public/swebench_verified.jsonl \
        --out results/pilot

Reads  <verdicts>/20260217_mini-v2.0.0_<model>/per_instance_details.json  (per task: resolved, api_calls)
       the SWE-bench Verified rows (task id, repository, and `difficulty`, the annotators' time estimate)
Writes <out>/ladder.csv       one row per task: models solving, median model calls of the solving runs, difficulty
       <out>/ladder_runs.csv  one row per run: tasks solved and the share of the 500 tasks
Prints these counts, the mean number of models solving per difficulty band, and the rank correlation
between the difficulty band (0 = under 15 minutes ... 3 = over 4 hours) and the number of models solving.
"""
import argparse, csv, glob, json, os, statistics as st, sys

sys.path.insert(0, os.path.dirname(__file__))
from path_measures import spearman   # the same rank correlation as in path_measures.py

BANDS = ['<15 min fix', '15 min - 1 hour', '1-4 hours', '>4 hours']   # SWE-bench Verified labels, easiest first


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--verdicts', required=True, help='folder with one 20260217_mini-v2.0.0_<model> folder per run')
    ap.add_argument('--verified', required=True, help='SWE-bench Verified rows, one JSON object per line')
    ap.add_argument('--out', required=True, help='output folder')
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    runs = {}
    for p in sorted(glob.glob(os.path.join(a.verdicts, '20260217_mini-v2.0.0_*', 'per_instance_details.json'))):
        model = os.path.basename(os.path.dirname(p)).split('20260217_mini-v2.0.0_')[1]
        runs[model] = json.load(open(p))
    tasks = [json.loads(line) for line in open(a.verified)]

    rows = []
    for t in tasks:
        iid = t['instance_id']
        solved = [m for m, d in runs.items() if (d.get(iid) or {}).get('resolved')]
        calls = [runs[m][iid]['api_calls'] for m in solved if 'api_calls' in runs[m][iid]]
        rows.append(dict(instance_id=iid, repo=t['repo'], difficulty=t['difficulty'], n_models=len(runs),
                         n_solved=len(solved), median_calls_solved=st.median(calls) if calls else ''))
    with open(os.path.join(a.out, 'ladder.csv'), 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    with open(os.path.join(a.out, 'ladder_runs.csv'), 'w', newline='') as f:
        w = csv.writer(f); w.writerow(['run', 'tasks_solved', 'share_of_tasks'])
        for m, d in runs.items():
            k = sum(1 for t in tasks if (d.get(t['instance_id']) or {}).get('resolved'))
            w.writerow([m, k, round(k / len(tasks), 4)])

    n, k = len(runs), [r['n_solved'] for r in rows]
    print(f'{n} runs, {len(rows)} tasks')
    print(f'solved by at least one run: {sum(x > 0 for x in k)} | by all {n}: {sum(x == n for x in k)} | '
          f'by 1 to 3: {sum(1 <= x <= 3 for x in k)} | by none: {sum(x == 0 for x in k)}')
    for b in BANDS:
        v = [r['n_solved'] for r in rows if r['difficulty'] == b]
        print(f'difficulty {b!r}: {len(v)} tasks, mean runs solving {st.mean(v):.1f}')
    band = [BANDS.index(r['difficulty']) for r in rows]
    print(f'rank correlation, difficulty band vs runs solving: {spearman(band, k):.3f}')


if __name__ == '__main__':
    main()
