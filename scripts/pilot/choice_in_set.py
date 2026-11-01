#!/usr/bin/env python3
"""The solutions the agents' passing patches contain, and where the merged solution sits among them, for every issue.
Rule-based: no judgement by hand or by a language model.

Outputs (in --out, default results/pilot/choice_in_set):
  tasks.csv    one row per issue: passing patches, distinct solutions, position of the merged solution, the commit
               that brought the merged solution into the main line (read by allocation.py), and the follow-up columns
               described under "Also in tasks.csv"
  runs.csv     one row per passing agent patch: its solution, whether it is the merged solution's
               (same_as_maintainers), the files it changes, and the share of its added lines that also appear in the
               merged solution (copies_maintainers)
  summary.md   the counts (also printed) and the commit up to which each project's history was read

Inputs:
  --verified   SWE-bench Verified issues (jsonl): repository, base commit, merged solution ("patch")
  --patches    submitted patches of the published runs, data/derived/<run>/patches.jsonl
  --runs       or: run folders with preds.json or trajectories
  --results    per-issue verdicts of those runs (per_instance_details.json), matched to runs by name
  --extra      our own runs, NAME=PREDS_JSON:SCORER_REPORT_JSON (optional, repeatable)
  --tasks      only these issues
  --repos-dir  full git clones of the projects; cloned from GitHub on first use (about 1.5 GB in all)

Definitions:
  place      the innermost function or class around a changed line, in a Python file that is not a test file,
             e.g. django/db/models/lookups.py::In.process_rhs ("<module>" if there is none); blank lines, comments
             and docstrings do not count
  solution   the set of places a patch changes (called "shape" in the code and the outputs)
  passing    the run's patch passed the maintainers' tests (the benchmark's verdict)
  position   of the merged solution among an issue's passing agent patches: "common" if its solution is the most
             frequent one (ties count), "present" if some passing patch has it, "absent" if none does
  landing    the commit on the project's main line (first-parent history) that brought the merged solution in:
             the first commit after the base that adds at least half of its added lines, or, if that commit sits on a
             pull-request branch, the first main-line commit (the merge) that contains it

Also in tasks.csv (not used in the technical appendix): whether a line the merged solution added was changed within
730 days by a commit touching at most 100 files (revised, revision_*), whether a passing agent patch had already
changed a place that revision changed (ahead_*), and whether an agent made the same edit in a file the merged solution
left unchanged (extended_*).
"""
import argparse
import ast
import csv
import difflib
import json
import re
import subprocess
import sys
import threading
import warnings
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

warnings.filterwarnings("ignore", category=SyntaxWarning)  # old escape sequences in the projects' own files

WINDOW_DAYS = 730
MASS_FILES = 100
COPY_SHARE = 0.5
TEST_PATH = re.compile(r"(^|/)tests/|^testing/|(^|/)test_[^/]*\.py$|_tests?\.py$|(^|/)conftest\.py$")
FIX_WORDS = re.compile(r"\b(fix|fixe[sd]|fixing|bug|regression|crash|revert(s|ed)?)\b", re.I)
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
TOKEN = re.compile(r"\w+|[^\w\s]")
SHA_LINE = re.compile(r"^([0-9a-f]{40}) \d+ \d+")


def is_source(path):
    return path.endswith(".py") and not TEST_PATH.search(path)


def meaningful(text):
    s = text.strip()
    return bool(s) and not s.startswith("#")


# ---------------------------------------------------------------- git
class Repo:
    def __init__(self, path):
        self.path = path
        self.files = {}
        self.main_line = None
        self.lock = threading.Lock()

    def on_main_line(self, rev):
        with self.lock:
            if self.main_line is None:
                self.main_line = set(self.git("rev-list", "--first-parent", "HEAD").split())
        return rev in self.main_line

    def git(self, *args):
        r = subprocess.run(["git", "-C", str(self.path), *args], capture_output=True, text=True, errors="replace")
        if r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args[:4])}: {r.stderr.strip()[:300]}")
        return r.stdout

    def show(self, rev, path):
        """File content at a revision, or None if the file does not exist there."""
        key = (rev, path)
        if key not in self.files:
            r = subprocess.run(["git", "-C", str(self.path), "show", f"{rev}:{path}"],
                               capture_output=True, text=True, errors="replace")
            self.files[key] = r.stdout if r.returncode == 0 else None
        return self.files[key]

    def date(self, rev):
        return datetime.fromisoformat(self.git("show", "-s", "--format=%cI", rev).strip())

    def is_ancestor(self, a, b):
        return subprocess.run(["git", "-C", str(self.path), "merge-base", "--is-ancestor", a, b]).returncode == 0

    def commit_diff(self, rev):
        return parse_patch(self.git("show", rev, "--format=", "--no-renames", "-U0"))


def open_repo(repos_dir, name):
    path = repos_dir / (name.replace("/", "__") + ".git")
    if not path.exists():
        print(f"cloning {name} into {path} (once) ...", file=sys.stderr)
        subprocess.run(["git", "clone", "--bare", "--quiet", f"https://github.com/{name}.git", str(path)], check=True)
    return Repo(path)


# ---------------------------------------------------------------- diffs
def strip_ab(name):
    name = name.split("\t")[0]
    return name[2:] if name[:2] in ("a/", "b/") else name


def parse_patch(text):
    """Unified diff -> {path: [hunk]}, hunk = {old, old_len, new, lines: [(tag, text)]}. Deleted files keep their path."""
    files, path, old, left_old, left_new = {}, None, None, 0, 0
    for line in (text or "").splitlines():
        if left_old > 0 or left_new > 0:
            tag = line[:1] or " "
            if tag == "\\":
                continue
            if tag in " -":
                left_old -= 1
            if tag in " +":
                left_new -= 1
            files[path][-1]["lines"].append((tag, line[1:]))
            continue
        if line.startswith("--- "):
            old = strip_ab(line[4:])
        elif line.startswith("+++ "):
            new = strip_ab(line[4:])
            path = old if new == "/dev/null" else new
            files.setdefault(path, [])
        elif line.startswith("@@") and path is not None:
            m = HUNK.match(line)
            if m:
                ol, nl = int(m[2] or 1), int(m[4] or 1)
                files[path].append({"old": int(m[1]), "old_len": ol, "new": int(m[3]), "lines": []})
                left_old, left_new = ol, nl
    return files


def changed_lines(hunk):
    """-> removed [(old_no, text)], added [(new_no, old_pos, text)]"""
    o, n, removed, added = hunk["old"], hunk["new"], [], []
    if hunk["old_len"] == 0:
        o += 1
    for tag, body in hunk["lines"]:
        if tag == " ":
            o, n = o + 1, n + 1
        elif tag == "-":
            removed.append((o, body))
            o += 1
        else:
            added.append((n, o, body))
            n += 1
    return removed, added


def apply_hunks(pre, hunks):
    """Post-image of a file, or None if the patch does not match the file exactly."""
    src, out, pos = pre.splitlines(), [], 0
    for h in hunks:
        start = h["old"] - 1 if h["old_len"] > 0 else h["old"]
        if start < pos:
            return None
        out.extend(src[pos:start])
        pos = start
        for tag, body in h["lines"]:
            if tag in " -":
                if pos >= len(src) or src[pos].rstrip() != body.rstrip():
                    return None
                if tag == " ":
                    out.append(src[pos])
                pos += 1
            else:
                out.append(body)
    out.extend(src[pos:])
    return "\n".join(out) + "\n"


def added_texts(files):
    return [b for p, hunks in files.items() if is_source(p) for h in hunks
            for _, _, b in changed_lines(h)[1] if meaningful(b)]


# ---------------------------------------------------------------- places
@lru_cache(maxsize=2048)
def scopes(text):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    spans = []

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + child.name
                first = min([d.lineno for d in child.decorator_list] + [child.lineno])
                spans.append((first, child.end_lineno, name))
                walk(child, name + ".")
            else:
                walk(child, prefix)

    walk(tree, "")
    return tuple(spans)


@lru_cache(maxsize=512)
def docstring_lines(text):
    """Line numbers inside docstrings; edits there are documentation, not code."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return frozenset()
    out = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
            out.update(range(body[0].lineno, body[0].end_lineno + 1))
    return frozenset(out)


def scope_at(spans, lineno):
    if spans is None:
        return "?"
    best = None
    for a, b, name in spans:
        if a <= lineno <= b and (best is None or b - a < best[0]):
            best = (b - a, name)
    return best[1] if best else "<module>"


def places(file_diffs, pre_of, post_of):
    """Places changed by a diff; pre_of(path) / post_of(path, hunks) give the file before and after."""
    out = set()
    for path, hunks in file_diffs.items():
        if not is_source(path):
            continue
        pre = pre_of(path)
        if pre is None:
            out.add(f"{path}::<new file>")
            continue
        post = post_of(path, hunks)
        sp_pre = scopes(pre)
        sp_post = scopes(post) if post is not None else None
        for h in hunks:
            removed, added = changed_lines(h)
            out.update(f"{path}::{scope_at(sp_pre, no)}" for no, body in removed if meaningful(body))
            for new_no, old_pos, body in added:
                if meaningful(body):
                    where = scope_at(sp_post, new_no) if sp_post is not None else scope_at(sp_pre, old_pos)
                    out.add(f"{path}::{where}")
    return frozenset(out)


def patch_places(repo, base, file_diffs):
    return places(file_diffs, lambda p: repo.show(base, p),
                  lambda p, hunks: apply_hunks(repo.show(base, p), hunks))


def commit_places(repo, rev, file_diffs):
    return places(file_diffs, lambda p: repo.show(f"{rev}^", p), lambda p, hunks: repo.show(rev, p))


# ---------------------------------------------------------------- similarity and edits
def code_share(a_lines, b_lines):
    """Share of a's added lines that appear verbatim (spaces ignored) among b's added lines, 0..1."""
    norm = lambda t: " ".join(t.split())
    a = [norm(t) for t in a_lines]
    b = {norm(t) for t in b_lines}
    return sum(t in b for t in a) / len(a) if a else 0.0


def edit_signature(old, new):
    a, b = TOKEN.findall(old), TOKEN.findall(new)
    ops = tuple((" ".join(a[i1:i2]), " ".join(b[j1:j2]))
                for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
                if tag != "equal")
    return ops if ops and sum(len(x) + len(y) for x, y in ops) >= 3 else None


def line_edits(hunks):
    """(signature, old line) for each removed line paired in order with an added line in the same change block."""
    sigs = []
    for h in hunks:
        rem, add = [], []
        for tag, body in h["lines"] + [(" ", "")]:
            if tag == "-":
                rem.append(body)
            elif tag == "+":
                add.append(body)
            else:
                for r, a in zip(rem, add):
                    s = edit_signature(r, a)
                    if s:
                        sigs.append((s, r))
                rem, add = [], []
    return sigs


# ---------------------------------------------------------------- the merged solution over time
def landing_commit(repo, base, gold):
    texts = {t.strip() for t in added_texts(gold)}
    if not texts:
        return None
    since = (repo.date(base) - timedelta(days=30)).isoformat()
    tried = set()
    for path, hunks in gold.items():
        if not is_source(path):
            continue
        for line in sorted({b.strip() for h in hunks for _, _, b in changed_lines(h)[1] if meaningful(b)},
                           key=len, reverse=True)[:3]:
            out = repo.git("log", "--format=%H", "--reverse", f"--since={since}", "-S", line, "HEAD", "--", path)
            for h in out.split()[:20]:
                if h in tried or repo.is_ancestor(h, base):
                    continue
                tried.add(h)
                if len(texts & {t.strip() for t in added_texts(repo.commit_diff(h))}) >= 0.5 * len(texts):
                    if repo.on_main_line(h):
                        return h
                    for c in repo.git("rev-list", "--ancestry-path", "--topo-order", "--reverse", f"{h}..HEAD").split():
                        if repo.on_main_line(c):
                            return c
                    return None
    return None


def first_revision(repo, landing, gold):
    """-> (revision commit or None, lines followed, lines whose following stopped at a mass change)"""
    texts = {t.strip() for t in added_texts(gold)}
    end_date = repo.date(landing) + timedelta(days=WINDOW_DAYS)
    end = repo.git("rev-list", "-1", f"--before={end_date.isoformat()}", "HEAD").strip()
    if not end or not repo.is_ancestor(landing, end):
        return None, 0, 0
    best, followed, stopped = None, 0, 0
    for path, hunks in parse_patch(repo.git("diff", "-U0", "--no-renames", f"{landing}^1", landing)).items():
        if not is_source(path):
            continue
        docs = docstring_lines(repo.show(landing, path) or "")
        nos = sorted(n for h in hunks for n, _, b in changed_lines(h)[1] if b.strip() in texts and n not in docs)
        if not nos:
            continue
        ranges = []
        for n in nos:
            if ranges and n == ranges[-1][1] + 1:
                ranges[-1][1] = n
            else:
                ranges.append([n, n])
        args = ["blame", "-w", "--reverse", f"{landing}..{end}", "--porcelain"]
        for a, b in ranges:
            args += ["-L", f"{a},{b}"]
        last_seen = [m[1] for m in map(SHA_LINE.match, repo.git(*args, "--", path).splitlines()) if m]
        for sha in last_seen:
            followed += 1
            if sha == end:
                continue
            nxt = repo.git("rev-list", "--reverse", "--no-merges", "--ancestry-path", f"{sha}..{end}", "--", path).split()
            if not nxt:
                continue
            if len(repo.git("show", "--format=", "--name-only", "--no-renames", nxt[0]).split()) > MASS_FILES:
                stopped += 1
                continue
            when = repo.date(nxt[0])
            if best is None or when < best[1]:
                best = (nxt[0], when)
    return best, followed, stopped


# ---------------------------------------------------------------- one task
def analyse(task, runs, repo):
    tid, base = task["instance_id"], task["base_commit"]
    gold = {p: h for p, h in parse_patch(task["patch"]).items() if is_source(p)}
    gold_shape = patch_places(repo, base, gold)
    gold_added = added_texts(gold)
    gold_edits = {s for p, hunks in gold.items() for s, _ in line_edits(hunks)}

    passing = []
    for run in runs:
        patch = run["preds"].get(tid)
        if patch and run["resolved"].get(tid):
            files = {p: h for p, h in parse_patch(patch).items() if is_source(p)}
            passing.append({"run": run["name"], "files": files, "shape": patch_places(repo, base, files),
                            "added": added_texts(files)})
    shapes = Counter(p["shape"] for p in passing if p["shape"])
    if not gold_shape:
        position = "merged solution changes no source file"
    elif not shapes:
        position = "no passing patch"
    elif gold_shape in shapes and shapes[gold_shape] == max(shapes.values()):
        position = "common"
    else:
        position = "present" if gold_shape in shapes else "absent"

    row = {"instance_id": tid, "repo": task["repo"], "n_runs": sum(tid in r["preds"] for r in runs),
           "n_passing": len(passing), "n_shapes": len(shapes), "n_shapes_without_copies": "",
           "position": position, "maintainers_shape": "; ".join(sorted(gold_shape))}

    # agents made the merged solution's edit in further files
    ext = []
    for p in passing:
        for path, hunks in p["files"].items():
            if path in gold:
                continue
            for sig, old_line in line_edits(hunks):
                if sig in gold_edits:
                    head = repo.show("HEAD", path)
                    still = head is not None and any(l.strip() == old_line.strip() for l in head.splitlines())
                    ext.append((p["run"], path, still))
    row["extended_runs"] = "; ".join(sorted({r for r, _, _ in ext}))
    row["extended_files"] = "; ".join(sorted({f for _, f, _ in ext}))
    row["extended_still_old_today"] = "; ".join(sorted({f for _, f, s in ext if s}))

    # the merged solution over time
    rev_added = []
    landing = landing_commit(repo, base, gold)
    row.update(landing=landing or "", landing_date="", lines_followed=0, lines_stopped=0, revised="",
               revision="", revision_date="", days_to_revision="", revision_subject="", revision_says_fix="", new_places="", ahead_runs="",
               ahead_independent_runs="")
    if landing:
        land_date = repo.date(landing)
        best, followed, stopped = first_revision(repo, landing, gold)
        row.update(landing_date=land_date.date().isoformat(), lines_followed=followed, lines_stopped=stopped)
        if followed > stopped:
            row["revised"] = bool(best)
        if best:
            rev, when = best
            rev_files = {p: h for p, h in repo.commit_diff(rev).items() if is_source(p)}
            new = commit_places(repo, rev, rev_files) - gold_shape
            rev_added = added_texts(rev_files)
            ahead = [p for p in passing if p["shape"] & new]
            row.update(revision=rev, revision_date=when.date().isoformat(), days_to_revision=(when - land_date).days,
                       revision_subject=repo.git("show", "-s", "--format=%s", rev).strip(),
                       revision_says_fix=bool(FIX_WORDS.search(repo.git("show", "-s", "--format=%B", rev))),
                       new_places="; ".join(sorted(new)),
                       ahead_runs="; ".join(p["run"] for p in ahead),
                       ahead_independent_runs="; ".join(p["run"] for p in ahead
                                                        if code_share(p["added"], rev_added) < COPY_SHARE))
    row["_runs"] = [{"instance_id": tid, "run": p["run"], "same_as_maintainers": p["shape"] == gold_shape,
                     "shape": "; ".join(sorted(p["shape"])), "n_places": len(p["shape"]),
                     "files": "; ".join(sorted(p["files"])),
                     "copies_maintainers": round(code_share(p["added"], gold_added), 3)} for p in passing]
    # solutions without patches that copy the merged solution (90% of lines) or the revision (50%)
    row["n_shapes_without_copies"] = len({p["shape"] for p in passing if p["shape"]
                                          and code_share(p["added"], gold_added) < 0.9
                                          and code_share(p["added"], rev_added) < COPY_SHARE})
    return row


# ---------------------------------------------------------------- inputs
def run_name(name):
    return re.sub(r"^\d{8}_mini-v[\d.]+_", "", name).lower()


def load_preds(path):
    path = Path(path)
    pj = path / "preds.json" if path.is_dir() else path
    if pj.exists():
        data = json.loads(pj.read_text())
        items = data.values() if isinstance(data, dict) else data
        return {d["instance_id"]: d.get("model_patch") or "" for d in items}
    out = {}
    for traj in path.glob("*/*.traj.json"):
        info = json.loads(traj.read_text()).get("info") or {}
        out[traj.parent.name] = info.get("submission") or ""
    return out


def load_patch_file(path):
    """data/derived/<run>/patches.jsonl: one JSON object per line with a task id and the submitted patch."""
    out, keys = {}, []
    for line in open(path):
        if not line.strip():
            continue
        d = json.loads(line)
        keys = list(d)
        tid = next((d[k] for k in ("instance_id", "task", "instance") if k in d), None)
        patch = next((d[k] for k in ("patch", "model_patch", "submission", "diff") if isinstance(d.get(k), str)), None)
        if tid and patch is not None:
            out[tid] = patch
    if not out:
        sys.exit(f"{path}: found no task id and patch fields (keys seen: {keys[:12]})")
    return out


def load_resolved(path):
    data = json.loads(Path(path).read_text())
    if "resolved_ids" in data:
        return {t: True for t in data["resolved_ids"]}
    return {t: bool(v.get("resolved")) for t, v in data.items() if isinstance(v, dict)}


def load_runs(args):
    results = {}
    for f in args.results:
        name = run_name(Path(f).parent.name)
        if name in results and Path(results[name]).read_bytes() != Path(f).read_bytes():
            sys.exit(f"two different verdict files for run {name}: {results[name]} and {f}; pass only one")
        results[name] = f
    runs = []
    sources = [(run_name(Path(f).parent.name), load_patch_file, f) for f in args.patches]
    sources += [(run_name(Path(d).parent.name if Path(d).name == "trajs" else Path(d).name), load_preds, d)
                for d in args.runs]
    for name, loader, src in sources:
        if name not in results:
            sys.exit(f"no verdicts for run {name}; verdict files found for: {', '.join(sorted(results))}")
        runs.append({"name": name, "preds": loader(src), "resolved": load_resolved(results[name])})
    for spec in args.extra:
        name, rest = spec.split("=", 1)
        preds, report = rest.split(":", 1)
        runs.append({"name": name, "preds": load_preds(preds), "resolved": load_resolved(report)})
    return runs


# ---------------------------------------------------------------- summary
def summary(rows):
    with_pass = [r for r in rows if r["n_passing"] > 0]
    multi = [r for r in rows if r["n_passing"] >= 2]
    pos = Counter(r["position"] for r in with_pass)
    counts = Counter(min(r["n_shapes"], 3) for r in multi)
    lines = [
        f"Issues: {len(rows)}; with at least one passing agent patch: {len(with_pass)}; with at least two: {len(multi)}",
        f"Distinct solutions among the passing patches (issues with at least two): one {counts[1]}, two {counts[2]}, "
        f"three or more {counts[3]}",
        f"Merged solution among the agents' solutions (issues with a passing patch): the most common one "
        f"{pos['common']}, present but not the most common {pos['present']}, absent {pos['absent']}",
        f"Commit that brought the merged solution into the main line found: {sum(1 for r in rows if r['landing'])}",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--verified", required=True)
    ap.add_argument("--patches", nargs="*", default=[], help="data/derived/<run>/patches.jsonl files")
    ap.add_argument("--runs", nargs="*", default=[], help="run folders with preds.json or trajectories")
    ap.add_argument("--results", nargs="*", default=[])
    ap.add_argument("--extra", nargs="*", default=[])
    ap.add_argument("--repos-dir", default="data/repos_full")
    ap.add_argument("--tasks", nargs="*")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--out", default="results/pilot/choice_in_set")
    args = ap.parse_args()

    tasks = [json.loads(l) for l in open(args.verified) if l.strip()]
    if args.tasks:
        tasks = [t for t in tasks if t["instance_id"] in set(args.tasks)]
    runs = load_runs(args)
    repos_dir = Path(args.repos_dir)
    repos_dir.mkdir(parents=True, exist_ok=True)
    repos = {name: open_repo(repos_dir, name) for name in sorted({t["repo"] for t in tasks})}

    def one(task):
        try:
            return analyse(task, runs, repos[task["repo"]])
        except Exception as e:  # one broken task must not stop the others; it is listed in the output
            print(f"{task['instance_id']}: {e}", file=sys.stderr)
            return {"instance_id": task["instance_id"], "repo": task["repo"], "n_runs": 0, "n_passing": 0,
                    "n_shapes": 0, "position": f"error: {e}"[:200], "revised": "", "extended_files": "",
                    "ahead_runs": "", "landing": ""}

    with ThreadPoolExecutor(args.jobs) as pool:
        rows = sorted(pool.map(one, tasks), key=lambda r: r["instance_id"])
    run_rows = [x for r in rows for x in r.pop("_runs", [])]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    fields = list(max(rows, key=len).keys())
    with open(out / "tasks.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    with open(out / "runs.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["instance_id", "run", "same_as_maintainers", "shape", "n_places", "files",
                                          "copies_maintainers"])
        w.writeheader()
        w.writerows(run_rows)
    heads = "\n".join(f"- {n}: history up to {r.git('rev-parse', 'HEAD').strip()[:10]} "
                      f"({r.date('HEAD').date()})" for n, r in repos.items())
    text = summary(rows)
    (out / "summary.md").write_text(f"# Summary\n\n```\n{text}\n```\n\nProject histories used:\n{heads}\n")
    print(text)
    print(f"\nwrote {out}/tasks.csv, runs.csv, summary.md")


if __name__ == "__main__":
    main()
