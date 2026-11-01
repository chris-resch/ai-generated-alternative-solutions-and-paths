"""Our repeated runs: how many passed, and how many distinct solutions they contain.

Reads the scorer's report of every run (data/runs/<job>/scores/*.json; one report per run, covering its two issues)
and results/runs/choice_in_set/runs.csv, which choice_in_set.py writes for the 8 issues of our runs from both the
11 published runs and ours (`make histories`). Our runs are those whose name starts with "qwen-".

Prints, and writes to --out:
  runs and passing runs (the scorer's verdict);
  distinct solutions among our passing runs, and the issues with two or more;
  our solutions that no passing patch of the 11 published models has;
  per issue: distinct solutions among our passing runs and among the published models' passing patches.

Usage:
    python scripts/runs/our_runs.py [--scores 'data/runs/*/scores/*.json'] \
        [--choice results/runs/choice_in_set/runs.csv] [--out results/runs/summary.md]
"""
import argparse
import csv
import glob
import json
import os
from collections import defaultdict


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scores", default="data/runs/*/scores/*.json")
    ap.add_argument("--choice", default="results/runs/choice_in_set/runs.csv")
    ap.add_argument("--out", default="results/runs/summary.md")
    args = ap.parse_args()

    runs = passed = 0
    for path in sorted(glob.glob(args.scores)):
        report = json.load(open(path))
        runs += len(report["submitted_ids"])
        passed += len(report["resolved_ids"])

    ours, published = defaultdict(set), defaultdict(set)
    with open(args.choice, newline="") as f:
        for row in csv.DictReader(f):                    # one row per passing patch
            (ours if row["run"].startswith("qwen-") else published)[row["instance_id"]].add(row["shape"])

    issues = sorted(set(ours) | set(published))
    lines = [f"Runs: {runs}; passing: {passed}",
             f"Distinct solutions among our passing runs: {sum(len(s) for s in ours.values())}; "
             f"issues with two or more: {sum(len(s) >= 2 for s in ours.values())} of {len(issues)}",
             f"Our solutions that no published model's passing patch has: "
             f"{sum(len(ours[i] - published[i]) for i in issues)}",
             "", "Distinct solutions per issue (ours / the 11 published models):"]
    lines += [f"  {i}: {len(ours[i])} / {len(published[i])}" for i in issues]
    text = "\n".join(lines)
    print(text)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        f.write(f"# Our repeated runs\n\n```\n{text}\n```\n")


if __name__ == "__main__":
    main()
