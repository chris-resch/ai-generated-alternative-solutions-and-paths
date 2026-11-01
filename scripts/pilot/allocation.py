#!/usr/bin/env python3
"""Who authored the merged solution of each issue, set against what the agents' paths show solving required.
Git history and existing outputs only; no model.

Inputs (defaults are the repository's paths):
  --verified      data/public/swebench_verified.jsonl        issue text, base commit, merged solution
  --choice-tasks  results/pilot/choice_in_set/tasks.csv      commit that brought the merged solution in (landing)
  --path-runs     results/pilot/paths/runs.csv               per run: passed, first edit, submitted files
  --steps         data/derived/*/path.jsonl                  every step of every run
  --repos-dir     data/repos_full                            full clones from choice_in_set.py
Output (--out, default results/pilot/allocation): summary.md (also printed: the issue counts and two regression
tables), tasks.csv (one row per issue), estimates.csv (every coefficient with its standard error, for figures),
requirement.csv (per issue and file the agents read before their first edit: the share of solving runs that read it,
whether the merged solution changed it, and its author's commits to it against the file's other authors). People
appear only as pseudonymous numbers; no name or e-mail address is written.

Measures:
  author        author of the commit that brought the merged solution into the main line; for a merged pull
                request, the author of most of its commits. People are e-mail addresses after the project's .mailmap;
                bots are dropped.
  candidates    everyone with a commit in the 365 days before the issue's base commit, plus the author.
  activity      log(1 + the person's commits in those 365 days).
  core          the smallest set of people whose commits make up 80% of the commits in those 365 days.
  files solving required   every source file a solving run searched or read before its first edit, weighted by
                the share of solving runs that did so.
  past commits to a set of files   the person's commits in the 3 years before the base commit that touched files
                of the set; a commit counts once for each such file it touched, times the file's weight (the share
                above; 1 for files the issue names and files the merged solution changed); the commits of the merged
                solution itself are not counted; used as log(1 + count).
  reach         for a solving run, the number of directories it read before its first edit other than the
                directories of the files it submitted; per issue the median over solving runs.
  solution size log(1 + the number of source files the merged solution changed).

Models:
  Table A1, who authored the merged solution: conditional logit, one row per issue and candidate, outcome 1 for the
    author; the comparison is among the candidates of the same issue (issue fixed effects). Columns: (1) activity;
    (2) + past commits to files the issue names; (3) + past commits to files solving required; (4) + past commits to
    files the merged solution changed. Standard errors clustered by author.
  Table A2, whether the author is core: logit, one row per issue. Columns: (1) reach; (2) + solution size;
    (3) + project fixed effects (projects whose authors are all core or all not core are left out). Robust
    standard errors.
"""
import argparse
import bisect
import csv
import glob
import json
import math
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

YEAR = 365 * 86400
TEST_PATH = re.compile(r"(^|/)tests/|^testing/|(^|/)test_[^/]*\.py$|_tests?\.py$|(^|/)conftest\.py$")
# bots only: people with GitHub's private addresses (<id>+<name>@users.noreply.github.com) are kept
BOT = re.compile(r"\[bot\]|dependabot|github-actions|pre-commit-ci|^noreply@github\.com$", re.I)
PY_PATH = re.compile(r"[\w./\\-]+\.py\b")
DOTTED = re.compile(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b")


def is_source(path):
    return path.endswith(".py") and not TEST_PATH.search(path)


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, errors="replace",
                          check=True).stdout


# ---------------------------------------------------------------- history
class History:
    """All non-merge commits on the main history of one project: time, author, files."""

    def __init__(self, repo):
        self.repo = repo
        out = git(repo, "log", "--no-merges", "--use-mailmap", "--format=%x00%ct%x09%aE%x09%H", "--name-only", "HEAD")
        rows = []
        for rec in out.split("\x00")[1:]:
            head, _, files = rec.partition("\n")
            t, email, sha = head.split("\t")
            rows.append((int(t), email.lower(), tuple(f for f in files.split("\n") if f), sha))
        rows.sort()
        self.times = [r[0] for r in rows]
        self.rows = rows
        self.files_at = {}

    def window(self, start, end, exclude=frozenset()):
        rows = self.rows[bisect.bisect_left(self.times, start):bisect.bisect_left(self.times, end)]
        return [r for r in rows if r[3] not in exclude]

    def fix_commits(self, base, landing):
        """The commits that make up the merged solution itself (they must not count as experience before it)."""
        return frozenset(git(self.repo, "rev-list", f"{base}..{landing}").split())

    def tree(self, rev):
        if rev not in self.files_at:
            self.files_at[rev] = [f for f in git(self.repo, "ls-tree", "-r", "--name-only", rev).split("\n") if f]
        return self.files_at[rev]

    def time(self, rev):
        return int(git(self.repo, "show", "-s", "--format=%ct", rev).strip())

    def author(self, landing):
        parents = git(self.repo, "show", "-s", "--format=%P", landing).split()
        if len(parents) > 1:
            authors = git(self.repo, "log", "--no-merges", "--use-mailmap", "--format=%aE",
                          f"{parents[0]}..{parents[1]}").lower().split()
            authors = [a for a in authors if not BOT.search(a)]
            return Counter(authors).most_common(1)[0][0] if authors else None
        a = git(self.repo, "show", "-s", "--use-mailmap", "--format=%aE", landing).strip().lower()
        return None if BOT.search(a) else a


def files_named(text, tree):
    """Repository files the issue text names, as paths (…/x.py) or dotted modules (pkg.mod.Class)."""
    src = [f for f in tree if is_source(f)]
    by_suffix = defaultdict(list)
    for f in src:
        parts = f.split("/")
        for k in range(1, len(parts) + 1):
            by_suffix["/".join(parts[-k:])].append(f)
    found = set()
    for m in PY_PATH.findall(text):
        parts = m.replace("\\", "/").strip("./").split("/")
        for k in range(len(parts), 0, -1):
            hit = by_suffix.get("/".join(parts[-k:]), [])
            if len(hit) == 1 or (hit and k >= 2):
                found.update(hit[:3])
                break
    for m in DOTTED.findall(text):
        parts = m.split(".")
        for k in range(len(parts), 1, -1):
            base = "/".join(parts[:k])
            hit = by_suffix.get(base + ".py", []) + by_suffix.get(base + "/__init__.py", [])
            if hit and len(hit) <= 3:
                found.update(hit)
                break
    return found


# ---------------------------------------------------------------- statistics
def newton(evaluate, k):
    """Maximize a concave log-likelihood; evaluate(beta) -> (ll, grad, hess)."""
    beta = np.zeros(k)
    ll, grad, hess = evaluate(beta)
    for _ in range(100):
        step = np.linalg.solve(hess - 1e-9 * np.eye(k), grad)
        t = 1.0
        while t > 1e-6:
            new = beta - t * step
            ll_new, g_new, h_new = evaluate(new)
            if ll_new >= ll - 1e-12:
                break
            t /= 2
        converged = np.abs(new - beta).max() < 1e-8
        beta, ll, grad, hess = new, ll_new, g_new, h_new
        if converged:
            break
    return beta, ll, hess


def sandwich(hess, scores, clusters):
    """Cluster-robust covariance: inverse information, outer product of the scores summed within clusters."""
    sums = defaultdict(lambda: 0)
    for s, c in zip(scores, clusters):
        sums[c] = sums[c] + s
    meat = sum(np.outer(v, v) for v in sums.values())
    g = len(sums)
    bread = np.linalg.pinv(-hess)
    return bread @ meat @ bread * g / max(g - 1, 1)


def clogit(groups, clusters):
    """Conditional logit. groups: list of (X: candidates x k, row of the author). -> beta, se, loglik."""
    k = groups[0][0].shape[1]

    def evaluate(beta):
        grad, hess, ll = np.zeros(k), np.zeros((k, k)), 0.0
        for X, y in groups:
            u = X @ beta
            u = u - u.max()
            p = np.exp(u)
            ll += u[y] - np.log(p.sum())
            p /= p.sum()
            xbar = p @ X
            grad += X[y] - xbar
            hess -= (X * p[:, None]).T @ X - np.outer(xbar, xbar)
        return ll, grad, hess

    beta, ll, hess = newton(evaluate, k)
    scores = []
    for X, y in groups:
        u = X @ beta
        p = np.exp(u - u.max())
        scores.append(X[y] - (p / p.sum()) @ X)
    return beta, np.sqrt(np.diag(sandwich(hess, scores, clusters))), ll


def logit(X, y):
    """Logit with an intercept (first column of X). -> beta, robust se, loglik."""
    k = X.shape[1]

    def evaluate(beta):
        p = 1 / (1 + np.exp(-(X @ beta)))
        ll = float(np.sum(y * np.log(p + 1e-300) + (1 - y) * np.log(1 - p + 1e-300)))
        return ll, X.T @ (y - p), -(X * (p * (1 - p))[:, None]).T @ X

    beta, ll, hess = newton(evaluate, k)
    p = 1 / (1 + np.exp(-(X @ beta)))
    scores = X * (y - p)[:, None]
    cov = sandwich(hess, scores, range(len(y))) * (len(y) - 1) / max(len(y) - k, 1)
    return beta, np.sqrt(np.diag(cov)), ll


def pvalue(b, se):
    return math.erfc(abs(b / se) / math.sqrt(2)) if se > 0 else float("nan")


def stars(p):
    return "***" if p < 0.01 else "**" if p < 0.05 else "*" if p < 0.1 else ""


def table(title, terms, fits, footer):
    """Text regression table: terms x columns, coefficient with stars and the standard error below."""
    width = 15
    out = [title, " " * 46 + "".join(f"({i + 1})".center(width) for i in range(len(fits)))]
    for term, label in terms:
        coef, se = [], []
        for f in fits:
            if term in f["names"]:
                j = f["names"].index(term)
                b, s = f["beta"][j], f["se"][j]
                coef.append(f"{b:.3f}{stars(pvalue(b, s))}".center(width))
                se.append(f"({s:.3f})".center(width))
            else:
                coef.append(" " * width)
                se.append(" " * width)
        out += [f"{label:46}" + "".join(coef), " " * 46 + "".join(se)]
    for label, key in footer:
        out.append(f"{label:46}" + "".join(str(f[key]).center(width) for f in fits))
    return out


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--verified", default="data/public/swebench_verified.jsonl")
    ap.add_argument("--choice-tasks", default="results/pilot/choice_in_set/tasks.csv")
    ap.add_argument("--path-runs", default="results/pilot/paths/runs.csv")
    ap.add_argument("--steps", nargs="*", default=sorted(glob.glob("data/derived/*/path.jsonl")))
    ap.add_argument("--repos-dir", default="data/repos_full")
    ap.add_argument("--out", default="results/pilot/allocation")
    args = ap.parse_args()

    tasks = {t["instance_id"]: t for t in (json.loads(l) for l in open(args.verified) if l.strip())}
    choice = {r["instance_id"]: r for r in read_csv(args.choice_tasks)}
    solving = {(r["instance_id"], r["model"]): r for r in read_csv(args.path_runs)
               if r["resolved"] == "True" and r["first_edit"] and r["friction_loop"] != "True"}

    # what solving needed, per task: files read before the first edit, and how far that reached
    reads, spans = defaultdict(Counter), defaultdict(list)
    n_solving = Counter()
    for f in args.steps:
        for line in open(f):
            d = json.loads(line)
            pr = solving.get((d["instance_id"], d["run"]))
            if not pr:
                continue
            ctx = {fn for a, cls, files, _ in d["steps"][:int(pr["first_edit"])] if a in "FD" and "src" in cls
                   for fn in files if is_source(fn)}
            n_solving[d["instance_id"]] += 1
            reads[d["instance_id"]].update(ctx)
            own_dirs = {fn.rsplit("/", 1)[0] for fn in pr["landing"].split("|") if fn}
            spans[d["instance_id"]].append(len({fn.rsplit("/", 1)[0] for fn in ctx} - own_dirs))

    histories = {}
    groups, clusters, rows, requirement, estimates = [], [], [], [], []
    count = Counter()
    ids = {}
    pid = lambda e: ids.setdefault(e, len(ids) + 1)
    for tid in sorted(tasks):
        t, c = tasks[tid], choice.get(tid)
        if not c or not c["n_passing"] or int(c["n_passing"]) == 0:
            continue
        count["with a passing agent patch"] += 1
        if not c["landing"]:
            continue
        count["... and the commit that merged the solution found"] += 1
        if n_solving[tid] == 0:
            continue
        count["... and at least one solving run (not stuck, edits a source file)"] += 1
        repo = t["repo"]
        if repo not in histories:
            print(f"reading the history of {repo} ...")
            histories[repo] = History(Path(args.repos_dir) / (repo.replace("/", "__") + ".git"))
        h = histories[repo]
        author = h.author(c["landing"])
        if not author:
            continue
        count["... and an author who is not a bot"] += 1
        base = h.time(t["base_commit"])
        own = h.fix_commits(t["base_commit"], c["landing"])
        year = h.window(base - YEAR, base, own)
        three = h.window(base - 3 * YEAR, base, own)
        activity = Counter(e for _, e, _, _ in year if not BOT.search(e))
        total = sum(activity.values())
        core, run = set(), 0
        for e, n in activity.most_common():
            if run >= 0.8 * total:
                break
            core.add(e)
            run += n

        merged = {line[6:] for line in t["patch"].splitlines() if line.startswith("+++ b/")}
        weights = {"report": {f: 1.0 for f in files_named(t["problem_statement"], h.tree(t["base_commit"]))},
                   "paths": {f: n / n_solving[tid] for f, n in reads[tid].items()},
                   "merged": {f: 1.0 for f in merged if is_source(f)}}
        past = {name: Counter() for name in weights}
        by_file = defaultdict(Counter)
        for _, e, files, _ in three:
            for name, w in weights.items():
                v = sum(w.get(f, 0.0) for f in files)
                if v:
                    past[name][e] += v
            for f in files:
                if f in weights["paths"] and not BOT.search(e):
                    by_file[f][e] += 1
        cands = sorted(set(activity) | {author})
        X = np.array([[math.log1p(activity[e]), math.log1p(past["report"][e]), math.log1p(past["paths"][e]),
                       math.log1p(past["merged"][e])] for e in cands])
        groups.append((X, cands.index(author)))
        clusters.append(pid(author))

        for f, wt in sorted(weights["paths"].items(), key=lambda kv: (-kv[1], kv[0])):
            mine = by_file[f][author]
            requirement.append({"instance_id": tid, "file": f, "share_of_solving_runs": round(wt, 3),
                                "in_merged_solution": f in merged, "author_commits": mine, "authors": len(by_file[f]),
                                "author_rank": 1 + sum(v > mine for v in by_file[f].values()) if mine else ""})
        mine = past["paths"][author]
        rank = (sum(past["paths"][e] < mine for e in cands)
                + 0.5 * (sum(past["paths"][e] == mine for e in cands) - 1)) / max(len(cands) - 1, 1)
        rows.append({"instance_id": tid, "repo": repo, "author": pid(author), "author_is_core": author in core,
                     "candidates": len(cands), "author_commits_year": activity[author],
                     "author_past_commits_required": round(mine, 2), "author_rank_required": round(rank, 3),
                     "files_issue_names": len(weights["report"]), "files_required": len(weights["paths"]),
                     "files_merged": len(weights["merged"]),
                     "reach": sorted(spans[tid])[len(spans[tid]) // 2]})

    n_two = sum(1 for c in choice.values() if c["n_passing"] and int(c["n_passing"]) >= 2)
    lines = ["Issues", f"  in the benchmark: {len(tasks)}; with at least two passing agent patches: {n_two}"]
    lines += [f"  {k}: {v}" for k, v in count.items()]
    lines += [f"  distinct authors: {len(ids)}; authors in the core: {sum(r['author_is_core'] for r in rows)}; "
              f"candidates per issue: median {sorted(r['candidates'] for r in rows)[len(rows) // 2]}, "
              f"{sum(r['candidates'] for r in rows)} rows in all",
              f"  issues whose author had no commit in the year before: "
              f"{sum(r['author_commits_year'] == 0 for r in rows)}", ""]
    req_by = defaultdict(list)
    for q in requirement:
        req_by[q["instance_id"]].append(q)
    median = lambda xs: sorted(xs)[len(xs) // 2]
    lines += ["Paths",
              f"  solving runs (passing, edit a source file, not stuck): {len(solving)} in "
              f"{len({k[0] for k in solving})} issues",
              f"  files solving required per issue (the issues above): median {median([r['files_required'] for r in rows])}, "
              f"of which the merged solution did not change: median "
              f"{median([sum(not q['in_merged_solution'] for q in req_by[r['instance_id']]) for r in rows])}",
              f"  issues with a file read by at least half of the solving runs outside the merged solution: "
              f"{sum(any(q['share_of_solving_runs'] >= 0.5 and not q['in_merged_solution'] for q in req_by[r['instance_id']]) for r in rows)}",
              f"  issues whose solving reached at least one directory beyond the changed code (reach >= 1): "
              f"{sum(r['reach'] >= 1 for r in rows)}", ""]

    # Table A1: who authored the merged solution
    names = ["activity", "issue", "required", "merged"]
    labels = [("activity", "Commits in the year before (activity)"),
              ("issue", "Past commits: files the issue names"),
              ("required", "Past commits: files solving required"),
              ("merged", "Past commits: files the merged solution changed")]
    ll0 = -sum(math.log(len(X)) for X, _ in groups)
    fits = []
    for k in range(1, 5):
        beta, se, ll = clogit([(X[:, :k], y) for X, y in groups], clusters)
        fits.append({"names": names[:k], "beta": beta, "se": se, "ll": f"{ll:.1f}",
                     "r2": f"{1 - ll / ll0:.3f}", "n": len(groups), "rows": sum(len(X) for X, _ in groups)})
        for nm, b, sd in zip(names[:k], beta, se):
            estimates.append({"model": f"authored ({k})", "term": nm, "estimate": round(b, 4), "se": round(sd, 4),
                              "p": round(pvalue(b, sd), 4), "low": round(b - 1.96 * sd, 4),
                              "high": round(b + 1.96 * sd, 4), "issues": len(groups)})
    lines += table("Table A1. Who authored the merged solution (conditional logit; outcome: 1 for the author among "
                   "the issue's candidates)", labels, fits,
                   [("Issues", "n"), ("Issue x candidate rows", "rows"), ("Log-likelihood", "ll"),
                    ("Pseudo R2 (vs. a random pick)", "r2")])
    lines += ["  Standard errors clustered by author in parentheses. * p<0.10, ** p<0.05, *** p<0.01.", ""]

    # Table A2: whether the author is core
    repos = sorted({r["repo"] for r in rows})
    varied = [rp for rp in repos if 0 < sum(r["author_is_core"] for r in rows if r["repo"] == rp)
              < sum(1 for r in rows if r["repo"] == rp)]
    fits2 = []
    for k in range(1, 4):
        use = [r for r in rows if k < 3 or r["repo"] in varied]
        cols = [[1.0, r["reach"]] + ([math.log1p(r["files_merged"])] if k >= 2 else [])
                + ([float(r["repo"] == rp) for rp in varied[1:]] if k == 3 else []) for r in use]
        X, yv = np.array(cols), np.array([float(r["author_is_core"]) for r in use])
        beta, se, ll = logit(X, yv)
        pbar = yv.mean()
        ll0 = len(yv) * (pbar * math.log(pbar) + (1 - pbar) * math.log(1 - pbar))
        nm = ["constant", "reach", "size"][:2 + (k >= 2)]
        fits2.append({"names": nm, "beta": beta[:len(nm)], "se": se[:len(nm)], "ll": f"{ll:.1f}",
                      "r2": f"{1 - ll / ll0:.3f}", "n": len(use), "fe": "yes" if k == 3 else "no"})
        for t_, b, sd in zip(nm, beta, se):
            estimates.append({"model": f"core ({k})", "term": t_, "estimate": round(b, 4), "se": round(sd, 4),
                              "p": round(pvalue(b, sd), 4), "low": round(b - 1.96 * sd, 4),
                              "high": round(b + 1.96 * sd, 4), "issues": len(use)})
    lines += table("Table A2. Whether the author of the merged solution is a core member (logit, one row per issue)",
                   [("reach", "Reach (directories read beyond those changed)"),
                    ("size", "Solution size (log(1 + source files changed))"), ("constant", "Constant")], fits2,
                   [("Project fixed effects", "fe"), ("Issues", "n"), ("Log-likelihood", "ll"),
                    ("Pseudo R2", "r2")])
    lines += [f"  Robust standard errors in parentheses. * p<0.10, ** p<0.05, *** p<0.01. Column (3) leaves out "
              f"{len(repos) - len(varied)} projects whose authors are all core or all not core.",
              f"  Core share by reach: " + ", ".join(
                  f"{lab} {sum(r['author_is_core'] for r in g) / len(g):.0%} of {len(g)}" for lab, g in
                  (("0:", [r for r in rows if r["reach"] == 0]), ("1:", [r for r in rows if r["reach"] == 1]),
                   ("2 or more:", [r for r in rows if r["reach"] >= 2])) if g)]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, data in (("tasks.csv", rows), ("estimates.csv", estimates), ("requirement.csv", requirement)):
        with open(out / name, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(data[0]))
            w.writeheader()
            w.writerows(data)
    text = "\n".join(lines)
    (out / "summary.md").write_text(f"# Who authored the merged solution\n\n```\n{text}\n```\n")
    print(text)


if __name__ == "__main__":
    main()
