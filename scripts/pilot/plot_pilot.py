"""Figures 2 and 3 of the technical appendix.

Reads only result files (no git, no model):
  results/pilot/choice_in_set/runs.csv                         solution of every passing patch
  results/pilot/allocation/tasks.csv, estimates.csv, requirement.csv
  results/pilot/paths/runs.csv, data/derived/*/path.jsonl      steps of every run
Writes <out>/<task>_requirement.{png,svg,pdf} (Figure 2: one issue's solving runs, grouped by solution, the files
solving required, and the author's past commits to them) and <out>/author_coefficients.{png,svg,pdf} (Figure 3:
Table A1, column (4), of allocation.py).

Usage:
    python scripts/pilot/plot_pilot.py [--task django__django-14351] [--out results/figures]
"""
import argparse
import csv
import glob
import json
import math
import os
import sys
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

# display names of the eleven published runs
NAMES = {'claude-4-5-haiku-high': 'Claude Haiku 4.5', 'claude-4-5-sonnet-high': 'Claude Sonnet 4.5',
         'claude-4-5-opus-high': 'Claude Opus 4.5', 'claude-4-6-opus': 'Claude Opus 4.6', 'gpt-5-mini': 'GPT-5 mini',
         'gpt-5-2-high': 'GPT-5.2', 'gemini-3-flash-high': 'Gemini 3 Flash', 'deepseek-3-2-high': 'DeepSeek V3.2',
         'glm-5-high': 'GLM-5', 'kimi-k2-5-high': 'Kimi K2.5', 'minimax-2-5-high': 'MiniMax M2.5'}

# colours
SURFACE, INK, INK2, MUTED, GRID, BASE = "#ffffff", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
BLUE, ORANGE = "#2a78d6", "#eb6834"
RAMP = ["#86b6ef", "#2a78d6", "#104281"]   # blue steps 250, 450, 650 (ordinal)
GRAYS = ["#c3c2b7", "#898781", "#52514e"]
STEP_LIGHT = "#eceae4"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 8, "axes.edgecolor": BASE, "axes.labelcolor": INK,
                     "xtick.color": INK2, "ytick.color": INK2, "axes.linewidth": 0.6, "svg.fonttype": "none"})


def read(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def estimate(rows, **match):
    hits = [r for r in rows if all(r[k] == v for k, v in match.items())]
    if len(hits) != 1:
        sys.exit(f"expected one estimate for {match}, found {len(hits)}: rerun the script that writes it")
    return float(hits[0]["estimate"]), float(hits[0]["low"]), float(hits[0]["high"])


def tidy(ax):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.tick_params(length=2.5, width=0.6)


def head(fig, x, top, title, note=""):
    """Panel title (bold, may span lines) with a note under it, in figure coordinates."""
    fig.text(x, top, title, fontsize=8.5, fontweight="bold", color=INK, va="top")
    if note:
        fig.text(x, top - 0.026 * (title.count("\n") + 1) - 0.006, note, fontsize=6.8, color=INK2, va="top",
                 linespacing=1.3)


# ---------------------------------------------------------------- Figure 3: who authored the merged solution
def author_figure(res, out):
    """Coefficient plot of Table A1, column (4): who authored the merged solution (conditional logit)."""
    est = read(f"{res}/allocation/estimates.csv")
    model = "authored (4)"
    rows = [("Past commits to files solving required", "required", BLUE),
            ("Past commits to files the merged solution changed", "merged", INK),
            ("Past commits to files the issue names", "issue", INK),
            ("Commits in the year before (activity)", "activity", INK)]
    fig = plt.figure(figsize=(7.0, 2.6), facecolor=SURFACE)
    ax = fig.add_axes([0.42, 0.24, 0.54, 0.62])
    for i, (label, term, col) in enumerate(rows):
        e, lo, hi = estimate(est, model=model, term=term)
        ax.plot([lo, hi], [i, i], color=col, linewidth=1.6, solid_capstyle="butt", zorder=3)
        ax.plot([e], [i], "o", color=col, markersize=5.5, markeredgecolor=SURFACE, markeredgewidth=0.8, zorder=4)
    ax.axvline(0, color=INK2, linewidth=0.8, linestyle=(0, (3, 2)), zorder=1)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([r[0] for r in rows], fontsize=7.2, color=INK)
    ax.set_ylim(len(rows) - 0.5, -0.5)
    ax.set_xlim(-0.6, 1.4)
    ax.xaxis.grid(True, color=GRID, linewidth=0.5, zorder=0)
    ax.set_xlabel("Log-odds of being the author of the merged solution, per log(1 + commits);\n"
                  "95% intervals, standard errors clustered by author", fontsize=7.2)
    tidy(ax)
    ax.tick_params(axis="y", length=0)
    ax.spines["left"].set_visible(False)
    ax.text(-0.6, -0.7, "\u2190 less likely to be the author", fontsize=7, color=INK2, va="bottom")
    ax.text(1.4, -0.7, "more likely to be the author \u2192", fontsize=7, color=INK2, va="bottom", ha="right")
    save(fig, f"{out}/author_coefficients")


# ---------------------------------------------------------------- Figure 2: one issue
def short_shape(shape):
    places = [p for p in shape.split("; ") if p]
    names = [p.split("::")[-1] for p in places]
    files = sorted({p.split("::")[0].rsplit("/", 1)[-1] for p in places})
    if len(places) <= 2:
        return f"{' + '.join(names)}  ({', '.join(files)})"
    return f"{len(places)} functions in {', '.join(files)}"


def short_file(path):
    parts = path.split("/")
    return "/".join(parts[-2:]) if len(parts) > 2 else path


def task_figure(task, res, paths_glob, out):
    alloc = {r["instance_id"]: r for r in read(f"{res}/allocation/tasks.csv")}
    if task not in alloc:
        sys.exit(f"{task} has no row in allocation/tasks.csv (no author or no solving run)")
    req = [r for r in read(f"{res}/allocation/requirement.csv") if r["instance_id"] == task]
    runs = {r["run"]: r for r in read(f"{res}/choice_in_set/runs.csv") if r["instance_id"] == task}
    prun = {r["model"]: r for r in read(f"{res}/paths/runs.csv") if r["instance_id"] == task}
    steps = {}
    for f in glob.glob(paths_glob):
        for line in open(f):
            if task in line:
                d = json.loads(line)
                if d["instance_id"] == task:
                    steps[d["run"]] = d["steps"]
    solving = [m for m, p in prun.items() if p["resolved"] == "True" and p["first_edit"]
               and p["friction_loop"] != "True" and m in runs and m in steps]
    fix_files = {r["file"] for r in req if r["in_merged_solution"] == "True"}
    context = {r["file"] for r in req if r["in_merged_solution"] != "True" and float(r["share_of_solving_runs"]) >= 0.5}

    def colour(cls, files):
        if any(f in fix_files for f in files):
            return ORANGE
        if any(f in context for f in files):
            return BLUE
        if "src" in cls and files:
            return INK2
        return BASE if files else STEP_LIGHT

    groups = defaultdict(list)
    for m in solving:
        groups[runs[m]["shape"]].append(m)
    order = sorted(groups, key=lambda s: (runs[groups[s][0]]["same_as_maintainers"] != "True", -len(groups[s]), s))
    longest = max(int(prun[m]["first_edit"]) + 1 for m in solving)
    req = sorted(req, key=lambda r: (-float(r["share_of_solving_runs"]), r["file"]))

    n_rows = len(solving) + len(order)
    height = max(3.4, 0.21 * n_rows + 1.9)
    fig = plt.figure(figsize=(7.4, height), facecolor=SURFACE)
    top = 1 - 1.2 / height
    a = fig.add_axes([0.13, 0.5 / height + 0.06, 0.40, top - 0.5 / height - 0.06])
    b = fig.add_axes([0.72, 0.5 / height + 0.06, 0.10, top - 0.5 / height - 0.06])

    # (a) paths before the first edit
    y = 0
    for s in order:
        a.text(0, y + 0.1, short_shape(s), fontsize=7, fontweight="bold", color=INK, va="center")
        y += 1
        for m in sorted(groups[s], key=lambda m: NAMES.get(m, m)):
            fe = int(prun[m]["first_edit"])
            for i, (act, cls, files, _) in enumerate(steps[m][:fe + 1]):
                a.add_patch(Rectangle((i, y - 0.36), 0.92, 0.72, facecolor=colour(cls, files), edgecolor="none"))
            a.plot([fe + 1.3] * 2, [y - 0.48, y + 0.48], color=INK, linewidth=1.6, solid_capstyle="butt")
            if runs[m]["same_as_maintainers"] == "True":
                a.text(fe + 3, y, "same as the merged solution", fontsize=6.8, color=INK2, va="center",
                       style="italic")
            a.text(-1.5, y, NAMES.get(m, m), fontsize=7, color=INK, ha="right", va="center")
            y += 1
    a.set_xlim(-0.5, longest + 3)
    a.set_ylim(y - 0.4, -0.6)
    a.set_yticks([])
    a.set_xlabel("Steps before the first edit (black bar)")
    tidy(a)
    a.spines["left"].set_visible(False)
    head(fig, 0.02, 1 - 0.12 / height, "A. Paths of the passing runs, by solution",
         f"{task.split('__')[-1]}: the {len(solving)} runs that pass the maintainers’ tests")
    leg = [(ORANGE, "file the merged solution changed"), (BLUE, "other file read by half or more"),
           (INK2, "other source file"), (BASE, "test or the agent’s own file")]
    for i, (col, lab) in enumerate(leg):
        lx, ly = 0.02 + (i % 2) * 0.25, 1 - (0.62 + 0.17 * (i // 2)) / height
        fig.patches.append(Rectangle((lx, ly), 0.012, 0.089 / height, transform=fig.transFigure,
                                     facecolor=col, edgecolor="none"))
        fig.text(lx + 0.018, ly + 0.045 / height, lab, fontsize=6.8, color=INK, va="center")

    # (b) what solving required, and the author's past work there
    for i, r in enumerate(req):
        w = float(r["share_of_solving_runs"])
        col = ORANGE if r["file"] in fix_files else (BLUE if r["file"] in context else MUTED)
        b.barh(i, w, height=0.64, color=col, edgecolor="none", zorder=2)
        b.text(-0.06, i, short_file(r["file"]), ha="right", va="center", fontsize=6.8, color=INK)
        n, k = int(r["authors"]), int(r["author_commits"])
        rank = {1: "most", 2: "2nd", 3: "3rd"}.get(int(r["author_rank"] or 0), f"#{r['author_rank']}") if k else ""
        b.text(1.08, i, f"{k}  ({rank} of {n})" if k else f"0  (of {n})", va="center", fontsize=6.8, color=INK)
    b.text(1.08, -0.9, "the author’s commits\n(rank among the\nfile’s authors)", fontsize=6.5,
           color=INK2, va="bottom")
    b.set_xlim(0, 1)
    b.set_ylim(len(req) - 0.5, -0.5)
    b.set_xticks([0, 1])
    b.set_xticklabels(["0", "100%"])
    b.set_yticks([])
    b.xaxis.grid(True, color=GRID, linewidth=0.5, zorder=0)
    b.set_xlabel("Solving runs that read\nthe file before editing", fontsize=7)
    tidy(b)
    b.spines["left"].set_visible(False)
    t = alloc[task]
    rank = float(t["author_rank_required"])
    place = "first" if rank == 1.0 else f"in the top {max(1, round((1 - rank) * 100))}%"
    head(fig, 0.58, 1 - 0.12 / height, "B. What solving required",
         f"The merged solution’s author ranks {place} of {t['candidates']}\npeople active in the year before by "
         f"past commits\nto these files (3 years), weighted by the share\nof solving runs that read them")
    save(fig, f"{out}/{task.split('__')[-1]}_requirement")


def save(fig, stem):
    for ext in ("png", "svg", "pdf"):
        fig.savefig(f"{stem}.{ext}", dpi=300, facecolor=SURFACE)
    plt.close(fig)
    print(f"wrote {stem}.png, .svg, .pdf")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--results", default="results/pilot")
    ap.add_argument("--paths", default="data/derived/*/path.jsonl")
    ap.add_argument("--task", default="django__django-14351")
    ap.add_argument("--out", default="results/figures")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    author_figure(args.results, args.out)
    task_figure(args.task, args.results, args.paths, args.out)


if __name__ == "__main__":
    main()
